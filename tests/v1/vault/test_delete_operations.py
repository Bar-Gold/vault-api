"""The delete operation: remove one store from a file, leaving its siblings alone.

A delete is a *content edit* — the entry goes, the file stays — so it takes the same chain
and the same rollbacks as a create. Most of these assert on what is still there afterwards.
"""
import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.helpers import (
    add_kubernetes_auth_role,
    build_kubernetes_auth_role,
    build_kv_store,
    build_kv_stores_document,
    render_values_yaml,
)
from app.v1.vault.operations import (
    VaultOperationError,
    delete_kv_store_operation,
    delete_kv_store_pull_request_operation,
)
from tests.fakes import make_pipeline

KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
ROLES = {"read": ["app01.corp.example.com"]}


def _seed(bitbucket, *names, path=VALUES_PATH):
    bitbucket.existing_files[path] = render_values_yaml(
        build_kv_stores_document(
            [build_kv_store(n, "payments secrets", ROLES) for n in names]
        )
    )


def _committed(bitbucket):
    return yaml.safe_load(bitbucket.committed[VALUES_PATH])


async def _delete(bitbucket, woodpecker, file=FILE, kv_name=KV):
    return await delete_kv_store_operation(
        bitbucket, woodpecker, file, kv_name, branch_suffix="abc123"
    )


async def _delete_pr_only(bitbucket, file=FILE, kv_name=KV):
    return await delete_kv_store_pull_request_operation(
        bitbucket, file, kv_name, branch_suffix="abc123"
    )


# --------------------------------------------------------------------------- #
# the removal itself
# --------------------------------------------------------------------------- #
async def test_delete_returns_the_success_message(bitbucket, woodpecker):
    _seed(bitbucket, KV)

    result = await _delete(bitbucket, woodpecker)

    assert result.status.value == "Succeeded"
    assert result.message == f"Successful deletion of {KV}"
    assert result.kv_name == KV
    assert result.file == FILE


async def test_delete_reports_the_merge_and_both_pipelines(bitbucket, woodpecker):
    _seed(bitbucket, KV)

    result = await _delete(bitbucket, woodpecker)

    assert result.pull_request.state == "MERGED"
    assert result.validation_pipeline.number == 2
    assert result.deploy_pipeline.number == 3


async def test_delete_removes_only_the_named_store(bitbucket, woodpecker):
    """The whole point of many stores per file."""
    _seed(bitbucket, "sibling", KV, "another")

    await _delete(bitbucket, woodpecker)

    assert [s["name"] for s in _committed(bitbucket)["kvStores"]] == [
        "sibling",
        "another",
    ]


async def test_deleting_the_last_store_leaves_an_empty_file(bitbucket, woodpecker):
    """The file stays behind so `GET /{file}` keeps answering and a create can append."""
    _seed(bitbucket, KV)

    await _delete(bitbucket, woodpecker)

    assert bitbucket.committed[VALUES_PATH] == "kvStores: []\n"
    assert _committed(bitbucket) == {"kvStores": []}


async def test_delete_sends_the_optimistic_lock_token(bitbucket, woodpecker):
    """The file always exists here, so the write is an edit and Bitbucket demands it."""
    _seed(bitbucket, KV)

    await _delete(bitbucket, woodpecker)

    assert bitbucket.source_commit_ids == ["file-commit-sha"]
    assert bitbucket.calls.index("get_last_commit") < bitbucket.calls.index("put_file")


async def test_delete_call_order(bitbucket, woodpecker):
    _seed(bitbucket, KV)

    await _delete(bitbucket, woodpecker)

    assert bitbucket.calls == [
        "get_file_content",  # read the target file
        "list_files",  # walk the values dir for kubernetesAuth referrers
        "get_file_content",
        "get_last_commit",
        "create_branch",
        "put_file",
        "create_pull_request",
        "get_pull_request",  # re-read for the current version
        "merge_pull_request",
    ]
    assert woodpecker.calls == [
        "list_pipelines",
        "await_pipeline",
        "list_pipelines",
        "await_pipeline",
    ]


