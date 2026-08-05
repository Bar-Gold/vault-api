"""Business logic for Vault KV stores and the service accounts bound to them.

A values file holds one **list** of named stores under `kvStores`, so a create appends to
`kv/<file>.yaml` rather than owning a file of its own; the file is created if this is its
first store.

A Kubernetes service account binding is **nested inside a store**, under
`k8sServiceAccounts`, not stored beside it. That single fact removes a whole category of
work: nothing points at a binding from elsewhere, so there is no referential rule, no
cross-file uniqueness scan and no orphan to refuse — deleting a store takes its bindings
with it. What a binding *means* in Vault — the mount, the policy, the HCL — is the deploy
pipeline's business; this service never generates any of it.

The create flow is a GitOps chain with two CI gates:

    1. commit the values file to a short-lived branch
    2. open a pull request against the base branch
    3. wait for the **validation** build on the pull request's commit — it blocks the merge
    4. merge the pull request
    5. wait for the **deploy** build on the merge commit — it applies the change to Vault

Both gates are read from **Bitbucket**, not from the CI server: the CI server posts its
result against the commit it built, and Bitbucket keeps it — that store is what a pull
request's Builds tab shows. So a gate is "ask Bitbucket about a sha we already hold", which
is why there are no pipeline matchers and no pre-flight watermark here.

The request blocks until step 5 finishes, then returns "Successful creation of <name>".
Update and delete run the same chain over a different document — one entry edited, one
entry removed — so all three share its rollbacks rather than each hand-rolling them.

There is no transaction, so each step rolls back what the earlier steps did:
committing fails -> delete the branch; the PR fails to open -> delete the branch; the
validation build fails or times out -> decline the PR and delete the branch. **The merge is
the point of no return**: once step 4 succeeds the change is on the base branch, so a
failing deploy build is reported as-is rather than rolled back — un-merging would need a
revert PR and a second human decision.
"""

import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import yaml
from loguru import logger
from tashtiot_apis_library.connectors import ExternalServiceError

