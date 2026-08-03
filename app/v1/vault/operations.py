"""Business logic for Vault KV stores.

A values file holds a **list** of stores under `kvStores`, so a create appends an entry to
`kv/<file>.yaml` rather than owning a file of its own. The file is created if this is its
first store.

The create flow is a GitOps chain with two CI gates:

    1. commit the values file to a short-lived branch
    2. open a pull request against the base branch
    3. wait for the **validation** pipeline (Woodpecker, `pull_request` event) — it blocks the merge
    4. merge the pull request
    5. wait for the **deploy** pipeline (Woodpecker, `push` event) — it applies the change to Vault

The request blocks until step 5 finishes, then returns "Successful creation of <name>".

There is no transaction, so each step rolls back what the earlier steps did:
committing fails -> delete the branch; the PR fails to open -> delete the branch; the
validation pipeline fails or times out -> decline the PR and delete the branch. **The merge
is the point of no return**: once step 4 succeeds the change is on the base branch, so a
failing deploy pipeline is reported as-is rather than rolled back — un-merging would need a
revert PR and a second human decision.
"""

import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
from loguru import logger
from tashtiot_apis_library.connectors import ExternalServiceError

from ...clients.bitbucket import PullRequest
from ...clients.woodpecker import Pipeline, PipelineTimeoutError
from ...helpers import (
    KVStoreNotFound,
    add_kv_store,
    build_branch_name,
    build_kv_store,
    find_kv_store,
    kv_store_names,
    render_values_yaml,
    update_kv_store,
    values_file_path,
    yaml_data_equals,
)
from .conf import config
from .schemas import (
    OperationStatus,
    PipelineInfo,
    PullRequestInfo,
    VaultKVCreate,
    VaultKVOperationResponse,
    VaultKVUpdate,
)


