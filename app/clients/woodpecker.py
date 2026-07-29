"""Woodpecker CI REST client.

Two pipelines gate a Vault change: the `pull_request` pipeline that validates the PR
(and blocks the merge), and the `push` pipeline that applies the merged change. This
client's job is to find those pipelines and wait for them to reach a terminal state.

Endpoints used:
  GET /api/repos/{repo_id}/pipelines?page={n}&perPage={n}
  GET /api/repos/{repo_id}/pipelines/{number}
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from . import http

SERVICE_NAME = "woodpecker"

# Woodpecker statuses that are not final. `blocked` means the pipeline is waiting for a
# human approval, so it stays pending until it is approved or the timeout fires.
PENDING_STATUSES = frozenset({"pending", "running", "blocked", "waiting_on_deps"})
SUCCESS_STATUS = "success"


class PipelineTimeoutError(Exception):
    """A pipeline did not appear, or did not finish, inside the configured budget."""

    def __init__(self, message: str, pipeline: Optional["Pipeline"] = None) -> None:
        super().__init__(message)
        self.message = message
        self.pipeline = pipeline


@dataclass
class Pipeline:
    number: int
    status: str
    event: str = ""
    branch: str = ""
    ref: str = ""
    refspec: str = ""
    commit: str = ""
    message: str = ""
    link: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "Pipeline":
        return cls(
            number=data.get("number", 0),
            status=data.get("status", ""),
            event=data.get("event", ""),
            branch=data.get("branch") or "",
            ref=data.get("ref") or "",
            refspec=data.get("refspec") or "",
            commit=data.get("commit") or "",
            message=data.get("message") or "",
            link=data.get("link") or data.get("forge_url"),
            raw=data,
        )

    @property
    def is_terminal(self) -> bool:
        return bool(self.status) and self.status not in PENDING_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == SUCCESS_STATUS


class WoodpeckerClient:
    """One instance per repository; constructed once in `create_app()`."""

    def __init__(
        self,
        http_client: Any,
        repo_id: str,
        poll_interval: float = 5.0,
        start_timeout: float = 120.0,
        completion_timeout: float = 900.0,
    ) -> None:
        self._client = http_client
        self._repo_id = repo_id
        # Defaults come from conf; tests override them to keep the suite fast.
        self.poll_interval = poll_interval
        self.start_timeout = start_timeout
        self.completion_timeout = completion_timeout

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        return await http.request(
            self._client, method, url, service_name=SERVICE_NAME, **kwargs
        )

    async def list_pipelines(self, per_page: int = 50) -> List[Pipeline]:
        """Most recent pipelines for the repo, newest first (Woodpecker's own ordering)."""
        response = await self._request(
            "GET",
            f"/api/repos/{self._repo_id}/pipelines",
            params={"page": 1, "perPage": per_page},
        )
        # A 2xx with an empty or non-JSON body (proxy hiccup, 204) must not blow up the
        # poll loop — treat it as "nothing yet" and let the timeout decide.
        try:
            data = response.json() or []
        except ValueError:
            logger.warning("Woodpecker returned a non-JSON pipeline list; treating it as empty")
            data = []
        return [Pipeline.from_api(item) for item in data]

    async def get_pipeline(self, number: int) -> Pipeline:
        response = await self._request(
            "GET", f"/api/repos/{self._repo_id}/pipelines/{number}"
        )
        return Pipeline.from_api(response.json())

    async def find_pipeline(
        self,
        matches: Callable[[Pipeline], bool],
        start_timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> Pipeline:
        """Wait for a pipeline matching `matches` to show up.

        The forge webhook that triggers the pipeline is asynchronous, so the pipeline does
        not exist the instant the PR is opened or merged — we poll the list until it does.
        """
        budget = self.start_timeout if start_timeout is None else start_timeout
        interval = self.poll_interval if poll_interval is None else poll_interval
        deadline = time.monotonic() + budget

        while True:
            for pipeline in await self.list_pipelines():
                if matches(pipeline):
                    logger.info(
                        f"Matched Woodpecker pipeline #{pipeline.number} "
                        f"(event={pipeline.event}, status={pipeline.status})"
                    )
                    return pipeline
            if time.monotonic() >= deadline:
                raise PipelineTimeoutError(
                    f"No matching Woodpecker pipeline appeared within {budget:g}s"
                )
            await asyncio.sleep(interval)

    async def wait_for_completion(
        self,
        number: int,
        completion_timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> Pipeline:
        """Poll pipeline `number` until it reaches a terminal status."""
        budget = self.completion_timeout if completion_timeout is None else completion_timeout
        interval = self.poll_interval if poll_interval is None else poll_interval
        deadline = time.monotonic() + budget

        pipeline = await self.get_pipeline(number)
        while not pipeline.is_terminal:
            if time.monotonic() >= deadline:
                raise PipelineTimeoutError(
                    f"Pipeline #{number} still {pipeline.status} after {budget:g}s",
                    pipeline=pipeline,
                )
            await asyncio.sleep(interval)
            pipeline = await self.get_pipeline(number)

        logger.info(f"Woodpecker pipeline #{number} finished with status={pipeline.status}")
        return pipeline

    async def await_pipeline(
        self,
        matches: Callable[[Pipeline], bool],
        start_timeout: Optional[float] = None,
        completion_timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> Pipeline:
        """`find_pipeline` + `wait_for_completion` — the call the operations actually make."""
        found = await self.find_pipeline(
            matches, start_timeout=start_timeout, poll_interval=poll_interval
        )
        if found.is_terminal:
            return found
        return await self.wait_for_completion(
            found.number, completion_timeout=completion_timeout, poll_interval=poll_interval
        )
