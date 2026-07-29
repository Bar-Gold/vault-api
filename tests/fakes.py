"""Duck-typed stand-ins for the Bitbucket and Woodpecker clients.

The clients themselves are covered end-to-end against their real REST shapes in
`tests/clients/` (respx). These fakes exist so the operation tests can assert on
*sequencing and rollback* — which calls happen, in what order, and what gets undone when a
step fails — without any HTTP in the way.
"""

from typing import Any, Dict, List, Optional

from tashtiot_apis_library.connectors import ExternalServiceError

from app.clients.bitbucket import PullRequest
from app.clients.woodpecker import Pipeline


def make_pipeline(number: int = 1, status: str = "success", event: str = "pull_request", **kwargs) -> Pipeline:
    return Pipeline(number=number, status=status, event=event, **kwargs)


class FakeBitbucket:
    """Records every call; `fail_on` injects a failure into a chosen method."""

    def __init__(
        self,
        existing_files: Optional[Dict[str, str]] = None,
        fail_on: Optional[Dict[str, Exception]] = None,
        merge_commit: Optional[str] = "merge-sha-1",
    ) -> None:
        self.calls: List[str] = []
        self.existing_files = dict(existing_files or {})
        self.fail_on = dict(fail_on or {})
        self.merge_commit = merge_commit
        self.pull_requests: Dict[int, PullRequest] = {}
        self.committed: Dict[str, str] = {}
        self._next_id = 101

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail_on:
            raise self.fail_on[name]

    async def get_file_content(self, path: str, at: Optional[str] = None) -> str:
        self._record("get_file_content")
        if path not in self.existing_files:
            raise ExternalServiceError(
                service_name="bitbucket", detail=f"{path} not found", status_code=404
            )
        return self.existing_files[path]

    async def create_branch(self, name: str, start_point: str) -> Dict[str, Any]:
        self._record("create_branch")
        return {"id": f"refs/heads/{name}"}

    async def delete_branch(self, name: str) -> None:
        self._record("delete_branch")

    async def put_file(self, path: str, branch: str, content: str, message: str, **kwargs) -> Dict[str, Any]:
        self._record("put_file")
        self.committed[path] = content
        return {"id": "commit-sha"}

    async def create_pull_request(
        self, title: str, description: str, from_branch: str, to_branch: str, reviewers=None
    ) -> PullRequest:
        self._record("create_pull_request")
        pull_request = PullRequest(
            id=self._next_id,
            version=0,
            title=title,
            from_branch=from_branch,
            to_branch=to_branch,
            state="OPEN",
            url=f"https://bitbucket.test/pr/{self._next_id}",
        )
        self.pull_requests[pull_request.id] = pull_request
        self._next_id += 1
        return pull_request

    async def get_pull_request(self, pull_request_id: int) -> PullRequest:
        self._record("get_pull_request")
        stored = self.pull_requests[pull_request_id]
        # CI status updates bump Bitbucket's optimistic-locking version; simulate that so
        # the operation is forced to re-read it before merging/declining.
        stored.version += 1
        return stored

    async def merge_pull_request(self, pull_request_id: int, version: int) -> PullRequest:
        self._record("merge_pull_request")
        stored = self.pull_requests[pull_request_id]
        stored.state = "MERGED"
        stored.merge_commit = self.merge_commit
        return stored

    async def decline_pull_request(self, pull_request_id: int, version: int) -> PullRequest:
        self._record("decline_pull_request")
        stored = self.pull_requests[pull_request_id]
        stored.state = "DECLINED"
        return stored


class FakeWoodpecker:
    """Serves a scripted list of pipelines and a scripted queue of `await_pipeline` results."""

    def __init__(
        self,
        existing: Optional[List[Pipeline]] = None,
        results: Optional[List[Any]] = None,
    ) -> None:
        self.existing = list(existing or [])
        self.results = list(results or [])
        self.matchers: List[Any] = []
        self.calls: List[str] = []

    async def list_pipelines(self, per_page: int = 50) -> List[Pipeline]:
        self.calls.append("list_pipelines")
        return self.existing

    async def await_pipeline(self, matches, **kwargs) -> Pipeline:
        self.calls.append("await_pipeline")
        self.matchers.append(matches)
        if not self.results:
            raise AssertionError("FakeWoodpecker.await_pipeline called with no scripted result")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