from ...clients.bitbucket import BuildStatus, BuildTimeoutError, PullRequest
from ...helpers import (
    K8sServiceAccountIdentity,
    K8sServiceAccountNotFound,
    add_k8s_service_account,
    add_kv_store,
    build_branch_name,
    build_k8s_service_account,
    build_kv_store,
    find_k8s_service_account,
    find_kv_store,
    k8s_service_account_identity,
    k8s_service_accounts,
    kv_store_names,
    remove_k8s_service_account,
    remove_kv_store,
    render_values_yaml,
    update_kv_store,
    values_file_path,
    yaml_data_equals,
)
from .conf import config
from .schemas import (
    BuildInfo,
    K8sServiceAccountCreate,
    OperationStatus,
    PullRequestInfo,
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
        pull_request: Optional[PullRequest] = None,
        validation_builds: Optional[List[BuildStatus]] = None,
        deploy_builds: Optional[List[BuildStatus]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kv_name = kv_name
        self.file = file
        self.status_code = status_code
        self.pull_request = pull_request
        self.validation_builds = validation_builds
        self.deploy_builds = deploy_builds

    def to_response(self) -> "VaultOperationResponse":
        """The failed response body, so the route only has to pick the HTTP status.

        This is the single constructor of the failure body, so a field it forgets is a
        field every failure response silently omits — failures are *returned* as a
        JSONResponse, so FastAPI never validates them against the model.
        """
        return VaultOperationResponse(
            status=OperationStatus.FAILED,
            message=self.message,
            kv_name=self.kv_name,
            file=self.file,
            pull_request=_pull_request_info(self.pull_request),
            validation_builds=_build_infos(self.validation_builds),
            deploy_builds=_build_infos(self.deploy_builds),
            error=self.message,
        )


# --------------------------------------------------------------------------- #
# small adapters
# --------------------------------------------------------------------------- #
def _pull_request_info(pull_request: Optional[PullRequest]) -> Optional[PullRequestInfo]:
    if pull_request is None:
        return None
    return PullRequestInfo(id=pull_request.id, url=pull_request.url, state=pull_request.state)


def _build_infos(builds: Optional[List[BuildStatus]]) -> Optional[List[BuildInfo]]:
    if builds is None:
        return None
    return [
        BuildInfo(key=build.key, state=build.state, name=build.name or None, url=build.url)
        for build in builds
    ]


def _build_failure(kind: str, builds: List[BuildStatus]) -> str:
    """Name every build that did not succeed, not just the first.

    Bitbucket stores one result per key per commit, so a commit can carry several — one per
    workflow. Reporting only one of them would send a reader to the wrong build page.
    """
    failed = [build for build in builds if not build.succeeded]
    return f"{kind} did not pass: " + ", ".join(str(build) for build in failed)


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
        ) from parse_error

    # `kvStores:` with nothing under it parses to None — an empty file, not a broken one.
    if parsed is not None and not isinstance(parsed, dict):
        raise VaultOperationError(
            f"{path} exists on {base_branch} but is not a YAML mapping",
            kv_name=kv_name,
            file=file,
        )
    return parsed


async def _walk_values_files(
    bitbucket: Any, skipped: Optional[List[str]] = None
) -> AsyncIterator[Tuple[str, str, Any]]:
    """Every parsed yaml under `VAULT_VALUES_DIR` on the base branch: file, path, document.

    Two callers, and both exist because a store name is the one thing in this format with a
    values-dir-wide namespace: the create's duplicate scan, and `_resolve_store`, which is
    how every other route finds a store without the caller naming a file. A file we did not
    write, or a bad merge, is logged and skipped rather than blocking an unrelated
    operation.

    It costs a directory listing plus a read per file, which is why a caller does it once,
    up front, and takes everything it needs out of the same pass.

    `file` is the relative path with its extension stripped — the value that round-trips
    through `values_file_path` for the flat layout this service writes. It is *reported*,
    never taken as input, so a hand-made file in a sub-directory yields a name that is not
    addressable and that costs nothing.
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
            if skipped is not None:
                skipped.append(path)
            continue

        yield relative_path.lstrip("/").rsplit(".", 1)[0], path, parsed


async def _assert_name_is_free(bitbucket: Any, kv_name: str, file: str) -> None:
    """Reject a create whose store name is already used **anywhere** in the values dir.

    Store names are global to Vault, so uniqueness cannot be scoped to one file — and that
    same global uniqueness is what lets every other route address a store by name alone.

    This is a check, not a lock: two creates in flight both pass it, and the second pull
    request then conflicts at merge. That is the same race the whole GitOps flow already
    has, and a human resolves it.
    """
    async for _, path, parsed in _walk_values_files(bitbucket):
        if kv_name in kv_store_names(parsed):
            raise VaultOperationError(
                f"{kv_name} already exists (defined in {path} on "
                f"{config.VAULT_VALUES_REPO_BASE_BRANCH})",
                kv_name=kv_name,
                file=file,
                status_code=409,
            )


async def _resolve_store(
    bitbucket: Any, kv_name: str
) -> Tuple[str, str, Dict[str, Any], Dict[str, Any]]:
    """Find the store named `kv_name`: its file, path, whole document and own entry.

    **This is why no route but the create mentions a file.** A store name is unique across
    the entire values directory — the create enforces exactly that — so the name alone
    addresses a store, and a caller never has to remember which file it was grouped into.

    Two steps, because the common case should not pay for the general one:

    1. try `<values dir>/<kv_name>.yaml`, the file a create with no `file` writes. One read,
       and it answers for every store created the normal way. It still has to *find* the
       store inside: a file may be named after one store while holding another.
    2. otherwise walk the directory, which is what finds a store grouped into a file under
       somebody else's name.

    A store that is in no file at all is the 404 — the same answer `GET`/`PATCH`/`DELETE`
    have always given for a store that is not there.
    """
    values_dir = config.VAULT_VALUES_DIR
    conventional_path = values_file_path(values_dir, kv_name)

    document = await _read_document(bitbucket, conventional_path, kv_name, kv_name)
    if document is not None:
        store = find_kv_store(document, kv_name)
        if store is not None:
            return kv_name, conventional_path, document, store

    skipped: List[str] = []
    async for file, path, parsed in _walk_values_files(bitbucket, skipped=skipped):
        if path == conventional_path or not isinstance(parsed, dict):
            continue
        store = find_kv_store(parsed, kv_name)
        if store is not None:
            return file, path, parsed, store

    # A file the walk could not parse might be the very one holding the store, and "not
    # defined anywhere" would send an operator looking for the wrong problem. The
    # conventional path does not need this — `_read_document` reports its own YAML error.
    unreadable = (
        f" ({len(skipped)} unparseable file(s) skipped: {', '.join(skipped)})"
        if skipped
        else ""
    )
    raise VaultOperationError(
        f"{kv_name} is not defined in any file under {values_dir} on "
        f"{config.VAULT_VALUES_REPO_BASE_BRANCH}{unreadable}",
        kv_name=kv_name,
        status_code=404,
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
    source_commit_id: Optional[str] = None,
) -> PullRequest:
    """branch -> commit -> open PR, and nothing after that.

    Both rollbacks live here: a failed commit or a failed PR deletes the branch, so a
    failure leaves the repo exactly as it was found. Touches no CI and never merges.

    Shared by `_commit_via_pull_request` (which goes on to gate and merge) and all four
    PR-only operations (which stop here). One implementation means they cannot drift apart
    in what they roll back. `kv_name` only names the subject in the log line; a binding
    operation passes the store it edits, since that is what the diff touches.
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
    *,
    path: str,
    content: str,
    branch: str,
    summary: str,
    description: str,
    kv_name: str = "",
    source_commit_id: Optional[str] = None,
) -> Tuple[PullRequest, List[BuildStatus], List[BuildStatus]]:
    """branch -> commit -> PR -> gate 1 -> merge -> gate 2, with every rollback.

    Shared by every mutating flow — stores and bindings alike: the only thing that differs
    between them is what gets written and whether an optimistic-lock token is needed.
    Keeping one implementation means the rollback asymmetry cannot drift between
    operations. `kv_name` is carried only so a failure names its subject.

    Both gates ask Bitbucket which builds it holds for a commit. There is no watermark to
    take before the branch is created: each gate names a commit that did not exist until
    this request made it, so nothing older can be mistaken for ours.

    Returns the merged pull request and both gates' builds. Raises `VaultOperationError`
    for every business failure, having already rolled back whatever is still safe to roll
    back.
    """
    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH

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

    # ---- 3. validation build (blocks the merge) ---------------------------- #
    if not pull_request.from_commit:
        # Without the source commit there is no sha to ask about, and merging unvalidated
        # is not an option — roll all the way back rather than degrade the gate.
        await _decline_and_cleanup(bitbucket, pull_request, branch)
        raise VaultOperationError(
            f"Bitbucket did not report a source commit for pull request "
            f"{pull_request.id}, so its builds cannot be watched",
            kv_name=kv_name,
            pull_request=pull_request,
        )

    try:
        validation = await bitbucket.await_builds(pull_request.from_commit)
    except BuildTimeoutError as timeout_error:
        await _decline_and_cleanup(bitbucket, pull_request, branch)
        raise VaultOperationError(
            f"Validation build did not complete: {timeout_error.message}",
            kv_name=kv_name,
            status_code=504,
            pull_request=pull_request,
            validation_builds=timeout_error.builds,
        ) from timeout_error

    if not all(build.succeeded for build in validation):
        await _decline_and_cleanup(bitbucket, pull_request, branch)
        raise VaultOperationError(
            _build_failure("Validation", validation),
            kv_name=kv_name,
            pull_request=pull_request,
            validation_builds=validation,
        )

    # ---- 4. merge (point of no return) ------------------------------------- #
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
            validation_builds=validation,
        ) from merge_error

    logger.info(f"Merged pull request {merged.id} for {kv_name}")

    # ---- 5. deploy build --------------------------------------------------- #
    deploy_commit = merged.merge_commit or await bitbucket.get_branch_head(base_branch)
    if not deploy_commit:
        raise VaultOperationError(
            f"Pull request {merged.id} merged, but Bitbucket reported no commit on "
            f"{base_branch} to watch the deploy build on",
            kv_name=kv_name,
            pull_request=merged,
            validation_builds=validation,
        )

    # A fast-forward merge leaves the base branch on the very commit the pull request
    # built, so its validation results are already sitting there. Skipping the keys gate 1
    # saw is what makes this wait for the deploy build instead of re-reading gate 1.
    already_seen: frozenset = (
        frozenset(build.key for build in validation)
        if deploy_commit == pull_request.from_commit
        else frozenset()
    )

    try:
        deploy = await bitbucket.await_builds(deploy_commit, exclude_keys=already_seen)
    except BuildTimeoutError as timeout_error:
        raise VaultOperationError(
            f"Deploy build did not complete: {timeout_error.message}. "
            f"The change is already merged to {base_branch}.",
            kv_name=kv_name,
            status_code=504,
            pull_request=merged,
            validation_builds=validation,
            deploy_builds=timeout_error.builds,
        ) from timeout_error

    if not all(build.succeeded for build in deploy):
        raise VaultOperationError(
            f"{_build_failure('Deploy', deploy)}. "
            f"The change is already merged to {base_branch} and needs a revert.",
            kv_name=kv_name,
            pull_request=merged,
            validation_builds=validation,
            deploy_builds=deploy,
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
    payload: VaultKVCreate,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Append the store to its file, then block until both pipelines have finished."""
    path, content, source_commit_id = await _prepare_create(bitbucket, payload)

    merged, validation, deploy = await _commit_via_pull_request(
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
        message=f"Successful creation of {payload.kv_name}",
        kv_name=payload.kv_name,
        file=payload.file,
        pull_request=_pull_request_info(merged),
        validation_builds=_build_infos(validation),
        deploy_builds=_build_infos(deploy),
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
    kv_name: str,
    payload: VaultKVUpdate,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Edit one store's description and/or roles. Its name and siblings are untouched.

    The store is found by name — `_resolve_store` supplies the file, so the caller does
    not name one and an unknown store is the 404 before any edit is attempted.

    An edit that changes nothing returns success without opening a pull request — the
    reason `yaml_data_equals` exists. That keeps repeat requests from filling the values
    repo with empty pull requests.
    """
    file, path, current, _ = await _resolve_store(bitbucket, kv_name)

    updated = update_kv_store(
        current,
        kv_name,
        description=payload.kv_description,
        roles=payload.roles,
    )

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
        validation_builds=_build_infos(validation),
        deploy_builds=_build_infos(deploy),
    )


