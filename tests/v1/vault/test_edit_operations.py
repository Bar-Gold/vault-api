"""The three edit operations: update, Kubernetes auth and AD group binding.

They share `_edit_values_operation`, so the tests here concentrate on what differs — the
mutation each applies, the no-op short circuit, and the fact that an edit carries
Bitbucket's optimistic-lock token. The chain and its rollbacks are covered in
`test_operations.py`, plus one rollback case here to prove edits take the same path.

`kubernetes_auth` and group bindings act on `policies`, which the create flow does not
write: this service commits a name and a description and leaves Vault's structure to the
deploy pipeline. So those tests seed a file whose shape has grown policies, and there is a
test for the 422 you get when it has not.
"""
import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.helpers import build_kv_values, render_values_yaml
from app.v1.vault.operations import (
    VaultOperationError,
    add_group_binding_operation,
    add_kubernetes_auth_operation,
    update_kv_mount_operation,
)
from app.v1.vault.schemas import (
    PolicyCapability,
    VaultKVGroupBinding,
    VaultKVKubernetesAuth,
    VaultKVUpdate,
)
from tests.fakes import make_pipeline

KV = "myapp"
VALUES_PATH = "kv/myapp.yaml"
READ_POLICY = "myapp-read"
WRITE_POLICY = "myapp-write"


@pytest.fixture
def existing(bitbucket, payload):
    """What a create leaves behind: a name and a description."""
    values = build_kv_values(payload.kv_name, payload.kv_description)
    bitbucket.existing_files[VALUES_PATH] = render_values_yaml(values)
    return values


@pytest.fixture
def with_policies(bitbucket):
    """A file whose pipeline-managed shape has grown policies."""
    values = {
        "kvname": KV,
        "description": "payments secrets",
        "policies": [
            {"name": READ_POLICY, "entities": ["group/readers"]},
            {"name": WRITE_POLICY, "entities": ["group/writers"]},
        ],
    }
    bitbucket.existing_files[VALUES_PATH] = render_values_yaml(values)
    return values


def _committed(bitbucket):
    return yaml.safe_load(bitbucket.committed[VALUES_PATH])


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #
async def test_update_replaces_the_description(existing, bitbucket, woodpecker):
    result = await update_kv_mount_operation(
        bitbucket, woodpecker, KV, VaultKVUpdate(kv_description="new text")
    )

    assert result.status.value == "Succeeded"
    assert result.message == f"Successful update of {KV}"
    assert _committed(bitbucket)["description"] == "new text"


async def test_update_replaces_the_owner(existing, bitbucket, woodpecker):
    await update_kv_mount_operation(
        bitbucket, woodpecker, KV, VaultKVUpdate(owner="new-dl@example.com")
    )

    assert _committed(bitbucket)["owner"] == "new-dl@example.com"


async def test_update_never_touches_the_name(existing, bitbucket, woodpecker):
    """Renaming means migrating secrets in Vault, not editing a field."""
    await update_kv_mount_operation(
        bitbucket, woodpecker, KV, VaultKVUpdate(kv_description="something else")
    )

    assert _committed(bitbucket)["kvname"] == KV


async def test_update_sends_the_optimistic_lock_token(existing, bitbucket, woodpecker):
    """Editing an existing file without sourceCommitId is rejected by Bitbucket."""
    await update_kv_mount_operation(
        bitbucket, woodpecker, KV, VaultKVUpdate(kv_description="new")
    )

    assert bitbucket.source_commit_ids == ["file-commit-sha"]
    assert bitbucket.calls.index("get_last_commit") < bitbucket.calls.index("put_file")


async def test_update_call_order(existing, bitbucket, woodpecker):
    await update_kv_mount_operation(
        bitbucket, woodpecker, KV, VaultKVUpdate(kv_description="new")
    )

    assert bitbucket.calls == [
        "get_file_content",
        "get_last_commit",
        "create_branch",
        "put_file",
        "create_pull_request",
        "get_pull_request",
        "merge_pull_request",
    ]
    assert woodpecker.calls == [
        "list_pipelines",
        "await_pipeline",
        "list_pipelines",
        "await_pipeline",
    ]


