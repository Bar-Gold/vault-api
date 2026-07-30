# vault-api

A FastAPI service that creates **HashiCorp Vault KV mounts and their policies through GitOps**,
built on the internal **`tashtiot-apis-library`** and following the patterns in the reference
`example-api`.

A create request is not a direct write to Vault. It opens a pull request against the Vault
values repo, waits for CI to validate it, merges it, and waits for the deploy pipeline that
applies the change — then answers `Successful creation of <mount path>`.

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
  ├─ 0. reject if the values file already exists on the base branch        -> 409
  ├─ 1. create branch  vault-kv/<env>-<app>-<rand>
  ├─ 2. commit         kv/<env>/<app>.yaml
  ├─ 3. open pull request -> base branch
  ├─ 4. WAIT for the Woodpecker `pull_request` pipeline  ── fails ──> decline PR + delete branch -> 502
  │                                                      ── times out ─> decline PR + delete branch -> 504
  ├─ 5. merge the pull request                           ── fails ──> leave PR open              -> 502
  ├─ 6. WAIT for the Woodpecker `push` pipeline          ── fails ──> report (already merged)    -> 502
  │                                                      ── times out ─> report (already merged) -> 504
  └─ 200/201 {"status":"Succeeded","message":"Successful creation of kingmagen/prod/myapp", ...}
```

**The request blocks for the whole chain.** That is deliberate (the endpoint's contract is the
final answer, not a job handle), but it has two consequences:

- any proxy/ingress in front of this service needs a read timeout larger than
  `CI_PIPELINE_START_TIMEOUT_SECONDS + 2 × CI_PIPELINE_TIMEOUT_SECONDS`;
- **step 5 is the point of no return.** Steps 1–4 are fully rolled back on failure. Once the PR
  is merged, a failing deploy pipeline is *reported*, not undone — un-merging needs a revert PR,
  which is a human decision. The error message says so explicitly.

### Request

```jsonc
POST /api/vault/v1/kv/
{
  "metadata": {                    // the six infra coordinates (InfraOperationRequest)
    "space": "net", "network": "net", "region": "kirya",
    "environment": "prod", "project": "payments"
  },
  "spec": {
    "app_name": "myapp",           // ^[a-z0-9]+(-[a-z0-9]+)*$, <=40 chars
    "owner": "team-dl@example.com",
    "kv_version": 2,               // 1 or 2, default 2
    "max_versions": 10,            // KV v2 only
    "delete_version_after": "720h",// KV v2 only, optional
    "readers": ["group/app-readers"],
    "writers": ["group/app-writers"]
  }
}
```

`spec.app_name` **is** the Vault mount path, used verbatim — nothing is prefixed for you. It
may be a multi-segment path, so `payments/vault-secrets` is a valid name and produces the
policies `payments-vault-secrets-read` / `-write` (policy names cannot contain slashes, so
they are flattened).

Two things to know:

- **Nothing namespaces mounts for you.** Include the team and/or environment in the name if
  you want them separated.
- **The environment is not part of the mount path**, but the values *file* is per environment.
  The same name in `prod` and `dev` gives two files pointing at one Vault mount — encode the
  environment in the name if the mounts should be distinct.

Names are lowercase alphanumeric segments separated by dashes, joined by single slashes
(`^[a-z0-9]+(-[a-z0-9]+)*(/...)*$`, max 128 chars). That also blocks `..`, so a name can never
escape the values directory.

### The committed values file (`kv/prod/myapp.yaml`)

This is the contract with the deploy pipeline — **change it in lockstep with the pipeline that
consumes it**:

```yaml
mount:
  path: kingmagen/prod/myapp
  type: kv
  options: {version: '2'}
  description: KV store for myapp (prod)
  config: {max_versions: 10}
policies:
  - name: kingmagen-prod-myapp-read
    rules: |
      path "kingmagen/prod/myapp/data/*" { capabilities = ["read", "list"] }
      path "kingmagen/prod/myapp/metadata/*" { capabilities = ["read", "list"] }
    entities: [group/app-readers]
  - name: kingmagen-prod-myapp-write
    rules: |
      ...
    entities: [group/app-writers]
