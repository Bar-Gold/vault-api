from typing import Annotated, Any

from fastapi import APIRouter, Path
from loguru import logger
from starlette.responses import JSONResponse
from tashtiot_apis_library.connectors import ExternalServiceError

from .conf import config
from .operations import (
    VaultOperationError,
    create_kubernetes_auth_operation,
    create_kubernetes_auth_pull_request_operation,
    create_kv_mount_operation,
    create_kv_pull_request_operation,
    delete_kubernetes_auth_operation,
    delete_kubernetes_auth_pull_request_operation,
    delete_kv_store_operation,
    delete_kv_store_pull_request_operation,
    get_kubernetes_auth_file_operation,
    get_kubernetes_auth_role_operation,
    get_kv_file_operation,
    get_kv_store_operation,
    update_kubernetes_auth_operation,
    update_kv_mount_operation,
)
from .schemas import (
    FILE_PATTERN,
    K8S_ROLE_NAME_PATTERN,
    KV_NAME_PATTERN,
    OperationStatus,
    VaultKubernetesAuthCreate,
    VaultKubernetesAuthUpdate,
    VaultKVCreate,
    VaultKVUpdate,
    VaultOperationResponse,
)

# The same patterns the create body enforces, applied to the path parameters that address a
# store. `file` is the only value that reaches a filesystem path, so a malformed one is a
# 422 here rather than an opaque Bitbucket 404 two calls later.
FileParam = Annotated[
    str,
    Path(
        max_length=128,
        pattern=FILE_PATTERN,
        examples=["payments"],
        description="Values file, committed as '<values dir>/<file>.yaml'.",
    ),
]

KVNameParam = Annotated[
    str,
    Path(
        max_length=128,
        pattern=KV_NAME_PATTERN,
        examples=["myapp"],
        description="Name of the KV store inside that file.",
    ),
]

