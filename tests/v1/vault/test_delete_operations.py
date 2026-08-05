"""The delete operation: remove one store from a file, leaving its siblings alone.

A delete is a *content edit* — the entry goes, the file stays — so it takes the same chain
and the same rollbacks as a create. Most of these assert on what is still there afterwards.
"""
import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.helpers import (
    build_k8s_service_account,
    build_kv_store,
    build_kv_stores_document,
    render_values_yaml,
)
from app.v1.vault.operations import (
    VaultOperationError,
    delete_kv_store_operation,
    delete_kv_store_pull_request_operation,
)
from tests.fakes import failing, passing

KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
ROLES = {"read": ["app01.corp.example.com"]}
ACCOUNT = build_k8s_service_account("vault", "payments", "dev")


def _seed(bitbucket, *names, path=VALUES_PATH):
    bitbucket.existing_files[path] = render_values_yaml(
        build_kv_stores_document(
            [build_kv_store(n, "payments secrets", ROLES) for n in names]
        )
    )


def _committed(bitbucket):
    return yaml.safe_load(bitbucket.committed[VALUES_PATH])


async def _delete(bitbucket, kv_name=KV):
    return await delete_kv_store_operation(
        bitbucket, kv_name, branch_suffix="abc123"
    )


async def _delete_pr_only(bitbucket, kv_name=KV):
    return await delete_kv_store_pull_request_operation(
        bitbucket, kv_name, branch_suffix="abc123"
    )


# --------------------------------------------------------------------------- #
# the removal itself
# --------------------------------------------------------------------------- #
async def test_delete_returns_the_success_message(bitbucket):
    _seed(bitbucket, KV)

    result = await _delete(bitbucket)

    assert result.status.value == "Succeeded"
    assert result.message == f"Successful deletion of {KV}"
    assert result.kv_name == KV
    assert result.file == FILE


async def test_delete_reports_the_merge_and_both_pipelines(bitbucket):
    _seed(bitbucket, KV)

    result = await _delete(bitbucket)

    assert result.pull_request.state == "MERGED"
    assert [b.state for b in result.validation_builds] == ["SUCCESSFUL"]
    assert [b.state for b in result.deploy_builds] == ["SUCCESSFUL"]


async def test_delete_removes_only_the_named_store(bitbucket):
    """The whole point of many stores per file."""
    _seed(bitbucket, "sibling", KV, "another")

    await _delete(bitbucket)

    assert [s["name"] for s in _committed(bitbucket)["kvStores"]] == [
        "sibling",
        "another",
    ]


async def test_deleting_the_last_store_leaves_an_empty_file(bitbucket):
    """The file stays behind so `GET /{file}` keeps answering and a create can append."""
    _seed(bitbucket, KV)

    await _delete(bitbucket)

    assert bitbucket.committed[VALUES_PATH] == "kvStores: []\n"
    assert _committed(bitbucket) == {"kvStores": []}


async def test_delete_sends_the_optimistic_lock_token(bitbucket):
    """The file always exists here, so the write is an edit and Bitbucket demands it."""
    _seed(bitbucket, KV)

    await _delete(bitbucket)

    assert bitbucket.source_commit_ids == ["file-commit-sha"]
    assert bitbucket.calls.index("get_last_commit") < bitbucket.calls.index("put_file")


async def test_delete_call_order(bitbucket):
    _seed(bitbucket, KV)

    await _delete(bitbucket)

    assert bitbucket.calls == [
        "get_file_content",  # kv/myapp.yaml — the conventional path, not there
        "list_files",        # so walk the dir to find which file holds the store
        "get_file_content",
        "get_last_commit",
        "create_branch",
        "put_file",
        "create_pull_request",
        "await_builds",  # gate 1, on the pull request's commit
        "get_pull_request",  # re-read for the current version
        "merge_pull_request",
        "await_builds",  # gate 2, on the merge commit
    ]


async def test_delete_names_both_coordinates_in_the_commit(bitbucket):
    _seed(bitbucket, KV)

    result = await _delete(bitbucket)

    assert bitbucket.commit_messages == [f"Delete KV store {KV} from {FILE}"]
    stored = bitbucket.pull_requests[result.pull_request.id]
    assert stored.from_branch == "vault-kv/payments-myapp-abc123"


async def test_delete_leaves_another_file_alone(bitbucket):
    """Only the addressed file is read and written."""
    _seed(bitbucket, KV)
    _seed(bitbucket, "elsewhere", path="kv/other-team.yaml")

    await _delete(bitbucket)

    assert list(bitbucket.committed) == [VALUES_PATH]