metadata: {app: myapp, team: kingmagen, environment: prod, owner: team-dl@example.com}
```

### Other routes

| Route | Purpose |
|-------|---------|
| `GET /api/vault/v1/kv/{app_name}?environment=prod` | the committed values file for a mount (404 if absent) |
| `PATCH /api/vault/v1/kv/{app_name}` | change the mount's `description` and/or `owner` |
| `POST /api/vault/v1/kv/{app_name}/kubernetes-auth` | bind Kubernetes service accounts to the read or write policy |
| `POST /api/vault/v1/kv/{app_name}/groups` | grant an AD group the read or write policy |

The three edit routes run the **same** pull request → CI → merge → CI chain as a create, and
answer `200`. Two behaviours worth relying on:

- **An edit that changes nothing opens no pull request.** Re-adding a group that is already
  bound, or an identical Kubernetes role, returns `Succeeded` with
  `"No changes required for <mount>"` and `pull_request: null`.
- **`PATCH` cannot rename.** The mount path and policy names are fixed at creation, because
  renaming a Vault mount means migrating its secrets, not editing a field.

```jsonc
// POST /api/vault/v1/kv/payments/vault-secrets/kubernetes-auth
{
  "metadata": { /* the infra coordinates, as above */ },
  "spec": {
    "role": "payments-api",            // optional; defaults to the flattened mount path
    "service_accounts": ["payments-api"],
    "namespaces": ["payments-prod"],
    "capability": "write",             // "read" (default) or "write"
    "ttl": "24h"                       // optional
  }
}

// POST /api/vault/v1/kv/payments/vault-secrets/groups
{
  "metadata": { /* ... */ },
  "spec": {"group": "AD\\payments-readers", "capability": "read"}
}
```

Which append to the values file:

```yaml
kubernetes_auth:
  - role: payments-api
    service_accounts: [payments-api]
    namespaces: [payments-prod]
    policies: [payments-vault-secrets-write]
    ttl: 24h
```

---

## How the library is used

`create_app()` (`app/main.py`) is the single wiring point, exactly as in the reference API: it
calls `general_create_app(enable_auth=True)`, builds each connector **once**, and injects them
into the router factory with `app.include_router(get_v1_vault_router(...))`. There is no
FastAPI `Depends` for connectors — the factory closure is the injection seam.

### Two-layer configuration

- **`app/global_conf.py`** (`global_config`) — cross-cutting: `BITBUCKET_*`, `WOODPECKER_*`,
  `VAULT_*`, `TEAM_NAME`, `HTTP_TIMEOUT_SECONDS`, `VERIFY_SSL`.
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
| `schemas.py` | pydantic request/response models (`VaultKVCreate` subclasses the library's `InfraOperationRequest`) |
| `operations.py` | the create chain, the rollbacks and the pipeline matchers; receives connectors as arguments |
| `routes.py` | `get_v1_vault_router(bitbucket, woodpecker) -> APIRouter`, prefixed with `config.API_PREFIX` |

`app/helpers.py` holds the pure functions: mount-path/policy naming, policy HCL rendering, the
values-file shape and YAML comparison.

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
  helpers.py         # naming, policy rendering, values-file shape (pure)
  clients/
    bitbucket.py     # branch / file / pull-request REST client
    woodpecker.py    # pipeline discovery + polling
  v1/vault/          # conf, schemas, operations, routes
tests/
  test_helpers.py    # pure helpers
  clients/           # both clients against their real REST shapes (respx)
  v1/vault/          # schemas, the create chain + rollbacks, routes end-to-end
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

## Docker

```bash
docker build -t vault-api .
docker run -p 5000:5000 --env-file .env vault-api
```

CI (`.woodpecker/build.yaml`) runs the test suite on every push to `master`, and builds and
pushes the image on git **tags**.