class VaultOperationError(Exception):
    """A create flow that failed for a business reason rather than a transport error.

    Carries everything the route needs to build the failed response body, so the route
    stays a thin mapping layer.
    """

    def __init__(
        self,
        message: str,
        kv_name: str,
        file: str = "",
        status_code: int = 502,
        pull_request: Optional[PullRequest] = None,
        validation_pipeline: Optional[Pipeline] = None,
        deploy_pipeline: Optional[Pipeline] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kv_name = kv_name
        self.file = file
        self.status_code = status_code
        self.pull_request = pull_request
        self.validation_pipeline = validation_pipeline
        self.deploy_pipeline = deploy_pipeline

    def to_response(self) -> "VaultKVOperationResponse":
        """The failed response body, so the route only has to pick the HTTP status."""
        return VaultKVOperationResponse(
            status=OperationStatus.FAILED,
            message=self.message,
            kv_name=self.kv_name,
            file=self.file,
            pull_request=_pull_request_info(self.pull_request),
            validation_pipeline=_pipeline_info(self.validation_pipeline),
            deploy_pipeline=_pipeline_info(self.deploy_pipeline),
            error=self.message,
        )


# --------------------------------------------------------------------------- #
# small adapters
# --------------------------------------------------------------------------- #
def _pull_request_info(pull_request: Optional[PullRequest]) -> Optional[PullRequestInfo]:
    if pull_request is None:
        return None
    return PullRequestInfo(id=pull_request.id, url=pull_request.url, state=pull_request.state)


def _pipeline_info(pipeline: Optional[Pipeline]) -> Optional[PipelineInfo]:
    if pipeline is None:
        return None
    return PipelineInfo(number=pipeline.number, status=pipeline.status, link=pipeline.link)


def _pipeline_failure(kind: str, pipeline: Pipeline) -> str:
    link = f" ({pipeline.link})" if pipeline.link else ""
    return f"{kind} pipeline #{pipeline.number} finished with status '{pipeline.status}'{link}"


# --------------------------------------------------------------------------- #
# pipeline matchers
# --------------------------------------------------------------------------- #
def pull_request_pipeline_matcher(branch: str, min_number: int) -> Callable[[Pipeline], bool]:
    """Match the validation pipeline for our PR.

    Woodpecker records the source branch differently per forge and event (`branch`, `ref`
    or `refspec`), so we look for our branch name in any of them. `min_number` rules out
    pipelines that already existed before this request started.
    """

    def _matches(pipeline: Pipeline) -> bool:
        if pipeline.event != "pull_request" or pipeline.number <= min_number:
            return False
        return branch in " ".join([pipeline.branch, pipeline.ref, pipeline.refspec])

    return _matches


def deploy_pipeline_matcher(
    base_branch: str, merge_commit: Optional[str], min_number: int
) -> Callable[[Pipeline], bool]:
    """Match the post-merge pipeline.

    Prefer the merge commit sha, which is unambiguous. Bitbucket does not always return it
    on the merge response, so fall back to "a push pipeline on the base branch newer than
    anything that existed before we merged".
    """

    def _matches(pipeline: Pipeline) -> bool:
        if pipeline.event != "push" or pipeline.number <= min_number:
            return False
        if merge_commit:
            return pipeline.commit == merge_commit
        return pipeline.branch == base_branch

    return _matches


async def _latest_pipeline_number(woodpecker: Any) -> int:
    """Highest existing pipeline number, used as the `min_number` watermark.

    A failure here only costs us the watermark, so it degrades to 0 rather than aborting
    the whole operation.
    """
    try:
        pipelines = await woodpecker.list_pipelines()
    except Exception as exc:
        logger.warning(f"Could not read the current Woodpecker pipeline list: {exc}")
        return 0
    return max((pipeline.number for pipeline in pipelines), default=0)


# --------------------------------------------------------------------------- #
# rollback helpers
# --------------------------------------------------------------------------- #
async def _delete_branch_quietly(bitbucket: Any, branch: str) -> None:
    try:
        await bitbucket.delete_branch(branch)
    except Exception as cleanup_error:
        logger.exception(f"Failed deleting branch {branch}: {cleanup_error}")


async def _decline_and_cleanup(bitbucket: Any, pull_request: PullRequest, branch: str) -> None:
    """Close the PR and remove the branch after a failed validation pipeline."""
    try:
        # Re-read the PR: CI status updates and reviewer activity bump Bitbucket's
        # optimistic-locking `version`, and declining with a stale one is rejected.
        current = await bitbucket.get_pull_request(pull_request.id)
        await bitbucket.decline_pull_request(current.id, current.version)
    except Exception as cleanup_error:
        logger.exception(
            f"Failed declining pull request {pull_request.id}: {cleanup_error}"
        )
    await _delete_branch_quietly(bitbucket, branch)


async def _read_document(
    bitbucket: Any, path: str, kv_name: str, file: str
) -> Optional[Dict[str, Any]]:
    """Parsed values file at the base branch, or None when it does not exist yet.

    None is a legitimate answer, not an error: the first store in a file creates it.
    """
    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH
    try:
        raw = await bitbucket.get_file_content(path, at=base_branch)
    except ExternalServiceError as exc:
        if exc.status_code == 404:
            return None
        raise

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as parse_error:
        raise VaultOperationError(
            f"{path} exists on {base_branch} but is not valid YAML: {parse_error}",
            kv_name=kv_name,
            file=file,
        ) from parse_error

    # `kvStores:` with nothing under it parses to None — an empty file, not a broken one.
    if parsed is not None and not isinstance(parsed, dict):
        raise VaultOperationError(
            f"{path} exists on {base_branch} but is not a YAML mapping",
            kv_name=kv_name,
            file=file,
        )
    return parsed


async def _assert_name_is_free(bitbucket: Any, kv_name: str, file: str) -> None:
    """Reject a create whose store name is already used **anywhere** in the values dir.

    Store names are global to Vault, so uniqueness cannot be scoped to one file. That costs
    a directory listing plus a read per file, which is why it happens once, up front.

    This is a check, not a lock: two creates in flight both pass it, and the second pull
    request then conflicts at merge. That is the same race the whole GitOps flow already
    has, and a human resolves it.
    """
    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH
    values_dir = config.VAULT_VALUES_DIR

    for relative_path in await bitbucket.list_files(values_dir, at=base_branch):
        if not relative_path.endswith((".yaml", ".yml")):
            continue
        path = f"{values_dir.strip('/')}/{relative_path.lstrip('/')}"
        try:
            raw = await bitbucket.get_file_content(path, at=base_branch)
            parsed = yaml.safe_load(raw)
        except ExternalServiceError as exc:
            if exc.status_code == 404:
                # Listed but unreadable — deleted between the list and the read.
                continue
            raise
        except yaml.YAMLError:
            # A file we did not write, or a bad merge. It cannot be scanned, but it must
            # not block an unrelated create either.
            logger.warning(f"Skipping unparseable values file during the name scan: {path}")
            continue

        if kv_name in kv_store_names(parsed):
            raise VaultOperationError(
                f"{kv_name} already exists (defined in {path} on {base_branch})",
                kv_name=kv_name,
                file=file,
                status_code=409,
            )


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #
async def _open_pull_request(
    bitbucket: Any,
    *,
    path: str,
    content: str,
    branch: str,
    summary: str,
    description: str,
    kv_name: str,
    source_commit_id: Optional[str] = None,
) -> PullRequest:
    """branch -> commit -> open PR, and nothing after that.

    Both rollbacks live here: a failed commit or a failed PR deletes the branch, so a
    failure leaves the repo exactly as it was found. Touches no CI and never merges.

    Shared by `_commit_via_pull_request` (which goes on to gate and merge) and
    `create_kv_pull_request_operation` (which stops here). One implementation means the
    two cannot drift apart in what they roll back.
    """
    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH

    await bitbucket.create_branch(branch, base_branch)
    try:
        await bitbucket.put_file(
            path=path,
            branch=branch,
            content=content,
            message=summary,
            source_commit_id=source_commit_id,
        )
    except Exception:
        await _delete_branch_quietly(bitbucket, branch)
        raise

    try:
        pull_request = await bitbucket.create_pull_request(
            title=summary,
            description=description,
            from_branch=branch,
            to_branch=base_branch,
            reviewers=config.PR_REVIEWERS,
        )
    except Exception:
        await _delete_branch_quietly(bitbucket, branch)
        raise

    logger.info(f"Opened pull request {pull_request.id} for {kv_name}")
    return pull_request


async def _commit_via_pull_request(
    bitbucket: Any,
    woodpecker: Any,
    *,
    path: str,
    content: str,
    branch: str,
    summary: str,
    description: str,
    kv_name: str,
    source_commit_id: Optional[str] = None,
) -> Tuple[PullRequest, Pipeline, Pipeline]:
    """branch -> commit -> PR -> gate 1 -> merge -> gate 2, with every rollback.

    Shared by the create and the edit flows: the only thing that differs between them is
    what gets written and whether an optimistic-lock token is needed. Keeping one
    implementation means the rollback asymmetry cannot drift between operations.

    Returns the merged pull request and both pipelines. Raises `VaultOperationError` for
    every business failure, having already rolled back whatever is still safe to roll back.
    """
    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH

    # Watermark before anything can trigger CI, so we never match a pre-existing pipeline.
    # This has to precede the branch creation, hence not folding it into _open_pull_request.
    baseline = await _latest_pipeline_number(woodpecker)

    # ---- 1-2. branch + commit + pull request ------------------------------- #
    pull_request = await _open_pull_request(
        bitbucket,
        path=path,
        content=content,
        branch=branch,
        summary=summary,
        description=description,
        kv_name=kv_name,
        source_commit_id=source_commit_id,
    )

    # ---- 3. validation pipeline (blocks the merge) ------------------------- #
    try:
        validation = await woodpecker.await_pipeline(
            pull_request_pipeline_matcher(branch, baseline)
        )
    except PipelineTimeoutError as timeout_error:
        await _decline_and_cleanup(bitbucket, pull_request, branch)
        raise VaultOperationError(
            f"Validation pipeline did not complete: {timeout_error.message}",
            kv_name=kv_name,
            status_code=504,
            pull_request=pull_request,
            validation_pipeline=timeout_error.pipeline,
        ) from timeout_error

    if not validation.succeeded:
        await _decline_and_cleanup(bitbucket, pull_request, branch)
        raise VaultOperationError(
            _pipeline_failure("Validation", validation),
            kv_name=kv_name,
            pull_request=pull_request,
            validation_pipeline=validation,
        )

    # ---- 4. merge (point of no return) ------------------------------------- #
    merge_baseline = await _latest_pipeline_number(woodpecker)
    try:
        current = await bitbucket.get_pull_request(pull_request.id)
        merged = await bitbucket.merge_pull_request(current.id, current.version)
    except ExternalServiceError as merge_error:
        # The PR is valid but unmergeable (conflict, missing approvals, stale version).
        # Leave it open — a human can resolve and merge it.
        raise VaultOperationError(
            f"Pull request {pull_request.id} passed validation but could not be merged: "
            f"{merge_error.detail}",
            kv_name=kv_name,
            pull_request=pull_request,
            validation_pipeline=validation,
        ) from merge_error

    logger.info(f"Merged pull request {merged.id} for {kv_name}")

    # ---- 5. deploy pipeline ------------------------------------------------ #
    try:
        deploy = await woodpecker.await_pipeline(
            deploy_pipeline_matcher(base_branch, merged.merge_commit, merge_baseline)
        )
    except PipelineTimeoutError as timeout_error:
        raise VaultOperationError(
            f"Deploy pipeline did not complete: {timeout_error.message}. "
            f"The change is already merged to {base_branch}.",
            kv_name=kv_name,
            status_code=504,
            pull_request=merged,
            validation_pipeline=validation,
            deploy_pipeline=timeout_error.pipeline,
        ) from timeout_error

    if not deploy.succeeded:
        raise VaultOperationError(
            f"{_pipeline_failure('Deploy', deploy)}. "
            f"The change is already merged to {base_branch} and needs a revert.",
            kv_name=kv_name,
            pull_request=merged,
            validation_pipeline=validation,
            deploy_pipeline=deploy,
        )

    return merged, validation, deploy


def _branch_for(file: str, kv_name: str, branch_suffix: Optional[str]) -> str:
    return build_branch_name(
        file, kv_name, branch_suffix or uuid.uuid4().hex[:8], config.BRANCH_PREFIX
    )


def _create_description(payload: VaultKVCreate) -> str:
    roles = "\n".join(
        f"  - {role}: {', '.join(hosts)}" for role, hosts in payload.roles.items()
    )
    return (
        f"Automated by vault-api.\n\n"
        f"- file: {config.VAULT_VALUES_DIR}/{payload.file}.yaml\n"
        f"- store: {payload.kv_name}\n"
        f"- description: {payload.kv_description}\n"
        f"- roles:\n{roles}\n"
    )


async def _prepare_create(
    bitbucket: Any, payload: VaultKVCreate
) -> Tuple[str, str, Optional[str]]:
    """Everything a create needs before it touches a branch.

    Returns the file path, the rendered document with the new store appended, and the
    optimistic-lock token — `None` when the file does not exist yet, which is what tells
    Bitbucket to treat the write as a create rather than an edit.
    """
    path = values_file_path(config.VAULT_VALUES_DIR, payload.file)

    # Names are global to Vault, so this scans every file, not just the target one.
    await _assert_name_is_free(bitbucket, payload.kv_name, payload.file)

    current = await _read_document(bitbucket, path, payload.kv_name, payload.file)
    updated = add_kv_store(
        current,
        build_kv_store(payload.kv_name, payload.kv_description, payload.roles),
    )

    source_commit_id = None
    if current is not None:
        source_commit_id = await bitbucket.get_last_commit(
            path, at=config.VAULT_VALUES_REPO_BASE_BRANCH
        )

    return path, render_values_yaml(updated), source_commit_id


async def create_kv_mount_operation(
    bitbucket: Any,
    woodpecker: Any,
    payload: VaultKVCreate,
    branch_suffix: Optional[str] = None,
) -> VaultKVOperationResponse:
    """Append the store to its file, then block until both pipelines have finished."""
    path, content, source_commit_id = await _prepare_create(bitbucket, payload)

    merged, validation, deploy = await _commit_via_pull_request(
        bitbucket,
        woodpecker,
        path=path,
        content=content,
        branch=_branch_for(payload.file, payload.kv_name, branch_suffix),
        summary=f"Create KV store {payload.kv_name} in {payload.file}",
        description=_create_description(payload),
        kv_name=payload.kv_name,
        source_commit_id=source_commit_id,
    )

    return VaultKVOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=f"Successful creation of {payload.kv_name}",
        kv_name=payload.kv_name,
        file=payload.file,
        pull_request=_pull_request_info(merged),
        validation_pipeline=_pipeline_info(validation),
        deploy_pipeline=_pipeline_info(deploy),
    )


