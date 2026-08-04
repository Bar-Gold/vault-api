"""Business logic for Vault KV stores and Kubernetes auth roles.

A values file holds two independent **lists** of named entries — stores under `kvStores`
and auth roles under `kubernetesAuth` — so a create of either kind appends to
`kv/<file>.yaml` rather than owning a file of its own. The file is created if this is its
first entry, and an operation on one key leaves the other untouched.

The two are not independent in one direction: a role's `access` names the stores it
reaches, so deleting a store something still points at is refused rather than orphaned.
What `(store, capability)` *means* in Vault — the mount, the policy, the HCL — is the
deploy pipeline's business; this service never generates any of it.

The create flow is a GitOps chain with two CI gates:

    1. commit the values file to a short-lived branch
    2. open a pull request against the base branch
    3. wait for the **validation** pipeline (Woodpecker, `pull_request` event) — it blocks the merge
    4. merge the pull request
    5. wait for the **deploy** pipeline (Woodpecker, `push` event) — it applies the change to Vault

The request blocks until step 5 finishes, then returns "Successful creation of <name>".
Update and delete run the same chain over a different document — one entry edited, one
entry removed — so all three share its rollbacks rather than each hand-rolling them.

There is no transaction, so each step rolls back what the earlier steps did:
committing fails -> delete the branch; the PR fails to open -> delete the branch; the
validation pipeline fails or times out -> decline the PR and delete the branch. **The merge
is the point of no return**: once step 4 succeeds the change is on the base branch, so a
failing deploy pipeline is reported as-is rather than rolled back — un-merging would need a
revert PR and a second human decision.
"""

import uuid
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set, Tuple

import yaml
from loguru import logger
from tashtiot_apis_library.connectors import ExternalServiceError

from ...clients.bitbucket import PullRequest
from ...clients.woodpecker import Pipeline, PipelineTimeoutError
from ...helpers import (
    KVStoreNotFound,
    add_kubernetes_auth_role,
    add_kv_store,
    build_branch_name,
    build_kubernetes_auth_role,
    build_kv_store,
    find_kubernetes_auth_role,
    find_kv_store,
    kubernetes_auth_role_identities,
    kubernetes_auth_roles,
    kv_store_names,
    kv_store_referrers,
    remove_kubernetes_auth_role,
    remove_kv_store,
    render_values_yaml,
    update_kubernetes_auth_role,
    update_kv_store,
    values_file_path,
    yaml_data_equals,
)
from .conf import config
from .schemas import (
    OperationStatus,
    PipelineInfo,
    PullRequestInfo,
    VaultKubernetesAuthCreate,
    VaultKubernetesAuthUpdate,
    VaultKVCreate,
    VaultKVUpdate,
    VaultOperationResponse,
)


