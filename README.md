# vault-api

A FastAPI service that **writes a file to a Bitbucket repo and watches the Woodpecker
pipelines that act on it**, built on the internal **`tashtiot-apis-library`** and following
the patterns in the reference `example-api`.

A create request is not a direct write to Vault. It opens a pull request against the values
repo, waits for CI to validate it, merges it, and waits for the deploy pipeline that applies
the change — then answers `Successful creation of <kv name>`.

**The service knows nothing about Vault.** It appends an entry to a values file — a KV store
or a Kubernetes auth role — and reports what the pipelines did with it; mounts, engine
versions and policies are the deploy pipeline's business.

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
  "file": "athena",                // which values file; ^[a-z0-9]+(-[a-z0-9]+)*$
  "kv_name": "athena-passwords",   // unique across ALL files; same pattern, no slash
  "kv_description": "Passwords for athena",
  "roles": {                       // required: >=1 role with >=1 principal
    "write": ["CN=svc-athena,OU=ServiceAccounts,DC=corp,DC=example,DC=com"],
    "read":  ["CN=app-athena,OU=ServiceAccounts,DC=corp,DC=example,DC=com"]
  }                                // either key, or both; values are not validated
}
```

`file` is the only field that reaches a filesystem path, and its pattern blocks `..` and
slashes, so a request can never escape the values directory. `kv_name` is a value inside the
document; it is single-segment because the read/update routes address a store as
`{file}/{kv_name}`.

### The committed file (`kv/payments.yaml`)

**One file holds many stores, and each store holds its own service account bindings.** A
create appends to `kvStores`, creating the file only if this is its first store. This is the
contract with the deploy pipeline — **change it in lockstep with the pipeline that consumes
it**:

```yaml
kvStores:
  - name: athena-passwords
    description: Passwords for athena
    roles:
      write:
        - CN=<CN>,OU=<OU>,DC=<DC>       # LDAP DNs, not hostnames
      k8sServiceAccounts:               # inside `roles`, level with `write`
        - serviceAccount: "vault"       # the whole triple is the binding's identity
          namespace: "athena"
          cluster: dev                  # OS4 cluster suffix
        - serviceAccount: "vault"       # can be many
          namespace: "athena-staging"
          cluster: dev
  - name: billing
    description: billing secrets
    roles:
      read:
        - CN=svc-billing,OU=ServiceAccounts,DC=corp,DC=example,DC=com
```

**A service account is listed inside the store it reaches**, under `roles` and level with
`read`/`write`. That direction matters: nothing points at a binding from elsewhere, so
deleting a store removes its bindings in the same diff — there is no dangling reference to
guard against, and no referential check anywhere in this service.

Because the bindings sit *under* `roles`, a `PATCH` that replaces `roles` **keeps them**.
The update body has no way to express a binding, so dropping them would be data loss you
could not undo in the same call. Changing who may read a secret and changing which workloads
are bound to it are separate operations, and stay that way.

A binding carries **no capability of its own**. Listing the account inside the store *is* the
grant; what it may then do is the deploy pipeline's to decide, exactly as with `roles`.

`read` and `write` are **separate keys** on `roles` — a store may carry either, or both. The
combined string `read/write` is not a role and is rejected with `422`. `ALLOWED_ROLE_KEYS` in
`app/v1/vault/schemas.py` is the single place to change if the pipeline ever wants a
different set.

The **values** under `roles` are not validated beyond non-blank and unique: the files carry
LDAP distinguished names as well as bare hostnames, and any pattern tight enough to describe
one rejects the other.

### Other routes

| Route | Purpose | Success |
|-------|---------|---------|
| `POST /api/vault/v1/kv/pull-request` | open the pull request and stop — no CI wait, no merge | `201` |
| `GET /api/vault/v1/kv/{file}` | the whole values file (404 if absent) | `200` |
| `GET /api/vault/v1/kv/{file}/{kv_name}` | one store out of it | `200` |
| `PATCH /api/vault/v1/kv/{file}/{kv_name}` | change one store's `description` and/or `roles` | `200` |
| `DELETE /api/vault/v1/kv/{file}/{kv_name}` | remove one store, through the same chain | `200` |
| `DELETE /api/vault/v1/kv/{file}/{kv_name}/pull-request` | open the removal pull request and stop | `201` |
| `POST /api/vault/v1/kv/{file}/{kv_name}/k8s-service-accounts` | bind a service account to that store | `201` |
| `POST /api/vault/v1/kv/{file}/{kv_name}/k8s-service-accounts/pull-request` | open the pull request and stop | `201` |
| `GET /api/vault/v1/kv/{file}/{kv_name}/k8s-service-accounts` | that store's bindings | `200` |
| `DELETE /api/vault/v1/kv/{file}/{kv_name}/k8s-service-accounts` | unbind one, by `?service_account=&namespace=&cluster=` | `200` |
| `DELETE /api/vault/v1/kv/{file}/{kv_name}/k8s-service-accounts/pull-request` | open the removal pull request and stop | `201` |

`file` and `kv_name` are pattern-checked in the URL as well as in a create body, so a
malformed one is a `422` here rather than an opaque Bitbucket `404` two calls later. The
unbind query parameters carry the same patterns the bind body enforces.

`GET /api/vault/v1/kv/{file}` returns *the file* — every store, each with its own bindings —
as parsed YAML, unchanged.

### `POST /pull-request` — the non-blocking half

Same body as a create, same uniqueness check, same commit, same pull request — then it
**returns**, answering `201` in one round-trip instead of blocking for two pipelines:

```jsonc
{"file": "athena", "kv_name": "athena-passwords", "kv_description": "Passwords for athena",
 "roles": {"write": ["CN=svc-athena,OU=ServiceAccounts,DC=corp,DC=example,DC=com"]}}
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

