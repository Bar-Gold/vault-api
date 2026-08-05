"""Bitbucket Server / Data Center REST client, scoped to one repository.

The library's `Git` connector covers file CRUD on a default ref, but this service is built
around **pull requests** (create branch -> commit -> open PR -> merge/decline), which the
connector does not expose. So the whole repo interaction lives here, on top of the
library's `BaseAPI` async client — the same pattern the reference API uses for its chat
proxy. Every non-2xx is normalised into the library's `ExternalServiceError`, which the
routes map to a 502.

It is also the **only** upstream: the CI gates are read from Bitbucket's own build-status
store — the thing behind a pull request's *Builds* tab — rather than from the CI server's
API. See the build-status section at the bottom of this module.

Endpoints used (Bitbucket Server 7.4+/Data Center):
  POST   /rest/branch-utils/1.0/projects/{k}/repos/{s}/branches
  DELETE /rest/branch-utils/1.0/projects/{k}/repos/{s}/branches
  PUT    /rest/api/1.0/projects/{k}/repos/{s}/browse/{path}
  GET    /rest/api/1.0/projects/{k}/repos/{s}/files/{dir}?at={ref}
  GET    /rest/api/1.0/projects/{k}/repos/{s}/commits?path={p}&until={ref}
  GET    /rest/api/1.0/projects/{k}/repos/{s}/commits/{commitId}/builds
  POST   /rest/api/1.0/projects/{k}/repos/{s}/pull-requests
  GET    /rest/api/1.0/projects/{k}/repos/{s}/pull-requests/{id}
  POST   /rest/api/1.0/projects/{k}/repos/{s}/pull-requests/{id}/merge?version={v}
  POST   /rest/api/1.0/projects/{k}/repos/{s}/pull-requests/{id}/decline?version={v}
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional

from loguru import logger
from tashtiot_apis_library.connectors import ExternalServiceError

from . import http

SERVICE_NAME = "bitbucket"

# Bitbucket's own vocabulary for a build result. `INPROGRESS` is the only state that is not
# final — `CANCELLED` and `UNKNOWN` are terminal *and* not success, so a gate that keyed off
# "not FAILED" would let both through.
PENDING_BUILD_STATES = frozenset({"INPROGRESS"})
SUCCESSFUL_BUILD_STATE = "SUCCESSFUL"


class BuildTimeoutError(Exception):
    """No build was reported for a commit, or one never left `INPROGRESS`, in time."""

    def __init__(self, message: str, builds: Optional[List["BuildStatus"]] = None) -> None:
        super().__init__(message)
        self.message = message
        # Whatever had been observed when the budget ran out, so the failure can name it.
        self.builds: List["BuildStatus"] = list(builds or [])


@dataclass
class BuildStatus:
    """One entry of a commit's build-status list — one row of the PR's Builds tab.

    `key` is the identity: Bitbucket stores one result per key per commit, so a re-run
    replaces its predecessor rather than adding a row. Woodpecker's key carries the event
    and the workflow (`ci/woodpecker/pr/build`, `ci/woodpecker/push/deploy`), which is what
    keeps a validation result and a deploy result apart when they land on the same commit.
    """

    key: str
    state: str
    name: str = ""
    url: Optional[str] = None
    description: str = ""
    build_number: str = ""
    parent: str = ""
    ref: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "BuildStatus":
        return cls(
            key=data.get("key") or "",
            state=data.get("state") or "",
            name=data.get("name") or "",
            url=data.get("url") or None,
            description=data.get("description") or "",
            # Bitbucket types this as a string ("3"), not a number.
            build_number=str(data.get("buildNumber") or ""),
            parent=data.get("parent") or "",
            ref=data.get("ref") or "",
            raw=data,
        )

    @property
    def is_terminal(self) -> bool:
        return bool(self.state) and self.state not in PENDING_BUILD_STATES

    @property
    def succeeded(self) -> bool:
        return self.state == SUCCESSFUL_BUILD_STATE

    def __str__(self) -> str:
        link = f" ({self.url})" if self.url else ""
        return f"{self.key} [{self.state}]{link}"


@dataclass
class PullRequest:
    """The subset of Bitbucket's pull-request payload this service acts on."""

    id: int
    version: int
    title: str
    from_branch: str
    to_branch: str
    state: str
    url: Optional[str] = None
    merge_commit: Optional[str] = None
    from_commit: str = ""

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "PullRequest":
        links = (data.get("links") or {}).get("self") or []
        merge_commit = ((data.get("properties") or {}).get("mergeCommit") or {}).get("id")
        from_ref = data.get("fromRef") or {}
        return cls(
            id=data["id"],
            version=data.get("version", 0),
            title=data.get("title", ""),
            from_branch=from_ref.get("displayId") or "",
            to_branch=((data.get("toRef") or {}).get("displayId")) or "",
            state=data.get("state", "OPEN"),
            url=links[0].get("href") if links else None,
            merge_commit=merge_commit,
            # The head of the source branch — the commit the validation build runs on, and
            # the one whose builds the PR's Builds tab shows.
            from_commit=from_ref.get("latestCommit") or "",
        )


