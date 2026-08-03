"""The create chain: sequencing, the two CI gates, and what gets rolled back when.

The connectors are duck-typed fakes (`tests/fakes.py`) so these tests assert on the order
of calls and on the rollback, not on HTTP.
"""
import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.clients.woodpecker import Pipeline, PipelineTimeoutError
from app.helpers import build_kv_store, build_kv_stores_document, render_values_yaml
from app.v1.vault.operations import (
    VaultOperationError,
    create_kv_mount_operation,
    deploy_pipeline_matcher,
    get_kv_file_operation,
    get_kv_store_operation,
    pull_request_pipeline_matcher,
)
from app.v1.vault.schemas import OperationStatus
from tests.fakes import FakeBitbucket, FakeWoodpecker, make_pipeline

KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
BRANCH = "vault-kv/payments-myapp-abc123"
ROLES = {"read": ["app01.corp.example.com"]}


def _file_with(*names, description="payments secrets"):
    """A rendered values file already holding these stores."""
    return render_values_yaml(
        build_kv_stores_document([build_kv_store(n, description, ROLES) for n in names])
    )


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
    assert response.message == f"Successful creation of {KV}"
    assert response.kv_name == KV
    assert response.error is None


async def test_happy_path_reports_pull_request_and_both_pipelines(payload, bitbucket, woodpecker):
    response = await _create(bitbucket, woodpecker, payload)

    assert response.pull_request.id == 101
    assert response.pull_request.state == "MERGED"
    assert response.validation_pipeline.number == 2
    assert response.deploy_pipeline.number == 3


async def test_happy_path_call_order(payload, bitbucket, woodpecker):
    await _create(bitbucket, woodpecker, payload)

    assert bitbucket.calls == [
        "list_files",  # scan every file for the name
        "get_file_content",  # read the target file (404 -> new file)
        "create_branch",
        "put_file",
        "create_pull_request",
        "get_pull_request",  # re-read for the current version
        "merge_pull_request",
    ]
    # Nothing was rolled back.
    assert "decline_pull_request" not in bitbucket.calls
    assert "delete_branch" not in bitbucket.calls


async def test_committed_file_is_a_kvstores_list(payload, bitbucket, woodpecker):
    await _create(bitbucket, woodpecker, payload)

    committed = yaml.safe_load(bitbucket.committed[VALUES_PATH])
    assert committed == {
        "kvStores": [
            {
                "name": KV,
                "description": payload.kv_description,
                "roles": {"read": ["app01.corp.example.com"]},
            }
        ]
    }


async def test_a_new_file_is_written_without_an_optimistic_lock_token(
    payload, bitbucket, woodpecker
):
    """sourceCommitId on a path that does not exist makes Bitbucket reject the write."""
    await _create(bitbucket, woodpecker, payload)

    assert bitbucket.source_commit_ids == [None]
    assert "get_last_commit" not in bitbucket.calls


async def test_appending_to_an_existing_file_keeps_the_existing_stores(
    payload, woodpecker
):
    """The whole point of the format: one file, several stores."""
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: _file_with("already-here")})

    await _create(bitbucket, woodpecker, payload)

    committed = yaml.safe_load(bitbucket.committed[VALUES_PATH])
    assert [s["name"] for s in committed["kvStores"]] == ["already-here", KV]


async def test_appending_to_an_existing_file_sends_the_lock_token(payload, woodpecker):
    """Editing a path that already exists needs Bitbucket's optimistic-lock token."""
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: _file_with("already-here")})

    await _create(bitbucket, woodpecker, payload)

    assert bitbucket.source_commit_ids == ["file-commit-sha"]
    assert bitbucket.calls.index("get_last_commit") < bitbucket.calls.index("put_file")


async def test_merge_uses_the_freshly_read_version(payload, bitbucket, woodpecker):
    """The fake bumps `version` on every read; merging with a stale one would 409 for real."""
    await _create(bitbucket, woodpecker, payload)

    assert bitbucket.pull_requests[101].version == 1


