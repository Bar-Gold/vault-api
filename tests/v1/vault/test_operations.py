"""The create chain: sequencing, the two CI gates, and what gets rolled back when.

The connector is a duck-typed fake (`tests/fakes.py`) so these tests assert on the order
of calls and on the rollback, not on HTTP. There is only one of them: the CI gates read
Bitbucket's build statuses — the pull request's Builds tab — so Bitbucket is the whole of
the outside world here.
"""
import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.clients.bitbucket import BuildStatus, BuildTimeoutError
from app.helpers import build_kv_store, build_kv_stores_document, render_values_yaml
from app.v1.vault.operations import (
    VaultOperationError,
    create_kv_mount_operation,
    get_kv_file_operation,
    get_kv_store_operation,
)
from app.v1.vault.schemas import OperationStatus
from tests.fakes import FakeBitbucket, failing, passing

KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
BRANCH = "vault-kv/payments-myapp-abc123"
ROLES = {"read": ["app01.corp.example.com"]}
# What FakeBitbucket reports as the head of the branch put_file committed to.
PR_COMMIT = f"sha-{BRANCH}"


def _file_with(*names, description="payments secrets"):
    """A rendered values file already holding these stores."""
    return render_values_yaml(
        build_kv_stores_document([build_kv_store(n, description, ROLES) for n in names])
    )


async def _create(bitbucket, payload):
    return await create_kv_mount_operation(
        bitbucket, payload, branch_suffix="abc123"
    )


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
async def test_happy_path_returns_the_success_message(payload, bitbucket):
    response = await _create(bitbucket, payload)

    assert response.status == OperationStatus.SUCCEEDED
    assert response.message == f"Successful creation of {KV}"
    assert response.kv_name == KV
    assert response.error is None


async def test_happy_path_reports_pull_request_and_both_gates(payload, bitbucket):
    response = await _create(bitbucket, payload)

    assert response.pull_request.id == 101
    assert response.pull_request.state == "MERGED"
    assert [b.key for b in response.validation_builds] == ["ci/woodpecker/pr/build"]
    assert [b.state for b in response.deploy_builds] == ["SUCCESSFUL"]


async def test_happy_path_call_order(payload, bitbucket):
    await _create(bitbucket, payload)

    assert bitbucket.calls == [
        "list_files",  # scan every file for the name
        "get_file_content",  # read the target file (404 -> new file)
        "create_branch",
        "put_file",
        "create_pull_request",
        "await_builds",  # gate 1, on the pull request's commit
        "get_pull_request",  # re-read for the current version
        "merge_pull_request",
        "await_builds",  # gate 2, on the merge commit
    ]
    # Nothing was rolled back.
    assert "decline_pull_request" not in bitbucket.calls
    assert "delete_branch" not in bitbucket.calls


async def test_each_gate_watches_its_own_commit(payload, bitbucket):
    """The whole point of reading builds from Bitbucket: a gate names a sha, not a pipeline.

    Gate 1 asks about the head of the branch the change was committed to — the commit the
    pull request shows in its Builds tab. Gate 2 asks about the merge commit.
    """
    await _create(bitbucket, payload)

    assert bitbucket.awaited_commits == [PR_COMMIT, "merge-sha-1"]


async def test_a_distinct_merge_commit_excludes_nothing(payload, bitbucket):
    """The exclusion only exists for fast-forwards; a real merge commit carries no history."""
    await _create(bitbucket, payload)

    assert bitbucket.excluded_keys == [frozenset(), frozenset()]


async def test_a_fast_forward_merge_skips_the_keys_gate_one_already_saw(payload, bitbucket):
    """A fast-forward leaves the base branch on the commit the PR built.

    Its validation results are already sitting on that sha, so without excluding them the
    deploy gate would open on gate 1's own answer and call the deploy a success before it
    started.
    """
    bitbucket.merge_commit = PR_COMMIT

    await _create(bitbucket, payload)

    assert bitbucket.awaited_commits == [PR_COMMIT, PR_COMMIT]
    assert bitbucket.excluded_keys[1] == frozenset({"ci/woodpecker/pr/build"})


