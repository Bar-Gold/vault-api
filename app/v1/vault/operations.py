"""Business logic for Vault KV mounts.

The create flow is a GitOps chain with two CI gates:

    1. commit the values file to a short-lived branch
    2. open a pull request against the base branch
    3. wait for the **validation** pipeline (Woodpecker, `pull_request` event) — it blocks the merge
    4. merge the pull request
    5. wait for the **deploy** pipeline (Woodpecker, `push` event) — it applies the change to Vault

The request blocks until step 5 finishes, then returns "Successful creation of <mount path>".

There is no transaction, so each step rolls back what the earlier steps did:
committing fails -> delete the branch; the PR fails to open -> delete the branch; the
validation pipeline fails or times out -> decline the PR and delete the branch. **The merge
is the point of no return**: once step 4 succeeds the change is on the base branch, so a
failing deploy pipeline is reported as-is rather than rolled back — un-merging would need a
revert PR and a second human decision.
"""

import uuid
from typing import Any, Callable, Dict, List, Optional

import yaml
from loguru import logger
from tashtiot_apis_library.connectors import ExternalServiceError

from ...clients.bitbucket import PullRequest
from ...clients.woodpecker import Pipeline, PipelineTimeoutError
from ...global_conf import global_config
from ...helpers import (
    build_mount_path,
    build_values_data,
    render_values_yaml,
    build_branch_name,
    values_file_path,
)
from .conf import config
from .schemas import (
    OperationStatus,
    PipelineInfo,
    PullRequestInfo,
    VaultKVCreate,
    VaultKVOperationResponse,
)