async def create_kv_pull_request_operation(
    bitbucket: Any,
    payload: VaultKVCreate,
    branch_suffix: Optional[str] = None,
) -> VaultKVOperationResponse:
    """Open the pull request and stop — no CI gates, no merge.

    The same duplicate guard, branch, commit and pull request as a create, minus everything
    that waits or merges. Returns as soon as Bitbucket has the PR, so it answers in one
    round-trip instead of blocking for the length of two pipelines.

    Nothing reaches the base branch: whoever reviews the pull request decides that. Note
    that this takes no Woodpecker client at all — the argument would be dead weight.
    """
    path, content, source_commit_id = await _prepare_create(bitbucket, payload)

    pull_request = await _open_pull_request(
        bitbucket,
        path=path,
        content=content,
        branch=_branch_for(payload.file, payload.kv_name, branch_suffix),
        summary=f"Create KV store {payload.kv_name} in {payload.file}",
        description=_create_description(payload),
        kv_name=payload.kv_name,
        source_commit_id=source_commit_id,
    )

    return VaultKVOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} for {payload.kv_name}. "
            f"It is not merged — review, CI and merge are up to you."
        ),
        kv_name=payload.kv_name,
        file=payload.file,
        pull_request=_pull_request_info(pull_request),
    )


# --------------------------------------------------------------------------- #
# edits to an existing store
# --------------------------------------------------------------------------- #
async def _require_document(
    bitbucket: Any, path: str, kv_name: str, file: str
) -> Dict[str, Any]:
    """Like `_read_document`, but a missing file is a 404 rather than an empty start."""
    document = await _read_document(bitbucket, path, kv_name, file)
    if document is None:
        raise VaultOperationError(
            f"{file} does not exist ({path} is not on "
            f"{config.VAULT_VALUES_REPO_BASE_BRANCH})",
            kv_name=kv_name,
            file=file,
            status_code=404,
        )
    return document


