"""Business logic for Vault KV stores and the grants that reach them.

A **KV store is a directory**, `KVs/<team>/<kv>/`, holding up to two files split by
`authType` — `oidc-access.yaml` and `k8s-access.yaml`. Each file carries one `access` list
of grants of its own kind. A store with no Kubernetes grants has no `k8s-access.yaml` at
all rather than an empty one.

The **Tashtiot per-team file** is the second layout: `KVs/Tashtiot/<team>.yaml`, one flat
file whose `access` list mixes both kinds. It grants a team access into the shared services
store, so it is per-team rather than per-KV and has no directory of its own. It is a
different resource with its own routes; everything after the commit is identical.

A grant is one entry: a principal, a path and the capabilities it has there. What a grant
*means* in Vault — the mount, the policy, the HCL — is the deploy pipeline's business, and
this service generates none of it.

The flow is a GitOps chain that stops at a green pull request:

    1. commit the access files to a short-lived branch
    2. open a pull request against the base branch
    3. wait for the **validation** build on the pull request's commit
    4. return — the pull request is open, green, and waiting for a human

**This service never merges.** It proposes a change and confirms CI is happy with it; who
merges, and when, is a human decision it deliberately does not make. Nothing it does ever
reaches the base branch.

The gate is read from **Bitbucket**, not from the CI server: the CI server posts its result
against the commit it built, and Bitbucket keeps it — that store is what a pull request's
Builds tab shows. So the gate is "ask Bitbucket about a sha we already hold", which is why
there are no pipeline matchers and no pre-flight watermark here.

**Every failure is fully rolled back**, and that is a consequence of not merging rather than
an effort: committing fails -> delete the branch; the PR fails to open -> delete the branch;
the validation build fails or times out -> decline the PR *and* delete the branch. There is
no point of no return, nothing half-applied, and nothing that needs a revert.
"""

import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import yaml
from loguru import logger
from tashtiot_apis_library.connectors import ExternalServiceError

from ...clients.bitbucket import BuildStatus, BuildTimeoutError, PullRequest
from ...helpers import (
    ACCESS_KEY,
    K8S_ACCESS_FILE,
    K8S_AUTH_TYPE,
    OIDC_ACCESS_FILE,
    AccessEntryNotFound,
    DuplicateAccessEntry,
    access_entry_identity,
    access_path_for,
    add_access_entry,
    auth_type_of,
    build_access_document,
    build_branch_name,
    build_k8s_access_entry,
    build_oidc_access_entry,
    kv_store_dir,
    partition_access,
    read_access_entries,
    remove_access_entry,
    render_values_yaml,
    replace_access_entries,
    tashtiot_access_path,
    yaml_data_equals,
)
from .conf import config
from .schemas import (
    BuildInfo,
    K8sAccessEntry,
    OperationStatus,
    PullRequestInfo,
    TashtiotAccessUpsert,
    VaultKVCreate,
    VaultKVUpdate,
    VaultOperationResponse,
)


