"""The PR-only create: branch, commit, open a pull request — and stop.

What separates it from the full chain is everything it does *not* do: no pipeline is
awaited, nothing is merged, and nothing reaches the base branch. Those absences are the
contract, so most of these tests assert on what did not happen.
"""
import pytest
import yaml
from tashtiot_apis_library.connectors import ExternalServiceError

from app.v1.vault.conf import config
from app.v1.vault.operations import (
    VaultOperationError,
    create_kv_pull_request_operation,
)
from app.v1.vault.schemas import VaultKVCreate

KV = "myapp"
VALUES_PATH = "kv/myapp.yaml"
PR_URL = f"{config.API_PREFIX}/pull-request"


async def _open(bitbucket, payload, **kwargs):
    return await create_kv_pull_request_operation(
        bitbucket, payload, branch_suffix="abc123", **kwargs
    )


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #
async def test_returns_the_open_pull_request(payload, bitbucket):
    result = await _open(bitbucket, payload)

    assert result.status.value == "Succeeded"
    assert result.pull_request.id == 101
    assert result.pull_request.state == "OPEN"
    assert f"Opened pull request 101 for {KV}" in result.message
    assert "not merged" in result.message


async def test_reports_no_pipelines(payload, bitbucket):
    """Nothing ran, so neither pipeline field may be populated."""
    result = await _open(bitbucket, payload)

    assert result.validation_pipeline is None
    assert result.deploy_pipeline is None


async def test_call_order_stops_at_the_pull_request(payload, bitbucket):
    await _open(bitbucket, payload)

    assert bitbucket.calls == [
        "get_file_content",  # duplicate guard
        "create_branch",
        "put_file",
        "create_pull_request",
    ]


async def test_never_merges(payload, bitbucket):
    """The whole point: the base branch is untouched."""
    await _open(bitbucket, payload)

    assert "merge_pull_request" not in bitbucket.calls
    assert "get_pull_request" not in bitbucket.calls
    assert "decline_pull_request" not in bitbucket.calls


async def test_commits_the_same_document_as_a_full_create(payload, bitbucket):
    await _open(bitbucket, payload)

    assert yaml.safe_load(bitbucket.committed[VALUES_PATH]) == {
        "kvname": KV,
        "description": payload.kv_description,
    }


async def test_commits_without_an_optimistic_lock_token(payload, bitbucket):
    """This is a create, not an edit — sourceCommitId would make Bitbucket reject it."""
    await _open(bitbucket, payload)

    assert bitbucket.source_commit_ids == [None]


async def test_branch_is_flattened_for_a_multi_segment_name(bitbucket):
    payload = VaultKVCreate(kv_name="payments/vault-secrets", kv_description="x")

    result = await _open(bitbucket, payload)

    stored = bitbucket.pull_requests[result.pull_request.id]
    assert stored.from_branch == "vault-kv/payments-vault-secrets-abc123"
    assert "kv/payments/vault-secrets.yaml" in bitbucket.committed


# --------------------------------------------------------------------------- #
# guards and rollbacks — inherited from the shared helper, so they must still hold
# --------------------------------------------------------------------------- #
async def test_duplicate_is_409_and_touches_nothing(payload, bitbucket):
    bitbucket.existing_files[VALUES_PATH] = "kvname: myapp\ndescription: already here\n"

    with pytest.raises(VaultOperationError) as error:
        await _open(bitbucket, payload)

    assert error.value.status_code == 409
    assert "already exists" in error.value.message
    assert bitbucket.calls == ["get_file_content"]


async def test_a_second_call_opens_a_second_pull_request(payload, bitbucket):
    """Deliberate, and a real consequence of not merging.

    The duplicate guard reads the base branch. Because this endpoint never merges, the
    file never lands there, so a repeat call cannot be seen as a duplicate — it opens
    another pull request. Catching it would mean listing open pull requests, which is a
    different and inherently racy check. Reviewers close the loser.
    """
    first = await _open(bitbucket, payload)
    bitbucket.calls.clear()

    second = await create_kv_pull_request_operation(
        bitbucket, payload, branch_suffix="def456"
    )

    assert first.pull_request.id != second.pull_request.id
    assert second.status.value == "Succeeded"
    # Both are still open; nothing was declined or merged behind the caller's back.
    assert {pr.state for pr in bitbucket.pull_requests.values()} == {"OPEN"}


async def test_failed_commit_deletes_the_branch(payload, bitbucket):
    bitbucket.fail_on["put_file"] = ExternalServiceError(
        service_name="bitbucket", detail="boom", status_code=500
    )

    with pytest.raises(ExternalServiceError):
        await _open(bitbucket, payload)

    assert "delete_branch" in bitbucket.calls
    assert "create_pull_request" not in bitbucket.calls


async def test_failed_pull_request_deletes_the_branch(payload, bitbucket):
    bitbucket.fail_on["create_pull_request"] = ExternalServiceError(
        service_name="bitbucket", detail="boom", status_code=500
    )

    with pytest.raises(ExternalServiceError):
        await _open(bitbucket, payload)

    assert "delete_branch" in bitbucket.calls


async def test_a_non_404_duplicate_check_failure_propagates(payload, bitbucket):
    """Only a 404 means "not there"; anything else must not be read as a free pass."""
    bitbucket.fail_on["get_file_content"] = ExternalServiceError(
        service_name="bitbucket", detail="boom", status_code=503
    )

    with pytest.raises(ExternalServiceError):
        await _open(bitbucket, payload)

    assert "create_branch" not in bitbucket.calls


# --------------------------------------------------------------------------- #
# through the app
# --------------------------------------------------------------------------- #
def test_route_requires_a_token(client):
    body = {"kv_name": KV, "kv_description": "x"}

    assert client.post(PR_URL, json=body).status_code == 401


def test_route_returns_201_and_leaves_the_pr_open(client, auth_headers, bitbucket, woodpecker):
    response = client.post(
        PR_URL, json={"kv_name": KV, "kv_description": "payments secrets"}, headers=auth_headers
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "Succeeded"
    assert body["pull_request"]["state"] == "OPEN"
    assert body["validation_pipeline"] is None
    assert body["deploy_pipeline"] is None
    # The fake would raise if a pipeline were awaited without a scripted result; assert
    # directly that Woodpecker was never consulted at all.
    assert woodpecker.calls == []


def test_route_rejects_a_bad_name(client, auth_headers, bitbucket):
    response = client.post(
        PR_URL, json={"kv_name": "My_App", "kv_description": "x"}, headers=auth_headers
    )

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_route_returns_409_for_a_duplicate(client, auth_headers, bitbucket):
    bitbucket.existing_files[VALUES_PATH] = "kvname: myapp\ndescription: already here\n"

    response = client.post(
        PR_URL, json={"kv_name": KV, "kv_description": "x"}, headers=auth_headers
    )

    assert response.status_code == 409


def test_the_fixed_segment_is_not_swallowed_by_the_path_converter(
    client, auth_headers, bitbucket
):
    """POST /pull-request must hit this endpoint, not be read as a kv_name."""
    response = client.post(
        PR_URL, json={"kv_name": KV, "kv_description": "x"}, headers=auth_headers
    )

    assert response.status_code == 201
    # A GET on the same path is a *read* of a KV literally named "pull-request", which is
    # a legal name — so the two coexist without either shadowing the other.
    assert client.get(PR_URL, headers=auth_headers).status_code == 404
