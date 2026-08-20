"""The chain and its rollbacks, against the duck-typed fake.

What these pin is *sequencing*: which upstream calls happen, in what order, and what gets
undone when a step fails. The committed bytes are `tests/test_helpers.py`'s job and the
HTTP surface is `test_routes.py`'s; here the question is always "what did the service do".
"""

import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.v1.vault.operations import (
    VaultOperationError,
    add_access_entry_operation,
    create_kv_mount_operation,
    create_kv_pull_request_operation,
    delete_kv_store_operation,
    delete_kv_store_pull_request_operation,
    get_kv_access_operation,
    get_kv_store_operation,
    list_kv_stores_operation,
    remove_access_entry_operation,
    update_kv_mount_operation,
)
from app.v1.vault.schemas import K8sAccessEntry, OidcAccessEntry, VaultKVUpdate
from tests.fakes import failing, passing
from tests.v1.vault.conftest import (
    DN,
    K8S_PATH,
    KV,
    OIDC_PATH,
    OTHER_DN,
    STORE_DIR,
    TEAM,
    k8s_entry,
    oidc_entry,
    seed_store,
)


async def _create(bitbucket, payload):
    return await create_kv_mount_operation(bitbucket, payload, branch_suffix="abc123")


def _committed(bitbucket, path):
    return yaml.safe_load(bitbucket.committed[path])


# --------------------------------------------------------------------------- #
# creating a store
# --------------------------------------------------------------------------- #
async def test_create_writes_both_access_files(bitbucket, payload):
    """A store is a directory of two files, so one create commits twice."""
    await _create(bitbucket, payload)

    assert sorted(bitbucket.committed) == [K8S_PATH, OIDC_PATH]
    assert _committed(bitbucket, OIDC_PATH)["access"][0]["dn"] == DN
    assert _committed(bitbucket, K8S_PATH)["access"][0]["authType"] == "k8s"


async def test_a_store_with_no_k8s_grants_writes_no_k8s_file(bitbucket, oidc_only_payload):
    """Not an empty list — no file. That is the shape the format specifies."""
    await _create(bitbucket, oidc_only_payload)

    assert list(bitbucket.committed) == [OIDC_PATH]


async def test_create_returns_the_success_message(bitbucket, payload):
    result = await _create(bitbucket, payload)

    assert result.status.value == "Succeeded"
    assert f"to create {KV}" in result.message
    assert "waiting for review" in result.message
    assert result.kv_name == KV
    assert result.file == STORE_DIR


async def test_create_leaves_the_pull_request_open(bitbucket, payload):
    """The whole point: this service proposes, a human merges."""
    result = await _create(bitbucket, payload)

    assert result.pull_request.state == "OPEN"
    assert [b.state for b in result.validation_builds] == ["SUCCESSFUL"]
    assert "merge_pull_request" not in bitbucket.calls


async def test_create_call_order(bitbucket, payload):
    await _create(bitbucket, payload)

    assert bitbucket.calls == [
        "list_files",  # the duplicate-name scan, dir-wide
        "create_branch",
        "put_file",  # oidc-access.yaml
        "put_file",  # k8s-access.yaml, same branch
        "create_pull_request",
        "await_builds",  # the validation build, on the pull request's commit
    ]
    # And nothing after it: no merge, no second gate.
    assert "merge_pull_request" not in bitbucket.calls
    assert bitbucket.calls.count("await_builds") == 1


async def test_create_sends_no_optimistic_lock_token(bitbucket, payload):
    """Both files are new, and the browse resource rejects a create that carries one."""
    await _create(bitbucket, payload)

    assert bitbucket.source_commit_ids == [None, None]


async def test_the_branch_names_team_and_store(bitbucket, payload):
    result = await _create(bitbucket, payload)

    stored = bitbucket.pull_requests[result.pull_request.id]
    assert stored.from_branch == f"vault-kv/{TEAM}-{KV}-abc123"


async def test_the_gate_watches_the_pull_requests_commit(bitbucket, payload):
    """One gate, on the sha the PR's Builds tab shows — never the base branch."""
    result = await _create(bitbucket, payload)

    stored = bitbucket.pull_requests[result.pull_request.id]
    assert bitbucket.awaited_commits == [stored.from_commit]


# --------------------------------------------------------------------------- #
# uniqueness
# --------------------------------------------------------------------------- #
async def test_a_duplicate_name_is_409_and_touches_nothing(bitbucket, payload):
    seed_store(bitbucket, oidc=[oidc_entry()])

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, payload)

    assert error.value.status_code == 409
    assert "create_branch" not in bitbucket.calls


