"""Duck-typed stand-in for the Bitbucket client.

The client itself is covered end-to-end against the real REST shapes in `tests/clients/`
(respx). This fake exists so the operation tests can assert on *sequencing and rollback* —
which calls happen, in what order, and what gets undone when a step fails — without any
HTTP in the way.

There is only one fake now: Bitbucket is the only upstream, because the CI gates read the
build statuses the pipelines post into Bitbucket rather than asking a CI server.
"""

from typing import Any, Dict, FrozenSet, List, Optional

from tashtiot_apis_library.connectors import ExternalServiceError

from app.clients.bitbucket import BuildStatus, PullRequest


def make_build(
    key: str = "ci/woodpecker/pr/build", state: str = "SUCCESSFUL", **kwargs
) -> BuildStatus:
    return BuildStatus(key=key, state=state, **kwargs)


def passing(key: str = "ci/woodpecker/pr/build") -> List[BuildStatus]:
    """The usual scripted gate result: one green build."""
    return [make_build(key=key, state="SUCCESSFUL")]


def failing(key: str = "ci/woodpecker/pr/build") -> List[BuildStatus]:
    return [make_build(key=key, state="FAILED", url="https://ci.test/1")]


class FakeBitbucket:
    """Records every call; `fail_on` injects a failure into a chosen method.

    `builds` is a **queue** popped once per `await_builds`, exactly as the pipeline results
    used to be: element 0 is the validation gate and element 1 the deploy gate. An
    `Exception` in the list is raised instead of returned, which is how a timeout or a red
    build is staged. Running dry is an `AssertionError`, so an unexpected extra gate fails
    loudly rather than silently passing.
    """

    def __init__(
        self,
        existing_files: Optional[Dict[str, str]] = None,
        fail_on: Optional[Dict[str, Exception]] = None,
        merge_commit: Optional[str] = "merge-sha-1",
        builds: Optional[List[Any]] = None,
    ) -> None:
        self.calls: List[str] = []
        self.existing_files = dict(existing_files or {})
        self.fail_on = dict(fail_on or {})
        self.merge_commit = merge_commit
        self.pull_requests: Dict[int, PullRequest] = {}
        self.committed: Dict[str, str] = {}
        self.commit_messages: List[str] = []
        # What put_file received as sourceCommitId, so the edit flow's optimistic-lock
        # handling can be asserted on.
        self.source_commit_ids: List[Optional[str]] = []
        self.last_commit_id: Optional[str] = "file-commit-sha"
        self.branch_head: Optional[str] = "base-head-sha"
        # Both gates default to green, so a test only scripts `builds` when it cares.
        self.builds: List[Any] = list(
            builds if builds is not None else [passing(), passing("ci/woodpecker/push/deploy")]
        )
        # The (commit, exclude_keys) each gate was opened on — this is what pins that the
        # validation gate watches the PR's commit and the deploy gate the merge commit.
        self.awaited_commits: List[str] = []
        self.excluded_keys: List[FrozenSet[str]] = []
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

    async def list_files(
        self, directory: str, at: Optional[str] = None, page_size: int = 1000
    ) -> List[str]:
        """Paths under `directory`, relative to it — mirrors the real client's contract.

        The real one turns a 404 into an empty list, so this returns [] for a directory
        with nothing in it rather than raising.
        """
        self._record("list_files")
        prefix = directory.strip("/") + "/"
        return sorted(
            path[len(prefix):]
            for path in self.existing_files
            if path.startswith(prefix)
        )

    async def create_branch(self, name: str, start_point: str) -> Dict[str, Any]:
        self._record("create_branch")
        return {"id": f"refs/heads/{name}"}

    async def delete_branch(self, name: str) -> None:
        self._record("delete_branch")

    async def get_last_commit(self, path: str, at: Optional[str] = None) -> Optional[str]:
        self._record("get_last_commit")
        return self.last_commit_id

    async def get_branch_head(self, branch: str) -> Optional[str]:
        self._record("get_branch_head")
        return self.branch_head

    async def put_file(
        self,
        path: str,
        branch: str,
        content: str,
        message: str,
        source_commit_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        self._record("put_file")
        self.committed[path] = content
        self.commit_messages.append(message)
        self.source_commit_ids.append(source_commit_id)
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
            # The head of the branch put_file just committed to — what the validation
            # gate watches, and what the PR's Builds tab shows.
            from_commit=f"sha-{from_branch}",
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

    async def await_builds(
        self, commit_id: str, exclude_keys: FrozenSet[str] = frozenset(), **kwargs
    ) -> List[BuildStatus]:
        self._record("await_builds")
        self.awaited_commits.append(commit_id)
        self.excluded_keys.append(exclude_keys)
        if not self.builds:
            raise AssertionError("FakeBitbucket.await_builds called with no scripted result")
        result = self.builds.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