async def test_the_deploy_gate_falls_back_to_the_base_branch_head(payload, bitbucket):
    """Not every merge strategy reports a merge commit; the head of the base branch does."""
    bitbucket.merge_commit = None

    await _create(bitbucket, payload)

    assert "get_branch_head" in bitbucket.calls
    assert bitbucket.awaited_commits == [PR_COMMIT, "base-head-sha"]


async def test_a_pull_request_without_a_source_commit_is_rolled_back(payload, bitbucket):
    """No sha means no gate, and merging unvalidated is not the fallback."""
    original = bitbucket.create_pull_request

    async def without_source_commit(*args, **kwargs):
        pull_request = await original(*args, **kwargs)
        pull_request.from_commit = ""
        return pull_request

    bitbucket.create_pull_request = without_source_commit

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    assert "did not report a source commit" in exc_info.value.message
    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls
    assert "merge_pull_request" not in bitbucket.calls


async def test_committed_file_is_a_kvstores_list(payload, bitbucket):
    await _create(bitbucket, payload)

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
    payload, bitbucket
):
    """sourceCommitId on a path that does not exist makes Bitbucket reject the write."""
    await _create(bitbucket, payload)

    assert bitbucket.source_commit_ids == [None]
    assert "get_last_commit" not in bitbucket.calls


async def test_appending_to_an_existing_file_keeps_the_existing_stores(
    payload
):
    """The whole point of the format: one file, several stores."""
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: _file_with("already-here")})

    await _create(bitbucket, payload)

    committed = yaml.safe_load(bitbucket.committed[VALUES_PATH])
    assert [s["name"] for s in committed["kvStores"]] == ["already-here", KV]


async def test_appending_to_an_existing_file_sends_the_lock_token(payload):
    """Editing a path that already exists needs Bitbucket's optimistic-lock token."""
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: _file_with("already-here")})

    await _create(bitbucket, payload)

    assert bitbucket.source_commit_ids == ["file-commit-sha"]
    assert bitbucket.calls.index("get_last_commit") < bitbucket.calls.index("put_file")


async def test_merge_uses_the_freshly_read_version(payload, bitbucket):
    """The fake bumps `version` on every read; merging with a stale one would 409 for real."""
    await _create(bitbucket, payload)

    assert bitbucket.pull_requests[101].version == 1


# --------------------------------------------------------------------------- #
# duplicate guard — names are global to Vault, so it scans every file
# --------------------------------------------------------------------------- #
async def test_a_name_already_in_the_target_file_is_rejected(payload):
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: _file_with(KV)})

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    assert exc_info.value.status_code == 409
    assert KV in exc_info.value.message
    assert "create_branch" not in bitbucket.calls


async def test_a_name_used_in_a_different_file_is_also_rejected(payload):
    """Store names are global to Vault, so uniqueness cannot be scoped to one file."""
    bitbucket = FakeBitbucket(existing_files={"kv/other-team.yaml": _file_with(KV)})

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    assert exc_info.value.status_code == 409
    assert "kv/other-team.yaml" in exc_info.value.message
    assert "create_branch" not in bitbucket.calls


async def test_a_different_name_in_the_same_file_is_fine(payload, bitbucket):
    bitbucket.existing_files[VALUES_PATH] = _file_with("something-else")

    response = await _create(bitbucket, payload)

    assert response.status == OperationStatus.SUCCEEDED


async def test_an_unparseable_file_does_not_block_an_unrelated_create(
    payload, bitbucket
):
    """A hand-edited file elsewhere must not make every create fail."""
    bitbucket.existing_files["kv/broken.yaml"] = "kvStores: [unclosed\n  ::: bad"

    response = await _create(bitbucket, payload)

    assert response.status == OperationStatus.SUCCEEDED


async def test_non_yaml_files_in_the_values_dir_are_skipped(
    payload, bitbucket
):
    bitbucket.existing_files["kv/README.md"] = "# not a values file\n"

    response = await _create(bitbucket, payload)

    assert response.status == OperationStatus.SUCCEEDED