class VaultOperationError(Exception):
    """A flow that failed for a business reason rather than a transport error.

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
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kv_name = kv_name
        self.file = file
        self.status_code = status_code
        self.pull_request = pull_request
        self.validation_builds = validation_builds

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


def to_document_entry(entry: Any) -> Dict[str, Any]:
    """A validated request entry as the dict that lands in the file.

    The single bridge between the request models and the document helpers. It is the only
    place that knows a request's `auth_type` selects which builder to call, which keeps
    every key name in `helpers.py` and every rule in `schemas.py`.
    """
    capabilities = [capability.value for capability in entry.capabilities]
    if isinstance(entry, K8sAccessEntry):
        return build_k8s_access_entry(
            entry.path, entry.name, entry.namespace, entry.cluster, capabilities
        )
    return build_oidc_access_entry(entry.path, entry.dn, capabilities)


def _entry_text(entry: Dict[str, Any]) -> str:
    """One grant as a human-readable token, for messages and PR descriptions."""
    capabilities = ",".join(entry.get("capabilities") or [])
    if auth_type_of(entry) == K8S_AUTH_TYPE:
        who = f"{entry.get('name')} in {entry.get('namespace')} on {entry.get('cluster')}"
    else:
        who = str(entry.get("dn"))
    return f"{who} on {entry.get('path')} [{capabilities}]"


# --------------------------------------------------------------------------- #
# what a commit changes
# --------------------------------------------------------------------------- #
@dataclass
class FileWrite:
    """One file to create or replace on the branch.

    `source_commit_id` is Bitbucket's optimistic-lock token, required when editing a path
    that already exists and refused when creating one — so it is per-file, not per-commit.
    A store writes two of these, which is why the chain takes a list.
    """

    path: str
    content: str
    source_commit_id: Optional[str] = None


@dataclass
class Change:
    """Everything one pull request does to the repo.

    `removes` holds paths *or directories*; deleting a store removes its whole directory.
    A change can carry both — an update that empties a store's Kubernetes grants rewrites
    `oidc-access.yaml` and removes `k8s-access.yaml` in the same commit.
    """

    writes: List[FileWrite]
    removes: List[str]

    @property
    def is_empty(self) -> bool:
        return not self.writes and not self.removes

    @property
    def primary_path(self) -> str:
        """What the response and error messages name as the subject."""
        if self.writes:
            return self.writes[0].path
        return self.removes[0] if self.removes else ""


# --------------------------------------------------------------------------- #
# rollback helpers
# --------------------------------------------------------------------------- #
async def _delete_branch_quietly(bitbucket: Any, branch: str) -> None:
    try:
        await bitbucket.delete_branch(branch)
    except Exception as cleanup_error:
        logger.exception(f"Failed deleting branch {branch}: {cleanup_error}")


async def _decline_and_cleanup(
    bitbucket: Any, pull_request: PullRequest, branch: str
) -> PullRequest:
    """Close the PR and remove the branch after a failed validation pipeline.

    **Returns the pull request as it now stands**, which is what the caller reports. The
    argument still says `OPEN` — it was read before the decline — so reporting it would
    tell a caller to go review a pull request that is closed and whose branch is gone.
    On a decline that itself failed the original is handed back unchanged, because the
    pull request really is still open then and claiming otherwise is the same lie
    inverted.
    """
    declined = pull_request
    try:
        # Re-read the PR: CI status updates and reviewer activity bump Bitbucket's
        # optimistic-locking `version`, and declining with a stale one is rejected.
        current = await bitbucket.get_pull_request(pull_request.id)
        declined = await bitbucket.decline_pull_request(current.id, current.version)
    except Exception as cleanup_error:
        logger.exception(
            f"Failed declining pull request {pull_request.id}: {cleanup_error}"
        )
    await _delete_branch_quietly(bitbucket, branch)
    return declined


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
async def _read_document(
    bitbucket: Any, path: str, kv_name: str = ""
) -> Optional[Dict[str, Any]]:
    """Parsed file at the base branch, or None when it does not exist.

    None is a legitimate answer, not an error: a store with no Kubernetes grants has no
    `k8s-access.yaml`, and the first write to any path creates it.
    """
    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH
    try:
        raw = await bitbucket.get_file_content(path, at=base_branch)
    except ExternalServiceError as exc:
        if exc.status_code == 404:
            return None
        raise

    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as invalid:
        raise VaultOperationError(
            f"{path} exists on {base_branch} but is not valid YAML: {invalid}",
            kv_name=kv_name,
            file=path,
            status_code=502,
        ) from invalid


async def _walk_store_dirs(
    bitbucket: Any, skipped: Optional[List[str]] = None
) -> AsyncIterator[Tuple[str, str, str]]:
    """Every store directory under the values dir: (team, kv_name, directory path).

    Bitbucket's `files/{dir}` resource lists **recursively**, returning paths relative to
    the directory, so one call yields `cloudops/my-passwords/oidc-access.yaml` and this
    only has to group them. A store is any path of exactly three segments whose last is one
    of the two access files — which is also what keeps the Tashtiot layout out of the walk,
    since `Tashtiot/cloudops.yaml` is two.

    Yielded once per directory, not once per file, and in a stable order so a duplicate
    scan and a resolution see the same repo the same way.
    """
    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH
    values_dir = config.VAULT_VALUES_DIR

    seen: Dict[Tuple[str, str], str] = {}
    for relative_path in await bitbucket.list_files(values_dir, at=base_branch):
        parts = relative_path.strip("/").split("/")
        if len(parts) != 3 or parts[2] not in (OIDC_ACCESS_FILE, K8S_ACCESS_FILE):
            if skipped is not None and relative_path.endswith((".yaml", ".yml")):
                # Not a store file. Recorded so a 404 can say what it walked past, but not
                # an error: the Tashtiot files live here too, and so may a README.
                skipped.append(f"{values_dir.strip('/')}/{relative_path.lstrip('/')}")
            continue
        team, kv_name, _ = parts
        seen.setdefault((team, kv_name), kv_store_dir(values_dir, team, kv_name))

    for (team, kv_name), directory in sorted(seen.items()):
        yield team, kv_name, directory


@dataclass
class ResolvedStore:
    """A store found by name: where it lives and everything it grants."""

    team: str
    kv_name: str
    directory: str
    oidc_document: Optional[Dict[str, Any]]
    k8s_document: Optional[Dict[str, Any]]

    @property
    def oidc_path(self) -> str:
        return f"{self.directory}/{OIDC_ACCESS_FILE}"

    @property
    def k8s_path(self) -> str:
        return f"{self.directory}/{K8S_ACCESS_FILE}"

    @property
    def access(self) -> List[Dict[str, Any]]:
        """Every grant, both kinds, oidc first — the order a create writes them."""
        return [
            *read_access_entries(self.oidc_document),
            *read_access_entries(self.k8s_document),
        ]


async def _resolve_store(bitbucket: Any, kv_name: str) -> ResolvedStore:
    """Find a store by name alone, wherever team it belongs to.

    A store name is unique across the whole values directory — the create enforces exactly
    that — so the name is the address and a caller never has to remember the team. The cost
    is a directory listing per read, edit and delete; unlike the old flat layout there is no
    single conventional path to try first, because the team sits between the values dir and
    the store.

    **If the walk skipped files it could not classify, the 404 names them** — otherwise a
    values directory in an unexpected shape reads as a deleted store and sends an operator
    after the wrong problem.
    """
    skipped: List[str] = []
    async for team, name, directory in _walk_store_dirs(bitbucket, skipped=skipped):
        if name != kv_name:
            continue
        return ResolvedStore(
            team=team,
            kv_name=kv_name,
            directory=directory,
            oidc_document=await _read_document(
                bitbucket, f"{directory}/{OIDC_ACCESS_FILE}", kv_name
            ),
            k8s_document=await _read_document(
                bitbucket, f"{directory}/{K8S_ACCESS_FILE}", kv_name
            ),
        )

    note = f" (skipped: {', '.join(skipped)})" if skipped else ""
    raise VaultOperationError(
        f"{kv_name} is not defined under {config.VAULT_VALUES_DIR} on "
        f"{config.VAULT_VALUES_REPO_BASE_BRANCH}{note}",
        kv_name=kv_name,
        status_code=404,
    )


async def _assert_name_is_free(bitbucket: Any, kv_name: str) -> None:
    """A store name is global to Vault, so this scans every team, not just the target one.

    A check, not a lock: two creates in flight both pass and the second pull request
    conflicts at merge, the same as any other GitOps race.
    """
    async for team, name, directory in _walk_store_dirs(bitbucket):
        if name == kv_name:
            raise VaultOperationError(
                f"{kv_name} already exists under {team} ({directory})",
                kv_name=kv_name,
                file=directory,
                status_code=409,
            )


# --------------------------------------------------------------------------- #
# the chain
# --------------------------------------------------------------------------- #
async def _open_pull_request(
    bitbucket: Any,
    *,
    change: Change,
    branch: str,
    summary: str,
    description: str,
    kv_name: str = "",
) -> PullRequest:
    """branch -> commit -> open PR, and nothing after that.

    Both rollbacks live here: a failed commit or a failed PR deletes the branch, so a
    failure leaves the repo exactly as it was found. Touches no CI and never merges.

    **One caller**, `_open_and_validate`. It stays a separate function because the two
    rollback stages are genuinely different — this one deletes the branch, its caller
    declines the pull request as well — and folding them together would put both stories in
    one place. The four PR-only operations that used to stop here are gone: without the
    wait, a caller could not know the change was green, which is the one guarantee this
    service still offers.

    **A change may touch several paths.** A store is two files, so a create commits twice on
    the same branch and the pull request carries both — Bitbucket's browse resource writes
    one path per call. Removals go last, and go through `delete_file`, which is the one
    operation Bitbucket Server has no REST endpoint for (`app/clients/git.py`).
    """
    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH

    await bitbucket.create_branch(branch, base_branch)
    try:
        for write in change.writes:
            await bitbucket.put_file(
                path=write.path,
                branch=branch,
                content=write.content,
                message=summary,
                source_commit_id=write.source_commit_id,
            )
        for path in change.removes:
            await bitbucket.delete_file(
                path=path,
                branch=branch,
                base_branch=base_branch,
                message=summary,
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


async def _open_and_validate(
    bitbucket: Any,
    *,
    change: Change,
    branch: str,
    summary: str,
    description: str,
    kv_name: str = "",
) -> Tuple[PullRequest, List[BuildStatus]]:
    """branch -> commit -> PR -> validation build, and **stop**.

    Shared by every mutating flow: the only thing that differs between them is the `Change`.
    Keeping one implementation means the rollback shape cannot drift between operations.
    `kv_name` is carried only so a failure names its subject.

    **This service never merges.** It proposes a change and confirms CI is happy with it;
    a human reviews and merges. That single decision removes the whole second half of the
    old chain — no merge, no deploy gate, no merge-commit resolution, and no `exclude_keys`
    watermark (that existed only because a fast-forward merge left gate 1's results sitting
    on the commit gate 2 would have asked about).

    **There is no longer a point of no return.** Every failure here is fully rolled back:
    a bad commit or a failed PR deletes the branch, and a red or slow validation build
    declines the pull request *and* deletes the branch. Nothing this service does can reach
    the base branch, so nothing it does can need a revert.

    The gate asks Bitbucket which builds it holds for a commit. There is no watermark to
    take beforehand: the commit did not exist until this request made it, so nothing older
    can be mistaken for ours.

    Returns the open pull request and the validation builds. Raises `VaultOperationError`
    for every business failure, having already rolled everything back.
    """
    # ---- 1-2. branch + commit + pull request ------------------------------- #
    pull_request = await _open_pull_request(
        bitbucket,
        change=change,
        branch=branch,
        summary=summary,
        description=description,
        kv_name=kv_name,
    )

    # ---- 3. validation build ----------------------------------------------- #
    if not pull_request.from_commit:
        # Without the source commit there is no sha to ask about, and reporting an
        # unvalidated pull request as validated is not the fallback — roll all the way
        # back rather than degrade the gate.
        declined = await _decline_and_cleanup(bitbucket, pull_request, branch)
        raise VaultOperationError(
            f"Bitbucket did not report a source commit for pull request "
            f"{pull_request.id}, so its builds cannot be watched",
            kv_name=kv_name,
            pull_request=declined,
        )

    try:
        validation = await bitbucket.await_builds(pull_request.from_commit)
    except BuildTimeoutError as timeout_error:
        declined = await _decline_and_cleanup(bitbucket, pull_request, branch)
        raise VaultOperationError(
            f"Validation build did not complete: {timeout_error.message}",
            kv_name=kv_name,
            status_code=504,
            pull_request=declined,
            validation_builds=timeout_error.builds,
        ) from timeout_error

    if not all(build.succeeded for build in validation):
        declined = await _decline_and_cleanup(bitbucket, pull_request, branch)
        raise VaultOperationError(
            _build_failure("Validation", validation),
            kv_name=kv_name,
            pull_request=declined,
            validation_builds=validation,
        )

    logger.info(
        f"Pull request {pull_request.id} for {kv_name} passed validation and is open "
        f"for review"
    )
    return pull_request, validation


def _branch_for(team: str, kv_name: str, branch_suffix: Optional[str]) -> str:
    return build_branch_name(
        team, kv_name, branch_suffix or uuid.uuid4().hex[:8], config.BRANCH_PREFIX
    )


def _describe(subject: str, directory: str, entries: List[Dict[str, Any]], change: str) -> str:
    grants = "\n".join(f"  - {_entry_text(entry)}" for entry in entries)
    return (
        f"Automated by vault-api.\n\n"
        f"- location: {directory}\n"
        f"- subject: {subject}\n"
        f"- change: {change}\n"
        f"- grants:\n{grants}\n"
    )


# --------------------------------------------------------------------------- #
# creating a store
# --------------------------------------------------------------------------- #
def _store_writes(
    directory: str,
    entries: List[Dict[str, Any]],
    oidc_source: Optional[str] = None,
    k8s_source: Optional[str] = None,
) -> Tuple[List[FileWrite], List[str]]:
    """Split grants across the store's two files, and say which files to drop.

    The single place the two-file layout is applied. A kind with no grants produces **no
    file** rather than an empty one — that is the format's shape, and it is what a create
    writes, so an edit that empties a kind removes its file to match. `partition_access`
    is what knows which entry goes where.
    """
    oidc_entries, k8s_entries = partition_access(entries)

    writes: List[FileWrite] = []
    removes: List[str] = []

    for file_entries, path, source in (
        (oidc_entries, f"{directory}/{OIDC_ACCESS_FILE}", oidc_source),
        (k8s_entries, f"{directory}/{K8S_ACCESS_FILE}", k8s_source),
    ):
        if file_entries:
            writes.append(
                FileWrite(
                    path=path,
                    content=render_values_yaml(build_access_document(file_entries)),
                    source_commit_id=source,
                )
            )
        elif source is not None:
            # The file exists on the base branch but has no grants left.
            removes.append(path)

    return writes, removes


async def _prepare_create(
    bitbucket: Any, payload: VaultKVCreate
) -> Tuple[str, Change, List[Dict[str, Any]]]:
    """Everything a create needs before it touches a branch.

    The name scan is the only values-dir-wide question here, and it is a check rather than a
    lock. Nothing else is read: the store is new, so neither access file exists and no
    optimistic-lock token applies.
    """
    await _assert_name_is_free(bitbucket, payload.kv_name)

    directory = kv_store_dir(config.VAULT_VALUES_DIR, payload.team, payload.kv_name)
    entries = [to_document_entry(entry) for entry in payload.access]
    writes, removes = _store_writes(directory, entries)

    return directory, Change(writes=writes, removes=removes), entries


async def create_kv_mount_operation(
    bitbucket: Any,
    payload: VaultKVCreate,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open a validated pull request that creates the store's directory.

    The store does not exist when this returns — the pull request does. It blocks for the
    validation build so the caller knows CI is green before a reviewer looks at it.
    """
    directory, change, entries = await _prepare_create(bitbucket, payload)

    pull_request, validation = await _open_and_validate(
        bitbucket,
        change=change,
        branch=_branch_for(payload.team, payload.kv_name, branch_suffix),
        summary=f"Create KV store {payload.kv_name} for {payload.team}",
        description=_describe(payload.kv_name, directory, entries, "store created"),
        kv_name=payload.kv_name,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} to create {payload.kv_name}. "
            f"It passed validation and is waiting for review."
        ),
        kv_name=payload.kv_name,
        file=directory,
        pull_request=_pull_request_info(pull_request),
        validation_builds=_build_infos(validation),
    )