async def update_kv_mount_operation(
    bitbucket: Any,
    woodpecker: Any,
    file: str,
    kv_name: str,
    payload: VaultKVUpdate,
    branch_suffix: Optional[str] = None,
) -> VaultKVOperationResponse:
    """Edit one store's description and/or roles. Its name and siblings are untouched.

    An edit that changes nothing returns success without opening a pull request — the
    reason `yaml_data_equals` exists. That keeps repeat requests from filling the values
    repo with empty pull requests.
    """
    path = values_file_path(config.VAULT_VALUES_DIR, file)
    current = await _require_document(bitbucket, path, kv_name, file)

    try:
        updated = update_kv_store(
            current,
            kv_name,
            description=payload.kv_description,
            roles=payload.roles,
        )
    except KVStoreNotFound as missing:
        raise VaultOperationError(
            f"{kv_name} is not defined in {path}",
            kv_name=kv_name,
            file=file,
            status_code=404,
        ) from missing

    if yaml_data_equals(current, updated):
        logger.info(f"No change required for {kv_name}; skipping the pull request")
        return VaultKVOperationResponse(
            status=OperationStatus.SUCCEEDED,
            message=f"No changes required for {kv_name}",
            kv_name=kv_name,
            file=file,
        )

    changes = ", ".join(
        part
        for part in (
            f"description: {payload.kv_description!r}"
            if payload.kv_description is not None
            else "",
            f"roles: {payload.roles}" if payload.roles is not None else "",
        )
        if part
    )

    # Editing an existing file needs Bitbucket's optimistic-lock token for that path.
    source_commit_id = await bitbucket.get_last_commit(
        path, at=config.VAULT_VALUES_REPO_BASE_BRANCH
    )

    merged, validation, deploy = await _commit_via_pull_request(
        bitbucket,
        woodpecker,
        path=path,
        content=render_values_yaml(updated),
        branch=_branch_for(file, kv_name, branch_suffix),
        summary=f"Update KV store {kv_name} in {file}",
        description=(
            f"Automated by vault-api.\n\n"
            f"- file: {path}\n"
            f"- store: {kv_name}\n"
            f"- changes: {changes}\n"
        ),
        kv_name=kv_name,
        source_commit_id=source_commit_id,
    )

    return VaultKVOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=f"Successful update of {kv_name}",
        kv_name=kv_name,
        file=file,
        pull_request=_pull_request_info(merged),
        validation_pipeline=_pipeline_info(validation),
        deploy_pipeline=_pipeline_info(deploy),
    )


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
async def get_kv_file_operation(bitbucket: Any, file: str) -> Dict[str, Any]:
    """The whole values file — every store it defines."""
    path = values_file_path(config.VAULT_VALUES_DIR, file)
    return await _require_document(bitbucket, path, kv_name="", file=file)


async def get_kv_store_operation(
    bitbucket: Any, file: str, kv_name: str
) -> Dict[str, Any]:
    """One store out of a values file."""
    path = values_file_path(config.VAULT_VALUES_DIR, file)
    document = await _require_document(bitbucket, path, kv_name, file)

    store = find_kv_store(document, kv_name)
    if store is None:
        raise VaultOperationError(
            f"{kv_name} is not defined in {path}",
            kv_name=kv_name,
            file=file,
            status_code=404,
        )
    return store


