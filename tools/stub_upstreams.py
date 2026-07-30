"""Local stand-in for Bitbucket Server + Woodpecker, so the whole create chain can be
exercised over real HTTP without Docker, a licence or network access.

Point the service at it and everything except the upstreams is real code:

    # terminal 1
    uv run --no-sync python tools/stub_upstreams.py --port 9000

    # terminal 2
    BITBUCKET_URL=http://127.0.0.1:9000 BITBUCKET_TOKEN=x \
    WOODPECKER_URL=http://127.0.0.1:9000 WOODPECKER_TOKEN=x \
    VAULT_URL=http://vault.invalid VAULT_TOKEN=x TEAM_NAME=kingmagen \
    VAULT_VALUES_REPO_PROJECT_KEY=INFRA VAULT_VALUES_REPO_SLUG=vault-values \
    WOODPECKER_REPO_ID=42 CI_POLL_INTERVAL_SECONDS=1 \
    uv run --no-sync python -m app.main --port 5055

Then drive scenarios:

    curl -X POST 127.0.0.1:9000/__control -H 'Content-Type: application/json' \
         -d '{"validation":"failure"}'          # -> 502, PR declined, branch deleted
    curl 127.0.0.1:9000/__state                 # inspect branches / PRs / pipelines

`validation` and `deploy` accept `success`, `failure` or `hang` (never reaches a terminal
status, so the service should answer 504). `polls` is how many status polls a pipeline stays
non-terminal before settling.

Deliberately faithful in two places, because they are the easy things to break:
  * a pull request's `version` is bumped every time its pipeline changes state, so merging
    with the version handed back by create -> 409, exactly like the real optimistic lock;
  * a merge writes the file onto the base branch, so a second create for the same mount
    hits the duplicate guard and gets a 409.
"""

import argparse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

BASE_BRANCH = "master"


def _error(status_code: int, message: str) -> JSONResponse:
    """Bitbucket's error envelope — `http.py`'s detail extractor expects this shape."""
    return JSONResponse({"errors": [{"message": message}]}, status_code=status_code)


def _parse_multipart(body: bytes, content_type: str) -> Dict[str, str]:
    """Minimal multipart/form-data reader, so the stub needs no python-multipart.

    Good enough for what `put_file` sends: one file part named `content` plus the
    `message` / `branch` / `sourceCommitId` fields.
    """
    if "boundary=" not in content_type:
        return {}
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"')
    fields: Dict[str, str] = {}
    for part in body.split(f"--{boundary}".encode()):
        head, separator, payload = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        headers = head.decode("utf-8", "replace")
        if 'name="' not in headers:
            continue
        name = headers.split('name="', 1)[1].split('"', 1)[0]
        fields[name] = payload.rstrip(b"\r\n").decode("utf-8", "replace")
    return fields


@dataclass
class Pipeline:
    number: int
    event: str
    status: str = "pending"
    branch: str = ""
    ref: str = ""
    refspec: str = ""
    commit: str = ""
    remaining: int = 1
    final: str = "success"
    hang: bool = False
    pull_request_id: Optional[int] = None

    def as_api(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "status": self.status,
            "event": self.event,
            "branch": self.branch,
            "ref": self.ref,
            "refspec": self.refspec,
            "commit": self.commit,
            "link": f"http://127.0.0.1/pipelines/{self.number}",
        }


@dataclass
class PullRequest:
    id: int
    version: int
    title: str
    from_branch: str
    to_branch: str
    state: str = "OPEN"
    merge_commit: Optional[str] = None
    description: str = ""
    reviewers: List[str] = field(default_factory=list)

    def as_api(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "state": self.state,
            "fromRef": {"displayId": self.from_branch},
            "toRef": {"displayId": self.to_branch},
            "links": {"self": [{"href": f"http://127.0.0.1/pull-requests/{self.id}"}]},
        }
        if self.merge_commit:
            body["properties"] = {"mergeCommit": {"id": self.merge_commit}}
        return body


