"""The three edit operations: update, Kubernetes auth and AD group binding.

They share `_edit_values_operation`, so the tests here concentrate on what differs — the
mutation each one applies, the no-op short circuit, and the fact that an edit carries
Bitbucket's optimistic-lock token. The chain and its rollbacks are covered once in
`test_operations.py`, plus one rollback case here to prove the edits go through the same path.
"""
import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.helpers import build_values_data, render_values_yaml
from app.v1.vault.operations import (
    VaultOperationError,
    add_group_binding_operation,
    add_kubernetes_auth_operation,
    update_kv_mount_operation,
)
from app.v1.vault.schemas import (
    PolicyCapability,
    VaultKVGroupBinding,
    VaultKVGroupBindingSpec,
    VaultKVKubernetesAuth,
    VaultKVKubernetesAuthSpec,
    VaultKVUpdate,
    VaultKVUpdateSpec,
)
from tests.fakes import make_pipeline

VALUES_PATH = "kv/prod/myapp.yaml"
MOUNT_PATH = "myapp"
READ_POLICY = "myapp-read"
WRITE_POLICY = "myapp-write"


@pytest.fixture
def existing(bitbucket, payload):
    """Put a freshly created mount's values file on the base branch."""
    _, values = build_values_data(payload, "kingmagen", "prod")
    bitbucket.existing_files[VALUES_PATH] = render_values_yaml(values)
    return values


def _committed(bitbucket):
    return yaml.safe_load(bitbucket.committed[VALUES_PATH])


def _update(metadata, **spec):
    return VaultKVUpdate(spec=VaultKVUpdateSpec(**spec), metadata=metadata)


def _k8s(metadata, **spec):
    return VaultKVKubernetesAuth(spec=VaultKVKubernetesAuthSpec(**spec), metadata=metadata)


def _group(metadata, **spec):
    return VaultKVGroupBinding(spec=VaultKVGroupBindingSpec(**spec), metadata=metadata)


# --------------------------------------------------------------------------- #
# update — description / owner
# --------------------------------------------------------------------------- #
async def test_update_replaces_the_description(existing, bitbucket, woodpecker, metadata):
    result = await update_kv_mount_operation(
        bitbucket, woodpecker, "myapp", _update(metadata, description="payments secrets")
    )

    assert result.status.value == "Succeeded"
    assert result.message == f"Successful update of {MOUNT_PATH}"
    assert _committed(bitbucket)["mount"]["description"] == "payments secrets"


async def test_update_replaces_the_owner(existing, bitbucket, woodpecker, metadata):
    await update_kv_mount_operation(
        bitbucket, woodpecker, "myapp", _update(metadata, owner="new-dl@example.com")
    )

    assert _committed(bitbucket)["metadata"]["owner"] == "new-dl@example.com"


async def test_update_never_touches_the_mount_path_or_policy_names(
    existing, bitbucket, woodpecker, metadata
):
    """Renaming is a migration, not an edit — an update must leave the identity alone."""
    await update_kv_mount_operation(
        bitbucket, woodpecker, "myapp", _update(metadata, description="something else")
    )

    committed = _committed(bitbucket)
    assert committed["mount"]["path"] == MOUNT_PATH
    assert [p["name"] for p in committed["policies"]] == [READ_POLICY, WRITE_POLICY]


async def test_update_sends_the_optimistic_lock_token(
    existing, bitbucket, woodpecker, metadata
):
    """Editing an existing file without sourceCommitId is rejected by Bitbucket."""
    await update_kv_mount_operation(
        bitbucket, woodpecker, "myapp", _update(metadata, description="new")
    )

    assert bitbucket.source_commit_ids == ["file-commit-sha"]
    assert bitbucket.calls.index("get_last_commit") < bitbucket.calls.index("put_file")


async def test_update_call_order(existing, bitbucket, woodpecker, metadata):
    await update_kv_mount_operation(
        bitbucket, woodpecker, "myapp", _update(metadata, description="new")
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
    existing, bitbucket, woodpecker, metadata
):
    unchanged = existing["mount"]["description"]

    result = await update_kv_mount_operation(
        bitbucket, woodpecker, "myapp", _update(metadata, description=unchanged)
    )

    assert result.status.value == "Succeeded"
    assert result.message == f"No changes required for {MOUNT_PATH}"
    assert result.pull_request is None
    assert "create_branch" not in bitbucket.calls
    assert woodpecker.calls == []


