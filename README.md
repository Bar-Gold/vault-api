# vault-api

A FastAPI service that **writes a file to a Bitbucket repo and watches the Woodpecker
pipelines that act on it**, built on the internal **`tashtiot-apis-library`** and following
the patterns in the reference `example-api`.

A create request is not a direct write to Vault. It opens a pull request against the values
repo, waits for CI to validate it, merges it, and waits for the deploy pipeline that applies
the change — then answers `Successful creation of <kv name>`.

**The service knows nothing about Vault.** It appends an entry to a values file holding a
list of KV stores, and reports what the pipelines did with it; mounts, engine versions and
policies are the deploy pipeline's business.

---

## Quick start

```bash
# Requires a uv.toml pointing at the internal Artifactory index (see below).
uv sync --group dev        # install deps (incl. tashtiot-apis-library) into .venv

cp .env.example .env       # then fill in the values you need
uv run python -m app.main  # serves on 0.0.0.0:5000  (app factory: app/main.py:create_app)
```

- Swagger UI: <http://localhost:5000/docs>
- Prometheus metrics: <http://localhost:5000/metrics>

Both come from the library's `general_create_app()`.

```bash
uv run pytest              # everything under tests/
uv run pytest tests/v1/vault -v
```

### Package index (`uv.toml`)

`tashtiot-apis-library` is served from the internal Artifactory, not public PyPI (public PyPI
only carries a stale `0.1.0` that lacks `InfraOperationRequest` and `fastapi_template.config_api`
— this service needs **`>=1.1.0`**). Copy `uv.toml.example` to `uv.toml` and fill in the
Artifactory user/token, or put it at `%APPDATA%\uv\uv.toml` to share it across projects.
A repo-root `uv.toml` wins if both exist.

The index URLs embed a `user:token`, so **`uv.toml` is gitignored** — keep it local. `uv.lock`
**is** committed; regenerate it with `uv lock` when dependencies change.

> ⚠️ **The committed `uv.lock` is stale and Docker/CI will fail until it is regenerated on-prem.**
>
> `[tool.uv.sources]` has been removed from `pyproject.toml`, but the committed `uv.lock` still
> records `tashtiot-apis-library` as `source = { editable = "../apis-library" }` — a path that
> exists only in a local dev checkout. `uv sync --frozen` therefore still fails in the Dockerfile
> and in the `.woodpecker` Test step. The lock can only be regenerated where Artifactory is
> reachable, so run this **on-prem**:
>
> ```bash
> cp uv.toml.example uv.toml     # fill in the real index URLs + credentials
> uv lock                        # resolves tashtiot-apis-library>=1.1.0 from Artifactory
> grep -c 'apis-library' uv.lock # must print 0 — no editable path left
> uv sync --frozen --group dev && uv run pytest
> ```
>
> Then delete the stale `NOTE:` comment above `uv sync --frozen` in `.woodpecker/build.yaml`,
> and commit the regenerated `uv.lock`.
>
> **Docker and CI also need the index itself, not just the lock.** `uv.toml` is gitignored, so
> no committed file declares the `pypi-local` / `pypi` indexes. `.woodpecker/build.yaml` injects
> `UV_INDEX_PYPI_LOCAL_USERNAME` / `_PASSWORD`, which are credentials *for an index name that is
> never defined*, and the Dockerfile copies no index config at all. Unless the
> `generic-python312` base image already supplies it, add the index URLs (no credentials) as
> `[[tool.uv.index]]` entries in `pyproject.toml` so those existing secrets resolve against them.
>
> Until the lock is regenerated, local runs must avoid re-resolving — use the existing `.venv`:
>
> ```bash
> uv run --no-sync pytest
> ```

---

## The create flow

```
POST /api/vault/v1/kv/
  │
  ├─ 0. reject if the store name is used in ANY file on the base branch    -> 409
  ├─ 1. create branch  vault-kv/<file>-<kv-name>-<rand>
  ├─ 2. commit         kv/<file>.yaml   (append to kvStores; create if first)
  ├─ 3. open pull request -> base branch
  ├─ 4. WAIT for the Woodpecker `pull_request` pipeline  ── fails ──> decline PR + delete branch -> 502
  │                                                      ── times out ─> decline PR + delete branch -> 504
  ├─ 5. merge the pull request                           ── fails ──> leave PR open              -> 502
  ├─ 6. WAIT for the Woodpecker `push` pipeline          ── fails ──> report (already merged)    -> 502
  │                                                      ── times out ─> report (already merged) -> 504
  └─ 201 {"status":"Succeeded","message":"Successful creation of myapp", ...}
```

**The request blocks for the whole chain.** That is deliberate (the endpoint's contract is the
final answer, not a job handle) — see `POST /pull-request` below for the non-blocking
alternative — but it has two consequences:

