"""The Kubernetes auth chain: sequencing, uniqueness, and what it refuses to write.

A role is a second kind of entry in the *same* values file as the KV stores it reaches, so
most of these assert on how the two coexist — and on the two questions a store create never
has to ask: is this identity taken, and do the stores this role names actually exist.

The chain itself is `_commit_via_pull_request`, reused verbatim, so the rollback tests here
are checking that it is still reused rather than re-testing it.
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
    create_kubernetes_auth_operation,
    create_kubernetes_auth_pull_request_operation,
    delete_kubernetes_auth_operation,
    delete_kubernetes_auth_pull_request_operation,
    get_kubernetes_auth_file_operation,
    get_kubernetes_auth_role_operation,
    update_kubernetes_auth_operation,
)
from app.v1.vault.schemas import VaultKubernetesAuthUpdate
from tests.fakes import FakeWoodpecker, make_pipeline

ROLE = "myapp-ci"
KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
ROLES = {"read": ["app01.corp.example.com"]}


def _role(name=ROLE, cluster="prod-il-1", access=None):
    return build_kubernetes_auth_role(
        name,
        "CI deployer",
        ["vault-reader"],
        ["payments"],
        access or {"read": [KV]},
        cluster=cluster,
    )


def _seed(bitbucket, *stores, roles=(), path=VALUES_PATH):
    """A values file holding the given KV stores and Kubernetes auth roles."""
    document = build_kv_stores_document(
        [build_kv_store(s, "payments secrets", ROLES) for s in stores]
    )
    for role in roles:
        document = add_kubernetes_auth_role(document, role)
    bitbucket.existing_files[path] = render_values_yaml(document)


def _committed(bitbucket, path=VALUES_PATH):
    return yaml.safe_load(bitbucket.committed[path])


async def _create(bitbucket, woodpecker, payload):
    return await create_kubernetes_auth_operation(
        bitbucket, woodpecker, payload, branch_suffix="abc123"
    )


async def _create_pr_only(bitbucket, payload, suffix="abc123"):
    return await create_kubernetes_auth_pull_request_operation(
        bitbucket, payload, branch_suffix=suffix
    )


# --------------------------------------------------------------------------- #
# create — happy path
# --------------------------------------------------------------------------- #
async def test_create_returns_the_success_message(bitbucket, woodpecker, role_payload):
    _seed(bitbucket, KV)

    result = await _create(bitbucket, woodpecker, role_payload)

    assert result.status.value == "Succeeded"
    assert result.message == f"Successful creation of {ROLE}"
    assert result.role_name == ROLE
    assert result.file == FILE


async def test_create_reports_the_role_name_not_the_kv_name(
    bitbucket, woodpecker, role_payload
):
    """A role name sitting in `kv_name` would be a lie every consumer had to learn."""
    _seed(bitbucket, KV)

    result = await _create(bitbucket, woodpecker, role_payload)

    assert result.kv_name == ""


async def test_create_reports_the_merge_and_both_pipelines(
    bitbucket, woodpecker, role_payload
):
    _seed(bitbucket, KV)

    result = await _create(bitbucket, woodpecker, role_payload)

    assert result.pull_request.state == "MERGED"
    assert result.validation_pipeline.number == 2
    assert result.deploy_pipeline.number == 3


async def test_create_call_order(bitbucket, woodpecker, role_payload):
    _seed(bitbucket, KV)

    await _create(bitbucket, woodpecker, role_payload)

    assert bitbucket.calls == [
        "list_files",  # one walk answers uniqueness *and* store existence
        "get_file_content",
        "get_file_content",  # read the target file
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


async def test_the_committed_entry_is_the_proposed_shape(
    bitbucket, woodpecker, role_payload
):
    """The contract with the deploy pipeline. Nothing about policies or mounts."""
    _seed(bitbucket, KV)

    await _create(bitbucket, woodpecker, role_payload)

    assert _committed(bitbucket)["kubernetesAuth"] == [
        {
            "name": ROLE,
            "description": "CI deployer for the payments app",
            "cluster": "prod-il-1",
            "serviceAccounts": ["vault-reader"],
            "namespaces": ["payments"],
            "access": {"read": [KV]},
        }
    ]


async def test_creating_a_role_leaves_the_kv_stores_alone(
    bitbucket, woodpecker, role_payload
):
    """Both kinds share one file; erasing the other half would look like a real diff."""
    _seed(bitbucket, KV, "billing")

    await _create(bitbucket, woodpecker, role_payload)

    assert [s["name"] for s in _committed(bitbucket)["kvStores"]] == [KV, "billing"]


async def test_creating_a_role_appends_to_the_existing_roles(
    bitbucket, woodpecker, role_payload
):
    _seed(bitbucket, KV, roles=[_role(name="already-here")])

    await _create(bitbucket, woodpecker, role_payload)

    assert [r["name"] for r in _committed(bitbucket)["kubernetesAuth"]] == [
        "already-here",
        ROLE,
    ]


async def test_appending_sends_the_optimistic_lock_token(
    bitbucket, woodpecker, role_payload
):
    _seed(bitbucket, KV)

    await _create(bitbucket, woodpecker, role_payload)

    assert bitbucket.source_commit_ids == ["file-commit-sha"]


async def test_the_branch_carries_its_own_prefix(bitbucket, woodpecker, role_payload):
    """A reviewer should see the change *kind* before opening the diff."""
    _seed(bitbucket, KV)

    result = await _create(bitbucket, woodpecker, role_payload)

    stored = bitbucket.pull_requests[result.pull_request.id]
    assert stored.from_branch == "vault-k8s-auth/payments-myapp-ci-abc123"
    assert bitbucket.commit_messages == [
        f"Create Kubernetes auth role {ROLE} in {FILE}"
    ]


async def test_a_role_in_a_new_file_writes_no_empty_store_list(
    bitbucket, woodpecker, role_payload
):
    """The file never asked for a `kvStores: []` sibling, so it must not get one."""
    _seed(bitbucket, KV, path="kv/other-team.yaml")
    role_payload.access = {"read": [KV]}

    await _create(bitbucket, woodpecker, role_payload)

    assert list(_committed(bitbucket)) == ["kubernetesAuth"]
    assert bitbucket.source_commit_ids == [None]


# --------------------------------------------------------------------------- #
# create — uniqueness
#
# `(cluster, name)` across the values dir, `name` alone within one file. A Vault k8s role
# is scoped to its auth mount, so the same name in two clusters is legitimate.
# --------------------------------------------------------------------------- #
async def test_the_same_identity_anywhere_is_409(bitbucket, woodpecker, role_payload):
    _seed(bitbucket, KV)
    _seed(bitbucket, "elsewhere", roles=[_role()], path="kv/platform.yaml")

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, woodpecker, role_payload)

    assert error.value.status_code == 409
    assert "already exists" in error.value.message
    assert "kv/platform.yaml" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_the_same_name_in_another_cluster_is_allowed(
    bitbucket, woodpecker, role_payload
):
    """A `deployer` role in prod and in staging are two different Vault roles."""
    _seed(bitbucket, KV)
    _seed(bitbucket, "elsewhere", roles=[_role(cluster="staging-il-1")], path="kv/other.yaml")

    result = await _create(bitbucket, woodpecker, role_payload)

    assert result.status.value == "Succeeded"


async def test_the_same_name_in_the_target_file_is_409_whatever_the_cluster(
    bitbucket, woodpecker, role_payload
):
    """`{file}/{role_name}` addressing would be ambiguous with two of them in one file."""
    _seed(bitbucket, KV, roles=[_role(cluster="staging-il-1")])

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, woodpecker, role_payload)

    assert error.value.status_code == 409
    assert "unique within a file" in error.value.message
    assert "staging-il-1" in error.value.message


async def test_an_absent_cluster_is_its_own_identity(bitbucket, woodpecker, role_payload):
    """`cluster` is optional, so its absence is a coordinate, not a wildcard."""
    _seed(bitbucket, KV)
    _seed(bitbucket, "elsewhere", roles=[_role(cluster=None)], path="kv/other.yaml")

    result = await _create(bitbucket, woodpecker, role_payload)

    assert result.status.value == "Succeeded"


async def test_two_roles_with_no_cluster_collide(bitbucket, woodpecker, role_payload):
    _seed(bitbucket, KV)
    _seed(bitbucket, "elsewhere", roles=[_role(cluster=None)], path="kv/other.yaml")
    role_payload.cluster = None

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, woodpecker, role_payload)

    assert error.value.status_code == 409


async def test_a_role_may_share_a_name_with_a_kv_store(
    bitbucket, woodpecker, role_payload
):
    """The two are separate namespaces; only `kvStores` feeds the store-name scan."""
    _seed(bitbucket, KV, ROLE)
    role_payload.access = {"read": [ROLE]}

    result = await _create(bitbucket, woodpecker, role_payload)

    assert result.status.value == "Succeeded"


# --------------------------------------------------------------------------- #
# create — the referenced stores must exist
# --------------------------------------------------------------------------- #
async def test_access_naming_an_unknown_store_is_409(
    bitbucket, woodpecker, role_payload
):
    _seed(bitbucket, "something-else")

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, woodpecker, role_payload)

    assert error.value.status_code == 409
    assert "unknown KV store(s) ['myapp']" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_a_store_in_another_file_satisfies_access(
    bitbucket, woodpecker, role_payload
):
    """The walk covers the whole values dir, so a cross-file reference is legitimate."""
    _seed(bitbucket, "unrelated")
    _seed(bitbucket, KV, path="kv/platform.yaml")

    result = await _create(bitbucket, woodpecker, role_payload)

    assert result.status.value == "Succeeded"


async def test_every_missing_store_is_named_at_once(bitbucket, woodpecker, role_payload):
    _seed(bitbucket, KV)
    role_payload.access = {"read": [KV, "nope"], "write": ["also-nope"]}

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, woodpecker, role_payload)

    assert "['also-nope', 'nope']" in error.value.message


async def test_an_unparseable_file_does_not_block_a_create(
    bitbucket, woodpecker, role_payload
):
    """A hand-edited file must not wedge an unrelated create — it is skipped."""
    _seed(bitbucket, KV)
    bitbucket.existing_files["kv/broken.yaml"] = "kvStores: [unclosed\n  ::: bad"

    result = await _create(bitbucket, woodpecker, role_payload)

    assert result.status.value == "Succeeded"


# --------------------------------------------------------------------------- #
# create — rollbacks, inherited from the shared chain
# --------------------------------------------------------------------------- #
async def test_failed_validation_declines_and_cleans_up(
    bitbucket, woodpecker, role_payload
):
    _seed(bitbucket, KV)
    woodpecker.results = [make_pipeline(number=2, status="failure", event="pull_request")]

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, woodpecker, role_payload)

    assert error.value.status_code == 502
    assert error.value.role_name == ROLE
    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls
    assert "merge_pull_request" not in bitbucket.calls


async def test_failed_commit_deletes_the_branch(bitbucket, woodpecker, role_payload):
    _seed(bitbucket, KV)
    bitbucket.fail_on["put_file"] = ExternalServiceError(
        service_name="bitbucket", detail="commit rejected", status_code=400
    )

    with pytest.raises(ExternalServiceError):
        await _create(bitbucket, woodpecker, role_payload)

    assert bitbucket.calls[-1] == "delete_branch"
    assert "create_pull_request" not in bitbucket.calls


async def test_failed_deploy_says_the_change_is_already_merged(bitbucket, role_payload):
    """The merge is the point of no return for this resource kind too."""
    _seed(bitbucket, KV)
    woodpecker = FakeWoodpecker(
        results=[
            make_pipeline(number=2, status="success", event="pull_request"),
            make_pipeline(number=3, status="failure", event="push"),
        ]
    )

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, woodpecker, role_payload)

    assert "already merged" in error.value.message
    assert "needs a revert" in error.value.message
    assert error.value.role_name == ROLE
    assert "decline_pull_request" not in bitbucket.calls
    assert "delete_branch" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# create — the PR-only twin
# --------------------------------------------------------------------------- #
async def test_pull_request_only_returns_the_open_pull_request(bitbucket, role_payload):
    _seed(bitbucket, KV)

    result = await _create_pr_only(bitbucket, role_payload)

    assert result.status.value == "Succeeded"
    assert result.pull_request.state == "OPEN"
    assert result.role_name == ROLE
    assert f"Opened pull request 101 for {ROLE}" in result.message
    assert "not merged" in result.message


async def test_pull_request_only_stops_at_the_pull_request(bitbucket, role_payload):
    _seed(bitbucket, KV)

    await _create_pr_only(bitbucket, role_payload)

    assert bitbucket.calls == [
        "list_files",
        "get_file_content",
        "get_file_content",
        "get_last_commit",
        "create_branch",
        "put_file",
        "create_pull_request",
    ]
    assert "merge_pull_request" not in bitbucket.calls


async def test_pull_request_only_reports_no_pipelines(bitbucket, role_payload):
    """Nothing was observed, so neither pipeline field may be populated."""
    _seed(bitbucket, KV)

    result = await _create_pr_only(bitbucket, role_payload)

    assert result.validation_pipeline is None
    assert result.deploy_pipeline is None


async def test_pull_request_only_commits_the_same_document(
    bitbucket, woodpecker, role_payload
):
    """The branch content must not depend on which endpoint asked for it."""
    _seed(bitbucket, KV)
    await _create_pr_only(bitbucket, role_payload)
    pr_only = bitbucket.committed[VALUES_PATH]

    bitbucket.committed.clear()
    await _create(bitbucket, woodpecker, role_payload)

    assert bitbucket.committed[VALUES_PATH] == pr_only


async def test_pull_request_only_shares_the_409s(bitbucket, role_payload):
    _seed(bitbucket, "something-else")

    with pytest.raises(VaultOperationError) as error:
        await _create_pr_only(bitbucket, role_payload)

    assert error.value.status_code == 409
    assert "create_branch" not in bitbucket.calls


async def test_a_second_pull_request_only_call_opens_a_second_pull_request(
    bitbucket, role_payload
):
    """The scan reads the base branch, where an unmerged role is not. Reviewers close one."""
    _seed(bitbucket, KV)

    first = await _create_pr_only(bitbucket, role_payload)
    second = await _create_pr_only(bitbucket, role_payload, suffix="def456")

    assert first.pull_request.id != second.pull_request.id
    assert {pr.state for pr in bitbucket.pull_requests.values()} == {"OPEN"}


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #
async def _update(bitbucket, woodpecker, **fields):
    return await update_kubernetes_auth_operation(
        bitbucket,
        woodpecker,
        FILE,
        ROLE,
        VaultKubernetesAuthUpdate(**fields),
        branch_suffix="abc123",
    )


async def test_update_replaces_a_list_wholesale(bitbucket, woodpecker):
    _seed(bitbucket, KV, roles=[_role()])

    result = await _update(bitbucket, woodpecker, namespaces=["payments-staging"])

    assert result.message == f"Successful update of {ROLE}"
    assert _committed(bitbucket)["kubernetesAuth"][0]["namespaces"] == [
        "payments-staging"
    ]


async def test_update_leaves_the_other_fields_and_the_stores_alone(bitbucket, woodpecker):
    _seed(bitbucket, KV, roles=[_role()])

    await _update(bitbucket, woodpecker, ttl="1h")

    entry = _committed(bitbucket)["kubernetesAuth"][0]
    assert entry["name"] == ROLE
    assert entry["cluster"] == "prod-il-1"
    assert entry["serviceAccounts"] == ["vault-reader"]
    assert [s["name"] for s in _committed(bitbucket)["kvStores"]] == [KV]


async def test_update_leaves_sibling_roles_alone(bitbucket, woodpecker):
    _seed(bitbucket, KV, roles=[_role(name="other"), _role()])

    await _update(bitbucket, woodpecker, ttl="1h")

    roles = _committed(bitbucket)["kubernetesAuth"]
    assert "ttl" not in roles[0]
    assert roles[1]["ttl"] == "1h"


async def test_a_no_op_update_opens_no_pull_request(bitbucket, woodpecker):
    """Repeat requests must not fill the values repo with empty pull requests."""
    _seed(bitbucket, KV, roles=[_role()])

    result = await _update(bitbucket, woodpecker, namespaces=["payments"])

    assert result.status.value == "Succeeded"
    assert result.message == f"No changes required for {ROLE}"
    assert result.pull_request is None
    assert "create_branch" not in bitbucket.calls


async def test_update_of_a_missing_file_is_404(bitbucket, woodpecker):
    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, woodpecker, ttl="1h")

    assert error.value.status_code == 404
    assert "does not exist" in error.value.message


async def test_update_of_a_missing_role_is_404(bitbucket, woodpecker):
    _seed(bitbucket, KV, roles=[_role(name="someone-else")])

    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, woodpecker, ttl="1h")

    assert error.value.status_code == 404
    assert "not defined in" in error.value.message
    assert error.value.role_name == ROLE


async def test_update_rechecks_the_stores_when_access_changes(bitbucket, woodpecker):
    """Otherwise an edit could introduce the dangling reference a create refuses."""
    _seed(bitbucket, KV, roles=[_role()])

    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, woodpecker, access={"read": ["nope"]})

    assert error.value.status_code == 409
    assert "unknown KV store(s) ['nope']" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_update_does_not_walk_the_directory_when_access_is_untouched(
    bitbucket, woodpecker
):
    _seed(bitbucket, KV, roles=[_role()])

    await _update(bitbucket, woodpecker, ttl="1h")

    assert "list_files" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
async def _delete(bitbucket, woodpecker, role_name=ROLE):
    return await delete_kubernetes_auth_operation(
        bitbucket, woodpecker, FILE, role_name, branch_suffix="abc123"
    )


async def test_delete_removes_only_the_named_role(bitbucket, woodpecker):
    _seed(bitbucket, KV, roles=[_role(name="keep"), _role()])

    result = await _delete(bitbucket, woodpecker)

    assert result.message == f"Successful deletion of {ROLE}"
    assert [r["name"] for r in _committed(bitbucket)["kubernetesAuth"]] == ["keep"]


async def test_delete_leaves_the_kv_stores_alone(bitbucket, woodpecker):
    """A store with no role pointing at it is perfectly valid."""
    _seed(bitbucket, KV, roles=[_role()])

    await _delete(bitbucket, woodpecker)

    assert [s["name"] for s in _committed(bitbucket)["kvStores"]] == [KV]
    assert _committed(bitbucket)["kubernetesAuth"] == []


async def test_delete_runs_no_referential_check(bitbucket, woodpecker):
    """Only the reverse direction can orphan anything, so this one need not scan."""
    _seed(bitbucket, KV, roles=[_role()])

    await _delete(bitbucket, woodpecker)

    assert "list_files" not in bitbucket.calls


async def test_delete_of_a_missing_role_is_404(bitbucket, woodpecker):
    _seed(bitbucket, KV, roles=[_role(name="someone-else")])

    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker)

    assert error.value.status_code == 404
    assert "create_branch" not in bitbucket.calls


async def test_delete_of_a_missing_file_is_404(bitbucket, woodpecker):
    with pytest.raises(VaultOperationError) as error:
        await _delete(bitbucket, woodpecker)

    assert error.value.status_code == 404
    assert "does not exist" in error.value.message


async def test_delete_pull_request_only_stops_at_the_pull_request(bitbucket):
    _seed(bitbucket, KV, roles=[_role()])

    result = await delete_kubernetes_auth_pull_request_operation(
        bitbucket, FILE, ROLE, branch_suffix="abc123"
    )

    assert result.pull_request.state == "OPEN"
    assert f"Opened pull request 101 to delete {ROLE}" in result.message
    assert "merge_pull_request" not in bitbucket.calls
    assert result.validation_pipeline is None


async def test_delete_pull_request_only_shares_the_404s(bitbucket):
    _seed(bitbucket, KV)

    with pytest.raises(VaultOperationError) as error:
        await delete_kubernetes_auth_pull_request_operation(bitbucket, FILE, ROLE)

    assert error.value.status_code == 404


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
async def test_reading_a_file_returns_its_roles(bitbucket):
    _seed(bitbucket, KV, roles=[_role(), _role(name="other")])

    roles = await get_kubernetes_auth_file_operation(bitbucket, FILE)

    assert [r["name"] for r in roles] == [ROLE, "other"]


async def test_a_file_with_no_roles_reads_as_an_empty_list(bitbucket):
    """The file exists, it just declares no roles — that is a 200, not a 404."""
    _seed(bitbucket, KV)

    assert await get_kubernetes_auth_file_operation(bitbucket, FILE) == []


async def test_reading_a_missing_file_is_404(bitbucket):
    with pytest.raises(VaultOperationError) as error:
        await get_kubernetes_auth_file_operation(bitbucket, FILE)

    assert error.value.status_code == 404


async def test_reading_one_role_returns_the_entry(bitbucket):
    _seed(bitbucket, KV, roles=[_role()])

    assert (await get_kubernetes_auth_role_operation(bitbucket, FILE, ROLE))[
        "name"
    ] == ROLE


async def test_reading_an_unknown_role_is_404(bitbucket):
    _seed(bitbucket, KV, roles=[_role(name="other")])

    with pytest.raises(VaultOperationError) as error:
        await get_kubernetes_auth_role_operation(bitbucket, FILE, ROLE)

    assert error.value.status_code == 404
    assert error.value.role_name == ROLE
