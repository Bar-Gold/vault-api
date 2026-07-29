from typing import Any

from fastapi import APIRouter, Query
from loguru import logger
from starlette.responses import JSONResponse
from tashtiot_apis_library.connectors import ExternalServiceError

from .conf import config
from .operations import (
    VaultOperationError,
    create_kv_mount_operation,
    get_kv_mount_operation,
)
from .schemas import (
    OperationStatus,
    VaultKVCreate,
    VaultKVOperationResponse,
)


def _json(body: VaultKVOperationResponse, status_code: int) -> JSONResponse:
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code)


def get_v1_vault_router(bitbucket: Any, woodpecker: Any) -> APIRouter:
    """Create the APIRouter for Vault KV operations."""
    router = APIRouter(prefix=config.API_PREFIX, tags=config.API_TAGS)

    @router.post(
        "/",
        response_model=VaultKVOperationResponse,
        status_code=201,
        summary="Create a Vault KV mount",
        description=(
            "Opens a Bitbucket pull request adding the KV mount and its read/write policies, "
            "waits for the validation pipeline, merges the pull request, then waits for the "
            "deploy pipeline that applies the change to Vault. The request blocks for the "
            "whole chain and returns 'Successful creation of <mount path>' on success."
        ),
    )
    async def create(payload: VaultKVCreate):
        logger.info(
            f"Creating Vault KV mount for app={payload.spec.app_name} "
            f"env={payload.metadata.environment}"
        )
        try:
            return await create_kv_mount_operation(bitbucket, woodpecker, payload)

        except VaultOperationError as operation_error:
            logger.error(f"Vault KV create failed: {operation_error.message}")
            return _json(operation_error.to_response(), operation_error.status_code)

        except ExternalServiceError as external_error:
            logger.error(
                f"Vault KV create failed in {external_error.service_name}: {external_error.detail}"
            )
            message = (
                f"Exception in {external_error.service_name}. errors: {external_error.detail}"
            )
            # An upstream that timed out is reported as a gateway timeout, matching how a
            # pipeline timeout is reported; everything else is a bad gateway.
            return _json(
                VaultKVOperationResponse(
                    status=OperationStatus.FAILED,
                    message=message,
                    mount_path="",
                    error=message,
                ),
                status_code=504 if external_error.status_code == 504 else 502,
            )

    @router.get(
        "/{app_name}",
        summary="Read a Vault KV mount definition",
        description="Returns the committed values file for the app's KV mount in an environment.",
    )
    async def get_mount(app_name: str, environment: str = Query(..., description="Infra environment, e.g. prod.")):
        logger.info(f"Reading Vault KV mount for app={app_name} env={environment}")
        try:
            return await get_kv_mount_operation(bitbucket, environment, app_name)
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