# --------------------------------------------------------------------------- #
# editing a store
# --------------------------------------------------------------------------- #
async def _source_commits(
    bitbucket: Any, store: ResolvedStore
) -> Tuple[Optional[str], Optional[str]]:
    """Bitbucket's optimistic-lock token for each of the store's files.

    Only for a file that already exists: without the token the browse resource rejects an
    edit as an attempted create, and *with* one it rejects a create as an attempted edit.
    A store with no `k8s-access.yaml` yields `None` for it, which is exactly right for the
    edit that adds the first Kubernetes grant.
    """
    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH
    oidc_source = (
        await bitbucket.get_last_commit(store.oidc_path, at=base_branch)
        if store.oidc_document is not None
        else None
    )
    k8s_source = (
        await bitbucket.get_last_commit(store.k8s_path, at=base_branch)
        if store.k8s_document is not None
        else None
    )
    return oidc_source, k8s_source


def _unchanged(store: ResolvedStore, entries: List[Dict[str, Any]]) -> bool:
    """Whether an edit would produce the files already on the base branch.

    Compares parsed documents rather than text, so a re-render (an indent rule, a key
    order) never counts as a change and never opens an empty pull request.
    """
    oidc_entries, k8s_entries = partition_access(entries)
    return yaml_data_equals(
        [read_access_entries(store.oidc_document), read_access_entries(store.k8s_document)],
        [oidc_entries, k8s_entries],
    )