### `DELETE` — a content edit, not a file removal

```jsonc
// DELETE /api/vault/v1/kv/payments/myapp
// -> 200 {"status":"Succeeded","message":"Successful deletion of myapp", ...}
```

Same pull request → CI → merge → CI chain, same rollbacks, same point of no return. What is
worth knowing:

- **Only the named entry goes.** Its siblings in the file are written back untouched.
- **Deleting the last store leaves the file behind**, holding `kvStores: []`. `GET /{file}`
  keeps answering `200`, and a later create appends to it normally. *This assumes the deploy
  pipeline reads an empty list as "remove everything this file declared"* — if it cannot,
  the file has to be deleted instead, which needs a Bitbucket call this service does not make
  today.
- **A repeat delete is `404`**, the same answer `GET` and `PATCH` already give for a store
  that is not there. The *state* is idempotent; the status code tells you which call did the
  work. A client whose connection dropped mid-delete cannot tell "already gone" from "never
  existed" — confirm with `GET /{file}`.
- **`DELETE .../pull-request` is the escape hatch**, answering `201` in one round-trip and
  leaving the removal for a reviewer to merge. It matters more here than for a create: this
  is the destructive one. Nothing reaches the base branch, so calling it twice opens two
  pull requests for the same removal.
- **Deleting a store takes its service account bindings with it**, in the same diff. There is
  no `409` for a "still referenced" store and no referential check anywhere — the bindings
  live inside the store, so there is nothing left over to dangle.

### `/k8s-service-accounts` — binding workloads to a store

Same chain, same rollbacks, same point of no return — as a sub-resource of the store, because
that is where the entry lives in the document:

```jsonc
POST /api/vault/v1/kv/athena/athena-passwords/k8s-service-accounts
{
  "service_account": "vault",    // RFC 1123 subdomain
  "namespace": "athena",         // RFC 1123 label
  "cluster": "dev"               // OS4 cluster suffix — required
}
```

- **A binding is a coordinate, not a policy.** It names a workload and nothing else. The
  deploy pipeline owns what a bound account may do with the store; this service never writes a
  policy name, a policy body, an HCL path, a mount path or an engine version.
- **The store must already exist** on the base branch, or the request is a `404`. A binding
  cannot create the store it lives in. Consequence: create a store with
  `POST /kv/pull-request` and bind to it immediately, and you get a `404` until that pull
  request merges — the same base-branch-only visibility that makes a repeat `/pull-request`
  open a second PR.