async def test_update_of_a_missing_mount_is_404(bitbucket, woodpecker, metadata):
    with pytest.raises(VaultOperationError) as error:
        await update_kv_mount_operation(
            bitbucket, woodpecker, "nope", _update(metadata, description="x")
        )

    assert error.value.status_code == 404
    assert "does not exist" in error.value.message


async def test_update_of_a_corrupt_values_file_is_reported(bitbucket, woodpecker, metadata):
    bitbucket.existing_files[VALUES_PATH] = "mount: [unclosed\n  ::: bad"

    with pytest.raises(VaultOperationError) as error:
        await update_kv_mount_operation(
            bitbucket, woodpecker, "myapp", _update(metadata, description="x")
        )

    assert "not valid YAML" in error.value.message


async def test_update_of_a_non_mapping_values_file_is_reported(
    bitbucket, woodpecker, metadata
):
    bitbucket.existing_files[VALUES_PATH] = "- just\n- a list\n"

    with pytest.raises(VaultOperationError) as error:
        await update_kv_mount_operation(
            bitbucket, woodpecker, "myapp", _update(metadata, description="x")
        )

    assert "not a YAML mapping" in error.value.message


async def test_failed_validation_on_an_edit_declines_and_cleans_up(
    existing, bitbucket, woodpecker, metadata
):
    """Edits go through the same chain, so they get the same rollback."""
    woodpecker.results = [make_pipeline(number=2, status="failure", event="pull_request")]

    with pytest.raises(VaultOperationError):
        await update_kv_mount_operation(
            bitbucket, woodpecker, "myapp", _update(metadata, description="new")
        )

    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls


# --------------------------------------------------------------------------- #
# Kubernetes auth
# --------------------------------------------------------------------------- #
async def test_kubernetes_auth_is_added(existing, bitbucket, woodpecker, metadata):
    result = await add_kubernetes_auth_operation(
        bitbucket,
        woodpecker,
        "myapp",
        _k8s(
            metadata,
            service_accounts=["myapp"],
            namespaces=["payments-prod"],
            ttl="24h",
        ),
    )

    assert result.status.value == "Succeeded"
    roles = _committed(bitbucket)["kubernetes_auth"]
    assert roles == [
        {
            "role": "myapp",
            "service_accounts": ["myapp"],
            "namespaces": ["payments-prod"],
            "policies": [READ_POLICY],
            "ttl": "24h",
        }
    ]


async def test_kubernetes_auth_defaults_the_role_name(
    existing, bitbucket, woodpecker, metadata
):
    await add_kubernetes_auth_operation(
        bitbucket,
        woodpecker,
        "myapp",
        _k8s(metadata, service_accounts=["sa"], namespaces=["ns"]),
    )

    assert _committed(bitbucket)["kubernetes_auth"][0]["role"] == "myapp"


async def test_kubernetes_auth_can_bind_the_write_policy(
    existing, bitbucket, woodpecker, metadata
):
    await add_kubernetes_auth_operation(
        bitbucket,
        woodpecker,
        "myapp",
        _k8s(
            metadata,
            role="myapp-writer",
            service_accounts=["sa"],
            namespaces=["ns"],
            capability=PolicyCapability.WRITE,
        ),
    )

    assert _committed(bitbucket)["kubernetes_auth"][0]["policies"] == [WRITE_POLICY]


async def test_kubernetes_auth_omits_ttl_when_not_given(
    existing, bitbucket, woodpecker, metadata
):
    await add_kubernetes_auth_operation(
        bitbucket,
        woodpecker,
        "myapp",
        _k8s(metadata, service_accounts=["sa"], namespaces=["ns"]),
    )

    assert "ttl" not in _committed(bitbucket)["kubernetes_auth"][0]


async def test_re_adding_an_identical_kubernetes_role_is_a_no_op(
    existing, bitbucket, woodpecker, metadata
):
    payload = _k8s(metadata, service_accounts=["sa"], namespaces=["ns"], ttl="1h")
    await add_kubernetes_auth_operation(bitbucket, woodpecker, "myapp", payload)
    # The first edit is now what is on the base branch.
    bitbucket.existing_files[VALUES_PATH] = bitbucket.committed[VALUES_PATH]
    bitbucket.calls.clear()
    woodpecker.calls.clear()
    woodpecker.results = [
        make_pipeline(number=4, status="success", event="pull_request"),
        make_pipeline(number=5, status="success", event="push", commit="merge-sha-1"),
    ]

    result = await add_kubernetes_auth_operation(bitbucket, woodpecker, "myapp", payload)

    assert result.message == f"No changes required for {MOUNT_PATH}"
    assert "create_branch" not in bitbucket.calls


