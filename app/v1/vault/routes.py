"""HTTP surface.

**One router, one prefix.** A grant is a sub-resource of a store, not a resource kind of
its own, so it hangs off `/{kv_name}/access` rather than getting a prefix of its own. The
Tashtiot per-team file *is* a different resource, but it shares the chain and the response
model, so it lives on the same router behind a fixed `/tashtiot` segment.

**Fixed segments are registered before `/{kv_name}`.** `/tashtiot` and `/stores` would
otherwise be shadowed by a store literally named one of those. Registration order is the
whole guard, and tests pin each one. `/pull-request` used to be a third: the routes that
opened a pull request *without* waiting for CI are gone, so nothing occupies that segment
and a store may be called `pull-request` with nothing to collide with.

**Path parameters are pattern-validated**, not just body fields. Starlette's path convertor
is `[^/]+`, so nothing is exploitable without them, but a malformed name would otherwise
surface as an opaque Bitbucket 404 two calls later rather than a 422 here.
"""

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Path, Query
from loguru import logger
from starlette.responses import JSONResponse
from tashtiot_apis_library.connectors import ExternalServiceError

from ...helpers import build_k8s_access_entry, build_oidc_access_entry
from .conf import config
from .operations import (
    VaultOperationError,
    add_access_entry_operation,
    create_kv_mount_operation,
    delete_kv_store_operation,
    delete_tashtiot_access_operation,
    get_kv_store_operation,
    get_tashtiot_access_operation,
    list_kv_stores_operation,
    remove_access_entry_operation,
    update_kv_mount_operation,
    upsert_tashtiot_access_operation,
)
from .schemas import (
    K8S_CLUSTER_MAX_LENGTH,
    K8S_CLUSTER_PATTERN,
    K8S_NAMESPACE_MAX_LENGTH,
    K8S_NAMESPACE_PATTERN,
    K8S_SERVICE_ACCOUNT_MAX_LENGTH,
    K8S_SERVICE_ACCOUNT_PATTERN,
    KV_NAME_PATTERN,
    TEAM_PATTERN,
    VAULT_PATH_MAX_LENGTH,
    VAULT_PATH_PATTERN,
    AccessEntry,
    AuthType,
    OperationStatus,
    TashtiotAccessUpsert,
    VaultKVCreate,
    VaultKVUpdate,
    VaultOperationResponse,
)

# A store is addressed by **name alone** — names are unique across the whole values
# directory, so a caller never has to remember which team owns theirs. The team appears
# only on a create, which is the one request that decides where the directory goes.
KVNameParam = Annotated[
    str,
    Path(
        max_length=128,
        pattern=KV_NAME_PATTERN,
        examples=["my-passwords"],
        description="Name of the KV store, unique across the whole values directory.",
    ),
]

TeamParam = Annotated[
    str,
    Path(
        max_length=128,
        pattern=TEAM_PATTERN,
        examples=["cloudops"],
        description="Team whose Tashtiot access file this is.",
    ),
]

# Revoking a grant carries its identity as **query parameters**, not a request body: DELETE
# bodies are widely dropped by proxies and clients. Which parameters are required depends on
# `auth_type` — an OIDC grant is identified by (path, dn) and a Kubernetes one by
# (path, name, namespace, cluster) — so they are validated together in `_revoked_entry`
# rather than by the signature alone.
AuthTypeQuery = Annotated[
    AuthType,
    Query(description="Which kind of grant to revoke: oidc or k8s."),
]

PathQuery = Annotated[
    str,
    Query(
        max_length=VAULT_PATH_MAX_LENGTH,
        pattern=VAULT_PATH_PATTERN,
        examples=["*"],
        description="Secret path pattern of the grant to revoke.",
    ),
]

DnQuery = Annotated[
    Optional[str],
    Query(
        max_length=512,
        examples=["CN=cloudops,OU=Groups,DC=corp,DC=example,DC=com"],
        description="OIDC only: distinguished name of the grant to revoke.",
    ),
]

