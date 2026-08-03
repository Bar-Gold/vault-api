from typing import Any

from fastapi import APIRouter
from loguru import logger
from starlette.responses import JSONResponse
from tashtiot_apis_library.connectors import ExternalServiceError

from .conf import config
from .operations import (
    VaultOperationError,
    create_kv_mount_operation,
    create_kv_pull_request_operation,
    get_kv_file_operation,
    get_kv_store_operation,
    update_kv_mount_operation,
)
from .schemas import (
    OperationStatus,
    VaultKVCreate,
    VaultKVOperationResponse,
    VaultKVUpdate,
)


def _json(body: VaultKVOperationResponse, status_code: int) -> JSONResponse:
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code)


async def _execute(operation: Any) -> Any:
    """Await an operation, mapping its two failure kinds onto response bodies.

    Every mutating route shares this: `VaultOperationError` already carries the status and
    a renderable body, and an `ExternalServiceError` becomes a 504 when the upstream timed
    out and a 502 otherwise. Failures are *returned*, not raised, so they reuse
    `VaultKVOperationResponse` without FastAPI validating them against the success model.
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
            VaultKVOperationResponse(
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
        response_model=VaultKVOperationResponse,
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
        response_model=VaultKVOperationResponse,
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
        response_model=VaultKVOperationResponse,
        summary="Update a KV store's description or roles",
        description=(
            "Edits one store inside a values file, through the same pull request and "
            "pipeline chain as a create. Its siblings in the file are untouched. The name "
            "is not editable — renaming means migrating the secrets in Vault. Roles are "
            "replaced wholesale, so a host is removed by omitting it. An edit that changes "
            "nothing succeeds without opening a pull request."
        ),
    )
    async def update(file: str, kv_name: str, payload: VaultKVUpdate):
        logger.info(f"Updating KV store {kv_name} in {file}")
        return await _execute(
            update_kv_mount_operation(bitbucket, woodpecker, file, kv_name, payload)
        )

    @router.get(
        "/{file}/{kv_name}",
        summary="Read one KV store",
        description="Returns a single entry from the values file's kvStores list.",
    )
    async def get_store(file: str, kv_name: str):
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
    async def get_file(file: str):
        logger.info(f"Reading values file {file}")
        return await _read(get_kv_file_operation(bitbucket, file))

    return router
