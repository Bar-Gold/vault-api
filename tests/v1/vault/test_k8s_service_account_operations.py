"""Binding and unbinding a service account: the chain, the guards, and the rollbacks.

These reuse `_open_pull_request` / `_commit_via_pull_request` unchanged, so the interesting
assertions are about what is *prepared* before the branch exists — and about what is
absent. A binding lives inside its store, so none of these operations walks the values
directory, and none of them can leave a dangling reference behind.
"""
import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.clients.woodpecker import PipelineTimeoutError
from app.helpers import (
    build_k8s_service_account,
    build_kv_store,
    build_kv_stores_document,
    render_values_yaml,
)
from app.v1.vault.operations import (
    VaultOperationError,
    add_k8s_service_account_operation,
    add_k8s_service_account_pull_request_operation,
    get_k8s_service_accounts_operation,
    remove_k8s_service_account_operation,
    remove_k8s_service_account_pull_request_operation,
)
from tests.fakes import make_pipeline

KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
ROLES = {"read": ["app01.corp.example.com"]}
IDENTITY = ("vault", "payments", "dev")


def _seed(bitbucket, *names, path=VALUES_PATH, accounts=None):
    document = build_kv_stores_document(
        [build_kv_store(n, "payments secrets", ROLES) for n in names]
    )
    if accounts is not None:
        for store in document["kvStores"]:
            store["roles"]["k8sServiceAccounts"] = list(accounts)
    bitbucket.existing_files[path] = render_values_yaml(document)


def _committed(bitbucket):
    return yaml.safe_load(bitbucket.committed[VALUES_PATH])


def _bindings(bitbucket, index=0):
    roles = _committed(bitbucket)["kvStores"][index].get("roles", {})
    return roles.get("k8sServiceAccounts", [])


async def _bind(bitbucket, woodpecker, payload, file=FILE, kv_name=KV):
    return await add_k8s_service_account_operation(
        bitbucket, woodpecker, file, kv_name, payload, branch_suffix="abc123"
    )


async def _bind_pr_only(bitbucket, payload, file=FILE, kv_name=KV):
    return await add_k8s_service_account_pull_request_operation(
        bitbucket, file, kv_name, payload, branch_suffix="abc123"
    )


async def _unbind(bitbucket, woodpecker, identity=IDENTITY, file=FILE, kv_name=KV):
    return await remove_k8s_service_account_operation(
        bitbucket, woodpecker, file, kv_name, identity, branch_suffix="abc123"
    )


async def _unbind_pr_only(bitbucket, identity=IDENTITY, file=FILE, kv_name=KV):
    return await remove_k8s_service_account_pull_request_operation(
        bitbucket, file, kv_name, identity, branch_suffix="abc123"
    )


# --------------------------------------------------------------------------- #
# binding — the happy path
# --------------------------------------------------------------------------- #
async def test_bind_succeeds(bitbucket, woodpecker, account_payload):
    _seed(bitbucket, KV)

    result = await _bind(bitbucket, woodpecker, account_payload)

    assert result.status.value == "Succeeded"
    assert result.kv_name == KV
    assert result.file == FILE


async def test_bind_writes_the_entry_inside_the_store(bitbucket, woodpecker, account_payload):
    """The nesting is the format — a top-level key would be the wrong document."""
    _seed(bitbucket, KV)

    await _bind(bitbucket, woodpecker, account_payload)

    document = _committed(bitbucket)
    assert "k8sServiceAccounts" not in document
    assert "k8sServiceAccounts" not in document["kvStores"][0]
    assert _bindings(bitbucket) == [
        {"serviceAccount": "vault", "namespace": "payments", "cluster": "dev"}
    ]


async def test_bind_leaves_the_stores_own_fields_alone(bitbucket, woodpecker, account_payload):
    _seed(bitbucket, KV)

    await _bind(bitbucket, woodpecker, account_payload)

    store = _committed(bitbucket)["kvStores"][0]
    assert store["description"] == "payments secrets"
    assert store["roles"]["read"] == ROLES["read"]


async def test_bind_leaves_sibling_stores_alone(bitbucket, woodpecker, account_payload):
    _seed(bitbucket, KV, "billing")

    await _bind(bitbucket, woodpecker, account_payload)

    assert "k8sServiceAccounts" not in _committed(bitbucket)["kvStores"][1]["roles"]


