from typing import Annotated, Any

from fastapi import APIRouter, Path, Query
from loguru import logger
from starlette.responses import JSONResponse
from tashtiot_apis_library.connectors import ExternalServiceError

from .conf import config
from .operations import (
    VaultOperationError,
    add_k8s_service_account_operation,
    add_k8s_service_account_pull_request_operation,
    create_kv_mount_operation,
    create_kv_pull_request_operation,
    delete_kv_store_operation,
    delete_kv_store_pull_request_operation,
    get_k8s_service_accounts_operation,
    get_kv_file_operation,
    get_kv_store_operation,
    remove_k8s_service_account_operation,
    remove_k8s_service_account_pull_request_operation,
    update_kv_mount_operation,
)
from .schemas import (
    FILE_PATTERN,
    K8S_CLUSTER_MAX_LENGTH,
    K8S_CLUSTER_PATTERN,
    K8S_NAMESPACE_MAX_LENGTH,
    K8S_NAMESPACE_PATTERN,
    K8S_SERVICE_ACCOUNT_MAX_LENGTH,
    K8S_SERVICE_ACCOUNT_PATTERN,
    KV_NAME_PATTERN,
    K8sServiceAccountCreate,
    OperationStatus,
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

# A binding has no name, so a delete has to carry all three parts of its identity. They are
# query parameters rather than a request body because DELETE bodies are widely dropped by
# proxies and clients, and validated with the same patterns the POST body enforces — an
# unbind for a name the cluster could never have accepted is a 422, not a 404 two calls
# later.
ServiceAccountQuery = Annotated[
    str,
    Query(
        max_length=K8S_SERVICE_ACCOUNT_MAX_LENGTH,
        pattern=K8S_SERVICE_ACCOUNT_PATTERN,
        examples=["vault"],
        description="ServiceAccount name of the binding to remove.",
    ),
]

NamespaceQuery = Annotated[
    str,
    Query(
        max_length=K8S_NAMESPACE_MAX_LENGTH,
        pattern=K8S_NAMESPACE_PATTERN,
        examples=["athena"],
        description="Namespace of the binding to remove.",
    ),
]

ClusterQuery = Annotated[
    str,
    Query(
        max_length=K8S_CLUSTER_MAX_LENGTH,
        pattern=K8S_CLUSTER_PATTERN,
        examples=["dev"],
        description="OS4 cluster suffix of the binding to remove.",
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

    # ----------------------------------------------------------------- #
    # Kubernetes service accounts — a sub-resource of one store
    #
    # These hang off `/{file}/{kv_name}` rather than getting a router of their own,
    # because that is what they are in the document: a list inside a store, not a
    # resource with a namespace of its own. There is no PATCH — an entry is three
    # scalars that together *are* its identity, so changing one is a remove plus an
    # add, and a PATCH would have nothing left to edit.
    #
    # Ordering: the `/pull-request` variants are four segments against the plain
    # ones' three, so nothing here is ambiguous. They are registered first anyway,
    # for one rule across the file.
    # ----------------------------------------------------------------- #
    @router.post(
        "/{file}/{kv_name}/k8s-service-accounts/pull-request",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a pull request binding a service account, without waiting for CI",
        description=(
            "Commits the same change as the bind endpoint and opens the same pull "
            "request, then returns immediately — no validation pipeline, no merge, no "
            "deploy pipeline. The binding is not on the base branch until a human "
            "merges. A failure still deletes the branch it created."
        ),
    )
    async def bind_service_account_pull_request_only(
        file: FileParam, kv_name: KVNameParam, payload: K8sServiceAccountCreate
    ):
        logger.info(
            f"Opening a pull request to bind {payload.service_account} to {kv_name} "
            f"in {file} (no CI, no merge)"
        )
        return await _execute(
            add_k8s_service_account_pull_request_operation(
                bitbucket, file, kv_name, payload
            )
        )

    @router.delete(
        "/{file}/{kv_name}/k8s-service-accounts/pull-request",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Open a pull request unbinding a service account, without waiting for CI",
        description=(
            "Commits the same removal as the unbind endpoint and opens the same pull "
            "request, then returns immediately. The binding is still in place on the "
            "base branch until a human merges."
        ),
    )
    async def unbind_service_account_pull_request_only(
        file: FileParam,
        kv_name: KVNameParam,
        service_account: ServiceAccountQuery,
        namespace: NamespaceQuery,
        cluster: ClusterQuery,
    ):
        logger.info(
            f"Opening a pull request to unbind {service_account} from {kv_name} "
            f"in {file} (no CI, no merge)"
        )
        return await _execute(
            remove_k8s_service_account_pull_request_operation(
                bitbucket, file, kv_name, (service_account, namespace, cluster)
            )
        )

    @router.post(
        "/{file}/{kv_name}/k8s-service-accounts",
        response_model=VaultOperationResponse,
        status_code=201,
        summary="Bind a Kubernetes service account to a KV store",
        description=(
            "Appends a (serviceAccount, namespace, cluster) entry to the store's "
            "k8sServiceAccounts list, through the same pull request and pipeline chain "
            "as a create. The store's other fields and its sibling stores are "
            "untouched. The same triple twice on one store is a 409; the same service "
            "account on a different store is normal and allowed."
        ),
    )
    async def bind_service_account(
        file: FileParam, kv_name: KVNameParam, payload: K8sServiceAccountCreate
    ):
        logger.info(f"Binding {payload.service_account} to {kv_name} in {file}")
        return await _execute(
            add_k8s_service_account_operation(
                bitbucket, woodpecker, file, kv_name, payload
            )
        )

    @router.delete(
        "/{file}/{kv_name}/k8s-service-accounts",
        response_model=VaultOperationResponse,
        summary="Unbind a Kubernetes service account from a KV store",
        description=(
            "Removes one entry from the store's k8sServiceAccounts list, through the "
            "same chain as a create. The binding has no name, so all three parts of its "
            "identity are required as query parameters. Removing the last one drops the "
            "k8sServiceAccounts key entirely, leaving the store as a fresh create would "
            "have written it. Unbinding something that is not bound is a 404."
        ),
    )
    async def unbind_service_account(
        file: FileParam,
        kv_name: KVNameParam,
        service_account: ServiceAccountQuery,
        namespace: NamespaceQuery,
        cluster: ClusterQuery,
    ):
        logger.info(f"Unbinding {service_account} from {kv_name} in {file}")
        return await _execute(
            remove_k8s_service_account_operation(
                bitbucket, woodpecker, file, kv_name, (service_account, namespace, cluster)
            )
        )

    @router.get(
        "/{file}/{kv_name}/k8s-service-accounts",
        summary="Read a KV store's Kubernetes service account bindings",
        description=(
            "Returns the store's k8sServiceAccounts list. A store that binds nothing "
            "answers 200 with an empty list; only a missing file or store is a 404."
        ),
    )
    async def get_service_accounts(file: FileParam, kv_name: KVNameParam):
        logger.info(f"Reading service account bindings for {kv_name} in {file}")
        return await _read(get_k8s_service_accounts_operation(bitbucket, file, kv_name))

    return router