async def test_delete_names_both_coordinates_in_the_commit(bitbucket, woodpecker):
    _seed(bitbucket, KV)

    result = await _delete(bitbucket, woodpecker)

    assert bitbucket.commit_messages == [f"Delete KV store {KV} from {FILE}"]
    stored = bitbucket.pull_requests[result.pull_request.id]
    assert stored.from_branch == "vault-kv/payments-myapp-abc123"


async def test_delete_leaves_another_file_alone(bitbucket, woodpecker):
    """Only the addressed file is read and written."""
    _seed(bitbucket, KV)
    _seed(bitbucket, "elsewhere", path="kv/other-team.yaml")

    await _delete(bitbucket, woodpecker)

    assert list(bitbucket.committed) == [VALUES_PATH]


# --------------------------------------------------------------------------- #
# failures
# --------------------------------------------------------------------------- #
async def test_delete_of_a_missing_file_is_404(bitbucket, woodpecker):
    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker, file="nope")

    assert error.value.status_code == 404
    assert "does not exist" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_delete_of_a_store_not_in_the_file_is_404(bitbucket, woodpecker):
    """The file exists and parses; the named store is simply not in it."""
    _seed(bitbucket, "someone-else")

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker)

    assert error.value.status_code == 404
    assert "not defined in" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_a_repeat_delete_is_404(bitbucket, woodpecker):
    """The state is idempotent; the status code says which call did the work."""
    _seed(bitbucket, KV)
    await _delete(bitbucket, woodpecker)
    # The merge lands the emptied file on the base branch.
    bitbucket.existing_files[VALUES_PATH] = bitbucket.committed[VALUES_PATH]

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker)

    assert error.value.status_code == 404


# --------------------------------------------------------------------------- #
# referential integrity
#
# A Kubernetes auth role names the stores it reaches. Deleting one out from under a role
# would orphan the binding silently, and cascading into those roles would be a
# multi-resource mutation hidden behind a single-resource URI — so this refuses instead,
# and names the blockers.
# --------------------------------------------------------------------------- #
def _seed_role(bitbucket, path, store, role_name="myapp-ci", capability="read"):
    document = build_kv_stores_document([])
    document = add_kubernetes_auth_role(
        document,
        build_kubernetes_auth_role(
            role_name,
            "CI deployer",
            ["vault-reader"],
            ["payments"],
            {capability: [store]},
        ),
    )
    bitbucket.existing_files[path] = render_values_yaml(document)


async def test_deleting_a_referenced_store_is_409(bitbucket, woodpecker):
    _seed(bitbucket, KV)
    _seed_role(bitbucket, "kv/platform.yaml", KV)

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker)

    assert error.value.status_code == 409
    assert "create_branch" not in bitbucket.calls


async def test_the_409_names_the_referrers_and_their_file(bitbucket, woodpecker):
    """The message is the actionable part: which roles, and where to find them."""
    _seed(bitbucket, KV)
    _seed_role(bitbucket, "kv/platform.yaml", KV)

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker)

    assert error.value.message == (
        f"{KV} is referenced by kubernetesAuth role(s) myapp-ci (kv/platform.yaml); "
        f"delete those first"
    )


async def test_a_referrer_in_the_same_file_also_blocks(bitbucket, woodpecker):
    _seed(bitbucket, KV)
    bitbucket.existing_files[VALUES_PATH] = render_values_yaml(
        add_kubernetes_auth_role(
            yaml.safe_load(bitbucket.existing_files[VALUES_PATH]),
            build_kubernetes_auth_role(
                "myapp-ci", "d", ["vault-reader"], ["payments"], {"read": [KV]}
            ),
        )
    )

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker)

    assert error.value.status_code == 409
    assert VALUES_PATH in error.value.message


async def test_a_write_reference_blocks_too(bitbucket, woodpecker):
    _seed(bitbucket, KV)
    _seed_role(bitbucket, "kv/platform.yaml", KV, capability="write")

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker)

    assert error.value.status_code == 409