async def test_bind_appends_to_existing_bindings(bitbucket, woodpecker, account_payload):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account("other", "payments", "dev")])

    await _bind(bitbucket, woodpecker, account_payload)

    assert [b["serviceAccount"] for b in _bindings(bitbucket)] == ["other", "vault"]


async def test_bind_message_names_the_whole_triple(bitbucket, woodpecker, account_payload):
    """A binding has no name, so the message has to spell out what was bound."""
    _seed(bitbucket, KV)

    result = await _bind(bitbucket, woodpecker, account_payload)

    assert result.message == "Successfully bound vault in payments on dev to myapp"


async def test_bind_uses_its_own_branch_prefix(bitbucket, woodpecker, account_payload):
    """A reviewer should see the change kind before opening the diff."""
    _seed(bitbucket, KV)

    result = await _bind(bitbucket, woodpecker, account_payload)

    stored = bitbucket.pull_requests[result.pull_request.id]
    assert stored.from_branch == "vault-k8s-sa/payments-myapp-abc123"


async def test_bind_call_order(bitbucket, woodpecker, account_payload):
    """No list_files: a binding is unique within its store, so there is nothing to scan."""
    _seed(bitbucket, KV)

    await _bind(bitbucket, woodpecker, account_payload)

    assert bitbucket.calls == [
        "get_file_content",
        "get_last_commit",
        "create_branch",
        "put_file",
        "create_pull_request",
        "get_pull_request",
        "merge_pull_request",
    ]


async def test_bind_sends_the_optimistic_lock_token(bitbucket, woodpecker, account_payload):
    """The file exists by definition, so the write is an edit."""
    _seed(bitbucket, KV)

    await _bind(bitbucket, woodpecker, account_payload)

    assert bitbucket.source_commit_ids == ["file-commit-sha"]


async def test_bind_names_both_coordinates_in_the_commit(bitbucket, woodpecker, account_payload):
    _seed(bitbucket, KV)

    await _bind(bitbucket, woodpecker, account_payload)

    assert bitbucket.commit_messages == [
        f"Bind service account vault to {KV} in {FILE}"
    ]


# --------------------------------------------------------------------------- #
# binding — the guards
# --------------------------------------------------------------------------- #
async def test_binding_to_an_unknown_file_is_404(bitbucket, woodpecker, account_payload):
    with pytest.raises(VaultOperationError) as error:
        await _bind(bitbucket, woodpecker, account_payload)

    assert error.value.status_code == 404
    assert "create_branch" not in bitbucket.calls


async def test_binding_to_an_unknown_store_is_404(bitbucket, woodpecker, account_payload):
    """A binding cannot create the store it lives in."""
    _seed(bitbucket, "someone-else")

    with pytest.raises(VaultOperationError) as error:
        await _bind(bitbucket, woodpecker, account_payload)

    assert error.value.status_code == 404
    assert "not defined in" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_binding_the_same_triple_twice_is_409(bitbucket, woodpecker, account_payload):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account(*IDENTITY)])

    with pytest.raises(VaultOperationError) as error:
        await _bind(bitbucket, woodpecker, account_payload)

    assert error.value.status_code == 409
    assert "already bound" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_the_same_account_in_another_namespace_is_allowed(
    bitbucket, woodpecker, account_payload
):
    """The whole triple is the identity, so this is a different binding."""
    _seed(bitbucket, KV, accounts=[build_k8s_service_account("vault", "other", "dev")])

    assert (await _bind(bitbucket, woodpecker, account_payload)).status.value == "Succeeded"


async def test_the_same_account_in_another_cluster_is_allowed(
    bitbucket, woodpecker, account_payload
):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account("vault", "payments", "prod")])

    assert (await _bind(bitbucket, woodpecker, account_payload)).status.value == "Succeeded"


async def test_the_same_account_on_another_store_never_conflicts(
    bitbucket, woodpecker, account_payload
):
    """One workload reaching two secrets is the normal case, not a duplicate."""
    _seed(bitbucket, KV)
    _seed(bitbucket, "billing", path="kv/platform.yaml", accounts=[build_k8s_service_account(*IDENTITY)])

    assert (await _bind(bitbucket, woodpecker, account_payload)).status.value == "Succeeded"


async def test_bind_never_walks_the_values_dir(bitbucket, woodpecker, account_payload):
    _seed(bitbucket, KV)
    _seed(bitbucket, "elsewhere", path="kv/platform.yaml")

    await _bind(bitbucket, woodpecker, account_payload)

    assert "list_files" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# binding — rollbacks