@dataclass
class State:
    branches: Dict[str, str] = field(default_factory=lambda: {BASE_BRANCH: "base-sha"})
    files: Dict[str, Dict[str, str]] = field(default_factory=lambda: {BASE_BRANCH: {}})
    pulls: Dict[int, PullRequest] = field(default_factory=dict)
    pipelines: List[Pipeline] = field(default_factory=list)
    # Every commit the service made, in order, so the message can be inspected too.
    commits: List[Dict[str, str]] = field(default_factory=list)
    control: Dict[str, Any] = field(
        default_factory=lambda: {"validation": "success", "deploy": "success", "polls": 1}
    )
    next_pull_id: int = 100
    next_pipeline: int = 1

    def add_pipeline(self, event: str, which: str, **kwargs: Any) -> Pipeline:
        outcome = self.control[which]
        pipeline = Pipeline(
            number=self.next_pipeline,
            event=event,
            remaining=int(self.control["polls"]),
            final="success" if outcome == "success" else "failure",
            hang=(outcome == "hang"),
            **kwargs,
        )
        self.next_pipeline += 1
        self.pipelines.append(pipeline)
        return pipeline


state = State()
app = FastAPI(title="Bitbucket + Woodpecker stub")


# --------------------------------------------------------------------------- #
# control plane
# --------------------------------------------------------------------------- #
@app.post("/__control")
async def set_control(payload: Dict[str, Any]) -> Dict[str, Any]:
    state.control.update(payload)
    return state.control


@app.post("/__reset")
async def reset() -> Dict[str, str]:
    global state
    state = State()
    return {"status": "reset"}


@app.get("/__state")
async def get_state() -> Dict[str, Any]:
    return {
        "control": state.control,
        "branches": sorted(state.branches),
        "files_on_base": sorted(state.files.get(BASE_BRANCH, {})),
        "commits": state.commits,
        "pull_requests": [
            {
                "id": p.id,
                "version": p.version,
                "state": p.state,
                "from": p.from_branch,
                "to": p.to_branch,
                "title": p.title,
                "description": p.description,
                "reviewers": p.reviewers,
            }
            for p in state.pulls.values()
        ],
        "pipelines": [
            {"number": p.number, "event": p.event, "status": p.status} for p in state.pipelines
        ],
    }


# --------------------------------------------------------------------------- #
# Bitbucket — branches
# --------------------------------------------------------------------------- #
@app.post("/rest/branch-utils/1.0/projects/{key}/repos/{slug}/branches")
async def create_branch(key: str, slug: str, body: Dict[str, Any]) -> Dict[str, Any]:
    name = body["name"]
    if name in state.branches:
        return _error(409, f"Branch {name} already exists")
    start = body.get("startPoint", "").replace("refs/heads/", "") or BASE_BRANCH
    state.branches[name] = state.branches.get(start, "base-sha")
    state.files[name] = dict(state.files.get(start, {}))
    return {"id": f"refs/heads/{name}", "displayId": name}


@app.delete("/rest/branch-utils/1.0/projects/{key}/repos/{slug}/branches")
async def delete_branch(key: str, slug: str, request: Request):
    body = await request.json()
    name = body.get("name", "").replace("refs/heads/", "")
    if name not in state.branches:
        return _error(404, f"Branch {name} does not exist")
    del state.branches[name]
    state.files.pop(name, None)
    return JSONResponse({}, status_code=204)


# --------------------------------------------------------------------------- #
# Bitbucket — files
# --------------------------------------------------------------------------- #
@app.put("/rest/api/1.0/projects/{key}/repos/{slug}/browse/{path:path}")
async def put_file(key: str, slug: str, path: str, request: Request):
    fields = _parse_multipart(
        await request.body(), request.headers.get("content-type", "")
    )
    branch = fields.get("branch", "")
    if branch not in state.branches:
        return _error(404, f"Branch {branch} does not exist")
    state.files.setdefault(branch, {})[path] = fields.get("content", "")
    state.commits.append(
        {"branch": branch, "path": path, "message": fields.get("message", "")}
    )
    return {"id": f"commit-{branch}", "message": fields.get("message", "")}


@app.get("/rest/api/1.0/projects/{key}/repos/{slug}/raw/{path:path}")
async def get_raw(key: str, slug: str, path: str, at: Optional[str] = None):
    ref = (at or BASE_BRANCH).replace("refs/heads/", "")
    body = state.files.get(ref, {}).get(path)
    if body is None:
        return _error(404, f"{path} not found on {ref}")
    return PlainTextResponse(body)