async def test_an_unreferenced_store_deletes_normally(bitbucket, woodpecker):
    """A role pointing at a *different* store must not block this one."""
    _seed(bitbucket, KV)
    _seed_role(bitbucket, "kv/platform.yaml", "billing")

    result = await _delete(bitbucket, woodpecker)

    assert result.status.value == "Succeeded"


async def test_deleting_the_role_first_unblocks_the_store(bitbucket, woodpecker):
    """The remedy the error message prescribes has to actually work."""
    _seed(bitbucket, KV)
    _seed_role(bitbucket, "kv/platform.yaml", KV)
    del bitbucket.existing_files["kv/platform.yaml"]

    assert (await _delete(bitbucket, woodpecker)).status.value == "Succeeded"


async def test_pull_request_only_delete_shares_the_409(bitbucket):
    """Both delete paths go through the same preparation, so neither can skip the check."""
    _seed(bitbucket, KV)
    _seed_role(bitbucket, "kv/platform.yaml", KV)

    with pytest.raises(VaultOperationError) as error:
        await _delete_pr_only(bitbucket)

    assert error.value.status_code == 409
    assert "create_branch" not in bitbucket.calls


async def test_delete_of_a_corrupt_file_is_reported(bitbucket, woodpecker):
    bitbucket.existing_files[VALUES_PATH] = "kvStores: [unclosed\n  ::: bad"

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker)

    assert "not valid YAML" in error.value.message


async def test_delete_propagates_a_non_404_read_failure(bitbucket, woodpecker):
    """Only a 404 means "no such file"; anything else is a transport/upstream problem."""
    bitbucket.fail_on["get_file_content"] = ExternalServiceError(
        service_name="bitbucket", detail="boom", status_code=503
    )

    with pytest.raises(ExternalServiceError):
        await _delete(bitbucket, woodpecker)

    assert "create_branch" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# rollbacks — inherited from the shared chain, so they must still hold
# --------------------------------------------------------------------------- #
async def test_failed_validation_declines_and_cleans_up(bitbucket, woodpecker):
    _seed(bitbucket, KV)
    woodpecker.results = [make_pipeline(number=2, status="failure", event="pull_request")]

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker)

    assert error.value.status_code == 502
    assert "Validation pipeline #2" in error.value.message
    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls
    assert "merge_pull_request" not in bitbucket.calls


async def test_failed_commit_deletes_the_branch(bitbucket, woodpecker):
    _seed(bitbucket, KV)
    bitbucket.fail_on["put_file"] = ExternalServiceError(
        service_name="bitbucket", detail="commit rejected", status_code=400
    )

    with pytest.raises(ExternalServiceError):
        await _delete(bitbucket, woodpecker)

    assert bitbucket.calls[-1] == "delete_branch"
    assert "create_pull_request" not in bitbucket.calls


async def test_failed_deploy_says_the_removal_is_already_merged(bitbucket):
    """Past the point of no return: reported, never rolled back."""
    from tests.fakes import FakeWoodpecker

    _seed(bitbucket, KV)
    woodpecker = FakeWoodpecker(
        results=[
            make_pipeline(number=2, status="success", event="pull_request"),
            make_pipeline(number=3, status="failure", event="push"),
        ]
    )

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker)

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
        "get_file_content",
        "list_files",
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

    assert result.validation_pipeline is None
    assert result.deploy_pipeline is None


async def test_pull_request_only_commits_the_same_document(bitbucket, woodpecker):
    """The branch content must not depend on which endpoint asked for it."""
    _seed(bitbucket, "sibling", KV)
    await _delete_pr_only(bitbucket)
    pr_only = bitbucket.committed[VALUES_PATH]

    bitbucket.committed.clear()
    await _delete(bitbucket, woodpecker)

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
        bitbucket, FILE, KV, branch_suffix="def456"
    )

    assert first.pull_request.id != second.pull_request.id
    assert {pr.state for pr in bitbucket.pull_requests.values()} == {"OPEN"}
