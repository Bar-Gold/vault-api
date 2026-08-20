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
    _open_pull_request,
    delete_kv_store_operation,
    delete_kv_store_pull_request_operation,
)
from tests.fakes import failing, passing

KV = "myapp"
# A store names its own file now, so the conventional layout is the default here and the
# grouped one — a file holding several stores, which only an older revision could produce —
# is set up explicitly by the tests that still exercise it.
FILE = KV
VALUES_PATH = f"kv/{KV}.yaml"
GROUPED_FILE = "payments"
GROUPED_PATH = f"kv/{GROUPED_FILE}.yaml"
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
    """A grouped file is edited, never deleted — its other stores are not ours to remove.

    Only an older revision of this service could have produced a file like this, but one
    that exists must not lose two stores because somebody deleted one.
    """
    _seed(bitbucket, "sibling", KV, "another", path=GROUPED_PATH)

    await _delete(bitbucket)

    assert bitbucket.deleted == []
    assert [s["name"] for s in yaml.safe_load(bitbucket.committed[GROUPED_PATH])["kvStores"]] == [
        "sibling",
        "another",
    ]


async def test_deleting_the_only_store_deletes_the_file(bitbucket):
    """A store names its own file, so removing it removes the file.

    Emptying it to an empty list instead would leave a dead file behind for every store
    ever deleted, and no create would ever produce that shape.
    """
    _seed(bitbucket, KV)

    await _delete(bitbucket)

    assert bitbucket.deleted == [VALUES_PATH]
    assert VALUES_PATH not in bitbucket.committed


async def test_a_file_removal_needs_no_optimistic_lock_token(bitbucket):
    """Git resolves the path from the branch it fetched, so there is no token to send.

    The token is Bitbucket's, and only the `browse` PUT wants one. A delete that goes
    through git never calls `get_last_commit` at all.
    """
    _seed(bitbucket, KV)

    await _delete(bitbucket)

    assert bitbucket.source_commit_ids == []
    assert "get_last_commit" not in bitbucket.calls


async def test_editing_a_grouped_file_still_sends_the_token(bitbucket):
    """The other branch: the file survives, so the write is an ordinary edit."""
    _seed(bitbucket, "sibling", KV, path=GROUPED_PATH)

    await _delete(bitbucket)

    assert bitbucket.source_commit_ids == ["file-commit-sha"]
    assert bitbucket.calls.index("get_last_commit") < bitbucket.calls.index("put_file")


async def test_delete_call_order(bitbucket):
    _seed(bitbucket, KV)

    await _delete(bitbucket)

    assert bitbucket.calls == [
        "get_file_content",  # kv/myapp.yaml — the conventional path, and the store is there
        "create_branch",
        "delete_file",  # git, not REST: Bitbucket cannot delete a file over REST
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
    assert stored.from_branch == f"vault-kv/{KV}-abc123"


async def test_delete_leaves_another_file_alone(bitbucket):
    """Only the addressed file is touched."""
    _seed(bitbucket, KV)
    _seed(bitbucket, "elsewhere", path="kv/other-team.yaml")

    await _delete(bitbucket)

    assert bitbucket.deleted == [VALUES_PATH]
    assert bitbucket.committed == {}


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
    # Simulate the merge landing the removal on the base branch.
    del bitbucket.existing_files[VALUES_PATH]

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
    """Nothing to unpick: the bindings are inside the store, and the file is gone."""
    _seed_bound(bitbucket, KV)

    await _delete(bitbucket)

    assert bitbucket.deleted == [VALUES_PATH]
    assert bitbucket.committed == {}


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
    _seed_bound(bitbucket, "sibling", KV, path=GROUPED_PATH)
    _seed_bound(bitbucket, "elsewhere", path="kv/platform.yaml")

    await _delete(bitbucket)

    assert "list_files" in bitbucket.calls
    # The file survives: it still holds `sibling`, so this is an edit, not a removal.
    assert list(bitbucket.committed) == [GROUPED_PATH]
    assert bitbucket.deleted == []


async def test_a_store_in_its_own_file_costs_one_read_and_no_walk(bitbucket):
    """The normal layout — a create with no `file` names the file after the store."""
    _seed(bitbucket, "solo", path="kv/solo.yaml")
    _seed(bitbucket, "elsewhere", path="kv/platform.yaml")

    await _delete(bitbucket, kv_name="solo")

    assert "list_files" not in bitbucket.calls
    assert bitbucket.calls[0] == "get_file_content"
    assert bitbucket.deleted == ["kv/solo.yaml"]


async def test_pull_request_only_delete_resolves_the_same_way(bitbucket):
    """Both delete paths share `_prepare_delete`, so they cannot resolve differently."""
    _seed_bound(bitbucket, "sibling", KV, path=GROUPED_PATH)
    _seed_bound(bitbucket, "elsewhere", path="kv/platform.yaml")

    result = await _delete_pr_only(bitbucket)

    assert result.status.value == "Succeeded"
    assert result.file == GROUPED_FILE
    assert list(bitbucket.committed) == [GROUPED_PATH]


async def test_a_binding_on_another_store_never_blocks(bitbucket):
    """The same service account bound to two stores is normal; deleting one is unaffected."""
    _seed_bound(bitbucket, KV, "billing")

    assert (await _delete(bitbucket)).status.value == "Succeeded"


async def test_delete_of_a_corrupt_grouped_file_names_it(bitbucket):
    """The walk skips a file it cannot parse, so the miss must say so.

    Without this the answer is a bare "not defined in any file", which sends an operator
    looking for a deleted store rather than a broken file.
    """
    # Corrupt a *different* file, so the store's own path 404s and the walk is what hits
    # the bad one. A corrupt file at the conventional path reports its own YAML error.
    bitbucket.existing_files[GROUPED_PATH] = "kvStores: [unclosed\n  ::: bad"

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket)

    assert error.value.status_code == 404
    assert "unparseable" in error.value.message
    assert GROUPED_PATH in error.value.message


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
    # A whole-file delete commits through git, so that is the call that fails here.
    bitbucket.fail_on["delete_file"] = ExternalServiceError(
        service_name="bitbucket", detail="git push rejected", status_code=400
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
        "get_file_content",  # kv/myapp.yaml — the conventional path, and the store is there
        "create_branch",
        "delete_file",  # git, not REST: Bitbucket cannot delete a file over REST
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


# --------------------------------------------------------------------------- #
# the remove/content guard
#
# `remove=True` is a separate flag rather than `content=None` meaning "delete", because a
# None arriving by accident would otherwise remove a file instead of raising. These pin
# that both halves of the guard actually fire — a guard nobody tests is a comment.
# --------------------------------------------------------------------------- #
async def test_a_removal_with_content_is_a_programming_error(bitbucket):
    with pytest.raises(ValueError, match="commits no content"):
        await _open_pull_request(
            bitbucket,
            path=VALUES_PATH,
            content="kvStores: []\n",
            remove=True,
            branch="b",
            summary="s",
            description="d",
        )

    assert bitbucket.calls == []


async def test_a_write_without_content_is_a_programming_error(bitbucket):
    """The dangerous direction: this must raise, never fall through to a delete."""
    with pytest.raises(ValueError, match="content is required"):
        await _open_pull_request(
            bitbucket,
            path=VALUES_PATH,
            branch="b",
            summary="s",
            description="d",
        )

    assert bitbucket.calls == []
    assert bitbucket.deleted == []