# --------------------------------------------------------------------------- #
# removing a store
# --------------------------------------------------------------------------- #
async def _prepare_delete(
    bitbucket: Any, kv_name: str
) -> Tuple[str, str, str, Optional[str]]:
    """Everything a delete needs before it touches a branch, the file included.

    The counterpart of `_prepare_create`, and shared by both delete paths for the same
    reason: they cannot then disagree about what is a 404. `_resolve_store` is that 404 —
    a store in no file cannot be deleted, and the caller never named a file to be wrong
    about.

    It still runs **no referential scan**: a store's Kubernetes service accounts are
    nested inside it, so removing the store removes its bindings in the same diff and no
    reference is left dangling. There is no `yaml_data_equals` short circuit either — if
    `remove_kv_store` did not raise, the document changed. A removal is never a no-op.
    """
    file, path, current, _ = await _resolve_store(bitbucket, kv_name)
    updated = remove_kv_store(current, kv_name)

    # The file exists by definition here, so the write is an edit and Bitbucket wants the
    # optimistic-lock token for that path.
    source_commit_id = await bitbucket.get_last_commit(
        path, at=config.VAULT_VALUES_REPO_BASE_BRANCH
    )

    return file, path, render_values_yaml(updated), source_commit_id


def _delete_description(path: str, kv_name: str) -> str:
    return (
        f"Automated by vault-api.\n\n"
        f"- file: {path}\n"
        f"- store: {kv_name}\n"
        f"- change: removed from kvStores\n"
    )