- any proxy/ingress in front of this service needs a read timeout larger than
  `CI_PIPELINE_START_TIMEOUT_SECONDS + 2 × CI_PIPELINE_TIMEOUT_SECONDS`;
- **step 5 is the point of no return.** Steps 1–4 are fully rolled back on failure. Once the PR
  is merged, a failing deploy pipeline is *reported*, not undone — un-merging needs a revert PR,
  which is a human decision. The error message says so explicitly.

### Request

```jsonc
POST /api/vault/v1/kv/
{
  "file": "payments",              // which values file; ^[a-z0-9]+(-[a-z0-9]+)*$
  "kv_name": "myapp",              // unique across ALL files; same pattern, no slash
  "kv_description": "payments secrets",
  "roles": {                       // required: >=1 role with >=1 host
    "read":  ["app01.corp.example.com"],
    "write": ["app02.corp.example.com"]    // either key, or both
  }
}
```

`file` is the only field that reaches a filesystem path, and its pattern blocks `..` and
slashes, so a request can never escape the values directory. `kv_name` is a value inside the
document; it is single-segment because the read/update routes address a store as
`{file}/{kv_name}`.

### The committed file (`kv/payments.yaml`)

**One file holds many stores.** A create appends to the `kvStores` list, creating the file
only if this is its first store. This is the contract with the deploy pipeline — **change it
in lockstep with the pipeline that consumes it**:

```yaml
kvStores:
  - name: myapp
    description: payments secrets
    roles:
      read:
        - app01.corp.example.com
      write:
        - app02.corp.example.com
  - name: billing
    description: billing secrets
    roles:
      read:
        - app03.corp.example.com
        - app04.corp.example.com
```

`read` and `write` are **separate keys** — a store may carry either, or both. The combined
string `read/write` is not a role and is rejected with `422`. `ALLOWED_ROLE_KEYS` in
`app/v1/vault/schemas.py` is the single place to change if the pipeline ever wants a
different set of role names.

### Other routes

| Route | Purpose |
|-------|---------|
| `POST /api/vault/v1/kv/pull-request` | open the pull request and stop — no CI wait, no merge |
| `GET /api/vault/v1/kv/{file}` | the whole values file (404 if absent) |
| `GET /api/vault/v1/kv/{file}/{kv_name}` | one store out of it |
| `PATCH /api/vault/v1/kv/{file}/{kv_name}` | change one store's `description` and/or `roles` |

### `POST /pull-request` — the non-blocking half

Same body as a create, same uniqueness check, same commit, same pull request — then it
**returns**, answering `201` in one round-trip instead of blocking for two pipelines:

```jsonc
{"file": "payments", "kv_name": "myapp", "kv_description": "payments secrets",
 "roles": {"read": ["app01.corp.example.com"]}}
// -> 201 {"status":"Succeeded", "pull_request":{"id":42,"state":"OPEN"},
//         "validation_pipeline":null, "deploy_pipeline":null}
```

Nothing reaches the base branch — a human reviews and merges. Rollbacks still apply: a failed
commit or a failed PR deletes the branch it created.

Two behaviours to know, both intentional:

- **CI still runs.** Opening a pull request triggers the validation pipeline in the forge like
  any other PR. This endpoint simply does not *wait* for it, which is why the pipeline fields
  come back `null` — nothing was observed, not nothing happened.
- **Calling it twice for the same name opens two pull requests.** The name scan reads the base
  branch, and an unmerged PR is not on it. Reviewers close the loser.

`PATCH` runs the **same** pull request → CI → merge → CI chain as a create, and answers `200`.
Three behaviours worth relying on:

- **Only the named store changes.** Its siblings in the file are written back untouched.
- **`roles` is replaced wholesale**, not merged — otherwise a host could never be removed.
- **An edit that changes nothing opens no pull request.** Re-applying the same change returns
  `Succeeded` with `"No changes required for <kv name>"` and `pull_request: null`.
- **`PATCH` cannot rename.** The name is fixed at creation, because renaming means migrating
  the secrets in Vault, not editing a field.

```jsonc
// PATCH /api/vault/v1/kv/payments/myapp
{"kv_description": "new text"}
{"roles": {"read": ["app02.corp.example.com"]}}    // or both
```

### Uniqueness

Store names are global to Vault, so a create scans **every** file in the values directory and
returns `409` if the name is used anywhere — not just in the target file. That costs a
directory listing plus a read per file. It is a check, not a lock: two creates in flight both
pass it and the second pull request conflicts at merge, which a human resolves.

---

## How the library is used

`create_app()` (`app/main.py`) is the single wiring point, exactly as in the reference API: it
calls `general_create_app(enable_auth=True)`, builds each connector **once**, and injects them
into the router factory with `app.include_router(get_v1_vault_router(...))`. There is no
FastAPI `Depends` for connectors — the factory closure is the injection seam.