async def test_non_404_on_the_duplicate_check_propagates(payload):
    bitbucket = FakeBitbucket(
        existing_files={"kv/other.yaml": _file_with("someone-else")},
        fail_on={
            "get_file_content": ExternalServiceError(
                service_name="bitbucket", detail="server on fire", status_code=500
            )
        },
    )

    with pytest.raises(ExternalServiceError):
        await _create(bitbucket, payload)

    assert "create_branch" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# rollback before the pull request exists
# --------------------------------------------------------------------------- #
async def test_failed_commit_deletes_the_branch(payload):
    bitbucket = FakeBitbucket(
        fail_on={
            "put_file": ExternalServiceError(
                service_name="bitbucket", detail="commit rejected", status_code=400
            )
        }
    )

    with pytest.raises(ExternalServiceError):
        await _create(bitbucket, payload)

    assert bitbucket.calls == [
        "list_files",
        "get_file_content",
        "create_branch",
        "put_file",
        "delete_branch",
    ]


async def test_failed_pull_request_deletes_the_branch(payload):
    bitbucket = FakeBitbucket(
        fail_on={
            "create_pull_request": ExternalServiceError(
                service_name="bitbucket", detail="no reviewers", status_code=400
            )
        }
    )

    with pytest.raises(ExternalServiceError):
        await _create(bitbucket, payload)

    assert bitbucket.calls[-1] == "delete_branch"


async def test_branch_cleanup_failure_does_not_mask_the_original_error(payload):
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
        await _create(bitbucket, payload)

    assert exc_info.value.detail == "commit rejected"


# --------------------------------------------------------------------------- #
# gate 1 — the validation build
# --------------------------------------------------------------------------- #
async def test_failed_validation_declines_the_pull_request(payload, bitbucket):
    bitbucket.builds = [failing()]

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    error = exc_info.value
    assert error.status_code == 502
    assert "Validation did not pass" in error.message
    assert "ci/woodpecker/pr/build [FAILED]" in error.message
    assert [b.state for b in error.validation_builds] == ["FAILED"]
    assert error.deploy_builds is None
    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls
    assert "merge_pull_request" not in bitbucket.calls


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED", "UNKNOWN"])
async def test_only_successful_passes_the_gate(payload, bitbucket, state):
    """CANCELLED and UNKNOWN are terminal but not success — a gate keyed off "not FAILED"
    would merge both."""
    bitbucket.builds = [[BuildStatus(key="ci/woodpecker/pr/build", state=state)]]

    with pytest.raises(VaultOperationError):
        await _create(bitbucket, payload)

    assert "merge_pull_request" not in bitbucket.calls


async def test_one_red_build_among_several_fails_the_gate(payload, bitbucket):
    """Bitbucket keeps one result per key, so a commit can carry one per workflow."""
    bitbucket.builds = [
        [
            BuildStatus(key="ci/woodpecker/pr/lint", state="SUCCESSFUL"),
            BuildStatus(key="ci/woodpecker/pr/test", state="FAILED"),
        ]
    ]

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    # Only the failing one is named, but the whole set is reported.
    assert "ci/woodpecker/pr/test" in exc_info.value.message
    assert "ci/woodpecker/pr/lint" not in exc_info.value.message
    assert len(exc_info.value.validation_builds) == 2


async def test_validation_timeout_declines_and_reports_504(payload, bitbucket):
    bitbucket.builds = [
        BuildTimeoutError(
            "Builds on sha-x were still running after 900s",
            builds=[BuildStatus(key="ci/woodpecker/pr/build", state="INPROGRESS")],
        )
    ]

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    error = exc_info.value
    assert error.status_code == 504
    assert "Validation build did not complete" in error.message
    assert [b.state for b in error.validation_builds] == ["INPROGRESS"]
    assert "decline_pull_request" in bitbucket.calls


