"""Local stand-in for Bitbucket Server, so the whole create chain can be exercised over
real HTTP without Docker, a licence or network access.

One upstream is now enough: the CI gates read the build statuses a pipeline posts *into*
Bitbucket — the pull request's Builds tab — so there is no CI server to impersonate. What
the stub fakes instead is the CI server's side effect: opening a pull request or merging
one attaches an `INPROGRESS` build status to the relevant commit, which then settles.

Point the service at it and everything except the upstream is real code:

    # terminal 1
    uv run --no-sync python tools/stub_upstreams.py --port 9000

    # terminal 2
    BITBUCKET_URL=http://127.0.0.1:9000 BITBUCKET_TOKEN=x \
    VAULT_VALUES_REPO_PROJECT_KEY=INFRA VAULT_VALUES_REPO_SLUG=vault-values \
    CI_POLL_INTERVAL_SECONDS=1 \
    uv run --no-sync python -m app.main --port 5055

Then drive scenarios:

    curl -X POST 127.0.0.1:9000/__control -H 'Content-Type: application/json' \
         -d '{"validation":"failure"}'          # -> 502, PR declined, branch deleted
    curl 127.0.0.1:9000/__state                 # inspect branches / PRs / builds

`validation` and `deploy` accept `success`, `failure` or `hang` (never leaves INPROGRESS,
so the service should answer 504). `polls` is how many reads a build stays INPROGRESS
before settling. `workflows` is how many build statuses a pipeline posts against one
commit, which is what makes the "wait for the slowest one" path reachable.

Deliberately faithful in three places, because they are the easy things to break:
  * a pull request's `version` is bumped every time a build status changes, so merging
    with the version handed back by create -> 409, exactly like the real optimistic lock;
  * a merge writes the file onto the base branch, so a second create for the same store
    hits the duplicate guard and gets a 409;
  * `fromRef.latestCommit` moves with each commit on the branch, because that sha is what
    the validation gate watches.
"""

import argparse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

BASE_BRANCH = "master"
# Merge commits get their own sha space so they are obvious in /__state and can never
# collide with a branch commit — the deploy gate keys off exactly this value.
MERGE_COMMIT_BASE = 0xDEAD0000


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
class Build:
    """One row of the Builds tab: a key, a state, and the commit it hangs off."""

    key: str
    commit: str
    state: str = "INPROGRESS"
    remaining: int = 1
    final: str = "SUCCESSFUL"
    hang: bool = False
    pull_request_id: Optional[int] = None

    def as_api(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "state": self.state,
            "name": f"vault-values {self.key}",
            "url": f"http://127.0.0.1/builds/{self.key}",
            "buildNumber": "1",
        }


@dataclass
class PullRequest:
    id: int
    version: int
    title: str
    from_branch: str
    to_branch: str
    from_commit: str
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
            "fromRef": {"displayId": self.from_branch, "latestCommit": self.from_commit},
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
    # Build statuses, keyed by the commit they were reported against.
    builds: Dict[str, List[Build]] = field(default_factory=dict)
    # Every commit the service made, in order, so the message can be inspected too.
    commits: List[Dict[str, str]] = field(default_factory=list)
    control: Dict[str, Any] = field(
        default_factory=lambda: {
            "validation": "success",
            "deploy": "success",
            "polls": 1,
            "workflows": 1,
        }
    )
    next_pull_id: int = 100
    next_commit: int = 1

    def new_commit_id(self, branch: str) -> str:
        # Hex, like a real sha — *not* derived from the branch name, which contains
        # slashes and would silently turn into extra URL path segments.
        commit_id = f"{self.next_commit:040x}"
        self.next_commit += 1
        self.branches[branch] = commit_id
        return commit_id

    def report_builds(
        self, commit: str, which: str, event: str, pull_request_id: Optional[int] = None
    ) -> None:
        """What the CI server does on a webhook: attach one build status per workflow."""
        outcome = self.control[which]
        for index in range(int(self.control["workflows"])):
            self.builds.setdefault(commit, []).append(
                Build(
                    key=f"ci/woodpecker/{event}/workflow-{index + 1}",
                    commit=commit,
                    remaining=int(self.control["polls"]),
                    final="SUCCESSFUL" if outcome == "success" else "FAILED",
                    hang=(outcome == "hang"),
                    pull_request_id=pull_request_id,
                )
            )


state = State()
app = FastAPI(title="Bitbucket stub")


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
        "branches": state.branches,
        "files_on_base": sorted(state.files.get(BASE_BRANCH, {})),
        "commits": state.commits,
        "pull_requests": [
            {
                "id": p.id,
                "version": p.version,
                "state": p.state,
                "from": p.from_branch,
                "from_commit": p.from_commit,
                "to": p.to_branch,
                "title": p.title,
                "description": p.description,
                "reviewers": p.reviewers,
            }
            for p in state.pulls.values()
        ],
        "builds": {
            commit: [{"key": b.key, "state": b.state} for b in builds]
            for commit, builds in state.builds.items()
        },
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
    commit_id = state.new_commit_id(branch)
    state.commits.append(
        {
            "branch": branch,
            "id": commit_id,
            "path": path,
            "message": fields.get("message", ""),
            # Present on edits, absent on creates — worth seeing in /__state.
            "source_commit_id": fields.get("sourceCommitId", ""),
        }
    )
    return {"id": commit_id, "message": fields.get("message", "")}