### Two-layer configuration

- **`app/global_conf.py`** (`global_config`) — cross-cutting: `BITBUCKET_*`, `WOODPECKER_*`,
  `HTTP_TIMEOUT_SECONDS`, `VERIFY_SSL`.
- **`app/v1/vault/conf.py`** (`config`) — the module's own: `API_PREFIX`, `API_TAGS`, the values
  repo layout, PR shaping and the `CI_*` timeouts.

`AUTH_*` / `AUTH_SSO_*` are **not** declared in either — the library reads those from the
environment itself.

### Connectors

| Client | Built on | Why |
|--------|----------|-----|
| `app/clients/bitbucket.py` | library `BaseAPI` | the library's `Git` connector does file CRUD on a default ref but exposes no **pull-request** operations, and PRs are the whole point of this service |
| `app/clients/woodpecker.py` | library `BaseAPI` | no Woodpecker connector exists in the library |

Both normalise every non-2xx into the library's `ExternalServiceError`, which the routes map to
a 502 — the same failure contract the reference API uses. `BaseAPI` defaults `verify=False`;
`create_app()` opts back in via `VERIFY_SSL`.

### Per-module layout (`app/v1/vault/`)

| File | Responsibility |
|------|----------------|
| `conf.py` | the module's `BaseSettings` + `config` singleton |
| `schemas.py` | pydantic request/response models (`VaultKVCreate` is a flat two-field body) |
| `operations.py` | the create chain, the rollbacks and the pipeline matchers; receives connectors as arguments |
| `routes.py` | `get_v1_vault_router(bitbucket, woodpecker) -> APIRouter`, prefixed with `config.API_PREFIX` |

`app/helpers.py` holds the pure functions: branch/file naming, the values-file shape, YAML
rendering and comparison.

---

## Authentication

Inbound auth is **global**: `general_create_app(enable_auth=True)` wires the library's
`AuthMiddleware` over every route (except `/docs`, `/metrics`, `/health`, `/openapi.json`,
`/static`, probes). It activates only when `AUTH_ENABLED=true` **and** exactly one verification
material is set (`AUTH_HS256_SECRET` / `AUTH_JWKS_URL` / `AUTH_OIDC_ISSUER` /
`AUTH_PUBLIC_KEY_*`). With the switch off the app boots open; turning it on with no material set
fails app creation. See `.env.example`.

---

## Project structure

```
app/
  main.py            # create_app(): the wiring point (connectors + routers)
  global_conf.py     # shared BaseSettings (global_config)
  helpers.py         # naming, values-file shape, YAML rendering (pure)
  clients/
    bitbucket.py     # branch / file / pull-request REST client
    woodpecker.py    # pipeline discovery + polling
    http.py          # the single funnel that raises ExternalServiceError
  v1/vault/          # conf, schemas, operations, routes
tests/
  test_helpers.py    # pure helpers
  clients/           # both clients against their real REST shapes (respx)
  v1/vault/          # schemas, the create chain + rollbacks, routes end-to-end
tools/
  stub_upstreams.py  # local Bitbucket + Woodpecker stand-in (see below)
```

### Driving the chain locally

`tools/stub_upstreams.py` impersonates both upstreams in one process, so the whole create
chain runs over real HTTP with no Docker, licence or network:

```bash
uv run --no-sync python tools/stub_upstreams.py --port 9000     # terminal 1
# terminal 2: point BITBUCKET_URL / WOODPECKER_URL at it — full env in the module docstring

curl -X POST 127.0.0.1:9000/__control -d '{"validation":"failure"}'   # success|failure|hang
curl 127.0.0.1:9000/__state                                           # branches, PRs, pipelines
```

Requires `tashtiot-apis-library >= 1.1.0`.

---

## Extension points

- **Remote Config provider.** Not wired here (nothing in this service is per-environment beyond
  the coordinates already in the request). Add it exactly as `example-api` does — guard on
  `CONFIG_API_URL`, call `enable_remote_config_api`, and pass the provider into the router
  factory — if you later need, say, a different values repo per environment.
- **Delete.** The only operation still missing. It is the same chain with a file removal as the
  commit step — reuse `_commit_via_pull_request` and keep the rollback shape.
- **Policy-shaped edits.** `kubernetes-auth` and `groups` endpoints existed and were removed:
  they edited a `policies` list that a create never writes, so they returned `422`
  unconditionally against every file this service produces. They are recoverable from git
  history if the deploy pipeline ever writes policies into the committed file — but policy
  *generation* stays out of this service.

## Docker

```bash
docker build -t vault-api .
docker run -p 5000:5000 --env-file .env vault-api
```

CI (`.woodpecker/build.yaml`) runs the test suite on every push to `master`, and builds and
pushes the image on git **tags**.
