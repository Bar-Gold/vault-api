"""The create chain: sequencing, the two CI gates, and what gets rolled back when.

The connectors are duck-typed fakes (`tests/fakes.py`) so these tests assert on the order
of calls and on the rollback, not on HTTP.
"""
import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.clients.woodpecker import Pipeline, PipelineTimeoutError
from app.v1.vault.operations import (
    VaultOperationError,
    create_kv_mount_operation,
    deploy_pipeline_matcher,
    get_kv_mount_operation,
    pull_request_pipeline_matcher,
)
from app.v1.vault.schemas import OperationStatus
from tests.fakes import FakeBitbucket, FakeWoodpecker, make_pipeline

MOUNT_PATH = "kingmagen/prod/myapp"
VALUES_PATH = "kv/prod/myapp.yaml"
BRANCH = "vault-kv/prod-myapp-abc123"


async def _create(bitbucket, woodpecker, payload):
    return await create_kv_mount_operation(
        bitbucket, woodpecker, payload, branch_suffix="abc123"
    )


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
async def test_happy_path_returns_the_success_message(payload, bitbucket, woodpecker):
    response = await _create(bitbucket, woodpecker, payload)

    assert response.status == OperationStatus.SUCCEEDED
    assert response.message == f"Successful creation of {MOUNT_PATH}"
    assert response.mount_path == MOUNT_PATH
    assert response.error is None


async def test_happy_path_reports_pull_request_and_both_pipelines(payload, bitbucket, woodpecker):
    response = await _create(bitbucket, woodpecker, payload)

    assert response.pull_request.id == 101
    assert response.pull_request.state == "MERGED"
    assert response.validation_pipeline.number == 2
    assert response.deploy_pipeline.number == 3
    assert response.policies == [
        "kingmagen-prod-myapp-read",
        "kingmagen-prod-myapp-write",
    ]


async def test_happy_path_call_order(payload, bitbucket, woodpecker):
    await _create(bitbucket, woodpecker, payload)

    assert bitbucket.calls == [
        "get_file_content",  # duplicate check
        "create_branch",
        "put_file",
        "create_pull_request",
        "get_pull_request",  # re-read for the current version
        "merge_pull_request",
    ]
    # Nothing was rolled back.
    assert "decline_pull_request" not in bitbucket.calls
    assert "delete_branch" not in bitbucket.calls


async def test_committed_file_is_the_rendered_values_yaml(payload, bitbucket, woodpecker):
    await _create(bitbucket, woodpecker, payload)

    committed = yaml.safe_load(bitbucket.committed[VALUES_PATH])
    assert committed["mount"]["path"] == MOUNT_PATH
    assert committed["mount"]["options"] == {"version": "2"}
    assert [p["name"] for p in committed["policies"]] == [
        "kingmagen-prod-myapp-read",
        "kingmagen-prod-myapp-write",
    ]


async def test_merge_uses_the_freshly_read_version(payload, bitbucket, woodpecker):
    """The fake bumps `version` on every read; merging with a stale one would 409 for real."""
    await _create(bitbucket, woodpecker, payload)

    assert bitbucket.pull_requests[101].version == 1


# --------------------------------------------------------------------------- #
# duplicate guard
# --------------------------------------------------------------------------- #
async def test_existing_mount_is_rejected_before_anything_is_created(payload, woodpecker):
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: "mount: {}\n"})

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    assert exc_info.value.status_code == 409
    assert MOUNT_PATH in exc_info.value.message
    assert bitbucket.calls == ["get_file_content"]


async def test_non_404_on_the_duplicate_check_propagates(payload, woodpecker):
    bitbucket = FakeBitbucket(
        fail_on={
            "get_file_content": ExternalServiceError(
                service_name="bitbucket", detail="server on fire", status_code=500
            )
        }
    )

    with pytest.raises(ExternalServiceError):
        await _create(bitbucket, woodpecker, payload)

    assert "create_branch" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# rollback before the pull request exists
# --------------------------------------------------------------------------- #
async def test_failed_commit_deletes_the_branch(payload, woodpecker):
    bitbucket = FakeBitbucket(
        fail_on={
            "put_file": ExternalServiceError(
                service_name="bitbucket", detail="commit rejected", status_code=400
            )
        }
    )

    with pytest.raises(ExternalServiceError):
        await _create(bitbucket, woodpecker, payload)

    assert bitbucket.calls == ["get_file_content", "create_branch", "put_file", "delete_branch"]


async def test_failed_pull_request_deletes_the_branch(payload, woodpecker):
    bitbucket = FakeBitbucket(
        fail_on={
            "create_pull_request": ExternalServiceError(
                service_name="bitbucket", detail="no reviewers", status_code=400
            )
        }
    )

    with pytest.raises(ExternalServiceError):
        await _create(bitbucket, woodpecker, payload)

    assert bitbucket.calls[-1] == "delete_branch"