async def _apply_store_edit(
    bitbucket: Any,
    store: ResolvedStore,
    entries: List[Dict[str, Any]],
) -> Change:
    """The `Change` that makes the store's files hold exactly `entries`."""
    oidc_source, k8s_source = await _source_commits(bitbucket, store)
    writes, removes = _store_writes(store.directory, entries, oidc_source, k8s_source)
    return Change(writes=writes, removes=removes)


async def update_kv_mount_operation(
    bitbucket: Any,
    kv_name: str,
    payload: VaultKVUpdate,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open a validated pull request that replaces a store's grant list.

    The store is found by name — `_resolve_store` supplies the team, so the caller does not
    name one and an unknown store is the 404 before any edit is attempted.

    An edit that changes nothing returns success **without opening a pull request**, which
    is what keeps repeat requests from filling the values repo with empty diffs — and is
    the only success path that answers 200 rather than 201.
    """
    store = await _resolve_store(bitbucket, kv_name)
    entries = [to_document_entry(entry) for entry in payload.access]

    if _unchanged(store, entries):
        logger.info(f"No change required for {kv_name}; skipping the pull request")
        return VaultOperationResponse(
            status=OperationStatus.SUCCEEDED,
            message=f"No changes required for {kv_name}",
            kv_name=kv_name,
            file=store.directory,
        )

    change = await _apply_store_edit(bitbucket, store, entries)

    pull_request, validation = await _open_and_validate(
        bitbucket,
        change=change,
        branch=_branch_for(store.team, kv_name, branch_suffix),
        summary=f"Update KV store {kv_name} for {store.team}",
        description=_describe(kv_name, store.directory, entries, "grants replaced"),
        kv_name=kv_name,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} to update {kv_name}. "
            f"It passed validation and is waiting for review."
        ),
        kv_name=kv_name,
        file=store.directory,
        pull_request=_pull_request_info(pull_request),
        validation_builds=_build_infos(validation),
    )


# --------------------------------------------------------------------------- #
# one grant at a time
#
# The sub-resource form of an edit: `PATCH` replaces the whole list, these change a single
# entry without the caller sending the rest. One pair of endpoints covers both kinds —
# which file an entry lands in follows from its `authType`, so there is nothing for a
# caller to choose and no second route to keep in step.
# --------------------------------------------------------------------------- #
async def _prepare_access_change(
    bitbucket: Any,
    kv_name: str,
    entry: Dict[str, Any],
    removing: bool,
) -> Tuple[ResolvedStore, Change, str]:
    """Add or remove one grant in whichever of the store's files it belongs to.

    Only that file is rewritten — the other is not read for a token and not touched, so a
    Kubernetes grant never shows up in the diff of an OIDC change.
    """
    store = await _resolve_store(bitbucket, kv_name)
    kind = auth_type_of(entry)
    path = access_path_for(
        config.VAULT_VALUES_DIR, store.team, store.kv_name, kind
    )
    document = store.k8s_document if kind == K8S_AUTH_TYPE else store.oidc_document

    if removing:
        try:
            updated = remove_access_entry(document, access_entry_identity(entry))
        except AccessEntryNotFound as missing:
            raise VaultOperationError(
                f"{_entry_text(entry)} is not granted on {kv_name}",
                kv_name=kv_name,
                file=store.directory,
                status_code=404,
            ) from missing
    else:
        try:
            updated = add_access_entry(document, entry)
        except DuplicateAccessEntry as duplicate:
            raise VaultOperationError(
                f"{_entry_text(entry)} is already granted on {kv_name}",
                kv_name=kv_name,
                file=store.directory,
                status_code=409,
            ) from duplicate

    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH
    source_commit_id = (
        await bitbucket.get_last_commit(path, at=base_branch)
        if document is not None
        else None
    )

    remaining = read_access_entries(updated)
    if remaining:
        change = Change(
            writes=[
                FileWrite(
                    path=path,
                    content=render_values_yaml(build_access_document(remaining)),
                    source_commit_id=source_commit_id,
                )
            ],
            removes=[],
        )
    else:
        # The last grant of this kind is gone, so the file goes with it — an empty
        # `access: []` is a shape no create writes.
        change = Change(writes=[], removes=[path])

    return store, change, path


async def add_access_entry_operation(
    bitbucket: Any,
    kv_name: str,
    payload: Any,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open a validated pull request that grants one principal access to a store."""
    entry = to_document_entry(payload)
    store, change, _ = await _prepare_access_change(bitbucket, kv_name, entry, removing=False)

    pull_request, validation = await _open_and_validate(
        bitbucket,
        change=change,
        branch=_branch_for(store.team, kv_name, branch_suffix),
        summary=f"Grant {_entry_text(entry)} on {kv_name}",
        description=_describe(kv_name, store.directory, [entry], "grant added"),
        kv_name=kv_name,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} to grant "
            f"{_entry_text(entry)} on {kv_name}. "
            f"It passed validation and is waiting for review."
        ),
        kv_name=kv_name,
        file=store.directory,
        pull_request=_pull_request_info(pull_request),
        validation_builds=_build_infos(validation),
    )