# --------------------------------------------------------------------------- #
# duplicate guard — names are global to Vault, so it scans every file
# --------------------------------------------------------------------------- #
async def test_a_name_already_in_the_target_file_is_rejected(payload, woodpecker):
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: _file_with(KV)})

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    assert exc_info.value.status_code == 409
    assert KV in exc_info.value.message
    assert "create_branch" not in bitbucket.calls


async def test_a_name_used_in_a_different_file_is_also_rejected(payload, woodpecker):
    """Store names are global to Vault, so uniqueness cannot be scoped to one file."""
    bitbucket = FakeBitbucket(existing_files={"kv/other-team.yaml": _file_with(KV)})

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, woodpecker, payload)

    assert exc_info.value.status_code == 409
    assert "kv/other-team.yaml" in exc_info.value.message
    assert "create_branch" not in bitbucket.calls


async def test_a_different_name_in_the_same_file_is_fine(payload, bitbucket, woodpecker):
    bitbucket.existing_files[VALUES_PATH] = _file_with("something-else")

    response = await _create(bitbucket, woodpecker, payload)

    assert response.status == OperationStatus.SUCCEEDED


async def test_an_unparseable_file_does_not_block_an_unrelated_create(
    payload, bitbucket, woodpecker
):
    """A hand-edited file elsewhere must not make every create fail."""
    bitbucket.existing_files["kv/broken.yaml"] = "kvStores: [unclosed\n  ::: bad"

    response = await _create(bitbucket, woodpecker, payload)

    assert response.status == OperationStatus.SUCCEEDED


async def test_non_yaml_files_in_the_values_dir_are_skipped(
    payload, bitbucket, woodpecker
):
    bitbucket.existing_files["kv/README.md"] = "# not a values file\n"

    response = await _create(bitbucket, woodpecker, payload)

    assert response.status == OperationStatus.SUCCEEDED


async def test_non_404_on_the_duplicate_check_propagates(payload, woodpecker):
    bitbucket = FakeBitbucket(
        existing_files={"kv/other.yaml": _file_with("someone-else")},
        fail_on={
            "get_file_content": ExternalServiceError(
                service_name="bitbucket", detail="server on fire", status_code=500
            )
        },
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

    assert bitbucket.calls == [
        "list_files",
        "get_file_content",
        "create_branch",
        "put_file",
        "delete_branch",
    ]


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
    assert response.kv_name == KV
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
# reads
# --------------------------------------------------------------------------- #
async def test_get_file_returns_every_store():
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: _file_with("one", "two")})

    data = await get_kv_file_operation(bitbucket, FILE)

    assert [s["name"] for s in data["kvStores"]] == ["one", "two"]


async def test_get_file_reports_a_corrupt_values_file():
    """A hand-edited/badly-merged file must not surface as a bare 500."""
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: "kvStores: [unclosed\n  ::: bad"})

    with pytest.raises(VaultOperationError) as exc_info:
        await get_kv_file_operation(bitbucket, FILE)

    assert "not valid YAML" in exc_info.value.message


async def test_get_file_of_a_missing_file_is_404():
    with pytest.raises(VaultOperationError) as exc_info:
        await get_kv_file_operation(FakeBitbucket(), "nope")

    assert exc_info.value.status_code == 404


async def test_get_store_returns_only_that_entry():
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: _file_with("one", "two")})

    store = await get_kv_store_operation(bitbucket, FILE, "two")

    assert store["name"] == "two"
    assert store["roles"] == ROLES


async def test_get_store_of_an_absent_name_is_404():
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: _file_with("one")})

    with pytest.raises(VaultOperationError) as exc_info:
        await get_kv_store_operation(bitbucket, FILE, "nope")

    assert exc_info.value.status_code == 404
    assert "not defined in" in exc_info.value.message


async def test_get_store_of_a_missing_file_is_404():
    with pytest.raises(VaultOperationError) as exc_info:
        await get_kv_store_operation(FakeBitbucket(), "nope", KV)

    assert exc_info.value.status_code == 404
