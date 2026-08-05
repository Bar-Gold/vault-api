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
from tests.fakes import failing

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


async def _update(bitbucket, payload, kv_name=KV):
    return await update_kv_mount_operation(
        bitbucket, kv_name, payload, branch_suffix="abc123"
    )


# --------------------------------------------------------------------------- #
# the edit itself
# --------------------------------------------------------------------------- #
async def test_update_replaces_the_description(bitbucket):
    _seed(bitbucket, KV)

    result = await _update(bitbucket, VaultKVUpdate(kv_description="new text"))

    assert result.status.value == "Succeeded"
    assert result.message == f"Successful update of {KV}"
    assert result.file == FILE
    assert _store(bitbucket, KV)["description"] == "new text"


async def test_update_replaces_roles_wholesale(bitbucket):
    """Merging would make removing a host impossible."""
    _seed(bitbucket, KV)

    await _update(bitbucket, VaultKVUpdate(roles=NEW_ROLES))

    assert _store(bitbucket, KV)["roles"] == NEW_ROLES


async def test_update_leaves_the_other_stores_untouched(bitbucket):
    """The whole point of many stores per file."""
    _seed(bitbucket, "sibling", KV, "another")

    await _update(bitbucket, VaultKVUpdate(kv_description="new text"))

    committed = _committed(bitbucket)
    assert [s["name"] for s in committed["kvStores"]] == ["sibling", KV, "another"]
    assert _store(bitbucket, "sibling")["description"] == "payments secrets"
    assert _store(bitbucket, "another")["description"] == "payments secrets"


async def test_update_never_touches_the_name(bitbucket):
    """Renaming means migrating secrets in Vault, not editing a field."""
    _seed(bitbucket, KV)

    await _update(bitbucket, VaultKVUpdate(kv_description="something else"))

    assert _store(bitbucket, KV)["name"] == KV


async def test_update_sends_the_optimistic_lock_token(bitbucket):
    """Editing an existing file without sourceCommitId is rejected by Bitbucket."""
    _seed(bitbucket, KV)

    await _update(bitbucket, VaultKVUpdate(kv_description="new"))

    assert bitbucket.source_commit_ids == ["file-commit-sha"]
    assert bitbucket.calls.index("get_last_commit") < bitbucket.calls.index("put_file")


async def test_update_call_order(bitbucket):
    _seed(bitbucket, KV)

    await _update(bitbucket, VaultKVUpdate(kv_description="new"))

    assert bitbucket.calls == [
        "get_file_content",  # kv/myapp.yaml — the conventional path, not there
        "list_files",        # so walk the dir to find which file holds the store
        "get_file_content",
        "get_last_commit",
        "create_branch",
        "put_file",
        "create_pull_request",
        "await_builds",
        "get_pull_request",
        "merge_pull_request",
        "await_builds",
    ]
    # The one `list_files` is resolution, not a duplicate scan: an edit never checks
    # uniqueness, because the store already exists.
    assert bitbucket.calls.count("list_files") == 1


# --------------------------------------------------------------------------- #
# the no-op short circuit
# --------------------------------------------------------------------------- #
async def test_update_that_changes_nothing_opens_no_pull_request(bitbucket):
    _seed(bitbucket, KV)

    result = await _update(
        bitbucket, VaultKVUpdate(kv_description="payments secrets")
    )

    assert result.status.value == "Succeeded"
    assert result.message == f"No changes required for {KV}"
    assert result.pull_request is None
    assert "create_branch" not in bitbucket.calls
    # No pull request means no CI gate either.
    assert "await_builds" not in bitbucket.calls


async def test_reapplying_the_same_roles_is_a_no_op(bitbucket):
    _seed(bitbucket, KV)

    result = await _update(bitbucket, VaultKVUpdate(roles=ROLES))

    assert result.message == f"No changes required for {KV}"
    assert "create_branch" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# failures
# --------------------------------------------------------------------------- #
async def test_update_of_a_store_in_no_file_is_404(bitbucket):
    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, VaultKVUpdate(kv_description="x"), kv_name="nope")

    assert error.value.status_code == 404
    assert "is not defined in any file" in error.value.message


async def test_update_of_a_store_not_in_the_file_is_404(bitbucket):
    """The file exists and parses; the named store is simply not in it."""
    _seed(bitbucket, "someone-else")

    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, VaultKVUpdate(kv_description="x"))

    assert error.value.status_code == 404
    assert "not defined in" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_update_of_a_corrupt_conventional_file_is_reported(bitbucket):
    """The store's own file is read directly, so its YAML error is reported as one."""
    bitbucket.existing_files["kv/solo.yaml"] = "kvStores: [unclosed\n  ::: bad"

    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, VaultKVUpdate(kv_description="x"), kv_name="solo")

    assert "not valid YAML" in error.value.message


async def test_update_of_a_corrupt_grouped_file_names_it(bitbucket):
    """A file the walk skipped has to be named, or the 404 blames the wrong thing."""
    bitbucket.existing_files[VALUES_PATH] = "kvStores: [unclosed\n  ::: bad"

    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, VaultKVUpdate(kv_description="x"))

    assert "unparseable" in error.value.message
    assert VALUES_PATH in error.value.message


async def test_update_of_a_non_mapping_conventional_file_is_reported(bitbucket):
    bitbucket.existing_files["kv/solo.yaml"] = "- just\n- a list\n"

    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, VaultKVUpdate(kv_description="x"), kv_name="solo")

    assert "not a YAML mapping" in error.value.message


async def test_update_propagates_a_non_404_read_failure(bitbucket):
    """Only a 404 means "no such file"; anything else is a transport/upstream problem."""
    bitbucket.fail_on["get_file_content"] = ExternalServiceError(
        service_name="bitbucket", detail="boom", status_code=503
    )

    with pytest.raises(ExternalServiceError):
        await _update(bitbucket, VaultKVUpdate(kv_description="x"))


async def test_failed_validation_on_an_edit_declines_and_cleans_up(bitbucket):
    """Edits go through the same chain, so they get the same rollback."""
    _seed(bitbucket, KV)
    bitbucket.builds = [failing()]

    with pytest.raises(VaultOperationError):
        await _update(bitbucket, VaultKVUpdate(kv_description="new"))

    assert "decline_pull_request" in bitbucket.calls
    assert "delete_branch" in bitbucket.calls