# --------------------------------------------------------------------------- #
async def test_a_failed_commit_deletes_the_branch(bitbucket, woodpecker, account_payload):
    _seed(bitbucket, KV)
    bitbucket.fail_on["put_file"] = ExternalServiceError(
        service_name="bitbucket", detail="nope", status_code=500
    )

    with pytest.raises(ExternalServiceError):
        await _bind(bitbucket, woodpecker, account_payload)

    assert bitbucket.calls[-1] == "delete_branch"


async def test_a_red_validation_pipeline_declines_and_cleans_up(
    bitbucket, account_payload
):
    from tests.fakes import FakeWoodpecker

    _seed(bitbucket, KV)
    woodpecker = FakeWoodpecker(results=[make_pipeline(number=2, status="failure")])

    with pytest.raises(VaultOperationError):
        await _bind(bitbucket, woodpecker, account_payload)

    assert "decline_pull_request" in bitbucket.calls
    assert bitbucket.calls[-1] == "delete_branch"


async def test_a_failed_deploy_pipeline_is_not_rolled_back(bitbucket, account_payload):
    """The merge is the point of no return here too."""
    from tests.fakes import FakeWoodpecker

    _seed(bitbucket, KV)
    woodpecker = FakeWoodpecker(
        results=[
            make_pipeline(number=2, status="success"),
            make_pipeline(number=3, status="failure", event="push"),
        ]
    )

    with pytest.raises(VaultOperationError) as error:
        await _bind(bitbucket, woodpecker, account_payload)

    assert "revert" in error.value.message
    assert "decline_pull_request" not in bitbucket.calls


async def test_a_validation_timeout_is_504(bitbucket, account_payload):
    from tests.fakes import FakeWoodpecker

    _seed(bitbucket, KV)
    woodpecker = FakeWoodpecker(
        results=[PipelineTimeoutError("timed out", pipeline=None)]
    )

    with pytest.raises(VaultOperationError) as error:
        await _bind(bitbucket, woodpecker, account_payload)

    assert error.value.status_code == 504


# --------------------------------------------------------------------------- #
# unbinding
# --------------------------------------------------------------------------- #
async def test_unbind_succeeds(bitbucket, woodpecker):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account(*IDENTITY)])

    result = await _unbind(bitbucket, woodpecker)

    assert result.status.value == "Succeeded"
    assert result.message == "Successfully unbound vault in payments on dev from myapp"


async def test_unbinding_the_last_one_drops_the_key(bitbucket, woodpecker):
    """Back to exactly the shape a fresh create writes — no empty list left behind."""
    _seed(bitbucket, KV, accounts=[build_k8s_service_account(*IDENTITY)])

    await _unbind(bitbucket, woodpecker)

    store = _committed(bitbucket)["kvStores"][0]
    assert "k8sServiceAccounts" not in store["roles"]
    assert store == build_kv_store(KV, "payments secrets", ROLES)


async def test_unbind_drops_only_the_matching_entry(bitbucket, woodpecker):
    _seed(
        bitbucket,
        KV,
        accounts=[
            build_k8s_service_account(*IDENTITY),
            build_k8s_service_account("vault", "payments", "prod"),
        ],
    )

    await _unbind(bitbucket, woodpecker)

    assert [b["cluster"] for b in _bindings(bitbucket)] == ["prod"]


async def test_unbind_uses_its_own_branch_prefix(bitbucket, woodpecker):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account(*IDENTITY)])

    result = await _unbind(bitbucket, woodpecker)

    stored = bitbucket.pull_requests[result.pull_request.id]
    assert stored.from_branch == "vault-k8s-sa/payments-myapp-abc123"


async def test_unbinding_something_not_bound_is_404(bitbucket, woodpecker):
    _seed(bitbucket, KV)

    with pytest.raises(VaultOperationError) as error:
        await _unbind(bitbucket, woodpecker)

    assert error.value.status_code == 404
    assert "is not bound to" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_unbinding_a_partial_match_is_404(bitbucket, woodpecker):
    """Right account, wrong cluster — not the same binding."""
    _seed(bitbucket, KV, accounts=[build_k8s_service_account("vault", "payments", "prod")])

    with pytest.raises(VaultOperationError) as error:
        await _unbind(bitbucket, woodpecker)

    assert error.value.status_code == 404


