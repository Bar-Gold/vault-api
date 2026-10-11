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
    delete_kv_store_operation,
    get_kv_store_operation,
    list_kv_stores_operation,
    remove_access_entry_operation,
    to_document_entry,
    to_revoked_entry,
    update_kv_mount_operation,
)
from app.v1.vault.schemas import (
    K8sAccessEntryRequest,
    OidcAccessEntryRequest,
    VaultKVUpdate,
)
from app.clients.bitbucket import PullRequest
from tests.fakes import failing, passing
from tests.v1.vault.conftest import (
    DN,
    K8S_PATH,
    KV,
    METADATA,
    OIDC_PATH,
    OTHER_DN,
    STORE_DIR,
    TEAM,
    delete_request,
    k8s_entry,
    k8s_revoke,
    oidc_entry,
    oidc_revoke,
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


async def test_a_store_with_no_oidc_grants_writes_no_oidc_file(bitbucket, k8s_only_payload):
    """The mirror of the above, and the direction that had no test.

    Either kind on its own is a valid store; only an *empty* access list is refused. A
    create carrying k8s grants alone must therefore commit exactly one file, and it must
    be the k8s one.
    """
    await _create(bitbucket, k8s_only_payload)

    assert list(bitbucket.committed) == [K8S_PATH]
    assert _committed(bitbucket, K8S_PATH)["access"][0]["authType"] == "k8s"


async def test_each_kind_lands_in_the_file_named_for_it(bitbucket, payload):
    """oidc grants to oidc-access.yaml, k8s grants to k8s-access.yaml — never crossed."""
    await _create(bitbucket, payload)

    assert OIDC_PATH.endswith("/oidc-access.yaml")
    assert K8S_PATH.endswith("/k8s-access.yaml")
    for path, expected in ((OIDC_PATH, "oidc"), (K8S_PATH, "k8s")):
        kinds = {entry["authType"] for entry in _committed(bitbucket, path)["access"]}
        assert kinds == {expected}


async def test_both_files_sit_in_the_team_and_store_directory(bitbucket, payload):
    """The committed paths are '<values>/<team>/<kv_name>/<file>' and nothing else."""
    await _create(bitbucket, payload)

    for path in bitbucket.committed:
        assert path.startswith(f"{STORE_DIR}/")
        assert path.split("/")[-3:-1] == [TEAM, KV]


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
        "list_pull_requests",  # ...and the half of it that reads unmerged branches
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
# uniqueness against unmerged pull requests
#
# The base-branch scan above cannot see a store that is still only proposed, and since
# nothing here merges that is where every new store lives until a human takes it. Without
# this half, a repeat create just opens a second pull request for the same name.
# --------------------------------------------------------------------------- #
def _open_create_pr(bitbucket, pull_request_id=900, team=TEAM, kv_name=KV,
                    branch=None, files=("oidc-access.yaml",)):
    """Stage somebody else's open pull request that creates a store."""
    bitbucket.open_pull_requests.append(
        PullRequest(
            id=pull_request_id,
            version=0,
            title=f"Create KV store {kv_name} for {team}",
            from_branch=branch or f"vault-kv/{team}-{kv_name}-deadbeef",
            to_branch="master",
            state="OPEN",
        )
    )
    bitbucket.pull_request_changes[pull_request_id] = [
        f"KVs/{team}/{kv_name}/{name}" for name in files
    ]


async def test_a_name_claimed_by_an_open_pull_request_is_409(bitbucket, payload):
    _open_create_pr(bitbucket)

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, payload)

    assert error.value.status_code == 409
    assert "900" in error.value.message
    assert "create_branch" not in bitbucket.calls


async def test_the_claim_is_found_under_any_team(bitbucket, payload):
    """Names are global, so an open pull request creating it for someone else still wins."""
    _open_create_pr(bitbucket, team="platform")

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, payload)

    assert error.value.status_code == 409


async def test_a_k8s_only_claim_is_found_too(bitbucket, payload):
    """A create writes one file per non-empty kind, so the claim may be either name."""
    _open_create_pr(bitbucket, files=("k8s-access.yaml",))

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, payload)

    assert error.value.status_code == 409


async def test_an_open_pull_request_for_another_store_does_not_collide(bitbucket, payload):
    _open_create_pr(bitbucket, kv_name="other")

    assert (await _create(bitbucket, payload)).status.value == "Succeeded"


async def test_a_pull_request_outside_the_branch_prefix_is_not_even_read(bitbucket, payload):
    """The prefix filter is what keeps this from costing a request per open pull request
    in a busy repo — somebody else's unrelated branch is skipped without asking."""
    _open_create_pr(bitbucket, branch="feature/unrelated")

    assert (await _create(bitbucket, payload)).status.value == "Succeeded"
    assert "get_pull_request_changes" not in bitbucket.calls