async def test_update_that_changes_nothing_opens_no_pull_request(
    existing, bitbucket, woodpecker
):
    result = await update_kv_mount_operation(
        bitbucket, woodpecker, KV, VaultKVUpdate(kv_description=existing["description"])
    )

    assert result.status.value == "Succeeded"
    assert result.message == f"No changes required for {KV}"
    assert result.pull_request is None
    assert "create_branch" not in bitbucket.calls
    assert woodpecker.calls == []


async def test_update_of_a_missing_kv_is_404(bitbucket, woodpecker):
    with pytest.raises(VaultOperationError) as error:
        await update_kv_mount_operation(
            bitbucket, woodpecker, "nope", VaultKVUpdate(kv_description="x")
        )

    assert error.value.status_code == 404
    assert "does not exist" in error.value.message


async def test_update_of_a_corrupt_file_is_reported(bitbucket, woodpecker):
    bitbucket.existing_files[VALUES_PATH] = "kvname: [unclosed\n  ::: bad"

    with pytest.raises(VaultOperationError) as error:
        await update_kv_mount_operation(
            bitbucket, woodpecker, KV, VaultKVUpdate(kv_description="x")
        )

    assert "not valid YAML" in error.value.message


async def test_update_of_a_non_mapping_file_is_reported(bitbucket, woodpecker):
    bitbucket.existing_files[VALUES_PATH] = "- just\n- a list\n"

    with pytest.raises(VaultOperationError) as error:
        await update_kv_mount_operation(
            bitbucket, woodpecker, KV, VaultKVUpdate(kv_description="x")
        )

    assert "not a YAML mapping" in error.value.message


async def test_failed_validation_on_an_edit_declines_and_cleans_up(
    existing, bitbucket, woodpecker
):
    """Edits go through the same chain, so they get the same rollback."""
    woodpecker.results = [make_pipeline(number=2, status="failure", event="pull_request")]

    with pytest.raises(VaultOperationError):
        await update_kv_mount_operation(
            bitbucket, woodpecker, KV, VaultKVUpdate(kv_description="new")
        )

    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls


# --------------------------------------------------------------------------- #
# Kubernetes auth
# --------------------------------------------------------------------------- #
async def test_kubernetes_auth_is_added(with_policies, bitbucket, woodpecker):
    result = await add_kubernetes_auth_operation(
        bitbucket,
        woodpecker,
        KV,
        VaultKVKubernetesAuth(
            service_accounts=["myapp"], namespaces=["payments-prod"], ttl="24h"
        ),
    )

    assert result.status.value == "Succeeded"
    assert _committed(bitbucket)["kubernetes_auth"] == [
        {
            "role": KV,
            "service_accounts": ["myapp"],
            "namespaces": ["payments-prod"],
            "policies": [READ_POLICY],
            "ttl": "24h",
        }
    ]


async def test_kubernetes_auth_can_bind_the_write_policy(
    with_policies, bitbucket, woodpecker
):
    await add_kubernetes_auth_operation(
        bitbucket,
        woodpecker,
        KV,
        VaultKVKubernetesAuth(
            role="myapp-writer",
            service_accounts=["sa"],
            namespaces=["ns"],
            capability=PolicyCapability.WRITE,
        ),
    )

    assert _committed(bitbucket)["kubernetes_auth"][0]["policies"] == [WRITE_POLICY]


async def test_re_adding_an_identical_role_is_a_no_op(
    with_policies, bitbucket, woodpecker
):
    payload = VaultKVKubernetesAuth(
        service_accounts=["sa"], namespaces=["ns"], ttl="1h"
    )
    await add_kubernetes_auth_operation(bitbucket, woodpecker, KV, payload)
    bitbucket.existing_files[VALUES_PATH] = bitbucket.committed[VALUES_PATH]
    bitbucket.calls.clear()

    result = await add_kubernetes_auth_operation(bitbucket, woodpecker, KV, payload)

    assert result.message == f"No changes required for {KV}"
    assert "create_branch" not in bitbucket.calls