class VaultOperationError(Exception):
    """A create flow that failed for a business reason rather than a transport error.

    Carries everything the route needs to build the failed response body, so the route
    stays a thin mapping layer.
    """

    def __init__(
        self,
        message: str,
        kv_name: str = "",
        file: str = "",
        status_code: int = 502,
        role_name: str = "",
        pull_request: Optional[PullRequest] = None,
        validation_pipeline: Optional[Pipeline] = None,
        deploy_pipeline: Optional[Pipeline] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kv_name = kv_name
        self.file = file
        self.status_code = status_code
        self.role_name = role_name
        self.pull_request = pull_request
        self.validation_pipeline = validation_pipeline
        self.deploy_pipeline = deploy_pipeline

    def to_response(self) -> "VaultOperationResponse":
        """The failed response body, so the route only has to pick the HTTP status.

        `role_name` has to be carried through here as well as `kv_name`: this is the single
        constructor of the failure body, so a field it forgets is a field every failure
        response silently omits.
        """
        return VaultOperationResponse(
            status=OperationStatus.FAILED,
            message=self.message,
            kv_name=self.kv_name,
            role_name=self.role_name,
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
    bitbucket: Any, path: str, kv_name: str, file: str, role_name: str = ""
) -> Optional[Dict[str, Any]]:
    """Parsed values file at the base branch, or None when it does not exist yet.

    None is a legitimate answer, not an error: the first entry in a file creates it.
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
            role_name=role_name,
        ) from parse_error

    # `kvStores:` with nothing under it parses to None — an empty file, not a broken one.
    if parsed is not None and not isinstance(parsed, dict):
        raise VaultOperationError(
            f"{path} exists on {base_branch} but is not a YAML mapping",
            kv_name=kv_name,
            file=file,
            role_name=role_name,
        )
    return parsed


async def _walk_values_files(bitbucket: Any) -> AsyncIterator[Tuple[str, Any]]:
    """Every parsed yaml under `VAULT_VALUES_DIR` on the base branch, path first.

    Three scans need this — the store-name scan, the auth-role uniqueness scan and the
    referential check a delete runs — so "read and parse every file, skipping the ones that
    cannot be parsed" has one implementation. A file we did not write, or a bad merge, is
    logged and skipped rather than blocking an unrelated operation.

    It costs a directory listing plus a read per file, which is why every caller does it
    once, up front, and takes everything it needs out of the same pass.
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
            logger.warning(f"Skipping unparseable values file: {path}")
            continue

        yield path, parsed


async def _assert_name_is_free(bitbucket: Any, kv_name: str, file: str) -> None:
    """Reject a create whose store name is already used **anywhere** in the values dir.

    Store names are global to Vault, so uniqueness cannot be scoped to one file.

    This is a check, not a lock: two creates in flight both pass it, and the second pull
    request then conflicts at merge. That is the same race the whole GitOps flow already
    has, and a human resolves it.
    """
    async for path, parsed in _walk_values_files(bitbucket):
        if kv_name in kv_store_names(parsed):
            raise VaultOperationError(
                f"{kv_name} already exists (defined in {path} on "
                f"{config.VAULT_VALUES_REPO_BASE_BRANCH})",
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
    kv_name: str = "",
    role_name: str = "",
    source_commit_id: Optional[str] = None,
) -> PullRequest:
    """branch -> commit -> open PR, and nothing after that.

    Both rollbacks live here: a failed commit or a failed PR deletes the branch, so a
    failure leaves the repo exactly as it was found. Touches no CI and never merges.

    Shared by `_commit_via_pull_request` (which goes on to gate and merge) and both PR-only
    operations (which stop here). One implementation means they cannot drift apart in what
    they roll back. `kv_name` / `role_name` only name the subject in the log line — exactly
    one of them is set, by whichever resource kind is being written.
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

    logger.info(f"Opened pull request {pull_request.id} for {role_name or kv_name}")
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
    kv_name: str = "",
    role_name: str = "",
    source_commit_id: Optional[str] = None,
) -> Tuple[PullRequest, Pipeline, Pipeline]:
    """branch -> commit -> PR -> gate 1 -> merge -> gate 2, with every rollback.

    Shared by every mutating flow over both resource kinds: the only thing that differs
    between them is what gets written and whether an optimistic-lock token is needed.
    Keeping one implementation means the rollback asymmetry cannot drift between
    operations. `kv_name` / `role_name` are carried only so a failure names its subject.

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
        role_name=role_name,
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
            role_name=role_name,
            status_code=504,
            pull_request=pull_request,
            validation_pipeline=timeout_error.pipeline,
        ) from timeout_error

    if not validation.succeeded:
        await _decline_and_cleanup(bitbucket, pull_request, branch)
        raise VaultOperationError(
            _pipeline_failure("Validation", validation),
            kv_name=kv_name,
            role_name=role_name,
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
            role_name=role_name,
            pull_request=pull_request,
            validation_pipeline=validation,
        ) from merge_error

    logger.info(f"Merged pull request {merged.id} for {role_name or kv_name}")

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
            role_name=role_name,
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
            role_name=role_name,
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
) -> VaultOperationResponse:
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

    return VaultOperationResponse(
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
) -> VaultOperationResponse:
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

    return VaultOperationResponse(
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
    bitbucket: Any, path: str, kv_name: str, file: str, role_name: str = ""
) -> Dict[str, Any]:
    """Like `_read_document`, but a missing file is a 404 rather than an empty start."""
    document = await _read_document(bitbucket, path, kv_name, file, role_name)
    if document is None:
        raise VaultOperationError(
            f"{file} does not exist ({path} is not on "
            f"{config.VAULT_VALUES_REPO_BASE_BRANCH})",
            kv_name=kv_name,
            file=file,
            role_name=role_name,
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
) -> VaultOperationResponse:
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
        return VaultOperationResponse(
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

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=f"Successful update of {kv_name}",
        kv_name=kv_name,
        file=file,
        pull_request=_pull_request_info(merged),
        validation_pipeline=_pipeline_info(validation),
        deploy_pipeline=_pipeline_info(deploy),
    )


