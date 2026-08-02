# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service that **writes a file to a Bitbucket repo and watches the Woodpecker
pipelines that act on it**. A create request opens a pull request, waits for the validating
pipeline, merges, waits for the deploying pipeline, and returns
`Successful creation of <kv name>`.

**Scope boundary — read this first.** The service deliberately knows nothing about Vault. It
commits a two-key document:

```yaml
kvname: myapp
description: payments secrets
```

Mounts, KV engine versions, policies, HCL — none of it is modelled here. That is the deploy
pipeline's business. `VAULT_URL` / `VAULT_TOKEN` are required settings that **no code reads**.
Resist adding Vault semantics: an earlier version generated mounts and policy HCL, and it was
removed on purpose.

Built on the internal `tashtiot-apis-library` (base app, `BaseAPI`, `ExternalServiceError`)
and follows the patterns of the reference `example-api` (sibling repo).

## Commands

uv-managed (Python 3.12). Package indexes come from `uv.toml` (internal Artifactory, untracked).

```bash
uv sync --group dev                 # install deps into .venv
uv run python -m app.main           # serves on 0.0.0.0:5000 (--host/--port override)
uv run pytest                       # everything under tests/
uv run pytest tests/v1/vault        # one suite
uv run pytest tests/v1/vault/test_operations.py::test_happy_path_call_order   # single test
docker build -t vault-api . && docker run -p 5000:5000 --env-file .env vault-api
```