async def remove_access_entry_operation(
    bitbucket: Any,
    kv_name: str,
    entry: Dict[str, Any],
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open a validated pull request that revokes one grant."""
    store, change, _ = await _prepare_access_change(bitbucket, kv_name, entry, removing=True)

    pull_request, validation = await _open_and_validate(
        bitbucket,
        change=change,
        branch=_branch_for(store.team, kv_name, branch_suffix),
        summary=f"Revoke {_entry_text(entry)} on {kv_name}",
        description=_describe(kv_name, store.directory, [entry], "grant removed"),
        kv_name=kv_name,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} to revoke "
            f"{_entry_text(entry)} on {kv_name}. "
            f"It passed validation and is waiting for review."
        ),
        kv_name=kv_name,
        file=store.directory,
        pull_request=_pull_request_info(pull_request),
        validation_builds=_build_infos(validation),
    )


# --------------------------------------------------------------------------- #
# removing a store
# --------------------------------------------------------------------------- #
async def _prepare_delete(bitbucket: Any, kv_name: str) -> ResolvedStore:
    """Resolve the store, and nothing else.

    A delete removes the store's **directory**, both files with it, so there is no document
    to render, nothing to diff and no optimistic-lock token to fetch — git resolves the path
    from the branch it fetched. `_resolve_store` is the 404.

    No referential scan either: a grant lives inside the store it reaches, so the directory
    takes every grant with it and nothing is left dangling.
    """
    return await _resolve_store(bitbucket, kv_name)


async def delete_kv_store_operation(
    bitbucket: Any,
    kv_name: str,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open a validated pull request that deletes the store's directory.

    The store is *not* deleted when this returns: the pull request is open and green, and a
    human merges it. Only the commit differs from any other change here — Bitbucket Server
    has no REST endpoint that deletes a path, so that call is git
    (`app/clients/git.py`), and it removes the directory recursively.
    """
    store = await _prepare_delete(bitbucket, kv_name)

    pull_request, validation = await _open_and_validate(
        bitbucket,
        change=Change(writes=[], removes=[store.directory]),
        branch=_branch_for(store.team, kv_name, branch_suffix),
        summary=f"Delete KV store {kv_name} from {store.team}",
        description=_describe(kv_name, store.directory, store.access, "store deleted"),
        kv_name=kv_name,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} to delete {kv_name}. "
            f"It passed validation and is waiting for review."
        ),
        kv_name=kv_name,
        file=store.directory,
        pull_request=_pull_request_info(pull_request),
        validation_builds=_build_infos(validation),
    )