# --------------------------------------------------------------------------- #
# removing a store
# --------------------------------------------------------------------------- #
async def _assert_no_referrers(bitbucket: Any, kv_name: str, file: str) -> None:
    """Refuse to delete a store a Kubernetes auth role still names in its `access`.

    Scoped to the whole values directory, not the target file: a role in `kv/platform.yaml`
    may perfectly well reach a store defined in `kv/payments.yaml`.

    The alternative was cascading the delete into those roles, which is a multi-resource
    mutation hidden behind a single-resource URI — the blast radius would be invisible in
    the request. Orphaning the binding silently is worse still, so this refuses and names
    the blockers, and the caller deletes them first.
    """
    blockers = []
    async for path, parsed in _walk_values_files(bitbucket):
        referrers = kv_store_referrers(parsed, kv_name)
        if referrers:
            blockers.append(f"{', '.join(referrers)} ({path})")

    if blockers:
        raise VaultOperationError(
            f"{kv_name} is referenced by kubernetesAuth role(s) {'; '.join(blockers)}; "
            f"delete those first",
            kv_name=kv_name,
            file=file,
            status_code=409,
        )


async def _prepare_delete(
    bitbucket: Any, file: str, kv_name: str
) -> Tuple[str, str, Optional[str]]:
    """Everything a delete needs before it touches a branch.

    The counterpart of `_prepare_create`, and shared by both delete paths for the same
    reason: they cannot then disagree about what is a 404.

    It walks the values directory like a create does, but for the opposite question: not
    "is this name free" (a create-only concern) but "does anything still point at it".
    Unlike an update there is no `yaml_data_equals` short circuit either — if
    `remove_kv_store` did not raise, the document changed. A removal is never a no-op.
    """
    path = values_file_path(config.VAULT_VALUES_DIR, file)
    current = await _require_document(bitbucket, path, kv_name, file)

    try:
        updated = remove_kv_store(current, kv_name)
    except KVStoreNotFound as missing:
        raise VaultOperationError(
            f"{kv_name} is not defined in {path}",
            kv_name=kv_name,
            file=file,
            status_code=404,
        ) from missing

    await _assert_no_referrers(bitbucket, kv_name, file)

    # The file exists by definition here, so the write is an edit and Bitbucket wants the
    # optimistic-lock token for that path.
    source_commit_id = await bitbucket.get_last_commit(
        path, at=config.VAULT_VALUES_REPO_BASE_BRANCH
    )

    return path, render_values_yaml(updated), source_commit_id


def _delete_description(path: str, kv_name: str) -> str:
    return (
        f"Automated by vault-api.\n\n"
        f"- file: {path}\n"
        f"- store: {kv_name}\n"
        f"- change: removed from kvStores\n"
    )


