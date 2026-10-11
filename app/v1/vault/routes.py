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

**No route takes query parameters, and every route but the reads takes a JSON body** that
carries the metadata block — the `DELETE`s included. A revoke's identity is in its body,
and a store or Tashtiot delete sends a body holding the metadata alone.
`test_no_mutating_route_takes_a_query_parameter` pins the rule against the OpenAPI schema.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Path
from loguru import logger
from starlette.responses import JSONResponse
from tashtiot_apis_library.connectors import ExternalServiceError

from .conf import config
from .operations import (
    VaultOperationError,
    add_access_entry_operation,
    add_tashtiot_access_entry_operation,
    create_kv_mount_operation,
    delete_kv_store_operation,
    delete_tashtiot_access_operation,
    get_kv_store_operation,
    get_tashtiot_access_operation,
    list_kv_stores_operation,
    remove_access_entry_operation,
    remove_tashtiot_access_entry_operation,
    update_kv_mount_operation,
    upsert_tashtiot_access_operation,
)
from .schemas import (
    KV_NAME_PATTERN,
    KV_NAME_READ_PATTERN,
    TEAM_PATTERN,
    TASHTIOT_K8S_ENTRY_EXAMPLE,
    TASHTIOT_K8S_REVOKE_EXAMPLE,
    TASHTIOT_OIDC_ENTRY_EXAMPLE,
    TASHTIOT_OIDC_REVOKE_EXAMPLE,
    TEAM_READ_PATTERN,
    AccessEntryRequest,
    AccessRevokeRequest,
    DeleteRequest,
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

# The same two, as the **read** routes accept them: capitals allowed. A create still writes
# a lowercase name, but the values directory may already hold a capitalised store or team —
# the walk finds those and `_resolve_store` matches them exactly, so rejecting the request
# here made them unreachable rather than malformed. Single segment either way, so the
# path-traversal guard is unchanged. Only `GET` uses these: an edit or a delete of such a
# store is still a 422, because writing to a name this service would not itself create is a
# separate decision.
KVNameReadParam = Annotated[
    str,
    Path(
        max_length=128,
        pattern=KV_NAME_READ_PATTERN,
        examples=["my-passwords"],
        description=(
            "Name of the KV store, unique across the whole values directory. Matched "
            "exactly, so capitals must match the directory's own spelling."
        ),
    ),
]

TeamReadParam = Annotated[
    str,
    Path(
        max_length=128,
        pattern=TEAM_READ_PATTERN,
        examples=["cloudops"],
        description=(
            "Team whose Tashtiot access file this is. Matched exactly, so capitals must "
            "match the file's own spelling."
        ),
    ),
]

def _json(body: VaultOperationResponse, status_code: int) -> JSONResponse:
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code)


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
    # No `tags=` on the router. FastAPI concatenates a router's tags with a route's, so a
    # default here would put the Tashtiot routes in both documentation sections. Each route
    # declares its own; registration order is unchanged, and still the collision guard.
    router = APIRouter(prefix=config.API_PREFIX)

    # ---------------------------------------------------------------- #
    # creating a store
    # ---------------------------------------------------------------- #
    @router.post(
        "/",
        tags=config.API_TAGS,
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
            "nothing is left behind. "
            "The metadata block is recorded in the pull-request description for "
            "audit and is never written to the access files."
        ),
    )
    async def create_kv(payload: VaultKVCreate):
        logger.info(
            f"Creating KV store {payload.kv_name} for {payload.team}"
            f" [{payload.metadata.as_log_text()}]"
        )
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
        tags=config.API_TASHTIOT_TAGS,
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request setting a team's shared-services grants",
        description=(
            "Writes '<values dir>/<Tashtiot dir>/<team>.yaml' - one flat file mixing both "
            "auth types in a single access list. An upsert: create and replace are the "
            "same call because the file's identity is the team. "
            "Answers 201 with the pull request open for review, or 200 with a null "
            "pull_request when the file already said exactly this. "
            "The metadata block is recorded in the pull-request description for "
            "audit and is never written to the access files. "
            "Metadata alone is not a change: a request whose grants already match "
            "still answers 200 without opening a pull request."
        ),
    )
    async def upsert_tashtiot(team: TeamParam, payload: TashtiotAccessUpsert):
        logger.info(
            f"Upserting {config.TASHTIOT_DIR} access for {team}"
            f" [{payload.metadata.as_log_text()}]"
        )
        return await _execute(
            upsert_tashtiot_access_operation(bitbucket, team, payload)
        )

    @router.get(
        "/tashtiot/{team}",
        tags=config.API_TASHTIOT_TAGS,
        summary="One team's shared-services grants",
        description="The parsed file, returned as-is. 404 when the team has none.",
    )
    async def get_tashtiot(team: TeamReadParam):
        return await _read(get_tashtiot_access_operation(bitbucket, team))

    @router.delete(
        "/tashtiot/{team}",
        tags=config.API_TASHTIOT_TAGS,
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request removing a team's shared-services grants",
        description=(
            "The file is still there when this returns; the pull request that removes it "
            "is open for review. "
            "The body is the metadata block alone, recorded in the pull-request "
            "description for audit."
        ),
    )
    async def delete_tashtiot(team: TeamParam, payload: DeleteRequest):
        logger.info(
            f"Deleting {config.TASHTIOT_DIR} access for {team}"
            f" [{payload.metadata.as_log_text()}]"
        )
        return await _execute(
            delete_tashtiot_access_operation(bitbucket, team, payload)
        )

    # One grant at a time, the same pair the store sub-resource has. `PUT /tashtiot/{team}`
    # replaces the whole list, which is the wrong tool for adding one principal to a file
    # that already has ten: a caller would have to read, edit and send the lot back, and
    # race anyone doing the same. Three segments, so neither collides with `/{kv_name}`
    # or `/{kv_name}/access`.
    @router.post(
        "/tashtiot/{team}/access",
        tags=config.API_TASHTIOT_TAGS,
        response_model=VaultOperationResponse,
        status_code=201,
        # Swagger prefills its editable box from these rather than from the shared models'
        # own `"*"`: one pair of models, two routes that want different starting points,
        # since a Tashtiot grant reaches *into* the shared services store and is scoped in
        # practice. Examples only — `path` stays required and omitting it is still a 422.
        #
        # Declared as `openapi_extra` rather than `Body(openapi_examples=...)` on the
        # payload **on purpose**. Annotating the payload replaces the discriminated union's
        # generated schema with a bare `anyOf`, which loses the `oneOf` + discriminator that
        # Swagger's branch picker is built from — and
        # `test_the_grant_body_schema_is_the_request_entry_pair` fails when it does.
        # FastAPI deep-merges `openapi_extra`, so this adds the examples and leaves the
        # schema untouched.
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "oidc": {
                                "summary": "An OIDC group, scoped to one team's subtree",
                                "value": TASHTIOT_OIDC_ENTRY_EXAMPLE,
                            },
                            "k8s": {
                                "summary": "A Kubernetes service account, same scoping",
                                "value": TASHTIOT_K8S_ENTRY_EXAMPLE,
                            },
                        }
                    }
                }
            }
        },
        summary="Open a validated pull request adding one shared-services grant",
        description=(
            "Appends one entry to the team's flat file, either auth type - the file "
            "mixes both. 409 when the identity is already granted, 404 when the team has "
            "no file yet (use PUT to create it). "
            "The grant is not in effect when this returns; the pull request that adds it "
            "is open for review. "
            "The metadata block is recorded in the pull-request description for "
            "audit and is never written to the access files."
        ),
    )
    async def add_tashtiot_access(team: TeamParam, payload: AccessEntryRequest):
        logger.info(
            f"Adding a {config.TASHTIOT_DIR} grant for {team}"
            f" [{payload.metadata.as_log_text()}]"
        )
        return await _execute(
            add_tashtiot_access_entry_operation(bitbucket, team, payload)
        )

    @router.delete(
        "/tashtiot/{team}/access",
        tags=config.API_TASHTIOT_TAGS,
        response_model=VaultOperationResponse,
        status_code=201,
        # The same prefill as the add above, minus capabilities: revoking is usually undoing
        # a grant someone added here, and the same `openapi_extra` spelling keeps the
        # branch picker for the same reason.
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "oidc": {
                                "summary": "An OIDC group, scoped to one team's subtree",
                                "value": TASHTIOT_OIDC_REVOKE_EXAMPLE,
                            },
                            "k8s": {
                                "summary": "A Kubernetes service account, same scoping",
                                "value": TASHTIOT_K8S_REVOKE_EXAMPLE,
                            },
                        }
                    }
                }
            }
        },
        summary="Open a validated pull request revoking one shared-services grant",
        description=(
            "The grant's identity is the JSON body, exactly as it is for a store: oidc "
            "needs path and dn; k8s needs path, name, namespace and cluster. A missing or "
            "mismatched field is a 422. Revoking the team's last grant removes the file. "
            "The grant is still in effect when this returns; the pull request that "
            "revokes it is open for review. "
            "The metadata block is recorded in the pull-request description for "
            "audit and is never written to the access files."
        ),
    )
    async def delete_tashtiot_access(team: TeamParam, payload: AccessRevokeRequest):
        logger.info(
            f"Revoking a {config.TASHTIOT_DIR} grant from {team}"
            f" [{payload.metadata.as_log_text()}]"
        )
        return await _execute(
            remove_tashtiot_access_entry_operation(bitbucket, team, payload)
        )

    # ---------------------------------------------------------------- #
    # listing
    # ---------------------------------------------------------------- #
    @router.get(
        "/stores",
        tags=config.API_TAGS,
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
        tags=config.API_TAGS,
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request granting one principal access",
        description=(
            "Appends one entry to whichever access file its auth_type selects, opens a "
            "pull request and waits for the validation build. A grant already present is "
            "a 409. The grant is not in effect when this returns - a human merges. "
            "The metadata block is recorded in the pull-request description for "
            "audit and is never written to the access files."
        ),
    )
    async def add_access(kv_name: KVNameParam, payload: AccessEntryRequest):
        logger.info(
            f"Granting access on {kv_name} [{payload.metadata.as_log_text()}]"
        )
        return await _execute(add_access_entry_operation(bitbucket, kv_name, payload))


    # No `GET /{kv_name}/access`. It returned exactly `GET /{kv_name}`'s `access` list,
    # unwrapped and with nothing else — the same upstream work for strictly less than the
    # store read already gives. Read the store; the grants are the `access` key.
    @router.delete(
        "/{kv_name}/access",
        tags=config.API_TAGS,
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request revoking one grant",
        description=(
            "The grant's identity is the JSON body: oidc needs path and dn; k8s needs "
            "path, name, namespace and cluster. A missing or mismatched field is a 422, "
            "not a wrong-grant revoke. Capabilities are not taken - they are not part of "
            "a grant's identity, and sending them is a 422. "
            "The grant is still in effect when this returns; the pull request that "
            "revokes it is open for review. "
            "The metadata block is recorded in the pull-request description for "
            "audit and is never written to the access files."
        ),
    )
    async def delete_access(kv_name: KVNameParam, payload: AccessRevokeRequest):
        logger.info(
            f"Revoking access on {kv_name} [{payload.metadata.as_log_text()}]"
        )
        return await _execute(
            remove_access_entry_operation(bitbucket, kv_name, payload)
        )


    # ---------------------------------------------------------------- #
    # the store itself
    #
    # Single-segment, registered last so every fixed segment above wins.
    # ---------------------------------------------------------------- #

    @router.get(
        "/{kv_name}",
        tags=config.API_TAGS,
        summary="One KV store",
        description=(
            "Where it lives and every grant on it. Found by name alone, wherever team "
            "owns it."
        ),
    )
    async def get_kv(kv_name: KVNameReadParam):
        return await _read(get_kv_store_operation(bitbucket, kv_name))

    @router.patch(
        "/{kv_name}",
        tags=config.API_TAGS,
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request replacing a store's grants",
        description=(
            "The access list is replaced wholesale: send the list you want, not a patch "
            "of it. "
            "Answers 201 with the pull request open for review, or 200 with a null "
            "pull_request when the store already said exactly this - that short circuit "
            "is what keeps repeat requests from filling the values repo with empty diffs. "
            "The metadata block is recorded in the pull-request description for "
            "audit and is never written to the access files. "
            "Metadata alone is not a change: a request whose grants already match "
            "still answers 200 without opening a pull request."
        ),
    )
    async def update_kv(kv_name: KVNameParam, payload: VaultKVUpdate):
        logger.info(
            f"Updating KV store {kv_name} [{payload.metadata.as_log_text()}]"
        )
        return await _execute(update_kv_mount_operation(bitbucket, kv_name, payload))

    @router.delete(
        "/{kv_name}",
        tags=config.API_TAGS,
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a validated pull request that deletes a KV store",
        description=(
            "Removes the store's whole directory, both access files with it, then waits "
            "for the validation build. "
            "**The store is not deleted when this returns** - the pull request that "
            "deletes it is open for review. A repeat call is a 404 only once that pull "
            "request has been merged. "
            "The body is the metadata block alone, recorded in the pull-request "
            "description for audit."
        ),
    )
    async def delete_kv(kv_name: KVNameParam, payload: DeleteRequest):
        logger.info(
            f"Deleting KV store {kv_name} [{payload.metadata.as_log_text()}]"
        )
        return await _execute(delete_kv_store_operation(bitbucket, kv_name, payload))

    return router
