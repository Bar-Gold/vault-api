"""The update operation.

It goes through `_edit_values_operation`, so the tests here concentrate on what is specific
to an edit — the mutation it applies, the no-op short circuit, and the fact that an edit
carries Bitbucket's optimistic-lock token. The chain and its rollbacks are covered in
`test_operations.py`, plus one rollback case here to prove edits take the same path.
"""
import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.helpers import build_kv_values, render_values_yaml
from app.v1.vault.operations import VaultOperationError, update_kv_mount_operation
from app.v1.vault.schemas import VaultKVUpdate
from tests.fakes import make_pipeline

KV = "myapp"
VALUES_PATH = "kv/myapp.yaml"


@pytest.fixture
def existing(bitbucket, payload):
    """What a create leaves behind: a name and a description."""
    values = build_kv_values(payload.kv_name, payload.kv_description)
    bitbucket.existing_files[VALUES_PATH] = render_values_yaml(values)
    return values


def _committed(bitbucket):
    return yaml.safe_load(bitbucket.committed[VALUES_PATH])


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


async def test_update_propagates_a_non_404_read_failure(bitbucket, woodpecker):
    """Only a 404 means "no such KV"; anything else is a transport/upstream problem."""
    bitbucket.fail_on["get_file_content"] = ExternalServiceError(
        service_name="bitbucket", detail="boom", status_code=503
    )

    with pytest.raises(ExternalServiceError):
        await update_kv_mount_operation(
            bitbucket, woodpecker, KV, VaultKVUpdate(kv_description="x")
        )


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