Docs at `http://localhost:5000/docs`, Prometheus metrics at `/metrics` (both provided by the
library's `general_create_app`). `asyncio_mode = "auto"` is set in `pyproject.toml`, so async
tests need no `@pytest.mark.asyncio`. There is no linter/formatter configured.

**Driving the real chain locally: `tools/stub_upstreams.py`.** A single FastAPI process that
impersonates both Bitbucket Server and Woodpecker, so the whole create chain runs over real
HTTP with no Docker, licence or network. Everything except the two upstreams is real code —
use it before reaching for more mocks.

```bash
uv run --no-sync python tools/stub_upstreams.py --port 9000     # terminal 1
# terminal 2: point BITBUCKET_URL/WOODPECKER_URL at it (full env in the module docstring)
curl -X POST 127.0.0.1:9000/__control -d '{"validation":"failure"}'  # or "hang" -> 504
curl 127.0.0.1:9000/__state       # branches, PRs, pipelines, and every commit made
curl -X POST 127.0.0.1:9000/__reset
```

`validation`/`deploy` take `success` | `failure` | `hang`; `polls` is how many status polls a
pipeline stays non-terminal. The stub reproduces the two traps on purpose: a PR's `version` is
bumped on every CI status change (so a stale version 409s at merge), and a merge lands the file
on the base branch (so a repeat create hits the duplicate guard). Keep both if you extend it.

`.woodpecker/build.yaml` runs the suite on push to `master` and builds/pushes the image on git
**tags** only, passing `APP_VERSION=${CI_COMMIT_TAG}` as a build arg (it surfaces in the Swagger UI).

## Routes

| Route | Body | Notes |
|-------|------|-------|
| `POST /api/vault/v1/kv/` | `{kv_name, kv_description}` | the chain below; blocks until the deploy pipeline ends |
| `POST /api/vault/v1/kv/pull-request` | `{kv_name, kv_description}` | steps 1-3 only: opens the PR and returns. No CI wait, no merge |
| `GET /api/vault/v1/kv/{kv_name}` | — | the committed file, parsed YAML returned as-is (not a schema) |
| `PATCH /api/vault/v1/kv/{kv_name}` | `{kv_description?, owner?}` | edits those two keys in place |

**`/pull-request` is a fixed segment registered before the `{kv_name:path}` routes.** There is
no POST on that path today so nothing is ambiguous, but if one is ever added this route must
keep winning — otherwise a KV named `pull-request` shadows the endpoint. A `GET` on the same
URL is still a normal read of a KV named `pull-request`; the two coexist.

`API_PREFIX` (default `/api/vault/v1/kv`) comes from `app/v1/vault/conf.py`.

**The create request is exactly two fields**, and `test_create_takes_exactly_two_fields`
pins that. There is no environment and no infra-coordinates `metadata` block; the name alone
identifies the KV, and the file lands at `{VAULT_VALUES_DIR}/{kv_name}.yaml`.

**`kv_name` is used verbatim** and may be multi-segment (`payments/vault-secrets`):

- Both `{kv_name}` routes use a **`:path` converter**, so a slash does not end the segment.
  Neither has a suffix after `{kv_name}`, so there is no route-collision subtlety to preserve
  — if you ever add one (`/{kv_name:path}/something`), the converter has to backtrack to the
  anchored suffix, and a KV literally named `team/something` becomes ambiguous.
- `KV_NAME_PATTERN` (segments of `[a-z0-9]`/dashes joined by single slashes) is the
  **path-traversal guard**, not just a style rule: the name lands in the file path. No
  leading/trailing slash, no empty segment, `..` cannot match.
- Branch names cannot contain slashes, so they go through `slugify_mount_path`
  (`payments/vault-secrets` → `payments-vault-secrets`): git cannot hold both `vault-kv/a`
  and `vault-kv/a/b-suffix`.

## Architecture

**App composition (`app/main.py`).** `create_app()` is the single wiring point: it calls
`general_create_app(enable_auth=True)`, constructs the Bitbucket and Woodpecker clients **once**
from config, and injects them into `get_v1_vault_router(bitbucket, woodpecker)`. Connectors are
never built per-request; the router factory closure is the injection seam (no FastAPI `Depends`
for connectors).

**Config is two-layered, all sourced from `.env`** (pydantic-settings `BaseSettings`):
- `app/global_conf.py` — `BITBUCKET_*`, `WOODPECKER_*`, `VAULT_*`, `TEAM_NAME`,
  `HTTP_TIMEOUT_SECONDS`, `VERIFY_SSL`.
- `app/v1/vault/conf.py` — `API_PREFIX`, `API_TAGS`, values-repo layout, PR shaping, `CI_*` timeouts.
- `AUTH_*` / `AUTH_SSO_*` are **not** declared here — the library reads them from the environment.

Both are singletons built at **import time**, so a missing required setting is an import error,
not a request error. Adding a required setting means adding it to `tests/conftest.py` too.

Every declared setting is read by something. `VAULT_URL`, `VAULT_TOKEN`, `TEAM_NAME`,
`API_DESCRIPTION`, `DEFAULT_KV_MAX_VERSIONS` and `DEFAULT_DELETE_VERSION_AFTER` used to be
declared here and read by nothing — leftovers from the removed Vault-semantics version. They
are gone; don't reintroduce them, and don't go looking for a Vault client.

**Per-module layout** (`app/v1/vault/`): `conf.py`, `schemas.py`, `operations.py`, `routes.py` —
the same four-file split the reference API uses.

**Clients (`app/clients/`).** `BitbucketClient` and `WoodpeckerClient` wrap the library's
`BaseAPI` async client. Bitbucket is hand-rolled rather than using the library's `Git` connector
because `Git` exposes no pull-request operations, and PRs are the entire point here. Both go
through **`app/clients/http.py`** — `request()` is the single funnel that raises the library's
`ExternalServiceError` (`ExternalServiceError(service_name, status_code, detail=None)`) for
three cases:

| Cause | `status_code` carried |
|-------|----------------------|
| `httpx.TimeoutException` | `504` |
| any other `httpx.RequestError` (DNS, connect, TLS) | `502` |
| non-2xx response | the upstream's own status |

Never call `self._client.request` directly from a client — a bare `httpx.RequestError` escapes
past the routes' `except ExternalServiceError` and the caller gets an opaque 500. The `504`
is load-bearing: `routes.py` keys off `external_error.status_code == 504` to answer 504 instead
of 502. Each client passes its own `detail_from_response` (Bitbucket digs through
`{"errors": [{"message": ...}]}`); `default_detail` is the fallback.

**Shared helpers (`app/helpers.py`)** are pure: `build_kv_values` (the committed document),
`slugify_mount_path`, `build_branch_name`, `values_file_path` and `render_values_yaml`.

The **edit** helper `update_kv_metadata` takes the parsed document and returns a **new** one,
never mutating the input. That is what lets `_edit_values_operation` diff old against new with
`yaml_data_equals` and skip a no-op commit. Any edit helper added later must keep that
contract.

`render_values_yaml` uses a `SafeDumper` subclass that writes multi-line strings as `|`
blocks — the default representer emits one escaped, width-wrapped scalar, which is
unreadable in the pull request diff a human is supposed to review. Only a multi-line
`description` can trigger it today.

## The create chain — read this before editing `operations.py`

**Two shared helpers, nested.** `_open_pull_request` owns branch → commit → PR and both of
their rollbacks (either failure deletes the branch). `_commit_via_pull_request` calls it and
then adds the gates and the merge. So there are three callers in total: the full create and
update go through `_commit_via_pull_request`; `create_kv_pull_request_operation` stops at
`_open_pull_request`. One implementation each means the rollback shape cannot drift.

The baseline watermark is taken **before** `_open_pull_request` and deliberately not folded
into it — it has to precede the branch creation, and the PR-only path has no use for it.

**One chain, two callers.** `_commit_via_pull_request` owns steps 2-6 below and is shared by
create and update, so the rollback asymmetry cannot drift apart between operations.
Create adds the duplicate guard in front; update goes through `_edit_values_operation`, which
reads the committed file, applies a pure mutation, and **short-circuits when nothing changed**
— returning success with `pull_request: null` rather than opening an empty PR. Edits also pass
a `source_commit_id` from `get_last_commit`; without that optimistic-lock token Bitbucket
rejects an edit to an existing path as an attempted create.

`create_kv_mount_operation` runs, in order:

1. duplicate guard — `get_file_content` on the base branch; a hit is a 409, a 404 proceeds
2. `create_branch` → `put_file`
3. `create_pull_request`
4. **gate 1**: `await_pipeline` on the `pull_request` event
5. `get_pull_request` (re-read for the current `version`) → `merge_pull_request`
6. **gate 2**: `await_pipeline` on the `push` event

There is **no transaction**; each step hand-rolls its own rollback:

| Failure | Rollback |
|---------|----------|
| `put_file` | delete the branch |
| `create_pull_request` | delete the branch |
| gate 1 fails or times out | decline the PR **and** delete the branch |
| merge fails | **nothing** — leave the PR open for a human |
| gate 2 fails or times out | **nothing** — already merged; the message says a revert is needed |

**Step 5 is the point of no return.** Preserve that asymmetry when editing. Rollback failures are
logged and never raised over the original error.

`create_kv_pull_request_operation` runs the duplicate guard and steps 2-3, then returns 201
with the PR `OPEN`. It takes **no Woodpecker client** — the argument would be dead weight.
Two consequences, both deliberate and both pinned by tests:

- **Opening a PR still triggers CI in the forge.** The endpoint does not suppress the
  validation pipeline; it just does not wait for it. The response's pipeline fields are `null`
  because nothing was *observed*, not because nothing ran.
- **A repeat call opens a second PR.** The duplicate guard reads the base branch, and an
  unmerged PR is not there, so it cannot see the first one. Catching it would mean listing
  open PRs — a different and inherently racy check. Reviewers close the loser.

Two details that are easy to break:
- **The version re-read before merging/declining.** Bitbucket's `version` is an optimistic lock
  that CI status updates bump; merging with the stale one from `create_pull_request` 409s.
- **The pipeline watermarks — there are two.** `_latest_pipeline_number` is called twice:
  `baseline` before the branch is created, and `merge_baseline` again immediately before the
  merge. Both matchers reject `number <= min_number`. The second one matters because by then the
  validation pipeline exists; reusing `baseline` would let `deploy_pipeline_matcher` latch onto it
  (or onto a rebuild) and report the wrong result. Either degrades to `0` if the list call fails.

Matchers are pure and separately tested: `pull_request_pipeline_matcher` looks for the branch in
`branch`/`ref`/`refspec` (Woodpecker records it differently per forge); `deploy_pipeline_matcher`
prefers the merge commit sha and falls back to the base branch.

Woodpecker statuses: anything in `PENDING_STATUSES` (`pending`, `running`, `blocked`,
`waiting_on_deps`) is non-terminal — `blocked` means awaiting human approval, so it correctly
hangs until the timeout. Only `success` counts as success.

## Auth & config provider

- **Inbound auth is global.** `create_app()` calls `general_create_app(enable_auth=True)`, wiring the
  library's `AuthMiddleware` over every route (except docs/metrics/health/probes). It activates only
  when `AUTH_ENABLED=true` **and** one `AUTH_*` verification material is set (HS256 / JWKS / OIDC
  discovery / local pubkey); otherwise the app boots open. Turning it on with no material set fails
  app creation, and setting two makes the library's mode selection ambiguous — which is why
  `tests/conftest.py` shadows the optional `AUTH_*` knobs and configures HS256 only.
- **No route reads the caller's claims.** Nothing here is per-identity, so there is no
  `get_current_claims` dependency; the middleware alone is the authorization boundary.
- **The Remote Config provider is deliberately not wired.** Unlike `example-api`'s DNS module,
  nothing in this service varies per environment beyond the coordinates the request already
  carries. If that changes, add it the same way: guard on `CONFIG_API_URL`, call
  `enable_remote_config_api`, and pass the provider into `get_v1_vault_router`.

## uv / packaging notes

- uv-managed, Python 3.12. `uv.lock` is committed; regenerate with `uv lock`. `uv.toml` (untracked,
  holds the Artifactory index + creds) is the uv equivalent of `pip.ini`; when both a `uv.toml` and a
  `[tool.uv]` section exist, uv.toml wins — so uv settings go there, not in `pyproject.toml`.
- Public PyPI only has `tashtiot-apis-library==0.1.0`, which lacks `InfraOperationRequest` and
  `fastapi_template.config_api`. This service needs **>=1.1.0**; without it `tests/v1/` cannot even
  be collected (`tests/test_helpers.py` and `tests/clients/` still run).
- **`[project.dependencies]` lists only what this service imports directly.** `prometheus-client`,
  `aiocache`, `cryptography` and `python-dotenv` arrive transitively via the library; re-declaring
  them here just lets the two drift. `loguru` and `pydantic-settings` *are* listed because app code
  imports them by name — they were previously undeclared and worked only by accident.
- **Known gap — the committed `uv.lock` is stale.** `[tool.uv.sources]` has been removed from
  `pyproject.toml`, but `uv.lock` still records `tashtiot-apis-library` as
  `source = { editable = "../apis-library" }`, so `uv sync --frozen` still fails in the Dockerfile
  and the `.woodpecker` Test step. Regenerating it requires Artifactory, which is reachable **only
  on-prem** — see the runbook in the README's "Package index" section. Consequences until then:
  - **Never run a bare `uv run` / `uv sync` off-prem.** With `pyproject.toml` and the lock now
    disagreeing, both try to re-resolve, and `>=1.1.0` is unavailable (public PyPI carries only
    `0.1.0`). Use `uv run --no-sync pytest` — it runs the tests against the existing `.venv`.
  - To develop against the local library, overlay it instead of re-adding a sources block
    (uv refuses `sources` in `uv.toml`): `uv sync --group dev && uv pip install -e ../apis-library`.
- **A clean lock alone does not fix CI.** `uv.toml` is gitignored, so nothing committed declares
  the `pypi-local` / `pypi` indexes; `.woodpecker/build.yaml` injects credentials
  (`UV_INDEX_PYPI_LOCAL_*`) for an index name that is never defined, and the Dockerfile copies no
  index config. If the base image doesn't supply it, declare the URLs (credentials stay in secrets)
  as `[[tool.uv.index]]` in `pyproject.toml`.

## Notes

- The create endpoint **blocks** for the whole chain. Any proxy in front needs a read timeout
  above `CI_PIPELINE_START_TIMEOUT_SECONDS + 2 × CI_PIPELINE_TIMEOUT_SECONDS`. `/pull-request`
  is the escape hatch when that is unacceptable: it answers in one round-trip (~100ms against
  the stub) and hands the merge to a human.
- Status codes: 201 create and PR-only, 200 edits and reads, 409 duplicate, 404 unknown mount, 502
  pipeline/transport failure, 504 pipeline or upstream timeout, 422 request validation.
  Failures reuse `VaultKVOperationResponse` via `VaultOperationError.to_response()`; every
  mutating route funnels through `_execute` in `routes.py`, which is the single place that
  mapping lives.
- **Failures are `return`ed as a `JSONResponse`, not raised.** So the POST decorator's
  `status_code=201` and `response_model=` describe the success path only — FastAPI does not
  validate or document the failure bodies. Adding a field to `VaultKVOperationResponse` means
  the failure path silently omits it unless `to_response()` is updated too.
- The committed document (`kvname` / `description`) is the contract with the deploy
  pipeline — change `build_kv_values` and the pipeline together.
- Tests: 183 of them. `tests/test_helpers.py` (pure, no library import), `tests/clients/` (both
  clients plus `http.py`'s error mapping, against their real REST shapes via respx),
  `tests/v1/vault/` (schemas, the chain + rollbacks with duck-typed fakes from `tests/fakes.py`,
  and routes end-to-end through `TestClient`). Two conftest details to respect:
  - `tests/conftest.py` sets `os.environ` **before** any `app.*` import (its own `import jwt` /
    `import pytest` sit below the env block behind `# noqa: E402`) because the config singletons
    are built at import time. Adding a required setting means adding it there, above that line.
    It also blanks the optional `AUTH_*` knobs so a developer `.env` cannot change outcomes, and
    zeroes the `CI_*` timeouts to keep the suite fast.
  - `tests/v1/vault/conftest.py`'s `client` fixture monkeypatches `app.main.BitbucketClient` and
    `app.main.WoodpeckerClient` — the classes, not instances. That works only because
    `create_app()` is the sole construction site; keep it that way.
- **Delete** is the only operation still missing. It is the same chain with a file removal as
  step 2 — keep the rollback shape and reuse `_commit_via_pull_request`.
- **`kubernetes-auth` and `groups` endpoints used to exist and were deleted.** They edited a
  `policies` list that a create never writes, so against every file this service produces they
  returned 422 unconditionally. If the deploy pipeline ever grows policies into the committed
  file and you want them back, they are in git history — but the rule stands: no policy
  *generation* in this service.
- Commits are expected to be **Conventional Commits** (`feat:`, `fix:`, `test:`, `feat!:` for a
  breaking change) — follow the existing `git log`. There is no changelog tooling: a `cliff.toml`
  was removed because nothing ran it (no tags, no `CHANGELOG.md`, and its header described GitHub
  Actions + setuptools-scm, neither of which exists here). The version is a literal in
  `pyproject.toml` plus the `APP_VERSION` build arg.