async def test_a_name_used_by_another_team_is_also_409(bitbucket, payload):
    """A store name is global to Vault, which is what makes the name a sufficient address."""
    seed_store(bitbucket, team="platform", kv_name=KV, oidc=[oidc_entry()])

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, payload)

    assert error.value.status_code == 409
    assert "platform" in error.value.message


async def test_a_different_name_under_another_team_does_not_collide(bitbucket, payload):
    seed_store(bitbucket, team="platform", kv_name="other", oidc=[oidc_entry()])

    assert (await _create(bitbucket, payload)).status.value == "Succeeded"


# --------------------------------------------------------------------------- #
# rollbacks
# --------------------------------------------------------------------------- #
async def test_a_failed_commit_deletes_the_branch(bitbucket, payload):
    bitbucket.fail_on["put_file"] = ExternalServiceError(
        service_name="bitbucket", detail="commit rejected", status_code=400
    )

    with pytest.raises(ExternalServiceError):
        await _create(bitbucket, payload)

    assert bitbucket.calls[-1] == "delete_branch"
    assert "create_pull_request" not in bitbucket.calls


async def test_a_failed_pull_request_deletes_the_branch(bitbucket, payload):
    bitbucket.fail_on["create_pull_request"] = ExternalServiceError(
        service_name="bitbucket", detail="no reviewers", status_code=400
    )

    with pytest.raises(ExternalServiceError):
        await _create(bitbucket, payload)

    assert bitbucket.calls[-1] == "delete_branch"


async def test_a_red_validation_build_declines_and_deletes(bitbucket, payload):
    bitbucket.builds = [failing()]

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, payload)

    assert "Validation did not pass" in error.value.message
    assert bitbucket.calls[-2:] == ["decline_pull_request", "delete_branch"]


async def test_nothing_is_ever_merged(bitbucket, payload):
    """There is no point of no return any more — the service cannot reach the base branch."""
    await _create(bitbucket, payload)

    assert "merge_pull_request" not in bitbucket.calls
    assert bitbucket.pull_requests[101].state == "OPEN"


async def test_a_build_timeout_declines_and_deletes(bitbucket, payload):
    """A slow gate rolls back exactly like a red one: there is nothing merged to keep."""
    from app.clients.bitbucket import BuildTimeoutError

    bitbucket.builds = [BuildTimeoutError("nothing reported", [])]

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, payload)

    assert error.value.status_code == 504
    assert bitbucket.calls[-2:] == ["decline_pull_request", "delete_branch"]


# --------------------------------------------------------------------------- #
# the PR-only variant
# --------------------------------------------------------------------------- #
async def test_pull_request_only_stops_before_the_gates(bitbucket, payload):
    result = await create_kv_pull_request_operation(
        bitbucket, payload, branch_suffix="abc123"
    )

    assert result.pull_request.state == "OPEN"
    assert result.validation_builds is None
    assert "await_builds" not in bitbucket.calls
    assert "merge_pull_request" not in bitbucket.calls


async def test_pull_request_only_commits_the_same_files(bitbucket, payload):
    """The branch content must not depend on which endpoint asked for it."""
    await create_kv_pull_request_operation(bitbucket, payload, branch_suffix="abc123")

    assert sorted(bitbucket.committed) == [K8S_PATH, OIDC_PATH]


async def test_a_second_pull_request_only_call_opens_a_second_pull_request(
    bitbucket, payload
):
    """Nothing was merged, so the scan cannot see the first one."""
    first = await create_kv_pull_request_operation(bitbucket, payload, branch_suffix="a")
    second = await create_kv_pull_request_operation(bitbucket, payload, branch_suffix="b")

    assert first.pull_request.id != second.pull_request.id


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
async def test_a_store_is_found_by_name_alone(bitbucket):
    seed_store(bitbucket, team="platform", kv_name="secrets", oidc=[oidc_entry()])

    store = await get_kv_store_operation(bitbucket, "secrets")

    assert store["team"] == "platform"
    assert store["directory"] == "KVs/platform/secrets"


async def test_a_read_returns_both_kinds_oidc_first(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()], k8s=[k8s_entry()])

    entries = await get_kv_access_operation(bitbucket, KV)

    assert [entry["authType"] for entry in entries] == ["oidc", "k8s"]