# --------------------------------------------------------------------------- #
# Bitbucket — pull requests
# --------------------------------------------------------------------------- #
@app.post("/rest/api/1.0/projects/{key}/repos/{slug}/pull-requests")
async def create_pull_request(key: str, slug: str, body: Dict[str, Any]) -> Dict[str, Any]:
    from_branch = body["fromRef"]["id"].replace("refs/heads/", "")
    to_branch = body["toRef"]["id"].replace("refs/heads/", "")
    pull = PullRequest(
        id=state.next_pull_id,
        version=0,
        title=body.get("title", ""),
        from_branch=from_branch,
        to_branch=to_branch,
        description=body.get("description", ""),
        reviewers=[
            r.get("user", {}).get("name", "") for r in body.get("reviewers", []) or []
        ],
    )
    state.next_pull_id += 1
    state.pulls[pull.id] = pull
    # The forge webhook fires here — this is the pipeline that gates the merge.
    state.add_pipeline(
        "pull_request",
        "validation",
        branch=from_branch,
        ref=f"refs/pull-requests/{pull.id}/from",
        refspec=f"{from_branch}:{to_branch}",
        commit=f"sha-{from_branch}",
        pull_request_id=pull.id,
    )
    return pull.as_api()


@app.get("/rest/api/1.0/projects/{key}/repos/{slug}/pull-requests/{pull_id}")
async def get_pull_request(key: str, slug: str, pull_id: int):
    pull = state.pulls.get(pull_id)
    if pull is None:
        return _error(404, f"Pull request {pull_id} not found")
    return pull.as_api()


@app.post("/rest/api/1.0/projects/{key}/repos/{slug}/pull-requests/{pull_id}/merge")
async def merge_pull_request(key: str, slug: str, pull_id: int, version: int = -1):
    pull = state.pulls.get(pull_id)
    if pull is None:
        return _error(404, f"Pull request {pull_id} not found")
    if version != pull.version:
        # The real optimistic lock: this is what a stale version from create hits.
        return _error(409, f"Expected version {pull.version} but got {version}")
    if pull.state != "OPEN":
        return _error(409, f"Pull request {pull_id} is {pull.state}")

    pull.state = "MERGED"
    pull.version += 1
    pull.merge_commit = f"merge-sha-{pull_id}"
    # Land the change on the base branch, so a repeat create hits the duplicate guard.
    state.files.setdefault(BASE_BRANCH, {}).update(state.files.get(pull.from_branch, {}))
    state.add_pipeline(
        "push",
        "deploy",
        branch=BASE_BRANCH,
        ref=f"refs/heads/{BASE_BRANCH}",
        commit=pull.merge_commit,
        pull_request_id=pull.id,
    )
    return pull.as_api()


@app.post("/rest/api/1.0/projects/{key}/repos/{slug}/pull-requests/{pull_id}/decline")
async def decline_pull_request(key: str, slug: str, pull_id: int, version: int = -1):
    pull = state.pulls.get(pull_id)
    if pull is None:
        return _error(404, f"Pull request {pull_id} not found")
    if version != pull.version:
        return _error(409, f"Expected version {pull.version} but got {version}")
    pull.state = "DECLINED"
    pull.version += 1
    return pull.as_api()


# --------------------------------------------------------------------------- #
# Woodpecker
# --------------------------------------------------------------------------- #
@app.get("/api/repos/{repo_id}/pipelines")
async def list_pipelines(repo_id: str, page: int = 1, perPage: int = 50) -> List[Dict[str, Any]]:
    return [p.as_api() for p in sorted(state.pipelines, key=lambda p: -p.number)][:perPage]


@app.get("/api/repos/{repo_id}/pipelines/{number}")
async def get_pipeline(repo_id: str, number: int):
    for pipeline in state.pipelines:
        if pipeline.number != number:
            continue
        if pipeline.hang:
            pipeline.status = "running"
        elif pipeline.status not in ("success", "failure"):
            if pipeline.remaining > 0:
                pipeline.remaining -= 1
                pipeline.status = "running"
            else:
                pipeline.status = pipeline.final
                # A CI status update bumps the PR version, invalidating any stale copy.
                pull = state.pulls.get(pipeline.pull_request_id or -1)
                if pull is not None:
                    pull.version += 1
        return pipeline.as_api()
    return JSONResponse({"message": f"pipeline {number} not found"}, status_code=404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bitbucket + Woodpecker stub.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