class BitbucketClient:
    """One instance per repository; constructed once in `create_app()`."""

    def __init__(
        self,
        http_client: Any,
        project_key: str,
        repo_slug: str,
        poll_interval: float = 5.0,
        start_timeout: float = 120.0,
        completion_timeout: float = 900.0,
    ) -> None:
        self._client = http_client
        self._project_key = project_key
        self._repo_slug = repo_slug
        # Build-status polling budgets. Defaults come from conf; tests zero them out.
        self.poll_interval = poll_interval
        self.start_timeout = start_timeout
        self.completion_timeout = completion_timeout

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    @property
    def _api(self) -> str:
        return f"/rest/api/1.0/projects/{self._project_key}/repos/{self._repo_slug}"

    @property
    def _branch_api(self) -> str:
        return f"/rest/branch-utils/1.0/projects/{self._project_key}/repos/{self._repo_slug}"

    @staticmethod
    def _detail(response: Any) -> str:
        """Bitbucket reports failures as {"errors": [{"message": ...}, ...]}."""
        try:
            body = response.json()
        except ValueError:
            return (response.text or "")[:500]
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            return "; ".join(str(e.get("message", e)) for e in errors)
        return str(body)[:500]

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        return await http.request(
            self._client,
            method,
            url,
            service_name=SERVICE_NAME,
            detail_from_response=self._detail,
            **kwargs,
        )

    # ------------------------------------------------------------------ #
    # branches
    # ------------------------------------------------------------------ #
    async def create_branch(self, name: str, start_point: str) -> Dict[str, Any]:
        logger.info(f"Creating branch {name} from {start_point}")
        response = await self._request(
            "POST",
            f"{self._branch_api}/branches",
            json={"name": name, "startPoint": f"refs/heads/{start_point}"},
        )
        return response.json()

    async def delete_branch(self, name: str) -> None:
        """Best-effort branch cleanup — used on the rollback paths."""
        logger.info(f"Deleting branch {name}")
        await self._request(
            "DELETE",
            f"{self._branch_api}/branches",
            json={"name": f"refs/heads/{name}"},
        )

    # ------------------------------------------------------------------ #
    # files
    # ------------------------------------------------------------------ #
    async def put_file(
        self,
        path: str,
        branch: str,
        content: str,
        message: str,
        source_commit_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or update a file on `branch`.

        Bitbucket's browse endpoint takes multipart/form-data; `sourceCommitId` is only
        sent when editing an existing file (it is Bitbucket's optimistic-locking token).
        """
        logger.info(f"Committing {path} on {branch}")
        data = {"message": message, "branch": branch}
        if source_commit_id:
            data["sourceCommitId"] = source_commit_id
        response = await self._request(
            "PUT",
            f"{self._api}/browse/{path.lstrip('/')}",
            files={"content": (path.rsplit("/", 1)[-1], content, "text/plain")},
            data=data,
        )
        return response.json()

    async def _newest_commit_id(self, params: Dict[str, Any]) -> Optional[str]:
        response = await self._request("GET", f"{self._api}/commits", params=params)
        values = response.json().get("values") or []
        return values[0].get("id") if values else None

    async def get_last_commit(self, path: str, at: Optional[str] = None) -> Optional[str]:
        """Id of the newest commit touching `path`, for `put_file`'s `source_commit_id`.

        Editing an existing file requires Bitbucket's optimistic-locking token; without it
        the browse endpoint rejects the PUT as an attempted create. Returns `None` when the
        path has no history, which is the caller's signal to treat the write as a create.
        """
        params: Dict[str, Any] = {"path": path, "limit": 1}
        if at:
            params["until"] = at
        return await self._newest_commit_id(params)

    async def get_branch_head(self, branch: str) -> Optional[str]:
        """Id of the newest commit on `branch`, whatever it touched.

        The fallback for the deploy gate: Bitbucket normally reports the merge commit on
        the merge response, but not on every merge strategy, and the deploy build is
        attached to whatever the merge left at the head of the base branch.
        """
        return await self._newest_commit_id({"until": branch, "limit": 1})

    async def list_files(
        self, directory: str, at: Optional[str] = None, page_size: int = 1000
    ) -> List[str]:
        """Paths under `directory`, relative to it, at a ref.

        Bitbucket paginates this and will not return everything in one page on a large
        directory, so follow `nextPageStart` until `isLastPage`. A 404 means the directory
        does not exist yet, which is an empty result rather than an error — the values
        directory is legitimately absent before the first store is ever created.
        """
        files: List[str] = []
        start = 0
        while True:
            params: Dict[str, Any] = {"limit": page_size, "start": start}
            if at:
                params["at"] = at
            try:
                response = await self._request(
                    "GET", f"{self._api}/files/{directory.strip('/')}", params=params
                )
            except ExternalServiceError as exc:
                if exc.status_code == 404:
                    return []
                raise

            body = response.json()
            files.extend(body.get("values") or [])
            if body.get("isLastPage", True):
                return files
            start = body.get("nextPageStart")
            if start is None:
                return files

    async def get_file_content(self, path: str, at: Optional[str] = None) -> str:
        """Raw file content at a ref (defaults to the repo's default branch)."""
        params = {"at": at} if at else None
        response = await self._request(
            "GET", f"{self._api}/raw/{path.lstrip('/')}", params=params
        )
        return response.text

    # ------------------------------------------------------------------ #
    # pull requests
    # ------------------------------------------------------------------ #
    async def create_pull_request(
        self,
        title: str,
        description: str,
        from_branch: str,
        to_branch: str,
        reviewers: Optional[List[str]] = None,
    ) -> PullRequest:
        logger.info(f"Opening pull request {from_branch} -> {to_branch}")
        body: Dict[str, Any] = {
            "title": title,
            "description": description,
            "state": "OPEN",
            "open": True,
            "closed": False,
            "fromRef": {
                "id": f"refs/heads/{from_branch}",
                "repository": {
                    "slug": self._repo_slug,
                    "project": {"key": self._project_key},
                },
            },
            "toRef": {
                "id": f"refs/heads/{to_branch}",
                "repository": {
                    "slug": self._repo_slug,
                    "project": {"key": self._project_key},
                },
            },
            "locked": False,
        }
        if reviewers:
            body["reviewers"] = [{"user": {"name": name}} for name in reviewers]

        response = await self._request("POST", f"{self._api}/pull-requests", json=body)
        return PullRequest.from_api(response.json())

    async def get_pull_request(self, pull_request_id: int) -> PullRequest:
        response = await self._request("GET", f"{self._api}/pull-requests/{pull_request_id}")
        return PullRequest.from_api(response.json())

    async def merge_pull_request(self, pull_request_id: int, version: int) -> PullRequest:
        """Merge the PR. `version` guards against merging a PR that changed underneath us."""
        logger.info(f"Merging pull request {pull_request_id} (version {version})")
        response = await self._request(
            "POST",
            f"{self._api}/pull-requests/{pull_request_id}/merge",
            params={"version": version},
        )
        return PullRequest.from_api(response.json())

    async def decline_pull_request(self, pull_request_id: int, version: int) -> PullRequest:
        logger.info(f"Declining pull request {pull_request_id} (version {version})")
        response = await self._request(
            "POST",
            f"{self._api}/pull-requests/{pull_request_id}/decline",
            params={"version": version},
        )
        return PullRequest.from_api(response.json())

    # ------------------------------------------------------------------ #
    # build statuses — the CI gates
    #
    # The CI server posts its result *to Bitbucket*, against the commit it built, and
    # Bitbucket keeps one result per `key` per commit. That store is what a pull request's
    # Builds tab renders, and reading it is how this service watches CI: the gates ask
    # Bitbucket about a commit rather than asking the CI server about a pipeline.
    #
    # That removes the whole matching problem. There is no pipeline number to discover, no
    # per-forge branch/ref/refspec guessing, and no "ignore anything that existed before we
    # started" watermark — the commit we are asking about was created seconds ago by
    # `put_file`, so every build on it is ours by construction.
    # ------------------------------------------------------------------ #
    async def get_build_statuses(
        self, commit_id: str, page_size: int = 100
    ) -> List[BuildStatus]:
        """Every build reported against one commit.

        A commit with no builds yet is an empty list, not a 404 — Bitbucket answers 200
        with an empty page, and a 404 (which is what an unknown commit gives) is left to
        propagate, because it means the caller asked about the wrong sha.
        """
        builds: List[BuildStatus] = []
        start = 0
        while True:
            response = await self._request(
                "GET",
                f"{self._api}/commits/{commit_id}/builds",
                params={"limit": page_size, "start": start},
            )
            body = response.json()
            builds.extend(BuildStatus.from_api(item) for item in body.get("values") or [])
            if body.get("isLastPage", True):
                return builds
            start = body.get("nextPageStart")
            if start is None:
                return builds

    async def find_builds(
        self,
        commit_id: str,
        exclude_keys: FrozenSet[str] = frozenset(),
        start_timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> List[BuildStatus]:
        """Wait for the CI server to report *something* against `commit_id`.

        The forge webhook is asynchronous, so a freshly pushed commit has no builds for a
        few seconds. An empty list therefore cannot mean "finished"; that is the whole
        reason this phase exists separately from `wait_for_builds`.
        """
        budget = self.start_timeout if start_timeout is None else start_timeout
        interval = self.poll_interval if poll_interval is None else poll_interval
        deadline = time.monotonic() + budget

        while True:
            builds = [
                build
                for build in await self.get_build_statuses(commit_id)
                if build.key not in exclude_keys
            ]
            if builds:
                logger.info(
                    f"Bitbucket reports {len(builds)} build(s) on {commit_id}: "
                    + ", ".join(str(build) for build in builds)
                )
                return builds
            if time.monotonic() >= deadline:
                raise BuildTimeoutError(
                    f"No build was reported against {commit_id} within {budget:g}s"
                )
            await asyncio.sleep(interval)

    async def wait_for_builds(
        self,
        commit_id: str,
        exclude_keys: FrozenSet[str] = frozenset(),
        completion_timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> List[BuildStatus]:
        """Poll `commit_id` until every build on it has left `INPROGRESS`."""
        budget = self.completion_timeout if completion_timeout is None else completion_timeout
        interval = self.poll_interval if poll_interval is None else poll_interval
        deadline = time.monotonic() + budget

        async def _current() -> List[BuildStatus]:
            return [
                build
                for build in await self.get_build_statuses(commit_id)
                if build.key not in exclude_keys
            ]

        builds = await _current()
        while not (builds and all(build.is_terminal for build in builds)):
            if time.monotonic() >= deadline:
                raise BuildTimeoutError(
                    f"Builds on {commit_id} were still running after {budget:g}s",
                    builds=builds,
                )
            await asyncio.sleep(interval)
            builds = await _current()

        logger.info(
            f"Builds on {commit_id} finished: "
            + ", ".join(str(build) for build in builds)
        )
        return builds

    async def await_builds(
        self,
        commit_id: str,
        exclude_keys: FrozenSet[str] = frozenset(),
        start_timeout: Optional[float] = None,
        completion_timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> List[BuildStatus]:
        """`find_builds` + `wait_for_builds` — the call the operations actually make.

        `exclude_keys` is the one watermark left. A fast-forward merge leaves the base
        branch pointing at the very commit the pull request built, so the deploy gate would
        otherwise open on the validation results and declare success without the deploy
        build having started. Excluding the keys the previous gate already saw makes that
        case wait for a genuinely new result.

        Known consequence, documented rather than worked around: a CI server that reports
        one build per workflow may not post all of them at once, so a gate that sees a
        single finished build while a sibling has yet to appear returns early.
        """
        builds = await self.find_builds(
            commit_id,
            exclude_keys=exclude_keys,
            start_timeout=start_timeout,
            poll_interval=poll_interval,
        )
        if all(build.is_terminal for build in builds):
            return builds
        return await self.wait_for_builds(
            commit_id,
            exclude_keys=exclude_keys,
            completion_timeout=completion_timeout,
            poll_interval=poll_interval,
        )