async def test_a_pull_request_touching_no_store_file_does_not_collide(bitbucket, payload):
    """Same rule as the store walk: three segments under the values dir, ending in one of
    the two access files. A Tashtiot file is two, and a README is neither."""
    _open_create_pr(bitbucket, pull_request_id=901)
    bitbucket.pull_request_changes[901] = [
        f"KVs/Tashtiot/{KV}.yaml",
        f"KVs/{TEAM}/{KV}/README.md",
        f"KVs/{TEAM}/{KV}/nested/oidc-access.yaml",
        f"other-dir/{TEAM}/{KV}/oidc-access.yaml",
    ]

    assert (await _create(bitbucket, payload)).status.value == "Succeeded"


async def test_the_base_branch_scan_still_runs_first(bitbucket, payload):
    """An existing store is the cheaper answer — no pull request listing needed."""
    seed_store(bitbucket, oidc=[oidc_entry()])

    with pytest.raises(VaultOperationError):
        await _create(bitbucket, payload)

    assert "list_pull_requests" not in bitbucket.calls


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
# what a rolled-back failure *reports*
#
# The call order above proves the rollback ran. These prove the response describes the
# world it left behind: the pull request the caller is told about was declined, and its
# branch is gone, so a caller keying off `state` is not sent to review a closed PR.
# --------------------------------------------------------------------------- #
async def test_a_red_build_reports_the_pull_request_as_declined(bitbucket, payload):
    bitbucket.builds = [failing()]

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, payload)

    assert error.value.pull_request is not None
    assert error.value.pull_request.state == "DECLINED"
    assert error.value.to_response().pull_request.state == "DECLINED"


async def test_a_build_timeout_reports_the_pull_request_as_declined(bitbucket, payload):
    from app.clients.bitbucket import BuildTimeoutError

    bitbucket.builds = [BuildTimeoutError("nothing reported", [])]

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, payload)

    assert error.value.pull_request.state == "DECLINED"


async def test_a_failed_decline_still_reports_the_pull_request_as_open(bitbucket, payload):
    """The inverse lie is just as bad: if the decline failed, the PR really is still open.

    `_decline_and_cleanup` swallows its own failure by design — a rollback error must not
    replace the original one — so the state it reports has to come from what actually
    happened, not from having attempted it.
    """
    bitbucket.builds = [failing()]
    bitbucket.fail_on["decline_pull_request"] = RuntimeError("bitbucket said no")

    with pytest.raises(VaultOperationError) as error:
        await _create(bitbucket, payload)

    assert error.value.pull_request.state == "OPEN"
    # The original failure survives; the rollback's own failure is logged, not raised.
    assert "Validation did not pass" in error.value.message




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

    store = await get_kv_store_operation(bitbucket, KV)

    assert [entry["authType"] for entry in store["access"]] == ["oidc", "k8s"]


async def test_a_store_with_no_k8s_file_reads_as_oidc_only(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    store = await get_kv_store_operation(bitbucket, KV)

    assert [entry["authType"] for entry in store["access"]] == ["oidc"]


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
        bitbucket,
        kv_name,
        VaultKVUpdate(access=access, metadata=METADATA),
        branch_suffix="abc123",
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
    payload = K8sAccessEntryRequest(
        path="*", auth_type="k8s", name="sa", namespace="ns",
        cluster="dev", capabilities=["read"], metadata=METADATA,
    )

    await add_access_entry_operation(bitbucket, KV, payload, branch_suffix="abc123")

    assert list(bitbucket.committed) == [K8S_PATH]


async def test_adding_a_duplicate_grant_is_409(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    payload = OidcAccessEntryRequest(
        path="*", auth_type="oidc", dn=DN, capabilities=["read"], metadata=METADATA
    )

    with pytest.raises(VaultOperationError) as error:
        await add_access_entry_operation(bitbucket, KV, payload, branch_suffix="abc123")

    assert error.value.status_code == 409
    assert "create_branch" not in bitbucket.calls


async def test_removing_a_grant_drops_it(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry(), oidc_entry(dn=OTHER_DN)])

    await remove_access_entry_operation(
        bitbucket, KV, oidc_revoke(dn=OTHER_DN), branch_suffix="abc123"
    )

    assert [e["dn"] for e in _committed(bitbucket, OIDC_PATH)["access"]] == [DN]


async def test_removing_the_last_grant_of_a_kind_removes_its_file(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()], k8s=[k8s_entry()])

    await remove_access_entry_operation(bitbucket, KV, k8s_revoke(), branch_suffix="abc123")

    assert bitbucket.deleted == [K8S_PATH]