async def delete_kv_store_operation(
    bitbucket: Any,
    woodpecker: Any,
    file: str,
    kv_name: str,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Remove one store from its file, then block until both pipelines have finished.

    A content edit of the file, not a file removal: the last store out leaves `kvStores: []`
    behind. That keeps `GET /{file}` answering 200, keeps a later create appending
    normally, and needs no Bitbucket call this service does not already make.

    The chain and every rollback come from `_commit_via_pull_request` unchanged, so the
    merge is the point of no return here too — a deploy pipeline that fails after a delete
    is reported, and the message says the removal already needs a revert.
    """
    path, content, source_commit_id = await _prepare_delete(bitbucket, file, kv_name)

    merged, validation, deploy = await _commit_via_pull_request(
        bitbucket,
        woodpecker,
        path=path,
        content=content,
        branch=_branch_for(file, kv_name, branch_suffix),
        summary=f"Delete KV store {kv_name} from {file}",
        description=_delete_description(path, kv_name),
        kv_name=kv_name,
        source_commit_id=source_commit_id,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=f"Successful deletion of {kv_name}",
        kv_name=kv_name,
        file=file,
        pull_request=_pull_request_info(merged),
        validation_pipeline=_pipeline_info(validation),
        deploy_pipeline=_pipeline_info(deploy),
    )


async def delete_kv_store_pull_request_operation(
    bitbucket: Any,
    file: str,
    kv_name: str,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open the pull request that removes the store, and stop — no CI gates, no merge.

    The escape hatch matters more for a delete than for a create: it is the most
    destructive operation here, and this hands the decision to a reviewer. Takes no
    Woodpecker client, exactly as the PR-only create does.

    Nothing reaches the base branch, so the store is still there afterwards — and a repeat
    call opens a second pull request for the same removal, for the same reason a repeat
    PR-only create does.
    """
    path, content, source_commit_id = await _prepare_delete(bitbucket, file, kv_name)

    pull_request = await _open_pull_request(
        bitbucket,
        path=path,
        content=content,
        branch=_branch_for(file, kv_name, branch_suffix),
        summary=f"Delete KV store {kv_name} from {file}",
        description=_delete_description(path, kv_name),
        kv_name=kv_name,
        source_commit_id=source_commit_id,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} to delete {kv_name}. "
            f"It is not merged — review, CI and merge are up to you."
        ),
        kv_name=kv_name,
        file=file,
        pull_request=_pull_request_info(pull_request),
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


# --------------------------------------------------------------------------- #
# Kubernetes auth roles
#
# A second kind of entry in the same values file, so every one of these runs the *same*
# chain over the *same* file: `_open_pull_request` and `_commit_via_pull_request` are
# reused verbatim, which is what keeps the rollback asymmetry identical. The merge is the
# point of no return here too.
#
# What differs is only what is prepared before the branch exists, because a role has two
# things a store does not: an identity that includes an optional cluster, and a reference
# to stores that must already be there.
# --------------------------------------------------------------------------- #
def _k8s_branch_for(file: str, role_name: str, branch_suffix: Optional[str]) -> str:
    """Its own prefix, so a reviewer can tell the change kind from the branch name."""
    return build_branch_name(
        file,
        role_name,
        branch_suffix or uuid.uuid4().hex[:8],
        config.K8S_AUTH_BRANCH_PREFIX,
    )


def _k8s_description(path: str, role_name: str, change: str) -> str:
    return (
        f"Automated by vault-api.\n\n"
        f"- file: {path}\n"
        f"- kubernetes auth role: {role_name}\n"
        f"- {change}\n"
    )


def _access_stores(access: Dict[str, List[str]]) -> Set[str]:
    return {store for stores in access.values() for store in stores}