ServiceAccountQuery = Annotated[
    Optional[str],
    Query(
        max_length=K8S_SERVICE_ACCOUNT_MAX_LENGTH,
        pattern=K8S_SERVICE_ACCOUNT_PATTERN,
        examples=["cloudops-external-secrets"],
        description="k8s only: service account name of the grant to revoke.",
    ),
]

NamespaceQuery = Annotated[
    Optional[str],
    Query(
        max_length=K8S_NAMESPACE_MAX_LENGTH,
        pattern=K8S_NAMESPACE_PATTERN,
        examples=["cloudops"],
        description="k8s only: namespace of the grant to revoke.",
    ),
]

ClusterQuery = Annotated[
    Optional[str],
    Query(
        max_length=K8S_CLUSTER_MAX_LENGTH,
        pattern=K8S_CLUSTER_PATTERN,
        examples=["dev"],
        description="k8s only: OpenShift cluster suffix of the grant to revoke.",
    ),
]


def _json(body: VaultOperationResponse, status_code: int) -> JSONResponse:
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code)


def _revoked_entry(
    auth_type: AuthType,
    path: str,
    dn: Optional[str],
    name: Optional[str],
    namespace: Optional[str],
    cluster: Optional[str],
):
    """Rebuild the identity of the grant being revoked from the query parameters.

    Raises `VaultOperationError` with a 422 when the parameters do not match the
    `auth_type`: a partial identity would otherwise resolve to *some* entry, and revoking
    the wrong grant is worse than refusing an ambiguous request. Capabilities are not taken
    — they are not part of an entry's identity, so asking for them would invite a caller to
    think a partial revoke was possible.
    """
    if auth_type == AuthType.K8S:
        missing = [
            field
            for field, value in (
                ("name", name), ("namespace", namespace), ("cluster", cluster)
            )
            if not value
        ]
        if missing:
            raise VaultOperationError(
                f"auth_type=k8s needs {', '.join(missing)}",
                status_code=422,
            )
        if dn:
            raise VaultOperationError("auth_type=k8s does not take dn", status_code=422)
        # Capabilities play no part in identity; [] keeps the builder's shape.
        return build_k8s_access_entry(path, name, namespace, cluster, [])

    if not dn:
        raise VaultOperationError("auth_type=oidc needs dn", status_code=422)
    extra = [
        field
        for field, value in (
            ("name", name), ("namespace", namespace), ("cluster", cluster)
        )
        if value
    ]
    if extra:
        raise VaultOperationError(
            f"auth_type=oidc does not take {', '.join(extra)}", status_code=422
        )
    return build_oidc_access_entry(path, dn, [])


async def _execute(operation: Any) -> Any:
    """Await an operation, mapping its two failure kinds onto response bodies.

    Every mutating route shares this: `VaultOperationError` already carries the status and
    a renderable body, and an `ExternalServiceError` becomes a 504 when the upstream timed
    out and a 502 otherwise. Failures are *returned*, not raised, so they reuse
    `VaultOperationResponse` without FastAPI validating them against the success model.

    It also owns the one success-status decision: **201 when a pull request was opened,
    200 when nothing was.** Every mutating route declares 201, and this downgrades the
    no-op short circuits, so no route has to know the rule.
    """
    try:
        result = await operation
        if isinstance(result, VaultOperationResponse) and result.pull_request is None:
            # Nothing was opened, so 201 would be a lie. The only way to get here is a
            # short circuit: an update or upsert whose document already matched. One rule,
            # one place — every route's decorator can then say 201 unconditionally.
            return _json(result, 200)
        return result

    except VaultOperationError as operation_error:
        logger.error(f"Vault KV operation failed: {operation_error.message}")
        return _json(operation_error.to_response(), operation_error.status_code)

    except ExternalServiceError as external_error:
        logger.error(
            f"Vault KV operation failed in {external_error.service_name}: "
            f"{external_error.detail}"
        )
        message = f"Exception in {external_error.service_name}. errors: {external_error.detail}"
        return _json(
            VaultOperationResponse(
                status=OperationStatus.FAILED,
                message=message,
                kv_name="",
                error=message,
            ),
            status_code=504 if external_error.status_code == 504 else 502,
        )