async def test_unbinding_from_an_unknown_store_is_404(bitbucket, woodpecker):
    _seed(bitbucket, "someone-else")

    with pytest.raises(VaultOperationError) as error:
        await _unbind(bitbucket, woodpecker)

    assert error.value.status_code == 404


async def test_unbind_is_never_a_no_op(bitbucket, woodpecker):
    """No `yaml_data_equals` short circuit: if the remove did not raise, it changed something."""
    _seed(bitbucket, KV, accounts=[build_k8s_service_account(*IDENTITY)])

    result = await _unbind(bitbucket, woodpecker)

    assert result.pull_request is not None


async def test_unbind_never_walks_the_values_dir(bitbucket, woodpecker):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account(*IDENTITY)])
    _seed(bitbucket, "elsewhere", path="kv/platform.yaml")

    await _unbind(bitbucket, woodpecker)

    assert "list_files" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# the PR-only twins
# --------------------------------------------------------------------------- #
async def test_pr_only_bind_stops_at_the_pull_request(bitbucket, account_payload):
    _seed(bitbucket, KV)

    result = await _bind_pr_only(bitbucket, account_payload)

    assert result.pull_request.state == "OPEN"
    assert result.validation_pipeline is None
    assert result.deploy_pipeline is None
    assert bitbucket.calls == [
        "get_file_content",
        "get_last_commit",
        "create_branch",
        "put_file",
        "create_pull_request",
    ]


async def test_pr_only_bind_shares_the_409(bitbucket, account_payload):
    """Both paths go through `_prepare_k8s_sa_add`, so neither can skip the guard."""
    _seed(bitbucket, KV, accounts=[build_k8s_service_account(*IDENTITY)])

    with pytest.raises(VaultOperationError) as error:
        await _bind_pr_only(bitbucket, account_payload)

    assert error.value.status_code == 409


async def test_pr_only_bind_shares_the_404(bitbucket, account_payload):
    _seed(bitbucket, "someone-else")

    with pytest.raises(VaultOperationError) as error:
        await _bind_pr_only(bitbucket, account_payload)

    assert error.value.status_code == 404


async def test_pr_only_unbind_stops_at_the_pull_request(bitbucket):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account(*IDENTITY)])

    result = await _unbind_pr_only(bitbucket)

    assert result.pull_request.state == "OPEN"
    assert "not merged" in result.message
    assert "merge_pull_request" not in bitbucket.calls


async def test_pr_only_unbind_shares_the_404(bitbucket):
    _seed(bitbucket, KV)

    with pytest.raises(VaultOperationError) as error:
        await _unbind_pr_only(bitbucket)

    assert error.value.status_code == 404


async def test_a_repeat_pr_only_bind_opens_a_second_pull_request(bitbucket, account_payload):
    """Nothing merged, so the base branch still has no binding for the guard to see."""
    _seed(bitbucket, KV)

    first = await _bind_pr_only(bitbucket, account_payload)
    second = await _bind_pr_only(bitbucket, account_payload)

    assert first.pull_request.id != second.pull_request.id


async def test_a_failed_pr_only_commit_still_deletes_the_branch(bitbucket, account_payload):
    _seed(bitbucket, KV)
    bitbucket.fail_on["create_pull_request"] = ExternalServiceError(
        service_name="bitbucket", detail="nope", status_code=500
    )

    with pytest.raises(ExternalServiceError):
        await _bind_pr_only(bitbucket, account_payload)

    assert bitbucket.calls[-1] == "delete_branch"


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
async def test_read_returns_the_bindings(bitbucket):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account(*IDENTITY)])

    assert await get_k8s_service_accounts_operation(bitbucket, FILE, KV) == [
        {"serviceAccount": "vault", "namespace": "payments", "cluster": "dev"}
    ]


async def test_read_of_a_store_that_binds_nothing_is_an_empty_list(bitbucket):
    """Not a 404 — the store exists, it just binds nothing."""
    _seed(bitbucket, KV)

    assert await get_k8s_service_accounts_operation(bitbucket, FILE, KV) == []


async def test_read_of_an_unknown_store_is_404(bitbucket):
    _seed(bitbucket, "someone-else")

    with pytest.raises(VaultOperationError) as error:
        await get_k8s_service_accounts_operation(bitbucket, FILE, KV)

    assert error.value.status_code == 404


async def test_read_of_an_unknown_file_is_404(bitbucket):
    with pytest.raises(VaultOperationError) as error:
        await get_k8s_service_accounts_operation(bitbucket, FILE, KV)

    assert error.value.status_code == 404