def _assert_stores_exist(
    known_stores: Set[str],
    access: Dict[str, List[str]],
    file: str,
    role_name: str,
) -> None:
    """Every store an `access` names must already be on the base branch.

    Consequence worth knowing: a store created through `POST /kv/pull-request` is on an
    unmerged branch, so a role referencing it straight away is a 409 until that pull
    request lands. Same base-branch-only visibility that makes a repeat PR-only create open
    a second pull request.
    """
    missing = sorted(_access_stores(access) - known_stores)
    if missing:
        raise VaultOperationError(
            f"access names unknown KV store(s) {missing}; they must already exist on "
            f"{config.VAULT_VALUES_REPO_BASE_BRANCH}",
            file=file,
            role_name=role_name,
            status_code=409,
        )


async def _prepare_k8s_auth_create(
    bitbucket: Any, payload: VaultKubernetesAuthCreate
) -> Tuple[str, str, Optional[str]]:
    """Everything a Kubernetes auth create needs before it touches a branch.

    Shared by both create paths for the same reason `_prepare_create` is: they cannot then
    diverge on what counts as a duplicate. One directory walk answers both questions it
    asks — is this role's identity taken, and do the stores it names exist.
    """
    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH
    path = values_file_path(config.VAULT_VALUES_DIR, payload.file)

    known_stores: Set[str] = set()
    async for scanned_path, parsed in _walk_values_files(bitbucket):
        known_stores.update(kv_store_names(parsed))

        for cluster, name in kubernetes_auth_role_identities(parsed):
            if name != payload.role_name:
                continue
            # Identity is `(cluster, name)`, with `""` standing in for an absent cluster:
            # `cluster` is optional here (an estate with one Kubernetes auth mount has none
            # to name), so its absence is itself a coordinate rather than a wildcard. The
            # same role name in two *different* clusters is legitimate — a Vault k8s role
            # is scoped to its mount.
            if cluster == (payload.cluster or ""):
                raise VaultOperationError(
                    f"{payload.role_name} already exists (defined in {scanned_path} on "
                    f"{base_branch})",
                    file=payload.file,
                    role_name=payload.role_name,
                    status_code=409,
                )
            # Different cluster, same file: the routes address a role as
            # `{file}/{role_name}`, so two of them in one file would be unaddressable.
            if scanned_path == path:
                raise VaultOperationError(
                    f"{payload.role_name} already exists in {scanned_path} for cluster "
                    f"'{cluster}'; a role name must be unique within a file",
                    file=payload.file,
                    role_name=payload.role_name,
                    status_code=409,
                )

    _assert_stores_exist(known_stores, payload.access, payload.file, payload.role_name)

    current = await _read_document(
        bitbucket, path, kv_name="", file=payload.file, role_name=payload.role_name
    )
    updated = add_kubernetes_auth_role(
        current,
        build_kubernetes_auth_role(
            payload.role_name,
            payload.role_description,
            payload.service_accounts,
            payload.namespaces,
            payload.access,
            cluster=payload.cluster,
            ttl=payload.ttl,
        ),
    )

    source_commit_id = None
    if current is not None:
        source_commit_id = await bitbucket.get_last_commit(path, at=base_branch)

    return path, render_values_yaml(updated), source_commit_id