async def _read(operation: Any) -> Any:
    """Await a read, mapping failures the same way but treating a 404 upstream as a 404.

    Differs from `_execute` in both directions: a missing file is the caller's mistake
    rather than a bad gateway, and the failure body is a bare `{"status", "error"}` dict
    because reads return raw parsed YAML on success, so there is no response model for a
    failure to match. Putting a read through `_execute` turns every unknown store into 502.
    """
    try:
        return await operation
    except VaultOperationError as operation_error:
        logger.error(f"Vault KV read failed: {operation_error.message}")
        return _json(operation_error.to_response(), operation_error.status_code)
    except ExternalServiceError as external_error:
        return JSONResponse(
            {
                "status": OperationStatus.FAILED.value,
                "error": (
                    f"Exception in {external_error.service_name}. "
                    f"errors: {external_error.detail}"
                ),
            },
            status_code=404 if external_error.status_code == 404 else 502,
        )


def get_v1_vault_router(bitbucket: Any) -> APIRouter:
    """Create the APIRouter for Vault KV operations."""
    router = APIRouter(prefix=config.API_PREFIX, tags=config.API_TAGS)

    # ---------------------------------------------------------------- #
    # creating a store
    # ---------------------------------------------------------------- #
    @router.post(
        "/",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request that creates a KV store",
        description=(
            "Writes '<values dir>/<team>/<kv_name>/' with an oidc-access.yaml and, if the "
            "request carries any k8s grants, a k8s-access.yaml. Opens a pull request and "
            "blocks until the validation build finishes. "
            "**The store is not created when this returns** - the pull request is, and it "
            "is open, green and waiting for a human to merge. This service never merges. "
            "A red or timed-out build declines the pull request and deletes the branch, so "
            "nothing is left behind."
        ),
    )
    async def create_kv(payload: VaultKVCreate):
        logger.info(f"Creating KV store {payload.kv_name} for {payload.team}")
        return await _execute(create_kv_mount_operation(bitbucket, payload))

    # Registered before `/{kv_name}` so a store named `pull-request` cannot shadow it.

    # ---------------------------------------------------------------- #
    # the Tashtiot per-team file
    #
    # Fixed segment, registered before `/{kv_name}`. Its own resource: per-team, one flat
    # file, both auth types in one list.
    # ---------------------------------------------------------------- #
    @router.put(
        "/tashtiot/{team}",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request setting a team's shared-services grants",
        description=(
            "Writes '<values dir>/<Tashtiot dir>/<team>.yaml' - one flat file mixing both "
            "auth types in a single access list. An upsert: create and replace are the "
            "same call because the file's identity is the team. "
            "Answers 201 with the pull request open for review, or 200 with a null "
            "pull_request when the file already said exactly this."
        ),
    )
    async def upsert_tashtiot(team: TeamParam, payload: TashtiotAccessUpsert):
        logger.info(f"Upserting {config.TASHTIOT_DIR} access for {team}")
        return await _execute(
            upsert_tashtiot_access_operation(bitbucket, team, payload)
        )

    @router.get(
        "/tashtiot/{team}",
        summary="One team's shared-services grants",
        description="The parsed file, returned as-is. 404 when the team has none.",
    )
    async def get_tashtiot(team: TeamParam):
        return await _read(get_tashtiot_access_operation(bitbucket, team))

    @router.delete(
        "/tashtiot/{team}",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request removing a team's shared-services grants",
        description=(
            "The file is still there when this returns; the pull request that removes it "
            "is open for review."
        ),
    )
    async def delete_tashtiot(team: TeamParam):
        logger.info(f"Deleting {config.TASHTIOT_DIR} access for {team}")
        return await _execute(delete_tashtiot_access_operation(bitbucket, team))

    # ---------------------------------------------------------------- #
    # listing
    # ---------------------------------------------------------------- #
    @router.get(
        "/stores",
        summary="Every KV store in the values directory",
        description=(
            "Team, name and directory for each. The cheap half of the walk: it lists "
            "directories rather than reading any access file."
        ),
    )
    async def list_stores():
        return await _read(list_kv_stores_operation(bitbucket))

    # ---------------------------------------------------------------- #
    # one grant at a time
    #
    # Two segments, so these cannot collide with the single-segment store routes.
    # ---------------------------------------------------------------- #
    @router.post(
        "/{kv_name}/access",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request granting one principal access",
        description=(
            "Appends one entry to whichever access file its auth_type selects, opens a "
            "pull request and waits for the validation build. A grant already present is "
            "a 409. The grant is not in effect when this returns - a human merges."
        ),
    )
    async def add_access(kv_name: KVNameParam, payload: AccessEntry):
        logger.info(f"Granting access on {kv_name}")
        return await _execute(add_access_entry_operation(bitbucket, kv_name, payload))


    # No `GET /{kv_name}/access`. It returned exactly `GET /{kv_name}`'s `access` list,
    # unwrapped and with nothing else — the same upstream work for strictly less than the
    # store read already gives. Read the store; the grants are the `access` key.
    @router.delete(
        "/{kv_name}/access",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request revoking one grant",
        description=(
            "The grant's identity travels as query parameters, since DELETE bodies are "
            "widely dropped. oidc needs path and dn; k8s needs path, name, namespace and "
            "cluster. A mismatched set is a 422, not a wrong-grant revoke. Capabilities "
            "are not taken - they are not part of a grant's identity. "
            "The grant is still in effect when this returns; the pull request that "
            "revokes it is open for review."
        ),
    )
    async def delete_access(
        kv_name: KVNameParam,
        auth_type: AuthTypeQuery,
        path: PathQuery,
        dn: DnQuery = None,
        name: ServiceAccountQuery = None,
        namespace: NamespaceQuery = None,
        cluster: ClusterQuery = None,
    ):
        try:
            entry = _revoked_entry(auth_type, path, dn, name, namespace, cluster)
        except VaultOperationError as invalid:
            return _json(invalid.to_response(), invalid.status_code)
        logger.info(f"Revoking access on {kv_name}")
        return await _execute(remove_access_entry_operation(bitbucket, kv_name, entry))


    # ---------------------------------------------------------------- #
    # the store itself
    #
    # Single-segment, registered last so every fixed segment above wins.
    # ---------------------------------------------------------------- #

    @router.get(
        "/{kv_name}",
        summary="One KV store",
        description=(
            "Where it lives and every grant on it. Found by name alone, wherever team "
            "owns it."
        ),
    )
    async def get_kv(kv_name: KVNameParam):
        return await _read(get_kv_store_operation(bitbucket, kv_name))

    @router.patch(
        "/{kv_name}",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request replacing a store's grants",
        description=(
            "The access list is replaced wholesale: send the list you want, not a patch "
            "of it. "
            "Answers 201 with the pull request open for review, or 200 with a null "
            "pull_request when the store already said exactly this - that short circuit "
            "is what keeps repeat requests from filling the values repo with empty diffs."
        ),
    )
    async def update_kv(kv_name: KVNameParam, payload: VaultKVUpdate):
        logger.info(f"Updating KV store {kv_name}")
        return await _execute(update_kv_mount_operation(bitbucket, kv_name, payload))

    @router.delete(
        "/{kv_name}",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request that deletes a KV store",
        description=(
            "Removes the store's whole directory, both access files with it, then waits "
            "for the validation build. "
            "**The store is not deleted when this returns** - the pull request that "
            "deletes it is open for review. A repeat call is a 404 only once that pull "
            "request has been merged."
        ),
    )
    async def delete_kv(kv_name: KVNameParam):
        logger.info(f"Deleting KV store {kv_name}")
        return await _execute(delete_kv_store_operation(bitbucket, kv_name))

    return router