@app.get("/rest/api/1.0/projects/{key}/repos/{slug}/files/{directory:path}")
async def list_files(
    key: str, slug: str, directory: str, at: Optional[str] = None,
    start: int = 0, limit: int = 1000,
):
    """Files under a directory — the create flow scans these for a duplicate store name.

    Paginated like the real thing, so the client's nextPageStart loop is actually
    exercised rather than assumed. A missing directory is a 404, which the client turns
    into an empty list (the values dir legitimately does not exist before the first
    store).
    """
    ref = (at or BASE_BRANCH).replace("refs/heads/", "")
    prefix = directory.strip("/") + "/"
    matches = sorted(
        path[len(prefix):]
        for path in state.files.get(ref, {})
        if path.startswith(prefix)
    )
    if not matches:
        return _error(404, f"{directory} not found on {ref}")

    page = matches[start : start + limit]
    next_start = start + limit
    is_last = next_start >= len(matches)
    body: Dict[str, Any] = {"values": page, "size": len(page), "isLastPage": is_last}
    if not is_last:
        body["nextPageStart"] = next_start
    return body


@app.get("/rest/api/1.0/projects/{key}/repos/{slug}/commits")
async def list_commits(
    key: str, slug: str, path: Optional[str] = None, until: Optional[str] = None, limit: int = 25
):
    """History — used for the edit flow's optimistic-lock token *and*, without a `path`,
    for the head of the base branch when a merge reports no merge commit."""
    ref = (until or BASE_BRANCH).replace("refs/heads/", "")
    if path is None:
        return {"values": [{"id": state.branches.get(ref, "base-sha")}], "size": 1}
    if path not in state.files.get(ref, {}):
        return {"values": [], "size": 0}
    return {"values": [{"id": f"commit-{ref}-{len(state.commits)}"}], "size": 1}


@app.get("/rest/api/1.0/projects/{key}/repos/{slug}/raw/{path:path}")
async def get_raw(key: str, slug: str, path: str, at: Optional[str] = None):
    ref = (at or BASE_BRANCH).replace("refs/heads/", "")
    body = state.files.get(ref, {}).get(path)
    if body is None:
        return _error(404, f"{path} not found on {ref}")
    return PlainTextResponse(body)


# --------------------------------------------------------------------------- #
# Bitbucket — build statuses
# --------------------------------------------------------------------------- #
@app.get("/rest/api/1.0/projects/{key}/repos/{slug}/commits/{commit_id}/builds")
async def get_builds(key: str, slug: str, commit_id: str, start: int = 0, limit: int = 100):
    """A commit's Builds tab, settling one poll at a time.

    Reading is what advances a build here — the same trick the pipeline poller used —
    so `polls` controls how many reads it stays INPROGRESS.
    """
    builds = state.builds.get(commit_id, [])
    for build in builds:
        if build.hang or build.state != "INPROGRESS":
            continue
        if build.remaining > 0:
            build.remaining -= 1
            continue
        build.state = build.final
        # A build status update bumps the PR version, invalidating any stale copy.
        pull = state.pulls.get(build.pull_request_id or -1)
        if pull is not None:
            pull.version += 1

    page = builds[start : start + limit]
    next_start = start + limit
    is_last = next_start >= len(builds)
    body: Dict[str, Any] = {
        "values": [b.as_api() for b in page],
        "size": len(page),
        "isLastPage": is_last,
    }
    if not is_last:
        body["nextPageStart"] = next_start
    return body


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
        from_commit=state.branches.get(from_branch, "base-sha"),
        description=body.get("description", ""),
        reviewers=[
            r.get("user", {}).get("name", "") for r in body.get("reviewers", []) or []
        ],
    )
    state.next_pull_id += 1
    state.pulls[pull.id] = pull
    # The forge webhook fires here — this is the build that gates the merge, and it lands
    # on the source branch head, which is what the PR's Builds tab shows.
    state.report_builds(pull.from_commit, "validation", "pr", pull_request_id=pull.id)
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
    pull.merge_commit = f"{MERGE_COMMIT_BASE + pull_id:040x}"
    state.branches[BASE_BRANCH] = pull.merge_commit
    # Land the change on the base branch, so a repeat create hits the duplicate guard.
    state.files.setdefault(BASE_BRANCH, {}).update(state.files.get(pull.from_branch, {}))
    state.report_builds(pull.merge_commit, "deploy", "push", pull_request_id=pull.id)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bitbucket stub.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