async def create_kubernetes_auth_operation(
    bitbucket: Any,
    woodpecker: Any,
    payload: VaultKubernetesAuthCreate,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Append the role to its file, then block until both pipelines have finished."""
    path, content, source_commit_id = await _prepare_k8s_auth_create(bitbucket, payload)

    merged, validation, deploy = await _commit_via_pull_request(
        bitbucket,
        woodpecker,
        path=path,
        content=content,
        branch=_k8s_branch_for(payload.file, payload.role_name, branch_suffix),
        summary=f"Create Kubernetes auth role {payload.role_name} in {payload.file}",
        description=_k8s_description(
            path, payload.role_name, f"access: {payload.access}"
        ),
        role_name=payload.role_name,
        source_commit_id=source_commit_id,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=f"Successful creation of {payload.role_name}",
        role_name=payload.role_name,
        file=payload.file,
        pull_request=_pull_request_info(merged),
        validation_pipeline=_pipeline_info(validation),
        deploy_pipeline=_pipeline_info(deploy),
    )


async def create_kubernetes_auth_pull_request_operation(
    bitbucket: Any,
    payload: VaultKubernetesAuthCreate,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open the pull request and stop — no CI gates, no merge.

    Takes no Woodpecker client, exactly as the PR-only KV create does, and carries the same
    two caveats: opening a pull request still triggers validation CI in the forge (this
    just does not wait for it), and a repeat call opens a second pull request because the
    uniqueness scan reads the base branch, where an unmerged role is not.
    """
    path, content, source_commit_id = await _prepare_k8s_auth_create(bitbucket, payload)

    pull_request = await _open_pull_request(
        bitbucket,
        path=path,
        content=content,
        branch=_k8s_branch_for(payload.file, payload.role_name, branch_suffix),
        summary=f"Create Kubernetes auth role {payload.role_name} in {payload.file}",
        description=_k8s_description(
            path, payload.role_name, f"access: {payload.access}"
        ),
        role_name=payload.role_name,
        source_commit_id=source_commit_id,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} for {payload.role_name}. "
            f"It is not merged — review, CI and merge are up to you."
        ),
        role_name=payload.role_name,
        file=payload.file,
        pull_request=_pull_request_info(pull_request),
    )