# --------------------------------------------------------------------------- #
# failures
# --------------------------------------------------------------------------- #
async def test_delete_of_a_store_in_no_file_is_404(bitbucket):
    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, kv_name="nope")

    assert error.value.status_code == 404
    assert "is not defined in any file" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_delete_of_a_store_not_in_the_file_is_404(bitbucket):
    """The file exists and parses; the named store is simply not in it."""
    _seed(bitbucket, "someone-else")

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket)

    assert error.value.status_code == 404
    assert "not defined in" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_a_repeat_delete_is_404(bitbucket):
    """The state is idempotent; the status code says which call did the work."""
    _seed(bitbucket, KV)
    await _delete(bitbucket)
    # The merge lands the emptied file on the base branch.
    bitbucket.existing_files[VALUES_PATH] = bitbucket.committed[VALUES_PATH]

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket)

    assert error.value.status_code == 404


# --------------------------------------------------------------------------- #
# bindings go with the store
#
# A store's Kubernetes service accounts are nested inside it, so a delete removes them in
# the same diff. There is nothing to orphan and nothing to refuse — which is why a delete
# runs no values-dir scan at all, and why an earlier revision's 409 for a "still
# referenced" store has no counterpart here.
# --------------------------------------------------------------------------- #
def _seed_bound(bitbucket, *names, path=VALUES_PATH, accounts=None):
    """Seed stores whose entries already carry a `k8sServiceAccounts` list."""
    document = build_kv_stores_document(
        [build_kv_store(n, "payments secrets", ROLES) for n in names]
    )
    for store in document["kvStores"]:
        store["roles"]["k8sServiceAccounts"] = list(
            accounts if accounts is not None else [ACCOUNT]
        )
    bitbucket.existing_files[path] = render_values_yaml(document)


async def test_deleting_a_bound_store_succeeds(bitbucket):
    """No referential check stands in the way — the binding is part of what is removed."""
    _seed_bound(bitbucket, KV)

    result = await _delete(bitbucket)

    assert result.status.value == "Succeeded"


async def test_deleting_a_bound_store_removes_its_bindings(bitbucket):
    _seed_bound(bitbucket, KV)

    await _delete(bitbucket)

    assert _committed(bitbucket) == {"kvStores": []}
    assert "serviceAccount" not in bitbucket.committed[VALUES_PATH]


async def test_deleting_one_store_leaves_a_siblings_bindings_alone(bitbucket):
    _seed_bound(bitbucket, KV, "billing")

    await _delete(bitbucket)

    remaining = _committed(bitbucket)["kvStores"]
    assert [s["name"] for s in remaining] == ["billing"]
    assert remaining[0]["roles"]["k8sServiceAccounts"] == [ACCOUNT]


async def test_delete_of_a_grouped_store_walks_to_find_its_file(bitbucket):
    """The caller names no file, so a store grouped under another name costs the walk.

    The walk is *resolution*, not a referential scan — there is still nothing pointing at
    this store to check, and only its own file is committed.
    """
    _seed_bound(bitbucket, KV)
    _seed_bound(bitbucket, "elsewhere", path="kv/platform.yaml")

    await _delete(bitbucket)

    assert "list_files" in bitbucket.calls
    assert list(bitbucket.committed) == [VALUES_PATH]


async def test_a_store_in_its_own_file_costs_one_read_and_no_walk(bitbucket):
    """The normal layout — a create with no `file` names the file after the store."""
    _seed(bitbucket, "solo", path="kv/solo.yaml")
    _seed(bitbucket, "elsewhere", path="kv/platform.yaml")

    await _delete(bitbucket, kv_name="solo")

    assert "list_files" not in bitbucket.calls
    assert bitbucket.calls[0] == "get_file_content"
    assert list(bitbucket.committed) == ["kv/solo.yaml"]


async def test_pull_request_only_delete_resolves_the_same_way(bitbucket):
    """Both delete paths share `_prepare_delete`, so they cannot resolve differently."""
    _seed_bound(bitbucket, KV)
    _seed_bound(bitbucket, "elsewhere", path="kv/platform.yaml")

    result = await _delete_pr_only(bitbucket)

    assert result.status.value == "Succeeded"
    assert result.file == FILE
    assert list(bitbucket.committed) == [VALUES_PATH]


async def test_a_binding_on_another_store_never_blocks(bitbucket):
    """The same service account bound to two stores is normal; deleting one is unaffected."""
    _seed_bound(bitbucket, KV, "billing")

    assert (await _delete(bitbucket)).status.value == "Succeeded"


async def test_delete_of_a_corrupt_grouped_file_names_it(bitbucket):
    """The walk skips a file it cannot parse, so the miss must say so.

    Without this the answer is a bare "not defined in any file", which sends an operator
    looking for a deleted store rather than a broken file.
    """
    bitbucket.existing_files[VALUES_PATH] = "kvStores: [unclosed\n  ::: bad"

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket)

    assert error.value.status_code == 404
    assert "unparseable" in error.value.message
    assert VALUES_PATH in error.value.message