async def test_branch_cleanup_failure_does_not_mask_the_original_error(payload, woodpecker):
    """A failing rollback is logged, never raised over the error that caused it."""
    bitbucket = FakeBitbucket(
        fail_on={
            "put_file": ExternalServiceError(
                service_name="bitbucket", detail="commit rejected", status_code=400
            ),
            "delete_branch": RuntimeError("branch already gone"),
        }
    )

    with pytest.raises(ExternalServiceError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    assert exc_info.value.detail == "commit rejected"


# --------------------------------------------------------------------------- #
# gate 1 — the validation pipeline
# --------------------------------------------------------------------------- #
async def test_failed_validation_declines_the_pull_request(payload, bitbucket):
    woodpecker = FakeWoodpecker(
        results=[make_pipeline(number=2, status="failure", event="pull_request")]
    )

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    error = exc_info.value
    assert error.status_code == 502
    assert "Validation pipeline #2 finished with status 'failure'" in error.message
    assert error.validation_pipeline.number == 2
    assert error.deploy_pipeline is None
    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls
    assert "merge_pull_request" not in bitbucket.calls


async def test_validation_timeout_declines_and_reports_504(payload, bitbucket):
    woodpecker = FakeWoodpecker(
        results=[
            PipelineTimeoutError(
                "Pipeline #2 still running after 900s",
                pipeline=Pipeline(number=2, status="running"),
            )
        ]
    )

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    error = exc_info.value
    assert error.status_code == 504
    assert "Validation pipeline did not complete" in error.message
    assert error.validation_pipeline.status == "running"
    assert "decline_pull_request" in bitbucket.calls


async def test_decline_failure_does_not_stop_the_branch_cleanup(payload):
    """A failing decline is logged; the branch is still removed and the real error surfaces."""
    bitbucket = FakeBitbucket(
        fail_on={"decline_pull_request": RuntimeError("pull request already closed")}
    )
    woodpecker = FakeWoodpecker(
        results=[make_pipeline(number=2, status="failure", event="pull_request")]
    )

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    assert "Validation pipeline #2" in exc_info.value.message
    assert "delete_branch" in bitbucket.calls


async def test_missing_validation_pipeline_still_cleans_up(payload, bitbucket):
    woodpecker = FakeWoodpecker(
        results=[PipelineTimeoutError("No matching Woodpecker pipeline appeared within 120s")]
    )

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    assert exc_info.value.validation_pipeline is None
    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls


# --------------------------------------------------------------------------- #
# the merge
# --------------------------------------------------------------------------- #
async def test_unmergeable_pull_request_is_left_open(payload, woodpecker):
    """A validated-but-unmergeable PR is a human's problem — do not decline it."""
    bitbucket = FakeBitbucket(
        fail_on={
            "merge_pull_request": ExternalServiceError(
                service_name="bitbucket", detail="The pull request has conflicts", status_code=409
            )
        }
    )

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    error = exc_info.value
    assert "could not be merged" in error.message
    assert "conflicts" in error.message
    assert error.pull_request.state == "OPEN"
    assert "decline_pull_request" not in bitbucket.calls
    assert "delete_branch" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# gate 2 — the deploy pipeline (past the point of no return)
# --------------------------------------------------------------------------- #
async def test_failed_deploy_reports_the_change_as_merged(payload, bitbucket):
    woodpecker = FakeWoodpecker(
        results=[
            make_pipeline(number=2, status="success", event="pull_request"),
            make_pipeline(number=3, status="failure", event="push"),
        ]
    )

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    error = exc_info.value
    assert error.status_code == 502
    assert "Deploy pipeline #3 finished with status 'failure'" in error.message
    assert "already merged" in error.message
    assert error.deploy_pipeline.number == 3
    assert error.pull_request.state == "MERGED"
    # Nothing is rolled back after a merge.
    assert "decline_pull_request" not in bitbucket.calls
    assert "delete_branch" not in bitbucket.calls


async def test_deploy_timeout_reports_504_and_the_merge(payload, bitbucket):
    woodpecker = FakeWoodpecker(
        results=[
            make_pipeline(number=2, status="success", event="pull_request"),
            PipelineTimeoutError("Pipeline #3 still running after 900s"),
        ]
    )

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    assert exc_info.value.status_code == 504
    assert "already merged" in exc_info.value.message


# --------------------------------------------------------------------------- #
# error -> response mapping
# --------------------------------------------------------------------------- #
async def test_operation_error_renders_a_failed_response(payload, bitbucket):
    woodpecker = FakeWoodpecker(
        results=[make_pipeline(number=2, status="failure", event="pull_request")]
    )

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    response = exc_info.value.to_response()
    assert response.status == OperationStatus.FAILED
    assert response.mount_path == MOUNT_PATH
    assert response.error == response.message
    assert response.deploy_pipeline is None


# --------------------------------------------------------------------------- #
# pipeline matchers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fields",
    [
        {"branch": BRANCH},
        {"ref": f"refs/heads/{BRANCH}"},
        {"refspec": f"{BRANCH}:master"},
    ],
)
def test_validation_matcher_finds_the_branch_wherever_woodpecker_puts_it(fields):
    matches = pull_request_pipeline_matcher(BRANCH, min_number=0)
    assert matches(Pipeline(number=5, status="pending", event="pull_request", **fields))