async def update_kubernetes_auth_operation(
    bitbucket: Any,
    woodpecker: Any,
    file: str,
    role_name: str,
    payload: VaultKubernetesAuthUpdate,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Edit one role. Its name, its cluster and its siblings are untouched.

    An edit that changes nothing returns success without opening a pull request, exactly as
    a KV update does. A changed `access` re-runs the "every store named exists" check —
    otherwise an edit could introduce the dangling reference a create refuses.
    """
    path = values_file_path(config.VAULT_VALUES_DIR, file)
    current = await _require_document(
        bitbucket, path, kv_name="", file=file, role_name=role_name
    )

    try:
        updated = update_kubernetes_auth_role(
            current,
            role_name,
            role_description=payload.role_description,
            service_accounts=payload.service_accounts,
            namespaces=payload.namespaces,
            access=payload.access,
            ttl=payload.ttl,
        )
    except KVStoreNotFound as missing:
        raise VaultOperationError(
            f"{role_name} is not defined in {path}",
            file=file,
            role_name=role_name,
            status_code=404,
        ) from missing

    if payload.access is not None:
        known_stores: Set[str] = set()
        async for _, parsed in _walk_values_files(bitbucket):
            known_stores.update(kv_store_names(parsed))
        _assert_stores_exist(known_stores, payload.access, file, role_name)

    if yaml_data_equals(current, updated):
        logger.info(f"No change required for {role_name}; skipping the pull request")
        return VaultOperationResponse(
            status=OperationStatus.SUCCEEDED,
            message=f"No changes required for {role_name}",
            role_name=role_name,
            file=file,
        )

    changes = ", ".join(
        f"{field}: {value}"
        for field, value in (
            ("description", payload.role_description),
            ("service accounts", payload.service_accounts),
            ("namespaces", payload.namespaces),
            ("access", payload.access),
            ("ttl", payload.ttl),
        )
        if value is not None
    )

    source_commit_id = await bitbucket.get_last_commit(
        path, at=config.VAULT_VALUES_REPO_BASE_BRANCH
    )

    merged, validation, deploy = await _commit_via_pull_request(
        bitbucket,
        woodpecker,
        path=path,
        content=render_values_yaml(updated),
        branch=_k8s_branch_for(file, role_name, branch_suffix),
        summary=f"Update Kubernetes auth role {role_name} in {file}",
        description=_k8s_description(path, role_name, f"changes: {changes}"),
        role_name=role_name,
        source_commit_id=source_commit_id,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=f"Successful update of {role_name}",
        role_name=role_name,
        file=file,
        pull_request=_pull_request_info(merged),
        validation_pipeline=_pipeline_info(validation),
        deploy_pipeline=_pipeline_info(deploy),
    )


async def _prepare_k8s_auth_delete(
    bitbucket: Any, file: str, role_name: str
) -> Tuple[str, str, Optional[str]]:
    """Everything a Kubernetes auth delete needs before it touches a branch.

    No referential check in this direction: a KV store with no auth role pointing at it is
    perfectly valid, so only the reverse (deleting a *store*) can orphan anything.
    """
    path = values_file_path(config.VAULT_VALUES_DIR, file)
    current = await _require_document(
        bitbucket, path, kv_name="", file=file, role_name=role_name
    )

    try:
        updated = remove_kubernetes_auth_role(current, role_name)
    except KVStoreNotFound as missing:
        raise VaultOperationError(
            f"{role_name} is not defined in {path}",
            file=file,
            role_name=role_name,
            status_code=404,
        ) from missing

    source_commit_id = await bitbucket.get_last_commit(
        path, at=config.VAULT_VALUES_REPO_BASE_BRANCH
    )

    return path, render_values_yaml(updated), source_commit_id


async def delete_kubernetes_auth_operation(
    bitbucket: Any,
    woodpecker: Any,
    file: str,
    role_name: str,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Remove one role from its file, then block until both pipelines have finished."""
    path, content, source_commit_id = await _prepare_k8s_auth_delete(
        bitbucket, file, role_name
    )

    merged, validation, deploy = await _commit_via_pull_request(
        bitbucket,
        woodpecker,
        path=path,
        content=content,
        branch=_k8s_branch_for(file, role_name, branch_suffix),
        summary=f"Delete Kubernetes auth role {role_name} from {file}",
        description=_k8s_description(path, role_name, "change: removed"),
        role_name=role_name,
        source_commit_id=source_commit_id,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=f"Successful deletion of {role_name}",
        role_name=role_name,
        file=file,
        pull_request=_pull_request_info(merged),
        validation_pipeline=_pipeline_info(validation),
        deploy_pipeline=_pipeline_info(deploy),
    )


async def delete_kubernetes_auth_pull_request_operation(
    bitbucket: Any,
    file: str,
    role_name: str,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open the pull request that removes the role, and stop — no CI gates, no merge."""
    path, content, source_commit_id = await _prepare_k8s_auth_delete(
        bitbucket, file, role_name
    )

    pull_request = await _open_pull_request(
        bitbucket,
        path=path,
        content=content,
        branch=_k8s_branch_for(file, role_name, branch_suffix),
        summary=f"Delete Kubernetes auth role {role_name} from {file}",
        description=_k8s_description(path, role_name, "change: removed"),
        role_name=role_name,
        source_commit_id=source_commit_id,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} to delete {role_name}. "
            f"It is not merged — review, CI and merge are up to you."
        ),
        role_name=role_name,
        file=file,
        pull_request=_pull_request_info(pull_request),
    )


async def get_kubernetes_auth_file_operation(
    bitbucket: Any, file: str
) -> List[Dict[str, Any]]:
    """Every Kubernetes auth role in a values file.

    A file with none answers `200` with an empty list rather than `404`: the file exists,
    it just declares no roles. Only a missing *file* is a 404.
    """
    path = values_file_path(config.VAULT_VALUES_DIR, file)
    document = await _require_document(bitbucket, path, kv_name="", file=file)
    return kubernetes_auth_roles(document)


async def get_kubernetes_auth_role_operation(
    bitbucket: Any, file: str, role_name: str
) -> Dict[str, Any]:
    """One Kubernetes auth role out of a values file."""
    path = values_file_path(config.VAULT_VALUES_DIR, file)
    document = await _require_document(
        bitbucket, path, kv_name="", file=file, role_name=role_name
    )

    role = find_kubernetes_auth_role(document, role_name)
    if role is None:
        raise VaultOperationError(
            f"{role_name} is not defined in {path}",
            file=file,
            role_name=role_name,
            status_code=404,
        )
    return role