async def test_delete_of_a_corrupt_conventional_file_is_reported(bitbucket):
    """Its own file is read directly, so a YAML error there is reported as one."""
    bitbucket.existing_files["kv/solo.yaml"] = "kvStores: [unclosed\n  ::: bad"

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, kv_name="solo")

    assert "not valid YAML" in error.value.message


async def test_delete_propagates_a_non_404_read_failure(bitbucket):
    """Only a 404 means "no such file"; anything else is a transport/upstream problem."""
    bitbucket.fail_on["get_file_content"] = ExternalServiceError(
        service_name="bitbucket", detail="boom", status_code=503
    )

    with pytest.raises(ExternalServiceError):
        await _delete(bitbucket)

    assert "create_branch" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# rollbacks — inherited from the shared chain, so they must still hold
# --------------------------------------------------------------------------- #
async def test_failed_validation_declines_and_cleans_up(bitbucket):
    _seed(bitbucket, KV)
    bitbucket.builds = [failing()]

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket)

    assert error.value.status_code == 502
    assert "Validation did not pass" in error.value.message
    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls
    assert "merge_pull_request" not in bitbucket.calls


async def test_failed_commit_deletes_the_branch(bitbucket):
    _seed(bitbucket, KV)
    bitbucket.fail_on["put_file"] = ExternalServiceError(
        service_name="bitbucket", detail="commit rejected", status_code=400
    )

    with pytest.raises(ExternalServiceError):
        await _delete(bitbucket)

    assert bitbucket.calls[-1] == "delete_branch"
    assert "create_pull_request" not in bitbucket.calls


async def test_failed_deploy_says_the_removal_is_already_merged(bitbucket):
    """Past the point of no return: reported, never rolled back."""

    _seed(bitbucket, KV)
    bitbucket.builds = [passing(), failing("ci/woodpecker/push/deploy")]

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket)

    assert "already merged" in error.value.message
    assert "needs a revert" in error.value.message
    assert "decline_pull_request" not in bitbucket.calls
    assert "delete_branch" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# the PR-only variant — everything it does *not* do is the contract
# --------------------------------------------------------------------------- #
async def test_pull_request_only_returns_the_open_pull_request(bitbucket):
    _seed(bitbucket, KV)

    result = await _delete_pr_only(bitbucket)

    assert result.status.value == "Succeeded"
    assert result.pull_request.state == "OPEN"
    assert f"Opened pull request 101 to delete {KV}" in result.message
    assert "not merged" in result.message


async def test_pull_request_only_stops_at_the_pull_request(bitbucket):
    _seed(bitbucket, KV)

    await _delete_pr_only(bitbucket)

    assert bitbucket.calls == [
        "get_file_content",  # kv/myapp.yaml — the conventional path, not there
        "list_files",        # so walk the dir to find which file holds the store
        "get_file_content",
        "get_last_commit",
        "create_branch",
        "put_file",
        "create_pull_request",
    ]
    assert "merge_pull_request" not in bitbucket.calls


async def test_pull_request_only_reports_no_pipelines(bitbucket):
    """Nothing was observed, so neither pipeline field may be populated."""
    _seed(bitbucket, KV)

    result = await _delete_pr_only(bitbucket)

    assert result.validation_builds is None
    assert result.deploy_builds is None


async def test_pull_request_only_commits_the_same_document(bitbucket):
    """The branch content must not depend on which endpoint asked for it."""
    _seed(bitbucket, "sibling", KV)
    await _delete_pr_only(bitbucket)
    pr_only = bitbucket.committed[VALUES_PATH]

    bitbucket.committed.clear()
    await _delete(bitbucket)

    assert bitbucket.committed[VALUES_PATH] == pr_only


async def test_pull_request_only_shares_the_404s(bitbucket):
    _seed(bitbucket, "someone-else")

    with pytest.raises(VaultOperationError) as error:
        await _delete_pr_only(bitbucket)

    assert error.value.status_code == 404
    assert "create_branch" not in bitbucket.calls


async def test_pull_request_only_failure_deletes_the_branch(bitbucket):
    _seed(bitbucket, KV)
    bitbucket.fail_on["create_pull_request"] = ExternalServiceError(
        service_name="bitbucket", detail="no reviewers", status_code=400
    )

    with pytest.raises(ExternalServiceError):
        await _delete_pr_only(bitbucket)

    assert bitbucket.calls[-1] == "delete_branch"


async def test_a_second_pull_request_only_call_opens_a_second_pull_request(bitbucket):
    """Nothing was merged, so the store is still on the base branch and still deletable."""
    _seed(bitbucket, KV)

    first = await _delete_pr_only(bitbucket)
    second = await delete_kv_store_pull_request_operation(
        bitbucket, KV, branch_suffix="def456"
    )

    assert first.pull_request.id != second.pull_request.id
    assert {pr.state for pr in bitbucket.pull_requests.values()} == {"OPEN"}