# --------------------------------------------------------------------------- #
# the Tashtiot per-team file
#
# A different resource, and mostly by *subtraction*: one flat file rather than a directory,
# both kinds in one `access` list rather than split, addressed by team rather than by a
# globally unique store name. So there is no name scan (a team owns exactly one of these,
# and its name is the address), no two-file partition and no directory to remove.
# Everything from the commit onwards is the same chain.
# --------------------------------------------------------------------------- #
def _tashtiot_path(team: str) -> str:
    return tashtiot_access_path(config.VAULT_VALUES_DIR, config.TASHTIOT_DIR, team)


async def _require_tashtiot(bitbucket: Any, team: str) -> Tuple[str, Dict[str, Any]]:
    """The team's file, or a 404. Used by every route but the create."""
    path = _tashtiot_path(team)
    document = await _read_document(bitbucket, path, team)
    if document is None:
        raise VaultOperationError(
            f"{team} has no {config.TASHTIOT_DIR} access file ({path} is not on "
            f"{config.VAULT_VALUES_REPO_BASE_BRANCH})",
            kv_name=team,
            file=path,
            status_code=404,
        )
    return path, document


async def upsert_tashtiot_access_operation(
    bitbucket: Any,
    team: str,
    payload: TashtiotAccessUpsert,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open a validated pull request that sets one team's shared-services grants.

    Create and replace are the same call because the file's identity is the team: there is
    no name to collide with and nothing to be a duplicate of, so a 409 would have nothing
    to describe. An upsert that changes nothing short-circuits like an update does, and
    answers 200.
    """
    path = _tashtiot_path(team)
    entries = [to_document_entry(entry) for entry in payload.access]
    current = await _read_document(bitbucket, path, team)

    if current is not None and yaml_data_equals(read_access_entries(current), entries):
        logger.info(f"No change required for {team}; skipping the pull request")
        return VaultOperationResponse(
            status=OperationStatus.SUCCEEDED,
            message=f"No changes required for {team}",
            kv_name=team,
            file=path,
        )

    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH
    source_commit_id = (
        await bitbucket.get_last_commit(path, at=base_branch)
        if current is not None
        else None
    )

    updated = replace_access_entries(current, entries)
    change = Change(
        writes=[
            FileWrite(
                path=path,
                content=render_values_yaml(updated),
                source_commit_id=source_commit_id,
            )
        ],
        removes=[],
    )

    # Two tenses, because the same word cannot serve both positions: the summary and
    # description read as a completed act ("Created Tashtiot access"), the message as an
    # intent ("to create"). One shared verb gave "to created".
    verb = "updated" if current is not None else "created"
    infinitive = "update" if current is not None else "create"
    pull_request, validation = await _open_and_validate(
        bitbucket,
        change=change,
        branch=_branch_for(config.TASHTIOT_DIR.lower(), team, branch_suffix),
        summary=f"{verb.capitalize()} {config.TASHTIOT_DIR} access for {team}",
        description=_describe(team, path, entries, f"{config.TASHTIOT_DIR} access {verb}"),
        kv_name=team,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} to {infinitive} "
            f"{config.TASHTIOT_DIR} access for {team}. "
            f"It passed validation and is waiting for review."
        ),
        kv_name=team,
        file=path,
        pull_request=_pull_request_info(pull_request),
        validation_builds=_build_infos(validation),
    )


async def delete_tashtiot_access_operation(
    bitbucket: Any,
    team: str,
    branch_suffix: Optional[str] = None,
) -> VaultOperationResponse:
    """Open a validated pull request that removes one team's Tashtiot file."""
    path, document = await _require_tashtiot(bitbucket, team)

    pull_request, validation = await _open_and_validate(
        bitbucket,
        change=Change(writes=[], removes=[path]),
        branch=_branch_for(config.TASHTIOT_DIR.lower(), team, branch_suffix),
        summary=f"Delete {config.TASHTIOT_DIR} access for {team}",
        description=_describe(
            team, path, read_access_entries(document), f"{config.TASHTIOT_DIR} access deleted"
        ),
        kv_name=team,
    )

    return VaultOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=(
            f"Opened pull request {pull_request.id} to delete "
            f"{config.TASHTIOT_DIR} access for {team}. "
            f"It passed validation and is waiting for review."
        ),
        kv_name=team,
        file=path,
        pull_request=_pull_request_info(pull_request),
        validation_builds=_build_infos(validation),
    )


