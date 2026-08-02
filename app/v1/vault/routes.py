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
    get_kv_mount_operation,
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


def get_v1_vault_router(bitbucket: Any, woodpecker: Any) -> APIRouter:
    """Create the APIRouter for Vault KV operations."""
    router = APIRouter(prefix=config.API_PREFIX, tags=config.API_TAGS)

    @router.post(
        "/",
        response_model=VaultKVOperationResponse,
        status_code=201,
        summary="Create a KV",
        description=(
            "Commits a file with the KV's name and description, opens a Bitbucket pull "
            "request, waits for the validation pipeline, merges, then waits for the deploy "
            "pipeline. The request blocks for the whole chain and returns "
            "'Successful creation of <kv name>' on success. What the KV means in Vault is "
            "the deploy pipeline's business, not this service's."
        ),
    )
    async def create(payload: VaultKVCreate):
        logger.info(f"Creating KV {payload.kv_name}")
        return await _execute(create_kv_mount_operation(bitbucket, woodpecker, payload))

    # Registered before the `/{kv_name:path}` routes. There is no POST on that path today,
    # so nothing is ambiguous — but if one is ever added, this fixed segment must keep
    # winning, or a KV named "pull-request" would shadow this endpoint.
    @router.post(
        "/pull-request",
        response_model=VaultKVOperationResponse,
        status_code=201,
        summary="Open a pull request for a new KV, without waiting for CI",
        description=(
            "Commits the same file as a create and opens the same pull request, then "
            "returns immediately — no validation pipeline, no merge, no deploy pipeline. "
            "The pull request is left OPEN for a human to review and merge, so nothing "
            "reaches the base branch. Use this when you want the request to answer in one "
            "round-trip instead of blocking for the whole chain. The duplicate guard still "
            "applies, and a failure still deletes the branch it created."
        ),
    )
    async def create_pull_request_only(payload: VaultKVCreate):
        logger.info(f"Opening a pull request for KV {payload.kv_name} (no CI, no merge)")
        return await _execute(create_kv_pull_request_operation(bitbucket, payload))

    @router.patch(
        "/{kv_name:path}",
        response_model=VaultKVOperationResponse,
        summary="Update a KV's description or owner",
        description=(
            "Edits the committed file in place through the same pull request and pipeline "
            "chain as a create. The name is not editable — renaming means migrating the "
            "secrets in Vault, not editing a field. An edit that changes nothing succeeds "
            "without opening a pull request."
        ),
    )
    async def update(kv_name: str, payload: VaultKVUpdate):
        logger.info(f"Updating KV {kv_name}")
        return await _execute(
            update_kv_mount_operation(bitbucket, woodpecker, kv_name, payload)
        )

    @router.get(
        "/{kv_name:path}",
        summary="Read a KV's committed file",
        description="Returns the file committed for this KV, parsed as YAML.",
    )
    async def get_mount(kv_name: str):
        logger.info(f"Reading KV {kv_name}")
        try:
            return await get_kv_mount_operation(bitbucket, kv_name)
        except VaultOperationError as operation_error:
            logger.error(f"Vault KV read failed: {operation_error.message}")
            return _json(operation_error.to_response(), operation_error.status_code)
        except ExternalServiceError as external_error:
            return JSONResponse(
                {
                    "status": OperationStatus.FAILED.value,
                    "error": f"Exception in {external_error.service_name}. errors: {external_error.detail}",
                },
                status_code=404 if external_error.status_code == 404 else 502,
            )

    return router