async def test_a_store_with_no_k8s_file_reads_as_oidc_only(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    assert [e["authType"] for e in await get_kv_access_operation(bitbucket, KV)] == ["oidc"]


async def test_an_unknown_store_is_404(bitbucket):
    with pytest.raises(VaultOperationError) as error:
        await get_kv_store_operation(bitbucket, "nope")

    assert error.value.status_code == 404


async def test_the_404_names_files_the_walk_could_not_classify(bitbucket):
    """Otherwise a values dir in an unexpected shape reads as a deleted store."""
    bitbucket.existing_files["KVs/stray.yaml"] = "access: []\n"

    with pytest.raises(VaultOperationError) as error:
        await get_kv_store_operation(bitbucket, "nope")

    assert "KVs/stray.yaml" in error.value.message


async def test_the_tashtiot_layout_is_not_walked_as_a_store(bitbucket):
    """`Tashtiot/cloudops.yaml` is two segments; a store is three."""
    bitbucket.existing_files["KVs/Tashtiot/cloudops.yaml"] = "access: []\n"
    seed_store(bitbucket, oidc=[oidc_entry()])

    stores = await list_kv_stores_operation(bitbucket)

    assert [s["kv_name"] for s in stores] == [KV]


async def test_listing_reports_team_name_and_directory(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    seed_store(bitbucket, team="platform", kv_name="secrets", oidc=[oidc_entry()])

    stores = await list_kv_stores_operation(bitbucket)

    assert {s["kv_name"] for s in stores} == {KV, "secrets"}
    assert {s["team"] for s in stores} == {TEAM, "platform"}


# --------------------------------------------------------------------------- #
# updating a store
# --------------------------------------------------------------------------- #
async def _update(bitbucket, access, kv_name=KV):
    return await update_kv_mount_operation(
        bitbucket, kv_name, VaultKVUpdate(access=access), branch_suffix="abc123"
    )


async def test_update_replaces_the_grant_list(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    await _update(bitbucket, [
        {"path": "*", "auth_type": "oidc", "dn": OTHER_DN, "capabilities": ["read"]}
    ])

    assert _committed(bitbucket, OIDC_PATH)["access"][0]["dn"] == OTHER_DN


async def test_update_sends_the_optimistic_lock_token_for_an_existing_file(bitbucket):
    """Without it the browse resource rejects an edit as an attempted create."""
    seed_store(bitbucket, oidc=[oidc_entry()])

    await _update(bitbucket, [
        {"path": "*", "auth_type": "oidc", "dn": OTHER_DN, "capabilities": ["read"]}
    ])

    assert bitbucket.source_commit_ids == ["file-commit-sha"]


async def test_adding_the_first_k8s_grant_creates_that_file_without_a_token(bitbucket):
    """The k8s file does not exist yet, so that write is a create, not an edit."""
    seed_store(bitbucket, oidc=[oidc_entry()])

    await _update(bitbucket, [
        {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read", "write"]},
        {"path": "*", "auth_type": "k8s", "name": "sa", "namespace": "ns",
         "cluster": "dev", "capabilities": ["read"]},
    ])

    assert K8S_PATH in bitbucket.committed
    assert bitbucket.source_commit_ids == ["file-commit-sha", None]


async def test_emptying_the_k8s_grants_removes_that_file(bitbucket):
    """An `access: []` file is a shape no create writes, so it is removed instead."""
    seed_store(bitbucket, oidc=[oidc_entry()], k8s=[k8s_entry()])

    await _update(bitbucket, [
        {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read", "write"]}
    ])

    assert bitbucket.deleted == [K8S_PATH]
    assert list(bitbucket.committed) == [OIDC_PATH]


async def test_a_no_op_update_opens_no_pull_request(bitbucket):
    """What keeps repeat requests from filling the values repo with empty diffs."""
    seed_store(bitbucket, oidc=[oidc_entry()])

    result = await _update(bitbucket, [
        {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read", "write"]}
    ])

    assert result.status.value == "Succeeded"
    assert result.pull_request is None
    assert "create_branch" not in bitbucket.calls


async def test_a_reordered_update_is_still_a_no_op(bitbucket):
    """`yaml_data_equals` compares parsed documents, so order is not a change."""
    seed_store(bitbucket, oidc=[oidc_entry(), oidc_entry(dn=OTHER_DN)])

    result = await _update(bitbucket, [
        {"path": "*", "auth_type": "oidc", "dn": OTHER_DN, "capabilities": ["read", "write"]},
        {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read", "write"]},
    ])

    assert result.pull_request is None


async def test_update_of_an_unknown_store_is_404(bitbucket):
    with pytest.raises(VaultOperationError) as error:
        await _update(bitbucket, [
            {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read"]}
        ], kv_name="nope")

    assert error.value.status_code == 404


# --------------------------------------------------------------------------- #
# one grant at a time
# --------------------------------------------------------------------------- #
async def test_adding_a_grant_appends_to_the_matching_file(bitbucket, grant_payload):
    seed_store(bitbucket, oidc=[oidc_entry()], k8s=[k8s_entry()])

    await add_access_entry_operation(bitbucket, KV, grant_payload, branch_suffix="abc123")

    assert [e["dn"] for e in _committed(bitbucket, OIDC_PATH)["access"]] == [DN, OTHER_DN]
    # The other file is not rewritten: a k8s grant never shows up in an oidc diff.
    assert K8S_PATH not in bitbucket.committed


async def test_a_k8s_grant_lands_in_the_k8s_file(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    payload = K8sAccessEntry(
        path="*", auth_type="k8s", name="sa", namespace="ns",
        cluster="dev", capabilities=["read"],
    )

    await add_access_entry_operation(bitbucket, KV, payload, branch_suffix="abc123")

    assert list(bitbucket.committed) == [K8S_PATH]


async def test_adding_a_duplicate_grant_is_409(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    payload = OidcAccessEntry(
        path="*", auth_type="oidc", dn=DN, capabilities=["read"]
    )

    with pytest.raises(VaultOperationError) as error:
        await add_access_entry_operation(bitbucket, KV, payload, branch_suffix="abc123")

    assert error.value.status_code == 409
    assert "create_branch" not in bitbucket.calls


async def test_removing_a_grant_drops_it(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry(), oidc_entry(dn=OTHER_DN)])

    await remove_access_entry_operation(
        bitbucket, KV, oidc_entry(dn=OTHER_DN), branch_suffix="abc123"
    )

    assert [e["dn"] for e in _committed(bitbucket, OIDC_PATH)["access"]] == [DN]


async def test_removing_the_last_grant_of_a_kind_removes_its_file(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()], k8s=[k8s_entry()])

    await remove_access_entry_operation(bitbucket, KV, k8s_entry(), branch_suffix="abc123")

    assert bitbucket.deleted == [K8S_PATH]


async def test_removing_a_grant_that_is_not_there_is_404(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    with pytest.raises(VaultOperationError) as error:
        await remove_access_entry_operation(
            bitbucket, KV, oidc_entry(dn=OTHER_DN), branch_suffix="abc123"
        )

    assert error.value.status_code == 404


async def test_capabilities_do_not_affect_which_grant_is_revoked(bitbucket):
    """A caller revoking a grant need not know what it currently permits."""
    seed_store(bitbucket, oidc=[oidc_entry(capabilities=["read", "write"])])

    await remove_access_entry_operation(
        bitbucket, KV, oidc_entry(capabilities=[]), branch_suffix="abc123"
    )

    assert bitbucket.deleted == [OIDC_PATH]


# --------------------------------------------------------------------------- #
# deleting a store
# --------------------------------------------------------------------------- #
async def test_delete_removes_the_whole_directory(bitbucket):
    """Both files go with it — a store is a directory, and git rm -r takes it whole."""
    seed_store(bitbucket, oidc=[oidc_entry()], k8s=[k8s_entry()])

    result = await delete_kv_store_operation(bitbucket, KV, branch_suffix="abc123")

    assert bitbucket.deleted == [STORE_DIR]
    assert bitbucket.committed == {}
    assert f"to delete {KV}" in result.message


async def test_delete_needs_no_optimistic_lock_token(bitbucket):
    """Git resolves the path from the branch it fetched; the token is Bitbucket's."""
    seed_store(bitbucket, oidc=[oidc_entry()])

    await delete_kv_store_operation(bitbucket, KV, branch_suffix="abc123")

    assert "get_last_commit" not in bitbucket.calls


async def test_delete_leaves_another_store_alone(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    seed_store(bitbucket, team="platform", kv_name="secrets", oidc=[oidc_entry()])

    await delete_kv_store_operation(bitbucket, KV, branch_suffix="abc123")

    assert bitbucket.deleted == [STORE_DIR]


async def test_delete_of_an_unknown_store_is_404(bitbucket):
    with pytest.raises(VaultOperationError) as error:
        await delete_kv_store_operation(bitbucket, "nope", branch_suffix="abc123")

    assert error.value.status_code == 404
    assert "create_branch" not in bitbucket.calls


async def test_a_repeat_delete_is_404(bitbucket):
    """The state is idempotent; the status code says which call did the work."""
    seed_store(bitbucket, oidc=[oidc_entry()])
    await delete_kv_store_operation(bitbucket, KV, branch_suffix="abc123")
    # Simulate the merge landing the removal on the base branch.
    del bitbucket.existing_files[OIDC_PATH]

    with pytest.raises(VaultOperationError) as error:
        await delete_kv_store_operation(bitbucket, KV, branch_suffix="def456")

    assert error.value.status_code == 404


async def test_delete_pull_request_only_stops_at_the_pull_request(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    result = await delete_kv_store_pull_request_operation(
        bitbucket, KV, branch_suffix="abc123"
    )

    assert result.pull_request.state == "OPEN"
    assert "await_builds" not in bitbucket.calls
    assert bitbucket.deleted == [STORE_DIR]