async def get_tashtiot_access_operation(bitbucket: Any, team: str) -> Dict[str, Any]:
    """One team's Tashtiot file, parsed and returned as-is."""
    _, document = await _require_tashtiot(bitbucket, team)
    return document


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
async def get_kv_store_operation(bitbucket: Any, kv_name: str) -> Dict[str, Any]:
    """One store: where it lives and every grant on it, both kinds together.

    Not a raw file, because a store is two of them. The `access` list is the union, oidc
    first, which is the order a create writes them.
    """
    store = await _resolve_store(bitbucket, kv_name)
    return {
        "team": store.team,
        "kv_name": store.kv_name,
        "directory": store.directory,
        ACCESS_KEY: store.access,
    }


async def get_kv_access_operation(bitbucket: Any, kv_name: str) -> List[Dict[str, Any]]:
    """Just the grants on one store. An empty list is a 200, not a 404."""
    store = await _resolve_store(bitbucket, kv_name)
    return store.access


async def list_kv_stores_operation(bitbucket: Any) -> List[Dict[str, Any]]:
    """Every store in the values directory: team, name and directory.

    The one route that answers without a name, and the cheap half of the walk — it lists
    directories rather than reading any file.
    """
    stores: List[Dict[str, Any]] = []
    async for team, kv_name, directory in _walk_store_dirs(bitbucket):
        stores.append({"team": team, "kv_name": kv_name, "directory": directory})
    return stores