async def test_changing_an_existing_role_replaces_it(
    with_policies, bitbucket, woodpecker
):
    """Upsert, not append — the same role must not end up in the file twice."""
    await add_kubernetes_auth_operation(
        bitbucket,
        woodpecker,
        KV,
        VaultKVKubernetesAuth(role="shared", service_accounts=["old"], namespaces=["ns"]),
    )
    bitbucket.existing_files[VALUES_PATH] = bitbucket.committed[VALUES_PATH]
    woodpecker.results = [
        make_pipeline(number=4, status="success", event="pull_request"),
        make_pipeline(number=5, status="success", event="push", commit="merge-sha-1"),
    ]

    await add_kubernetes_auth_operation(
        bitbucket,
        woodpecker,
        KV,
        VaultKVKubernetesAuth(role="shared", service_accounts=["new"], namespaces=["ns"]),
    )

    roles = _committed(bitbucket)["kubernetes_auth"]
    assert len(roles) == 1
    assert roles[0]["service_accounts"] == ["new"]


async def test_kubernetes_auth_on_a_file_without_policies_is_422(
    existing, bitbucket, woodpecker
):
    """What a plain create leaves behind has no policies to bind to."""
    with pytest.raises(VaultOperationError) as error:
        await add_kubernetes_auth_operation(
            bitbucket,
            woodpecker,
            KV,
            VaultKVKubernetesAuth(service_accounts=["sa"], namespaces=["ns"]),
        )

    assert error.value.status_code == 422
    assert "no 'read' policy" in error.value.message


# --------------------------------------------------------------------------- #
# AD group bindings
# --------------------------------------------------------------------------- #
async def test_group_is_added_to_the_read_policy(with_policies, bitbucket, woodpecker):
    result = await add_group_binding_operation(
        bitbucket,
        woodpecker,
        KV,
        VaultKVGroupBinding(group="AD\\payments-ro", capability=PolicyCapability.READ),
    )

    assert result.status.value == "Succeeded"
    assert "AD\\payments-ro" in result.message
    policies = {p["name"]: p for p in _committed(bitbucket)["policies"]}
    assert policies[READ_POLICY]["entities"] == ["group/readers", "AD\\payments-ro"]
    assert policies[WRITE_POLICY]["entities"] == ["group/writers"]


async def test_group_is_added_to_the_write_policy(with_policies, bitbucket, woodpecker):
    await add_group_binding_operation(
        bitbucket,
        woodpecker,
        KV,
        VaultKVGroupBinding(group="AD\\payments-rw", capability=PolicyCapability.WRITE),
    )

    policies = {p["name"]: p for p in _committed(bitbucket)["policies"]}
    assert policies[WRITE_POLICY]["entities"] == ["group/writers", "AD\\payments-rw"]
    assert policies[READ_POLICY]["entities"] == ["group/readers"]


async def test_re_adding_a_bound_group_is_a_no_op(with_policies, bitbucket, woodpecker):
    result = await add_group_binding_operation(
        bitbucket,
        woodpecker,
        KV,
        VaultKVGroupBinding(group="group/readers", capability=PolicyCapability.READ),
    )

    assert result.message == f"No changes required for {KV}"
    assert "create_branch" not in bitbucket.calls


async def test_group_binding_without_a_policy_is_422(existing, bitbucket, woodpecker):
    with pytest.raises(VaultOperationError) as error:
        await add_group_binding_operation(
            bitbucket,
            woodpecker,
            KV,
            VaultKVGroupBinding(group="AD\\x", capability=PolicyCapability.WRITE),
        )

    assert error.value.status_code == 422


async def test_group_binding_propagates_a_non_404_read_failure(bitbucket, woodpecker):
    bitbucket.fail_on["get_file_content"] = ExternalServiceError(
        service_name="bitbucket", detail="boom", status_code=503
    )

    with pytest.raises(ExternalServiceError):
        await add_group_binding_operation(
            bitbucket,
            woodpecker,
            KV,
            VaultKVGroupBinding(group="AD\\x", capability=PolicyCapability.READ),
        )