async def delete_kv_store_operation(
    bitbucket: Any,
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
    file, path, content, source_commit_id = await _prepare_delete(bitbucket, kv_name)

    merged, validation, deploy = await _commit_via_pull_request(
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
        message=f"Successful deletion of {kv_name}",
        kv_name=kv_name,
        file=file,
        pull_request=_pull_request_info(merged),
        validation_builds=_build_infos(validation),
        deploy_builds=_build_infos(deploy),
    )


async def delete_kv_store_pull_request_operation(
    bitbucket: Any,
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
    file, path, content, source_commit_id = await _prepare_delete(bitbucket, kv_name)

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


async def get_kv_store_operation(bitbucket: Any, kv_name: str) -> Dict[str, Any]:
    """One store, found by name wherever it lives."""
    _, _, _, store = await _resolve_store(bitbucket, kv_name)
    return store


# --------------------------------------------------------------------------- #
# Kubernetes service accounts
#
# A binding is a sub-resource of a store, not a resource kind of its own, and that changes
# what these operations have to do rather than how they do it. The chain is unchanged —
# `_open_pull_request` and `_commit_via_pull_request` are reused verbatim, so the rollback
# asymmetry is identical and the merge is still the point of no return.
#
# What is *absent* is the point. Because the binding lives inside the store, there is no
# cross-file scan here at all: no uniqueness walk (a binding is unique within its store,
# and the same service account may legitimately reach many stores), no store-existence
# check (the store is the thing being edited — if it is not there, that is the 404), and
# no referential rule (nothing points at a binding, so nothing can dangle).
# --------------------------------------------------------------------------- #
def _k8s_sa_branch_for(file: str, kv_name: str, branch_suffix: Optional[str]) -> str:
    """Its own prefix, so a reviewer can tell the change kind from the branch name.

    Keyed on the store, not the binding: the triple has no name to slug, and the store is
    what the diff touches.
    """
    return build_branch_name(
        file,
        kv_name,
        branch_suffix or uuid.uuid4().hex[:8],
        config.K8S_SA_BRANCH_PREFIX,
    )


def _identity_text(identity: K8sServiceAccountIdentity) -> str:
    """The triple as one human-readable token, for messages and PR descriptions."""
    service_account, namespace, cluster = identity
    return f"{service_account} in {namespace} on {cluster}"


def _k8s_sa_description(
    path: str, kv_name: str, identity: K8sServiceAccountIdentity, change: str
) -> str:
    service_account, namespace, cluster = identity
    return (
        f"Automated by vault-api.\n\n"
        f"- file: {path}\n"
        f"- store: {kv_name}\n"
        f"- change: {change}\n"
        f"- serviceAccount: {service_account}\n"
        f"- namespace: {namespace}\n"
        f"- cluster: {cluster}\n"
    )


async def _prepare_k8s_sa_add(
    bitbucket: Any, kv_name: str, payload: K8sServiceAccountCreate
) -> Tuple[str, str, str, Optional[str], K8sServiceAccountIdentity]:
    """Everything adding a binding needs before it touches a branch, the file included.

    Shared by the blocking path and its PR-only twin, the same way `_prepare_create` is, so
    the two cannot disagree about what is a 404 and what is a 409. A binding is addressed
    through its store, so it inherits the store's "found by name" rule for free.
    """
    file, path, document, store = await _resolve_store(bitbucket, kv_name)

    account = build_k8s_service_account(
        payload.service_account, payload.namespace, payload.cluster
    )
    identity = k8s_service_account_identity(account)

    # Unique within the store only. The same service account reaching two stores is the
    # normal case — that is how one workload gets at two secrets.
    if find_k8s_service_account(store, identity) is not None:
        raise VaultOperationError(
            f"{_identity_text(identity)} is already bound to {kv_name} in {path}",
            kv_name=kv_name,
            file=file,
            status_code=409,
        )

    updated = add_k8s_service_account(document, kv_name, account)

    # The file exists by definition here, so the write is an edit and Bitbucket wants the
    # optimistic-lock token for that path.
    source_commit_id = await bitbucket.get_last_commit(
        path, at=config.VAULT_VALUES_REPO_BASE_BRANCH
    )

    return file, path, render_values_yaml(updated), source_commit_id, identity


async def _prepare_k8s_sa_remove(
    bitbucket: Any, kv_name: str, identity: K8sServiceAccountIdentity
) -> Tuple[str, str, str, Optional[str]]:
    """Everything removing a binding needs before it touches a branch, the file included.

    No `yaml_data_equals` short circuit, for the same reason a store delete has none: if
    `remove_k8s_service_account` did not raise, the document changed.
    """
    file, path, document, _ = await _resolve_store(bitbucket, kv_name)

    try:
        updated = remove_k8s_service_account(document, kv_name, identity)
    except K8sServiceAccountNotFound as missing:
        raise VaultOperationError(
            f"{_identity_text(identity)} is not bound to {kv_name} in {path}",
            kv_name=kv_name,
            file=file,
            status_code=404,
        ) from missing

    source_commit_id = await bitbucket.get_last_commit(
        path, at=config.VAULT_VALUES_REPO_BASE_BRANCH
    )

    return file, path, render_values_yaml(updated), source_commit_id


async def add_k8s_service_account_operation(
    bitbucket: Any,
    kv_name: str,
    payload: K8sServiceAccountCreate,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Bind a service account to a store, then block until both pipelines have finished."""
    file, path, content, source_commit_id, identity = await _prepare_k8s_sa_add(
        bitbucket, kv_name, payload
    )

    merged, validation, deploy = await _commit_via_pull_request(
        bitbucket,
        path=path,
        content=content,
        branch=_k8s_sa_branch_for(file, kv_name, branch_suffix),
        summary=f"Bind service account {payload.service_account} to {kv_name} in {file}",
        description=_k8s_sa_description(
            path, kv_name, identity, "added to k8sServiceAccounts"
        ),
        kv_name=kv_name,
        source_commit_id=source_commit_id,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=f"Successfully bound {_identity_text(identity)} to {kv_name}",
        kv_name=kv_name,
        file=file,
        pull_request=_pull_request_info(merged),
        validation_builds=_build_infos(validation),
        deploy_builds=_build_infos(deploy),
    )


async def add_k8s_service_account_pull_request_operation(
    bitbucket: Any,
    kv_name: str,
    payload: K8sServiceAccountCreate,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open the pull request that adds the binding, and stop — no CI gates, no merge.

    Takes no Woodpecker client, exactly as the other PR-only operations do, and carries the
    same two consequences: the forge still runs the validation pipeline (it is simply not
    waited on), and a repeat call opens a second pull request because the first is not on
    the base branch for the duplicate check to see.
    """
    file, path, content, source_commit_id, identity = await _prepare_k8s_sa_add(
        bitbucket, kv_name, payload
    )

    pull_request = await _open_pull_request(
        bitbucket,
        path=path,
        content=content,
        branch=_k8s_sa_branch_for(file, kv_name, branch_suffix),
        summary=f"Bind service account {payload.service_account} to {kv_name} in {file}",
        description=_k8s_sa_description(
            path, kv_name, identity, "added to k8sServiceAccounts"
        ),
        kv_name=kv_name,
        source_commit_id=source_commit_id,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} to bind "
            f"{_identity_text(identity)} to {kv_name}. "
            f"It is not merged — review, CI and merge are up to you."
        ),
        kv_name=kv_name,
        file=file,
        pull_request=_pull_request_info(pull_request),
    )


async def remove_k8s_service_account_operation(
    bitbucket: Any,
    kv_name: str,
    identity: K8sServiceAccountIdentity,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Unbind a service account from a store, then block until both pipelines finish.

    Removing the store's last binding drops the `k8sServiceAccounts` key entirely, leaving
    the store exactly as a fresh create would have written it.
    """
    file, path, content, source_commit_id = await _prepare_k8s_sa_remove(
        bitbucket, kv_name, identity
    )

    service_account, _, _ = identity
    merged, validation, deploy = await _commit_via_pull_request(
        bitbucket,
        path=path,
        content=content,
        branch=_k8s_sa_branch_for(file, kv_name, branch_suffix),
        summary=f"Unbind service account {service_account} from {kv_name} in {file}",
        description=_k8s_sa_description(
            path, kv_name, identity, "removed from k8sServiceAccounts"
        ),
        kv_name=kv_name,
        source_commit_id=source_commit_id,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=f"Successfully unbound {_identity_text(identity)} from {kv_name}",
        kv_name=kv_name,
        file=file,
        pull_request=_pull_request_info(merged),
        validation_builds=_build_infos(validation),
        deploy_builds=_build_infos(deploy),
    )


async def remove_k8s_service_account_pull_request_operation(
    bitbucket: Any,
    kv_name: str,
    identity: K8sServiceAccountIdentity,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open the pull request that removes the binding, and stop — no CI gates, no merge."""
    file, path, content, source_commit_id = await _prepare_k8s_sa_remove(
        bitbucket, kv_name, identity
    )

    service_account, _, _ = identity
    pull_request = await _open_pull_request(
        bitbucket,
        path=path,
        content=content,
        branch=_k8s_sa_branch_for(file, kv_name, branch_suffix),
        summary=f"Unbind service account {service_account} from {kv_name} in {file}",
        description=_k8s_sa_description(
            path, kv_name, identity, "removed from k8sServiceAccounts"
        ),
        kv_name=kv_name,
        source_commit_id=source_commit_id,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} to unbind "
            f"{_identity_text(identity)} from {kv_name}. "
            f"It is not merged — review, CI and merge are up to you."
        ),
        kv_name=kv_name,
        file=file,
        pull_request=_pull_request_info(pull_request),
    )


async def get_k8s_service_accounts_operation(
    bitbucket: Any, kv_name: str
) -> List[Dict[str, Any]]:
    """Every binding on one store.

    A store that binds nothing answers 200 with an empty list — it simply has no
    `k8sServiceAccounts` key. Only a store that is in no file at all is a 404.
    """
    _, _, _, store = await _resolve_store(bitbucket, kv_name)
    return k8s_service_accounts(store)