RoleNameParam = Annotated[
    str,
    Path(
        max_length=128,
        pattern=K8S_ROLE_NAME_PATTERN,
        examples=["myapp-ci"],
        description="Name of the Kubernetes auth role inside that file.",
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
    """
    try:
        return await operation

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
    """Await a read, mapping failures the same way but treating a 404 upstream as a 404."""
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


def get_v1_vault_router(bitbucket: Any, woodpecker: Any) -> APIRouter:
    """Create the APIRouter for Vault KV operations."""
    router = APIRouter(prefix=config.API_PREFIX, tags=config.API_TAGS)

    @router.post(
        "/",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Create a KV store",
        description=(
            "Appends a store to the values file named by 'file', creating that file if it "
            "is the first one. Opens a Bitbucket pull request, waits for the validation "
            "pipeline, merges, then waits for the deploy pipeline. The request blocks for "
            "the whole chain. The store name must be unique across every file in the "
            "values directory."
        ),
    )
    async def create(payload: VaultKVCreate):
        logger.info(f"Creating KV store {payload.kv_name} in {payload.file}")
        return await _execute(create_kv_mount_operation(bitbucket, woodpecker, payload))

    # Registered before the `/{file}` routes. There is no POST on those paths today, so
    # nothing is ambiguous — but if one is ever added, this fixed segment must keep
    # winning, or a file named "pull-request" would shadow this endpoint.
    @router.post(
        "/pull-request",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a pull request for a new KV store, without waiting for CI",
        description=(
            "Commits the same change as a create and opens the same pull request, then "
            "returns immediately — no validation pipeline, no merge, no deploy pipeline. "
            "The pull request is left OPEN for a human to review and merge, so nothing "
            "reaches the base branch. The uniqueness check still applies, and a failure "
            "still deletes the branch it created."
        ),
    )
    async def create_pull_request_only(payload: VaultKVCreate):
        logger.info(
            f"Opening a pull request for KV store {payload.kv_name} in {payload.file} "
            f"(no CI, no merge)"
        )
        return await _execute(create_kv_pull_request_operation(bitbucket, payload))

    @router.patch(
        "/{file}/{kv_name}",
        response_model=VaultOperationResponse,
        summary="Update a KV store's description or roles",
        description=(
            "Edits one store inside a values file, through the same pull request and "
            "pipeline chain as a create. Its siblings in the file are untouched. The name "
            "is not editable — renaming means migrating the secrets in Vault. Roles are "
            "replaced wholesale, so a host is removed by omitting it. An edit that changes "
            "nothing succeeds without opening a pull request."
        ),
    )
    async def update(file: FileParam, kv_name: KVNameParam, payload: VaultKVUpdate):
        logger.info(f"Updating KV store {kv_name} in {file}")
        return await _execute(
            update_kv_mount_operation(bitbucket, woodpecker, file, kv_name, payload)
        )

    # Three segments against the delete route's two, so unlike POST /pull-request there is
    # no ambiguity to protect with ordering — registered first anyway, for one rule.
    @router.delete(
        "/{file}/{kv_name}/pull-request",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a pull request removing a KV store, without waiting for CI",
        description=(
            "Commits the same removal as a delete and opens the same pull request, then "
            "returns immediately — no validation pipeline, no merge, no deploy pipeline. "
            "The store is still defined on the base branch until a human merges. A failure "
            "still deletes the branch it created."
        ),
    )
    async def delete_pull_request_only(file: FileParam, kv_name: KVNameParam):
        logger.info(
            f"Opening a pull request to delete KV store {kv_name} from {file} "
            f"(no CI, no merge)"
        )
        return await _execute(
            delete_kv_store_pull_request_operation(bitbucket, file, kv_name)
        )

    @router.delete(
        "/{file}/{kv_name}",
        response_model=VaultOperationResponse,
        summary="Delete a KV store",
        description=(
            "Removes one store from a values file, through the same pull request and "
            "pipeline chain as a create. Its siblings in the file are untouched, and "
            "removing the last one leaves the file behind with an empty kvStores list. "
            "Deleting a store that is not there is a 404, so a repeat call reports 404 "
            "rather than pretending it did the work again."
        ),
    )
    async def delete_store(file: FileParam, kv_name: KVNameParam):
        logger.info(f"Deleting KV store {kv_name} from {file}")
        return await _execute(
            delete_kv_store_operation(bitbucket, woodpecker, file, kv_name)
        )

    @router.get(
        "/{file}/{kv_name}",
        summary="Read one KV store",
        description="Returns a single entry from the values file's kvStores list.",
    )
    async def get_store(file: FileParam, kv_name: KVNameParam):
        logger.info(f"Reading KV store {kv_name} from {file}")
        return await _read(get_kv_store_operation(bitbucket, file, kv_name))

    @router.get(
        "/{file}",
        summary="Read a whole values file",
        description=(
            "Returns the committed file for this name, parsed as YAML — every store it "
            "defines under kvStores."
        ),
    )
    async def get_file(file: FileParam):
        logger.info(f"Reading values file {file}")
        return await _read(get_kv_file_operation(bitbucket, file))

    return router


def get_v1_kubernetes_auth_router(bitbucket: Any, woodpecker: Any) -> APIRouter:
    """Create the APIRouter for Kubernetes auth roles.

    A prefix of its own rather than a segment under `/kv`: anything nested there would sit
    in a `{file}` or `{kv_name}` position, so a file or store actually named
    "kubernetes-auth" would fight it for the URL. A separate prefix has zero shadowing risk
    in either direction.
    """
    router = APIRouter(
        prefix=config.API_K8S_AUTH_PREFIX, tags=config.API_K8S_AUTH_TAGS
    )

    @router.post(
        "/",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Create a Kubernetes auth role",
        description=(
            "Appends a role to the values file named by 'file' — the same file the KV "
            "stores live in — creating that file if it is the first entry in it. Opens a "
            "Bitbucket pull request, waits for the validation pipeline, merges, then waits "
            "for the deploy pipeline. The request blocks for the whole chain. Every KV "
            "store named in 'access' must already exist on the base branch."
        ),
    )
    async def create_role(payload: VaultKubernetesAuthCreate):
        logger.info(
            f"Creating Kubernetes auth role {payload.role_name} in {payload.file}"
        )
        return await _execute(
            create_kubernetes_auth_operation(bitbucket, woodpecker, payload)
        )

    # Registered before the `/{file}` routes, the same rule the KV router follows: this
    # fixed segment sits in a `{file}` position, so a file named "pull-request" would
    # shadow the endpoint if the order were reversed. A GET on the same URL is still an
    # ordinary read of a file with that name.
    @router.post(
        "/pull-request",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a pull request for a new Kubernetes auth role, without waiting for CI",
        description=(
            "Commits the same change as a create and opens the same pull request, then "
            "returns immediately — no validation pipeline, no merge, no deploy pipeline. "
            "The pull request is left OPEN for a human to review and merge. The uniqueness "
            "and store-existence checks still apply, and a failure still deletes the "
            "branch it created."
        ),
    )
    async def create_role_pull_request_only(payload: VaultKubernetesAuthCreate):
        logger.info(
            f"Opening a pull request for Kubernetes auth role {payload.role_name} in "
            f"{payload.file} (no CI, no merge)"
        )
        return await _execute(
            create_kubernetes_auth_pull_request_operation(bitbucket, payload)
        )

    @router.patch(
        "/{file}/{role_name}",
        response_model=VaultOperationResponse,
        summary="Update a Kubernetes auth role",
        description=(
            "Edits one role inside a values file, through the same pull request and "
            "pipeline chain as a create. Its siblings are untouched. Neither the name nor "
            "the cluster is editable — both are part of the role's identity in Vault, so "
            "changing either is a delete plus a create. Service accounts, namespaces and "
            "access are replaced wholesale, so an entry is removed by omitting it. An edit "
            "that changes nothing succeeds without opening a pull request."
        ),
    )
    async def update_role(
        file: FileParam, role_name: RoleNameParam, payload: VaultKubernetesAuthUpdate
    ):
        logger.info(f"Updating Kubernetes auth role {role_name} in {file}")
        return await _execute(
            update_kubernetes_auth_operation(
                bitbucket, woodpecker, file, role_name, payload
            )
        )

    @router.delete(
        "/{file}/{role_name}/pull-request",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a pull request removing a Kubernetes auth role, without waiting for CI",
        description=(
            "Commits the same removal as a delete and opens the same pull request, then "
            "returns immediately. The role is still defined on the base branch until a "
            "human merges. A failure still deletes the branch it created."
        ),
    )
    async def delete_role_pull_request_only(file: FileParam, role_name: RoleNameParam):
        logger.info(
            f"Opening a pull request to delete Kubernetes auth role {role_name} from "
            f"{file} (no CI, no merge)"
        )
        return await _execute(
            delete_kubernetes_auth_pull_request_operation(bitbucket, file, role_name)
        )

    @router.delete(
        "/{file}/{role_name}",
        response_model=VaultOperationResponse,
        summary="Delete a Kubernetes auth role",
        description=(
            "Removes one role from a values file, through the same pull request and "
            "pipeline chain as a create. The KV stores it referenced are left alone — a "
            "store with no role pointing at it is perfectly valid. Deleting a role that is "
            "not there is a 404."
        ),
    )
    async def delete_role(file: FileParam, role_name: RoleNameParam):
        logger.info(f"Deleting Kubernetes auth role {role_name} from {file}")
        return await _execute(
            delete_kubernetes_auth_operation(bitbucket, woodpecker, file, role_name)
        )

    @router.get(
        "/{file}/{role_name}",
        summary="Read one Kubernetes auth role",
        description="Returns a single entry from the values file's kubernetesAuth list.",
    )
    async def get_role(file: FileParam, role_name: RoleNameParam):
        logger.info(f"Reading Kubernetes auth role {role_name} from {file}")
        return await _read(
            get_kubernetes_auth_role_operation(bitbucket, file, role_name)
        )

    @router.get(
        "/{file}",
        summary="Read every Kubernetes auth role in a values file",
        description=(
            "Returns the file's kubernetesAuth list. A file that declares no roles answers "
            "200 with an empty list; only a missing file is a 404."
        ),
    )
    async def get_roles(file: FileParam):
        logger.info(f"Reading Kubernetes auth roles from {file}")
        return await _read(get_kubernetes_auth_file_operation(bitbucket, file))

    return router