- **All three fields are required, and together they are the identity.** There is no name and
  no `PATCH`: changing a binding is an unbind plus a bind. Unbinding therefore takes all three
  as query parameters — `DELETE .../k8s-service-accounts?service_account=vault&namespace=athena&cluster=dev`
  — and a partial triple is a `422`, not a wrong-binding delete. They travel as query
  parameters because DELETE bodies are widely dropped by proxies and clients.
- **`*` in `service_account` or `namespace` is rejected** with `422`. It would bind every
  workload in the cluster; that escalation should need a human editing the YAML, not an API
  call.
- **Names follow Kubernetes' rules, not ours.** A namespace is an RFC 1123 *label* (no dots,
  ≤63); a ServiceAccount name is an RFC 1123 *subdomain* (dots allowed, ≤253). Neither may
  contain uppercase — a name this service accepted but the cluster rejects would fail at
  admission, long after the pull request merged.
- **Unbinding the last one drops the `k8sServiceAccounts` key** from `roles`, leaving the
  store exactly as a fresh create would have written it, rather than an empty list no create
  would produce.

### Uniqueness

Store names are global to Vault, so a create scans **every** file in the values directory and
returns `409` if the name is used anywhere — not just in the target file. That costs a
directory listing plus a read per file, and it is a check, not a lock: two creates in flight
both pass it and the second pull request conflicts at merge, which a human resolves.

Bindings are scoped far more narrowly: unique on the whole `(serviceAccount, namespace,
cluster)` triple **within one store**, and nothing beyond it. The same service account
reaching two stores is the normal case — that is how one workload gets at two secrets — and
the same account in two namespaces or two clusters is two distinct bindings. Nothing about a
binding is directory-wide, so binding and unbinding never scan the values directory at all.

---

## How the library is used

`create_app()` (`app/main.py`) is the single wiring point, exactly as in the reference API: it
calls `general_create_app(enable_auth=True)`, builds each connector **once**, and injects them
into the router factory with `app.include_router(get_v1_vault_router(...))`. One router:
service account bindings are a sub-resource of a store, not a resource kind of their own.
There is no FastAPI `Depends` for connectors — the factory closure is the injection seam.

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
| `conf.py` | the module's `BaseSettings` + `config` singleton (both API prefixes, both branch prefixes) |
| `schemas.py` | pydantic request/response models (`VaultKVCreate` is a flat four-field body); one `VaultOperationResponse` for both resource kinds |
| `operations.py` | the create/update/delete chain for both kinds, the rollbacks and the pipeline matchers; receives connectors as arguments |
| `routes.py` | `get_v1_vault_router(...)`, prefixed with `config.API_PREFIX`; stores and their bindings both hang off it |

`app/helpers.py` holds the pure functions: branch/file naming, both entry shapes, YAML
rendering and comparison. Every transform returns a **new** document and preserves the sibling
top-level key, which is what lets the two kinds share one file safely.

All settings added for Kubernetes auth are **defaulted**, so nothing new is required in `.env`.

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
  test_helpers.py    # pure helpers: stores and their nested bindings
  clients/           # both clients against their real REST shapes (respx)
  v1/vault/          # schemas, the chains + rollbacks, routes end-to-end
                     #   test_k8s_service_account_*.py mirror the KV files one-for-one
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
- **A second kind of top-level entry.** The `kvStores` helpers in `app/helpers.py` are thin
  wrappers over one key-parameterised family (`_entries` / `_add_entry` / `_update_entry` /
  `_remove_entry`, each taking the top-level key). Add a second by parameterising, never by
  copy-pasting the family — the "return a new document, preserve every sibling key" contract
  is what `yaml_data_equals` and the no-op short circuit depend on. For a second list *nested
  inside* a store, `_mutate_store` is the counterpart.
- **Policy-shaped edits.** Two earlier Kubernetes shapes were removed. The first, plus a
  `groups` endpoint, edited a `policies` list a create never writes, so they returned `422`
  unconditionally. The second modelled bindings as a top-level `kubernetesAuth` list of named
  roles pointing back at stores; the pipeline's format nests them in the store instead.
  Neither is a source to copy from. Policy *generation* stays out of this service
  permanently; `groups` has no replacement.

## Docker

```bash
docker build -t vault-api .
docker run -p 5000:5000 --env-file .env vault-api
```

CI (`.woodpecker/build.yaml`) runs the test suite on every push to `master`, and builds and
pushes the image on git **tags**.
