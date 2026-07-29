"""Bitbucket Server / Data Center REST client, scoped to one repository.

The library's `Git` connector covers file CRUD on a default ref, but this service is built
around **pull requests** (create branch -> commit -> open PR -> merge/decline), which the
connector does not expose. So the whole repo interaction lives here, on top of the
library's `BaseAPI` async client — the same pattern the reference API uses for its chat
proxy. Every non-2xx is normalised into the library's `ExternalServiceError`, which the
routes map to a 502.

Endpoints used (Bitbucket Server 7+/Data Center):
  POST   /rest/branch-utils/1.0/projects/{k}/repos/{s}/branches
  DELETE /rest/branch-utils/1.0/projects/{k}/repos/{s}/branches
  PUT    /rest/api/1.0/projects/{k}/repos/{s}/browse/{path}
  POST   /rest/api/1.0/projects/{k}/repos/{s}/pull-requests
  GET    /rest/api/1.0/projects/{k}/repos/{s}/pull-requests/{id}
  POST   /rest/api/1.0/projects/{k}/repos/{s}/pull-requests/{id}/merge?version={v}
  POST   /rest/api/1.0/projects/{k}/repos/{s}/pull-requests/{id}/decline?version={v}
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger

from . import http

SERVICE_NAME = "bitbucket"


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

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "PullRequest":
        links = (data.get("links") or {}).get("self") or []
        merge_commit = ((data.get("properties") or {}).get("mergeCommit") or {}).get("id")
        return cls(
            id=data["id"],
            version=data.get("version", 0),
            title=data.get("title", ""),
            from_branch=((data.get("fromRef") or {}).get("displayId")) or "",
            to_branch=((data.get("toRef") or {}).get("displayId")) or "",
            state=data.get("state", "OPEN"),
            url=links[0].get("href") if links else None,
            merge_commit=merge_commit,
        )


class BitbucketClient:
    """One instance per repository; constructed once in `create_app()`."""

    def __init__(self, http_client: Any, project_key: str, repo_slug: str) -> None:
        self._client = http_client
        self._project_key = project_key
        self._repo_slug = repo_slug

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
