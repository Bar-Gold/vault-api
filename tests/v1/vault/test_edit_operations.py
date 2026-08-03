"""The update operation: edit one store inside a file, leaving its siblings alone.

The chain and its rollbacks are covered in `test_operations.py`, plus one rollback case
here to prove edits take the same path.
"""
import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.helpers import build_kv_store, build_kv_stores_document, render_values_yaml
from app.v1.vault.operations import VaultOperationError, update_kv_mount_operation
from app.v1.vault.schemas import VaultKVUpdate
from tests.fakes import make_pipeline

KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
ROLES = {"read": ["app01.corp.example.com"]}
NEW_ROLES = {"read": ["app02.corp.example.com", "app03.corp.example.com"]}


def _seed(bitbucket, *names, description="payments secrets"):
    bitbucket.existing_files[VALUES_PATH] = render_values_yaml(
        build_kv_stores_document([build_kv_store(n, description, ROLES) for n in names])
    )


def _committed(bitbucket):
    return yaml.safe_load(bitbucket.committed[VALUES_PATH])


def _store(bitbucket, name):
    return next(s for s in _committed(bitbucket)["kvStores"] if s["name"] == name)


async def _update(bitbucket, woodpecker, payload, file=FILE, kv_name=KV):
    return await update_kv_mount_operation(
        bitbucket, woodpecker, file, kv_name, payload, branch_suffix="abc123"
    )


# --------------------------------------------------------------------------- #
# the edit itself
# --------------------------------------------------------------------------- #
async def test_update_replaces_the_description(bitbucket, woodpecker):
    _seed(bitbucket, KV)

    result = await _update(bitbucket, woodpecker, VaultKVUpdate(kv_description="new text"))

    assert result.status.value == "Succeeded"
    assert result.message == f"Successful update of {KV}"
    assert result.file == FILE
    assert _store(bitbucket, KV)["description"] == "new text"


async def test_update_replaces_roles_wholesale(bitbucket, woodpecker):
    """Merging would make removing a host impossible."""
    _seed(bitbucket, KV)

    await _update(bitbucket, woodpecker, VaultKVUpdate(roles=NEW_ROLES))

    assert _store(bitbucket, KV)["roles"] == NEW_ROLES


async def test_update_leaves_the_other_stores_untouched(bitbucket, woodpecker):
    """The whole point of many stores per file."""
    _seed(bitbucket, "sibling", KV, "another")

    await _update(bitbucket, woodpecker, VaultKVUpdate(kv_description="new text"))

    committed = _committed(bitbucket)
    assert [s["name"] for s in committed["kvStores"]] == ["sibling", KV, "another"]
    assert _store(bitbucket, "sibling")["description"] == "payments secrets"
    assert _store(bitbucket, "another")["description"] == "payments secrets"


async def test_update_never_touches_the_name(bitbucket, woodpecker):
    """Renaming means migrating secrets in Vault, not editing a field."""
    _seed(bitbucket, KV)

    await _update(bitbucket, woodpecker, VaultKVUpdate(kv_description="something else"))

    assert _store(bitbucket, KV)["name"] == KV


async def test_update_sends_the_optimistic_lock_token(bitbucket, woodpecker):
    """Editing an existing file without sourceCommitId is rejected by Bitbucket."""
    _seed(bitbucket, KV)

    await _update(bitbucket, woodpecker, VaultKVUpdate(kv_description="new"))

    assert bitbucket.source_commit_ids == ["file-commit-sha"]
    assert bitbucket.calls.index("get_last_commit") < bitbucket.calls.index("put_file")


async def test_update_call_order(bitbucket, woodpecker):
    _seed(bitbucket, KV)

    await _update(bitbucket, woodpecker, VaultKVUpdate(kv_description="new"))

    assert bitbucket.calls == [
        "get_file_content",
        "get_last_commit",
        "create_branch",
        "put_file",
        "create_pull_request",
        "get_pull_request",
        "merge_pull_request",
    ]
    # An edit does not scan for duplicates — the store already exists.
    assert "list_files" not in bitbucket.calls
    assert woodpecker.calls == [
        "list_pipelines",
        "await_pipeline",
        "list_pipelines",
        "await_pipeline",
    ]


# --------------------------------------------------------------------------- #
# the no-op short circuit
# --------------------------------------------------------------------------- #
async def test_update_that_changes_nothing_opens_no_pull_request(bitbucket, woodpecker):
    _seed(bitbucket, KV)

    result = await _update(
        bitbucket, woodpecker, VaultKVUpdate(kv_description="payments secrets")
    )

    assert result.status.value == "Succeeded"
    assert result.message == f"No changes required for {KV}"
    assert result.pull_request is None
    assert "create_branch" not in bitbucket.calls
    assert woodpecker.calls == []


async def test_reapplying_the_same_roles_is_a_no_op(bitbucket, woodpecker):
    _seed(bitbucket, KV)

    result = await _update(bitbucket, woodpecker, VaultKVUpdate(roles=ROLES))

    assert result.message == f"No changes required for {KV}"
    assert "create_branch" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# failures
# --------------------------------------------------------------------------- #
async def test_update_of_a_missing_file_is_404(bitbucket, woodpecker):
    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, woodpecker, VaultKVUpdate(kv_description="x"), file="nope")

    assert error.value.status_code == 404
    assert "does not exist" in error.value.message


async def test_update_of_a_store_not_in_the_file_is_404(bitbucket, woodpecker):
    """The file exists and parses; the named store is simply not in it."""
    _seed(bitbucket, "someone-else")

    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, woodpecker, VaultKVUpdate(kv_description="x"))

    assert error.value.status_code == 404
    assert "not defined in" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_update_of_a_corrupt_file_is_reported(bitbucket, woodpecker):
    bitbucket.existing_files[VALUES_PATH] = "kvStores: [unclosed\n  ::: bad"

    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, woodpecker, VaultKVUpdate(kv_description="x"))

    assert "not valid YAML" in error.value.message


async def test_update_of_a_non_mapping_file_is_reported(bitbucket, woodpecker):
    bitbucket.existing_files[VALUES_PATH] = "- just\n- a list\n"

    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, woodpecker, VaultKVUpdate(kv_description="x"))

    assert "not a YAML mapping" in error.value.message


async def test_update_propagates_a_non_404_read_failure(bitbucket, woodpecker):
    """Only a 404 means "no such file"; anything else is a transport/upstream problem."""
    bitbucket.fail_on["get_file_content"] = ExternalServiceError(
        service_name="bitbucket", detail="boom", status_code=503
    )

    with pytest.raises(ExternalServiceError):
        await _update(bitbucket, woodpecker, VaultKVUpdate(kv_description="x"))


async def test_failed_validation_on_an_edit_declines_and_cleans_up(bitbucket, woodpecker):
    """Edits go through the same chain, so they get the same rollback."""
    _seed(bitbucket, KV)
    woodpecker.results = [make_pipeline(number=2, status="failure", event="pull_request")]

    with pytest.raises(VaultOperationError):
        await _update(bitbucket, woodpecker, VaultKVUpdate(kv_description="new"))

    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls
