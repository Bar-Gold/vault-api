"""Bitbucket client contract: exact REST paths, bodies and error mapping.

Driven through a real `httpx.AsyncClient` with respx intercepting, so the URLs and request
bodies asserted here are the ones a real Bitbucket Server would receive.
"""
import json

import httpx
import pytest
import respx
from tashtiot_apis_library.connectors import ExternalServiceError

from app.clients.bitbucket import BitbucketClient, PullRequest

BASE = "https://bitbucket.test"
API = f"{BASE}/rest/api/1.0/projects/INFRA/repos/vault-values"
BRANCH_API = f"{BASE}/rest/branch-utils/1.0/projects/INFRA/repos/vault-values"


@pytest.fixture
def bitbucket():
    client = httpx.AsyncClient(base_url=BASE)
    return BitbucketClient(client, project_key="INFRA", repo_slug="vault-values")


def _pr_payload(pr_id=101, version=0, state="OPEN", merge_commit=None):
    payload = {
        "id": pr_id,
        "version": version,
        "title": "Create Vault KV mount kingmagen/prod/myapp",
        "state": state,
        "fromRef": {"displayId": "vault-kv/prod-myapp-abc"},
        "toRef": {"displayId": "master"},
        "links": {"self": [{"href": f"{BASE}/pull-requests/{pr_id}"}]},
    }
    if merge_commit:
        payload["properties"] = {"mergeCommit": {"id": merge_commit}}
    return payload


# --------------------------------------------------------------------------- #
# branches
# --------------------------------------------------------------------------- #
@respx.mock
async def test_create_branch_posts_start_point(bitbucket):
    route = respx.post(f"{BRANCH_API}/branches").mock(
        return_value=httpx.Response(200, json={"id": "refs/heads/vault-kv/prod-myapp-abc"})
    )

    await bitbucket.create_branch("vault-kv/prod-myapp-abc", "master")

    body = json.loads(route.calls.last.request.content)
    assert body == {"name": "vault-kv/prod-myapp-abc", "startPoint": "refs/heads/master"}


@respx.mock
async def test_delete_branch_sends_full_ref(bitbucket):
    route = respx.delete(f"{BRANCH_API}/branches").mock(return_value=httpx.Response(204))

    await bitbucket.delete_branch("vault-kv/prod-myapp-abc")

    body = json.loads(route.calls.last.request.content)
    assert body == {"name": "refs/heads/vault-kv/prod-myapp-abc"}


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
@respx.mock
async def test_put_file_sends_multipart_with_message_and_branch(bitbucket):
    route = respx.put(f"{API}/browse/kv/prod/myapp.yaml").mock(
        return_value=httpx.Response(200, json={"id": "commit-sha"})
    )

    await bitbucket.put_file(
        path="kv/prod/myapp.yaml",
        branch="vault-kv/prod-myapp-abc",
        content="mount: {}\n",
        message="Create Vault KV mount kingmagen/prod/myapp",
    )

    request = route.calls.last.request
    assert request.headers["content-type"].startswith("multipart/form-data")
    sent = request.content.decode()
    assert "mount: {}" in sent
    assert 'name="message"' in sent
    assert "Create Vault KV mount kingmagen/prod/myapp" in sent
    assert 'name="branch"' in sent
    assert "vault-kv/prod-myapp-abc" in sent
    # sourceCommitId is only for edits, so a create must not send it.
    assert "sourceCommitId" not in sent


@respx.mock
async def test_put_file_includes_source_commit_id_when_editing(bitbucket):
    route = respx.put(f"{API}/browse/kv/prod/myapp.yaml").mock(
        return_value=httpx.Response(200, json={"id": "commit-sha"})
    )

    await bitbucket.put_file(
        path="kv/prod/myapp.yaml",
        branch="master",
        content="mount: {}\n",
        message="edit",
        source_commit_id="old-sha",
    )

    sent = route.calls.last.request.content.decode()
    assert "sourceCommitId" in sent
    assert "old-sha" in sent


# --------------------------------------------------------------------------- #
# listing — the cross-file duplicate scan depends on this
# --------------------------------------------------------------------------- #
@respx.mock
async def test_list_files_returns_paths_relative_to_the_directory(bitbucket):
    route = respx.get(f"{API}/files/kv").mock(
        return_value=httpx.Response(
            200,
            json={
                "values": ["payments.yaml", "infra.yaml"],
                "size": 2,
                "isLastPage": True,
            },
        )
    )

    files = await bitbucket.list_files("kv", at="master")

    assert files == ["payments.yaml", "infra.yaml"]
    assert route.calls.last.request.url.params["at"] == "master"


@respx.mock
async def test_list_files_follows_pagination(bitbucket):
    """Bitbucket will not return a large directory in one page."""
    pages = [
        httpx.Response(
            200,
            json={
                "values": ["a.yaml", "b.yaml"],
                "size": 2,
                "isLastPage": False,
                "nextPageStart": 2,
            },
        ),
        httpx.Response(
            200, json={"values": ["c.yaml"], "size": 1, "isLastPage": True}
        ),
    ]
    route = respx.get(f"{API}/files/kv").mock(side_effect=pages)

    files = await bitbucket.list_files("kv")

    assert files == ["a.yaml", "b.yaml", "c.yaml"]
    assert route.call_count == 2
    assert route.calls[1].request.url.params["start"] == "2"


@respx.mock
async def test_list_files_treats_a_missing_directory_as_empty(bitbucket):
    """The values dir legitimately does not exist before the first store is created."""
    respx.get(f"{API}/files/kv").mock(
        return_value=httpx.Response(404, json={"errors": [{"message": "not found"}]})
    )

    assert await bitbucket.list_files("kv") == []


@respx.mock
async def test_list_files_propagates_a_real_failure(bitbucket):
    """Only a 404 is benign; a 500 must not look like an empty directory."""
    respx.get(f"{API}/files/kv").mock(
        return_value=httpx.Response(500, json={"errors": [{"message": "boom"}]})
    )

    with pytest.raises(ExternalServiceError) as exc_info:
        await bitbucket.list_files("kv")

    assert exc_info.value.status_code == 500


@respx.mock
async def test_get_file_content_returns_raw_text_at_ref(bitbucket):
    route = respx.get(f"{API}/raw/kv/prod/myapp.yaml").mock(
        return_value=httpx.Response(200, text="mount:\n  path: kingmagen/prod/myapp\n")
    )

    content = await bitbucket.get_file_content("kv/prod/myapp.yaml", at="master")

    assert "kingmagen/prod/myapp" in content
    assert route.calls.last.request.url.params["at"] == "master"


@respx.mock
async def test_missing_file_raises_external_service_error_404(bitbucket):
    respx.get(f"{API}/raw/kv/prod/nope.yaml").mock(
        return_value=httpx.Response(404, json={"errors": [{"message": "The path does not exist"}]})
    )

    with pytest.raises(ExternalServiceError) as exc_info:
        await bitbucket.get_file_content("kv/prod/nope.yaml")

    assert exc_info.value.status_code == 404
    assert "does not exist" in exc_info.value.detail


# --------------------------------------------------------------------------- #
# pull requests
# --------------------------------------------------------------------------- #
@respx.mock
async def test_create_pull_request_body_and_parsing(bitbucket):
    route = respx.post(f"{API}/pull-requests").mock(
        return_value=httpx.Response(201, json=_pr_payload(pr_id=101, version=0))
    )

    pull_request = await bitbucket.create_pull_request(
        title="Create Vault KV mount kingmagen/prod/myapp",
        description="body",
        from_branch="vault-kv/prod-myapp-abc",
        to_branch="master",
        reviewers=["alice", "bob"],
    )

    body = json.loads(route.calls.last.request.content)
    assert body["fromRef"]["id"] == "refs/heads/vault-kv/prod-myapp-abc"
    assert body["toRef"]["id"] == "refs/heads/master"
    assert body["fromRef"]["repository"]["slug"] == "vault-values"
    assert body["fromRef"]["repository"]["project"]["key"] == "INFRA"
    assert body["reviewers"] == [{"user": {"name": "alice"}}, {"user": {"name": "bob"}}]

    assert pull_request.id == 101
    assert pull_request.version == 0
    assert pull_request.state == "OPEN"
    assert pull_request.url == f"{BASE}/pull-requests/101"


@respx.mock
async def test_create_pull_request_omits_reviewers_when_none(bitbucket):
    route = respx.post(f"{API}/pull-requests").mock(
        return_value=httpx.Response(201, json=_pr_payload())
    )

    await bitbucket.create_pull_request(
        title="t", description="d", from_branch="b", to_branch="master", reviewers=[]
    )

    assert "reviewers" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_merge_sends_version_and_parses_merge_commit(bitbucket):
    route = respx.post(f"{API}/pull-requests/101/merge").mock(
        return_value=httpx.Response(
            200, json=_pr_payload(state="MERGED", version=3, merge_commit="deadbeef")
        )
    )

    merged = await bitbucket.merge_pull_request(101, 3)

    assert route.calls.last.request.url.params["version"] == "3"
    assert merged.state == "MERGED"
    assert merged.merge_commit == "deadbeef"


@respx.mock
async def test_decline_sends_version(bitbucket):
    route = respx.post(f"{API}/pull-requests/101/decline").mock(
        return_value=httpx.Response(200, json=_pr_payload(state="DECLINED", version=2))
    )

    declined = await bitbucket.decline_pull_request(101, 2)

    assert route.calls.last.request.url.params["version"] == "2"
    assert declined.state == "DECLINED"


@respx.mock
async def test_get_pull_request_reads_current_version(bitbucket):
    respx.get(f"{API}/pull-requests/101").mock(
        return_value=httpx.Response(200, json=_pr_payload(version=7))
    )

    assert (await bitbucket.get_pull_request(101)).version == 7


@respx.mock
async def test_conflict_on_merge_is_mapped_with_bitbucket_message(bitbucket):
    respx.post(f"{API}/pull-requests/101/merge").mock(
        return_value=httpx.Response(
            409, json={"errors": [{"message": "The pull request has conflicts"}]}
        )
    )

    with pytest.raises(ExternalServiceError) as exc_info:
        await bitbucket.merge_pull_request(101, 1)

    assert exc_info.value.status_code == 409
    assert exc_info.value.service_name == "bitbucket"
    assert "conflicts" in exc_info.value.detail


@respx.mock
async def test_json_error_body_without_an_errors_key_is_still_reported(bitbucket):
    respx.post(f"{API}/pull-requests").mock(
        return_value=httpx.Response(400, json={"message": "branch is not mergeable"})
    )

    with pytest.raises(ExternalServiceError) as exc_info:
        await bitbucket.create_pull_request(
            title="t", description="d", from_branch="b", to_branch="master"
        )

    assert "not mergeable" in exc_info.value.detail


@respx.mock
async def test_non_json_error_body_still_maps(bitbucket):
    respx.post(f"{API}/pull-requests").mock(return_value=httpx.Response(500, text="gateway boom"))

    with pytest.raises(ExternalServiceError) as exc_info:
        await bitbucket.create_pull_request(
            title="t", description="d", from_branch="b", to_branch="master"
        )

    assert exc_info.value.status_code == 500
    assert "boom" in exc_info.value.detail


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_pull_request_parsing_tolerates_missing_optional_fields():
    pull_request = PullRequest.from_api({"id": 7})
    assert pull_request.id == 7
    assert pull_request.version == 0
    assert pull_request.url is None
    assert pull_request.merge_commit is None
    assert pull_request.state == "OPEN"


# --------------------------------------------------------------------------- #
# last commit (the optimistic-lock token for edits)
# --------------------------------------------------------------------------- #
@respx.mock
async def test_get_last_commit_returns_the_newest_id(bitbucket):
    route = respx.get(f"{API}/commits").mock(
        return_value=httpx.Response(
            200, json={"values": [{"id": "abc123"}, {"id": "older"}]}
        )
    )

    assert await bitbucket.get_last_commit("kv/prod/myapp.yaml", at="master") == "abc123"

    request = route.calls[0].request
    assert request.url.params["path"] == "kv/prod/myapp.yaml"
    assert request.url.params["until"] == "master"
    assert request.url.params["limit"] == "1"


@respx.mock
async def test_get_last_commit_omits_until_without_a_ref(bitbucket):
    route = respx.get(f"{API}/commits").mock(
        return_value=httpx.Response(200, json={"values": [{"id": "abc123"}]})
    )

    await bitbucket.get_last_commit("kv/prod/myapp.yaml")

    assert "until" not in route.calls[0].request.url.params


@respx.mock
async def test_get_last_commit_is_none_for_a_path_with_no_history(bitbucket):
    """No history means the write is a create, not an edit — the caller sends no token."""
    respx.get(f"{API}/commits").mock(return_value=httpx.Response(200, json={"values": []}))

    assert await bitbucket.get_last_commit("kv/prod/new.yaml", at="master") is None


@respx.mock
async def test_get_last_commit_maps_a_failure(bitbucket):
    respx.get(f"{API}/commits").mock(
        return_value=httpx.Response(404, json={"errors": [{"message": "Repo not found"}]})
    )

    with pytest.raises(ExternalServiceError) as error:
        await bitbucket.get_last_commit("kv/prod/myapp.yaml", at="master")

    assert error.value.status_code == 404
    assert "Repo not found" in error.value.detail