async def test_removing_a_grant_that_is_not_there_is_404(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    with pytest.raises(VaultOperationError) as error:
        await remove_access_entry_operation(
            bitbucket, KV, oidc_revoke(dn=OTHER_DN), branch_suffix="abc123"
        )

    assert error.value.status_code == 404


async def test_capabilities_do_not_affect_which_grant_is_revoked(bitbucket):
    """A caller revoking a grant need not know what it currently permits — a revoke
    carries no capabilities at all."""
    seed_store(bitbucket, oidc=[oidc_entry(capabilities=["read", "write"])])

    await remove_access_entry_operation(bitbucket, KV, oidc_revoke(), branch_suffix="abc123")

    assert bitbucket.deleted == [OIDC_PATH]


# --------------------------------------------------------------------------- #
# deleting a store
# --------------------------------------------------------------------------- #
async def test_delete_removes_the_whole_directory(bitbucket):
    """Both files go with it — a store is a directory, and git rm -r takes it whole."""
    seed_store(bitbucket, oidc=[oidc_entry()], k8s=[k8s_entry()])

    result = await delete_kv_store_operation(bitbucket, KV, delete_request(), branch_suffix="abc123")

    assert bitbucket.deleted == [STORE_DIR]
    assert bitbucket.committed == {}
    assert f"to delete {KV}" in result.message


async def test_delete_needs_no_optimistic_lock_token(bitbucket):
    """Git resolves the path from the branch it fetched; the token is Bitbucket's."""
    seed_store(bitbucket, oidc=[oidc_entry()])

    await delete_kv_store_operation(bitbucket, KV, delete_request(), branch_suffix="abc123")

    assert "get_last_commit" not in bitbucket.calls


async def test_delete_leaves_another_store_alone(bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    seed_store(bitbucket, team="platform", kv_name="secrets", oidc=[oidc_entry()])

    await delete_kv_store_operation(bitbucket, KV, delete_request(), branch_suffix="abc123")

    assert bitbucket.deleted == [STORE_DIR]


async def test_delete_of_an_unknown_store_is_404(bitbucket):
    with pytest.raises(VaultOperationError) as error:
        await delete_kv_store_operation(bitbucket, "nope", delete_request(), branch_suffix="abc123")

    assert error.value.status_code == 404
    assert "create_branch" not in bitbucket.calls


async def test_a_repeat_delete_is_404(bitbucket):
    """The state is idempotent; the status code says which call did the work."""
    seed_store(bitbucket, oidc=[oidc_entry()])
    await delete_kv_store_operation(bitbucket, KV, delete_request(), branch_suffix="abc123")
    # Simulate the merge landing the removal on the base branch.
    del bitbucket.existing_files[OIDC_PATH]

    with pytest.raises(VaultOperationError) as error:
        await delete_kv_store_operation(bitbucket, KV, delete_request(), branch_suffix="def456")

    assert error.value.status_code == 404


# --------------------------------------------------------------------------- #
# metadata: request-only, and the pull-request description is its only home
# --------------------------------------------------------------------------- #
def test_to_document_entry_drops_the_metadata():
    """The single bridge from request model to document dict reads named fields only.

    This is the seam that makes the whole guarantee structural rather than incidental: a
    request entry carries `metadata`, and nothing downstream ever sees it.
    """
    oidc = to_document_entry(
        OidcAccessEntryRequest(
            path="*", auth_type="oidc", dn=DN, capabilities=["read"], metadata=METADATA
        )
    )
    k8s = to_document_entry(
        K8sAccessEntryRequest(
            path="*", auth_type="k8s", name="sa", namespace="ns", cluster="dev",
            capabilities=["read"], metadata=METADATA,
        )
    )

    assert set(oidc) == {"path", "authType", "dn", "capabilities"}
    assert set(k8s) == {"path", "authType", "name", "namespace", "cluster", "capabilities"}


def test_to_revoked_entry_drops_the_metadata():
    """The second bridge reads named fields only, exactly like the first — and builds the
    identity with no capabilities, since a revoke carries none."""
    oidc = to_revoked_entry(oidc_revoke())
    k8s = to_revoked_entry(k8s_revoke())

    assert oidc == oidc_entry(capabilities=())
    assert k8s == k8s_entry(capabilities=())


async def test_a_revoke_and_a_delete_description_carry_the_metadata_block(bitbucket):
    """Every route but the reads takes the block, the `DELETE`s included, so the pull
    requests they open are audited like every other."""
    seed_store(bitbucket, oidc=[oidc_entry(), oidc_entry(dn=OTHER_DN)])

    await remove_access_entry_operation(
        bitbucket, KV, oidc_revoke(dn=OTHER_DN), branch_suffix="abc123"
    )
    assert "  - island: island-a\n" in bitbucket.descriptions[-1]

    bitbucket.builds = [passing()]
    await delete_kv_store_operation(bitbucket, KV, delete_request(), branch_suffix="abc123")
    assert "  - island: island-a\n" in bitbucket.descriptions[-1]


async def test_a_create_description_carries_the_metadata_block(bitbucket, payload):
    """Between `change` and `grants`, in declaration order, never sorted."""
    await _create(bitbucket, payload)

    description = bitbucket.descriptions[-1]
    block = (
        "- metadata:\n"
        "  - project: cloudops\n"
        "  - network: internal\n"
        "  - region: il-central\n"
        "  - space: shared\n"
        "  - environment: production\n"
        "  - island: island-a\n"
    )

    assert block in description
    assert description.index("- change:") < description.index("- metadata:")
    assert description.index("- metadata:") < description.index("- grants:")