async def test_changing_an_existing_kubernetes_role_replaces_it(
    existing, bitbucket, woodpecker, metadata
):
    """Upsert, not append — the same role name must not end up in the file twice."""
    await add_kubernetes_auth_operation(
        bitbucket,
        woodpecker,
        "myapp",
        _k8s(metadata, role="shared", service_accounts=["old"], namespaces=["ns"]),
    )
    bitbucket.existing_files[VALUES_PATH] = bitbucket.committed[VALUES_PATH]
    woodpecker.results = [
        make_pipeline(number=4, status="success", event="pull_request"),
        make_pipeline(number=5, status="success", event="push", commit="merge-sha-1"),
    ]

    await add_kubernetes_auth_operation(
        bitbucket,
        woodpecker,
        "myapp",
        _k8s(metadata, role="shared", service_accounts=["new"], namespaces=["ns"]),
    )

    roles = _committed(bitbucket)["kubernetes_auth"]
    assert len(roles) == 1
    assert roles[0]["service_accounts"] == ["new"]


async def test_kubernetes_auth_without_a_matching_policy_is_422(
    bitbucket, woodpecker, metadata
):
    bitbucket.existing_files[VALUES_PATH] = yaml.safe_dump(
        {"mount": {"path": MOUNT_PATH}, "policies": []}
    )

    with pytest.raises(VaultOperationError) as error:
        await add_kubernetes_auth_operation(
            bitbucket,
            woodpecker,
            "myapp",
            _k8s(metadata, service_accounts=["sa"], namespaces=["ns"]),
        )

    assert error.value.status_code == 422
    assert "no 'read' policy" in error.value.message


# --------------------------------------------------------------------------- #
# AD group bindings
# --------------------------------------------------------------------------- #
async def test_group_is_added_to_the_read_policy(existing, bitbucket, woodpecker, metadata):
    result = await add_group_binding_operation(
        bitbucket,
        woodpecker,
        "myapp",
        _group(metadata, group="AD\\payments-ro", capability=PolicyCapability.READ),
    )

    assert result.status.value == "Succeeded"
    assert "AD\\payments-ro" in result.message
    policies = {p["name"]: p for p in _committed(bitbucket)["policies"]}
    assert policies[READ_POLICY]["entities"] == ["group/readers", "AD\\payments-ro"]
    # The write policy is untouched.
    assert policies[WRITE_POLICY]["entities"] == ["group/writers"]


async def test_group_is_added_to_the_write_policy(existing, bitbucket, woodpecker, metadata):
    await add_group_binding_operation(
        bitbucket,
        woodpecker,
        "myapp",
        _group(metadata, group="AD\\payments-rw", capability=PolicyCapability.WRITE),
    )

    policies = {p["name"]: p for p in _committed(bitbucket)["policies"]}
    assert policies[WRITE_POLICY]["entities"] == ["group/writers", "AD\\payments-rw"]
    assert policies[READ_POLICY]["entities"] == ["group/readers"]


async def test_re_adding_a_bound_group_is_a_no_op(existing, bitbucket, woodpecker, metadata):
    result = await add_group_binding_operation(
        bitbucket,
        woodpecker,
        "myapp",
        _group(metadata, group="group/readers", capability=PolicyCapability.READ),
    )

    assert result.message == f"No changes required for {MOUNT_PATH}"
    assert "create_branch" not in bitbucket.calls


async def test_group_binding_without_a_matching_policy_is_422(
    bitbucket, woodpecker, metadata
):
    bitbucket.existing_files[VALUES_PATH] = yaml.safe_dump(
        {"mount": {"path": MOUNT_PATH}, "policies": [{"name": "unrelated"}]}
    )

    with pytest.raises(VaultOperationError) as error:
        await add_group_binding_operation(
            bitbucket,
            woodpecker,
            "myapp",
            _group(metadata, group="AD\\x", capability=PolicyCapability.WRITE),
        )

    assert error.value.status_code == 422


async def test_group_binding_propagates_a_non_404_read_failure(
    bitbucket, woodpecker, metadata
):
    bitbucket.fail_on["get_file_content"] = ExternalServiceError(
        service_name="bitbucket", detail="boom", status_code=503
    )

    with pytest.raises(ExternalServiceError):
        await add_group_binding_operation(
            bitbucket,
            woodpecker,
            "myapp",
            _group(metadata, group="AD\\x", capability=PolicyCapability.READ),
        )