class VaultOperationError(Exception):
    """A create flow that failed for a business reason rather than a transport error.

    Carries everything the route needs to build the failed response body, so the route
    stays a thin mapping layer.
    """

    def __init__(
        self,
        message: str,
        mount_path: str,
        status_code: int = 502,
        policies: Optional[List[str]] = None,
        pull_request: Optional[PullRequest] = None,
        validation_pipeline: Optional[Pipeline] = None,
        deploy_pipeline: Optional[Pipeline] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.mount_path = mount_path
        self.status_code = status_code
        self.policies = policies or []
        self.pull_request = pull_request
        self.validation_pipeline = validation_pipeline
        self.deploy_pipeline = deploy_pipeline

    def to_response(self) -> "VaultKVOperationResponse":
        """The failed response body, so the route only has to pick the HTTP status."""
        return VaultKVOperationResponse(
            status=OperationStatus.FAILED,
            message=self.message,
            mount_path=self.mount_path,
            policies=self.policies,
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


async def _assert_absent(bitbucket: Any, path: str, mount_path: str) -> None:
    """Reject a create for a mount that is already defined on the base branch."""
    try:
        await bitbucket.get_file_content(path, at=config.VAULT_VALUES_REPO_BASE_BRANCH)
    except ExternalServiceError as exc:
        if exc.status_code == 404:
            return
        raise
    raise VaultOperationError(
        f"{mount_path} already exists ({path} is already on "
        f"{config.VAULT_VALUES_REPO_BASE_BRANCH})",
        mount_path=mount_path,
        status_code=409,
    )


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #
async def create_kv_mount_operation(
    bitbucket: Any,
    woodpecker: Any,
    payload: VaultKVCreate,
    branch_suffix: Optional[str] = None,
) -> VaultKVOperationResponse:
    """Run the full PR -> CI -> merge -> CI chain, blocking until the deploy pipeline ends."""
    environment = payload.metadata.environment
    team = global_config.TEAM_NAME
    app_name = payload.spec.app_name

    mount_path, values = build_values_data(payload, team, environment)
    policies = [policy["name"] for policy in values["policies"]]
    yaml_body = render_values_yaml(values)
    path = values_file_path(config.VAULT_VALUES_DIR, environment, app_name)

    suffix = branch_suffix or uuid.uuid4().hex[:8]
    branch = build_branch_name(environment, app_name, suffix, config.BRANCH_PREFIX)
    base_branch = config.VAULT_VALUES_REPO_BASE_BRANCH

    await _assert_absent(bitbucket, path, mount_path)

    # Watermark before anything can trigger CI, so we never match a pre-existing pipeline.
    baseline = await _latest_pipeline_number(woodpecker)

    # ---- 1. branch + commit ------------------------------------------------ #
    await bitbucket.create_branch(branch, base_branch)
    try:
        await bitbucket.put_file(
            path=path,
            branch=branch,
            content=yaml_body,
            message=f"Create Vault KV mount {mount_path}",
        )
    except Exception:
        await _delete_branch_quietly(bitbucket, branch)
        raise

    # ---- 2. pull request --------------------------------------------------- #
    try:
        pull_request = await bitbucket.create_pull_request(
            title=f"Create Vault KV mount {mount_path}",
            description=(
                f"Automated by vault-api.\n\n"
                f"- mount: `{mount_path}` (KV v{int(payload.spec.kv_version)})\n"
                f"- policies: {', '.join(policies)}\n"
                f"- owner: {payload.spec.owner}\n"
                f"- environment: {environment}\n"
            ),
            from_branch=branch,
            to_branch=base_branch,
            reviewers=config.PR_REVIEWERS,
        )
    except Exception:
        await _delete_branch_quietly(bitbucket, branch)
        raise

    logger.info(f"Opened pull request {pull_request.id} for {mount_path}")

    # ---- 3. validation pipeline (blocks the merge) ------------------------- #
    try:
        validation = await woodpecker.await_pipeline(
            pull_request_pipeline_matcher(branch, baseline)
        )
    except PipelineTimeoutError as timeout_error:
        await _decline_and_cleanup(bitbucket, pull_request, branch)
        raise VaultOperationError(
            f"Validation pipeline did not complete: {timeout_error.message}",
            mount_path=mount_path,
            status_code=504,
            policies=policies,
            pull_request=pull_request,
            validation_pipeline=timeout_error.pipeline,
        ) from timeout_error

    if not validation.succeeded:
        await _decline_and_cleanup(bitbucket, pull_request, branch)
        raise VaultOperationError(
            _pipeline_failure("Validation", validation),
            mount_path=mount_path,
            policies=policies,
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
            mount_path=mount_path,
            policies=policies,
            pull_request=pull_request,
            validation_pipeline=validation,
        ) from merge_error

    logger.info(f"Merged pull request {merged.id} for {mount_path}")

    # ---- 5. deploy pipeline ------------------------------------------------ #
    try:
        deploy = await woodpecker.await_pipeline(
            deploy_pipeline_matcher(base_branch, merged.merge_commit, merge_baseline)
        )
    except PipelineTimeoutError as timeout_error:
        raise VaultOperationError(
            f"Deploy pipeline did not complete: {timeout_error.message}. "
            f"The change is already merged to {base_branch}.",
            mount_path=mount_path,
            status_code=504,
            policies=policies,
            pull_request=merged,
            validation_pipeline=validation,
            deploy_pipeline=timeout_error.pipeline,
        ) from timeout_error

    if not deploy.succeeded:
        raise VaultOperationError(
            f"{_pipeline_failure('Deploy', deploy)}. "
            f"The change is already merged to {base_branch} and needs a revert.",
            mount_path=mount_path,
            policies=policies,
            pull_request=merged,
            validation_pipeline=validation,
            deploy_pipeline=deploy,
        )

    return VaultKVOperationResponse(
        status=OperationStatus.SUCCEEDED,
        message=f"Successful creation of {mount_path}",
        mount_path=mount_path,
        policies=policies,
        pull_request=_pull_request_info(merged),
        validation_pipeline=_pipeline_info(validation),
        deploy_pipeline=_pipeline_info(deploy),
    )


async def get_kv_mount_operation(bitbucket: Any, environment: str, app_name: str) -> Dict[str, Any]:
    """Read the committed values file for a mount from the base branch."""
    path = values_file_path(config.VAULT_VALUES_DIR, environment, app_name)
    content = await bitbucket.get_file_content(
        path, at=config.VAULT_VALUES_REPO_BASE_BRANCH
    )
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError as parse_error:
        # The file is in the repo but is not valid YAML — hand-edited, or a bad merge.
        # Say so instead of surfacing a bare 500.
        raise VaultOperationError(
            f"{path} exists on {config.VAULT_VALUES_REPO_BASE_BRANCH} but is not valid YAML: "
            f"{parse_error}",
            mount_path=build_mount_path(global_config.TEAM_NAME, environment, app_name),
        ) from parse_error