async def test_decline_failure_does_not_stop_the_branch_cleanup(payload):
    """A failing decline is logged; the branch is still removed and the real error surfaces."""
    bitbucket = FakeBitbucket(
        fail_on={"decline_pull_request": RuntimeError("pull request already closed")},
        builds=[failing()],
    )

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    assert "Validation did not pass" in exc_info.value.message
    assert "delete_branch" in bitbucket.calls


async def test_a_build_that_never_appears_still_cleans_up(payload, bitbucket):
    """Nothing was observed, so there is nothing to report — but the rollback still runs."""
    bitbucket.builds = [BuildTimeoutError("No build was reported against sha-x within 120s")]

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    assert exc_info.value.validation_builds == []
    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls


# --------------------------------------------------------------------------- #
# the merge
# --------------------------------------------------------------------------- #
async def test_unmergeable_pull_request_is_left_open(payload):
    """A validated-but-unmergeable PR is a human's problem — do not decline it."""
    bitbucket = FakeBitbucket(
        fail_on={
            "merge_pull_request": ExternalServiceError(
                service_name="bitbucket", detail="The pull request has conflicts", status_code=409
            )
        }
    )

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    error = exc_info.value
    assert "could not be merged" in error.message
    assert "conflicts" in error.message
    assert error.pull_request.state == "OPEN"
    assert "decline_pull_request" not in bitbucket.calls
    assert "delete_branch" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# gate 2 — the deploy build (past the point of no return)
# --------------------------------------------------------------------------- #
async def test_failed_deploy_reports_the_change_as_merged(payload, bitbucket):
    bitbucket.builds = [passing(), failing("ci/woodpecker/push/deploy")]

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    error = exc_info.value
    assert error.status_code == 502
    assert "Deploy did not pass" in error.message
    assert "already merged" in error.message
    assert [b.key for b in error.deploy_builds] == ["ci/woodpecker/push/deploy"]
    assert error.pull_request.state == "MERGED"
    # Nothing is rolled back after a merge.
    assert "decline_pull_request" not in bitbucket.calls
    assert "delete_branch" not in bitbucket.calls


async def test_deploy_timeout_reports_504_and_the_merge(payload, bitbucket):
    bitbucket.builds = [
        passing(),
        BuildTimeoutError("Builds on merge-sha-1 were still running after 900s"),
    ]

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    assert exc_info.value.status_code == 504
    assert "already merged" in exc_info.value.message


async def test_no_commit_to_watch_after_the_merge_is_reported_as_merged(payload, bitbucket):
    """Past the point of no return, so this reports rather than rolls back."""
    bitbucket.merge_commit = None
    bitbucket.branch_head = None

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    assert "reported no commit" in exc_info.value.message
    assert exc_info.value.pull_request.state == "MERGED"
    assert "decline_pull_request" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# error -> response mapping
# --------------------------------------------------------------------------- #
async def test_operation_error_renders_a_failed_response(payload, bitbucket):
    bitbucket.builds = [failing()]

    with pytest.raises(VaultOperationError) as exc_info:
        await _create(bitbucket, payload)

    response = exc_info.value.to_response()
    assert response.status == OperationStatus.FAILED
    assert response.kv_name == KV
    assert response.error == response.message
    assert response.validation_builds[0].key == "ci/woodpecker/pr/build"
    assert response.deploy_builds is None


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

    store = await get_kv_store_operation(bitbucket, "two")

    assert store["name"] == "two"
    assert store["roles"] == ROLES


async def test_get_store_of_an_absent_name_is_404():
    bitbucket = FakeBitbucket(existing_files={VALUES_PATH: _file_with("one")})

    with pytest.raises(VaultOperationError) as exc_info:
        await get_kv_store_operation(bitbucket, "nope")

    assert exc_info.value.status_code == 404
    assert "not defined in" in exc_info.value.message


async def test_get_store_of_a_missing_file_is_404():
    with pytest.raises(VaultOperationError) as exc_info:
        await get_kv_store_operation(FakeBitbucket(), KV)

    assert exc_info.value.status_code == 404