def test_validation_matcher_ignores_other_events_and_branches():
    matches = pull_request_pipeline_matcher(BRANCH, min_number=0)
    assert not matches(Pipeline(number=5, status="pending", event="push", branch=BRANCH))
    assert not matches(
        Pipeline(number=5, status="pending", event="pull_request", branch="vault-kv/other")
    )


def test_validation_matcher_ignores_pipelines_older_than_the_watermark():
    matches = pull_request_pipeline_matcher(BRANCH, min_number=5)
    assert not matches(Pipeline(number=5, status="success", event="pull_request", branch=BRANCH))
    assert matches(Pipeline(number=6, status="success", event="pull_request", branch=BRANCH))


def test_deploy_matcher_prefers_the_merge_commit():
    matches = deploy_pipeline_matcher("master", "deadbeef", min_number=0)
    assert matches(Pipeline(number=9, status="running", event="push", commit="deadbeef"))
    # Same branch, different commit — that is somebody else's push.
    assert not matches(
        Pipeline(number=9, status="running", event="push", branch="master", commit="other")
    )


def test_deploy_matcher_falls_back_to_the_base_branch():
    matches = deploy_pipeline_matcher("master", None, min_number=0)
    assert matches(Pipeline(number=9, status="running", event="push", branch="master"))
    assert not matches(Pipeline(number=9, status="running", event="push", branch="develop"))


def test_deploy_matcher_ignores_pull_request_events():
    matches = deploy_pipeline_matcher("master", None, min_number=0)
    assert not matches(Pipeline(number=9, status="running", event="pull_request", branch="master"))


async def test_watermark_degrades_to_zero_when_the_list_call_fails(payload, bitbucket, woodpecker):
    """Losing the watermark must not abort the request — it only widens the match."""

    async def unavailable(*args, **kwargs):
        raise ExternalServiceError(
            service_name="woodpecker", detail="service unavailable", status_code=503
        )

    woodpecker.list_pipelines = unavailable

    response = await _create(bitbucket, woodpecker, payload)

    assert response.status == OperationStatus.SUCCEEDED
    # min_number fell back to 0, so even pipeline #1 is eligible.
    assert woodpecker.matchers[0](
        Pipeline(number=1, status="success", event="pull_request", branch=BRANCH)
    )


async def test_watermark_excludes_pipelines_that_existed_before_the_request(payload, bitbucket):
    """The matcher handed to Woodpecker must not accept a pre-existing pipeline."""
    woodpecker = FakeWoodpecker(
        existing=[make_pipeline(number=5, status="success", event="pull_request")],
        results=[
            make_pipeline(number=6, status="success", event="pull_request"),
            make_pipeline(number=7, status="success", event="push"),
        ],
    )

    await _create(bitbucket, woodpecker, payload)

    validation_matcher = woodpecker.matchers[0]
    stale = Pipeline(number=5, status="success", event="pull_request", branch=BRANCH)
    fresh = Pipeline(number=6, status="success", event="pull_request", branch=BRANCH)
    assert not validation_matcher(stale)
    assert validation_matcher(fresh)


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
async def test_get_mount_reads_the_values_file_from_the_base_branch():
    bitbucket = FakeBitbucket(
        existing_files={VALUES_PATH: yaml.safe_dump({"mount": {"path": MOUNT_PATH}})}
    )

    data = await get_kv_mount_operation(bitbucket, "prod", "myapp")

    assert data["mount"]["path"] == MOUNT_PATH


async def test_get_mount_reports_a_corrupt_values_file():
    """A hand-edited/badly-merged file must not surface as a bare 500."""
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: "mount: [unclosed\n  ::: bad"})

    with pytest.raises(VaultOperationError) as exc_info:
        await get_kv_mount_operation(bitbucket, "prod", "myapp")

    assert "not valid YAML" in exc_info.value.message
    assert exc_info.value.mount_path == MOUNT_PATH


async def test_get_mount_propagates_not_found():
    bitbucket = FakeBitbucket()

    with pytest.raises(ExternalServiceError) as exc_info:
        await get_kv_mount_operation(bitbucket, "prod", "nope")

    assert exc_info.value.status_code == 404
