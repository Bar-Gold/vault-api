# vault-api

A FastAPI service that **writes a file to a Bitbucket repo and watches the builds that act
on it**, built on the internal **`tashtiot-apis-library`** and following the patterns in the
reference `example-api`.

A create request is not a direct write to Vault, and not a merge either. It opens a pull
request against the values repo, waits for CI to validate it, and answers with the pull
request **open and waiting for review**. Who merges, and when, is a human decision this
service deliberately does not make — so nothing it does ever reaches the base branch.

**Bitbucket is the only upstream.** The CI gates are read from Bitbucket's own build-status
store — the thing a pull request's **Builds** tab renders — rather than from the CI server's
API. A CI server posts its result *into* Bitbucket against the commit it built, so watching
CI is just "ask Bitbucket which builds it holds for this sha". There is no CI URL, token or
repo id to configure; the pipelines still run, they are simply observed through the forge.

**The service knows nothing about Vault.** It writes YAML holding a list of **grants** — who
may do what, over which secret path — and reports what the pipelines did with it. Mounts,
engine versions and policies are the deploy pipeline's business.

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
  ├─ 0. reject if the store name is used by ANY team on the base branch    -> 409
  ├─ 1. create branch  vault-kv/<team>-<kv-name>-<rand>
  ├─ 2. commit         KVs/<team>/<kv-name>/oidc-access.yaml
  │                    KVs/<team>/<kv-name>/k8s-access.yaml  (only if it has k8s grants)
  ├─ 3. open pull request -> base branch
  ├─ 4. WAIT for the builds on the PR's commit           ── red ────> decline PR + delete branch -> 502
  │       (Bitbucket: fromRef.latestCommit)              ── times out ─> decline PR + delete branch -> 504
  └─ 201 {"status":"Succeeded","message":"Opened pull request 42 to create my-passwords. ..."}

         the pull request is OPEN and green. A human merges it.
```

**The request blocks until CI answers.** That is deliberate — the endpoint's contract is "the
change is proposed *and* green", not a job handle — and there is no variant that returns
sooner. Two consequences:

- any proxy/ingress in front needs a read timeout larger than
  `CI_PIPELINE_START_TIMEOUT_SECONDS + CI_PIPELINE_TIMEOUT_SECONDS` — one gate's worth;
- **there is no point of no return.** Every failure is fully rolled back: a bad commit or a
  failed PR deletes the branch, and a red or slow build declines the pull request *and*
  deletes the branch. Nothing is ever half-applied, and nothing ever needs a revert.

**What the gate actually reads.** `GET /rest/build-status/1.0/commits/{sha}`, polled until every
build on that commit has left `INPROGRESS`. That resource is deprecated but is the only one
that *lists* a commit's builds: the repo-scoped 7.4+ replacement requires a `key` parameter and
answers with one build, which cannot serve a gate that does not know the keys yet. `SUCCESSFUL`
is the only pass — `CANCELLED` and `UNKNOWN` are terminal *and* failures. A commit can carry
several builds (Bitbucket keeps one per key, so one per workflow), and **all** of them must be
green. An empty list is never "finished": the forge webhook is asynchronous, so the gate first
waits for at least one build to be reported (`CI_PIPELINE_START_TIMEOUT_SECONDS`) and only then
for them to settle (`CI_PIPELINE_TIMEOUT_SECONDS`).

### Request

```jsonc
POST /api/vault/v1/kv/
{
  "team": "cloudops",              // first directory segment; ^[a-z0-9]+(-[a-z0-9]+)*$
  "kv_name": "my-passwords",       // second segment, and unique across ALL teams
  "access": [                      // required: at least one grant
    {
      "path": "*",                 // "*" = every secret here; "services/+/tyk/*" scopes it
      "auth_type": "oidc",         // oidc | k8s — picks which file it lands in
      "dn": "CN=cloudops,OU=Groups,DC=corp,DC=example,DC=com",
      "capabilities": ["read", "write"]
    },
    {
      "path": "*",
      "auth_type": "k8s",
      "name": "cloudops-external-secrets",   // service account name
      "namespace": "cloudops",
      "cluster": "dev",                      // must match a k8sMethods[].name
      "capabilities": ["read"]
    }
  ]
}
```

**Three fields, and the body mirrors the committed document.** A caller sends the same
`access` entries that land in the file, so `path` and `capabilities` are expressible — a
translated shape could express neither. There is **no** `kv_description`: the format defines
no home for it, and this service does not invent keys. Sending an unknown field (or one from
an older revision, like `file` or `roles`) is a `422`, not a silent drop.

`team` and `kv_name` are the only values that reach a filesystem path, and their pattern
blocks `..` and slashes, so a request can never escape the values directory. `path` is a
*Vault* path, not a filesystem one, so it deliberately allows `/`, `*` and `+`.

### The committed files (`KVs/cloudops/my-passwords/`)

**A store is a directory holding up to two files, split by auth type.** This is the contract
with the deploy pipeline — **change it in lockstep with the pipeline that consumes it**:

```
KVs/
  cloudops/
    my-passwords/
      oidc-access.yaml
      k8s-access.yaml
```

`oidc-access.yaml`:

```yaml
access:
  - path: '*'
    authType: oidc
    dn: CN=cloudops,OU=Groups,DC=corp,DC=example,DC=com
    capabilities:
      - read
      - write
```

`k8s-access.yaml`:

```yaml
access:
  - path: '*'
    authType: k8s
    name: cloudops-external-secrets
    namespace: cloudops
    cluster: dev
    capabilities:
      - read
```

Four things about that text are deliberate:

- **A store with no Kubernetes grants gets no `k8s-access.yaml` at all** — not one holding
  `access: []`. That is the shape a create writes, so an edit that empties a kind removes its
  file to match.
- **The model is principal → capabilities**, the inverse of a role map. One entry is one
  principal's grant on one path, so the same DN on two paths is two entries.
- **List items indent under their key**, and `capabilities` is a block list. PyYAML's default
  is the flush form, which parses identically but is not the pipeline's format.
- **`*` is quoted.** A bare `*` is a YAML alias and would not round-trip.

### The Tashtiot per-team file

The second layout, and the only one that is not a directory:

```jsonc
PUT /api/vault/v1/kv/tashtiot/cloudops
{
  "access": [
    {"path": "+/cloudops/*", "auth_type": "oidc", "dn": "CN=cloudops,...",
     "capabilities": ["read"]},
    {"path": "services/+/tyk/*", "auth_type": "k8s", "name": "argocd",
     "namespace": "perimeter", "cluster": "dev", "capabilities": ["read", "write"]}
  ]
}
```

lands in `KVs/Tashtiot/cloudops.yaml` as **one flat file mixing both kinds in one list** — no
directory, no oidc/k8s split. It grants a team access into the shared services store (redis,
zookeeper, kafka…), which is why its paths are usually scoped rather than `*`: consume one
service and you want `read` on yours; deploy a service and you want to see all of them.

It is an **upsert**: create and replace are the same call, because the file's identity *is*
the team, so there is nothing to collide with and a `409` would have nothing to describe. An
upsert that changes nothing returns `200` with a null pull request; one that changes
something returns `201`.

### Other routes

| Route | Notes |
|-------|-------|
| `GET /stores` | every store: team, name, directory. Lists directories, reads no file |
| `GET /{kv_name}` | one store, found by name alone — whichever team owns it |
| `PATCH /{kv_name}` | replaces the grant list wholesale |
| `GET /{kv_name}/access` | just the grants; an empty list is a `200`, not a `404` |
| `POST /{kv_name}/access` | append one grant; the `auth_type` picks the file |
| `DELETE /{kv_name}/access?auth_type=…&path=…` | revoke one grant |
| `GET`/`DELETE /tashtiot/{team}` | read or remove one team's shared-services file |

**There is no `/pull-request` variant of anything.** Routes that opened a pull request
without waiting for CI were removed: without the wait a caller could not know the change
was green, which is the one guarantee left once the service stopped merging.

**A store is addressed by name alone.** Names are unique across every team — the create
enforces it — so a caller never has to remember who owns theirs. `team` appears on the create
and nowhere else.

**Revoking takes query parameters, not a body**, since DELETE bodies are widely dropped by
proxies. oidc needs `path` and `dn`; k8s needs `path`, `name`, `namespace` and `cluster`. A
mismatched set is a `422`, not a wrong-grant revoke — a partial identity would otherwise
resolve to *some* entry.

**Capabilities are not part of a grant's identity.** Granting `read` and later `read`+`write`
to the same principal on the same path is an edit of one entry, so a duplicate is a `409` and
a revoke does not need to know what the grant currently permits.

### `DELETE` — removes the directory

```jsonc
// DELETE /api/vault/v1/kv/my-passwords
// -> 201 {"status":"Succeeded","message":"Opened pull request 43 to delete my-passwords. ..."}
//        the store is still there: the pull request that removes it is open for review.
```

Same chain, same rollbacks, same point of no return. What is worth knowing:

- **The whole directory goes**, both access files with it. There is nothing to orphan — a
  grant lives inside the store it reaches.
- **This is the one operation that is not REST.** Bitbucket Server has no endpoint that
  deletes a path, so the service pushes a `git rm -r` commit over **git-over-HTTPS**, reusing
  `BITBUCKET_TOKEN` as a bearer header — no SSH key, no extra secret. It needs the `git`
  binary in the image; the Dockerfile checks for it at build time. Everything either side —
  branch, pull request, the gate, rollback — is unchanged.
- **A repeat delete is `404`**, the same answer `GET` and `PATCH` give for a store that is not
  there. The *state* is idempotent; the status code tells you which call did the work.

### Uniqueness

- **Store names are global.** Unique across every team, which is why a create walks the whole
  values directory and what makes the name a sufficient address everywhere else. It is a
  check, not a lock: two creates in flight both pass and the second pull request conflicts at
  merge, like any other GitOps race.
- **Grants are unique within their file**, on `(authType, path, dn)` or
  `(authType, path, name, namespace, cluster)`. The same service account reaching two stores
  is the normal case; the same account in two namespaces is two distinct grants.

## How the library is used

`create_app()` (`app/main.py`) is the single wiring point, exactly as in the reference API: it
calls `general_create_app(enable_auth=True)`, builds each connector **once**, and injects them
into the router factory with `app.include_router(get_v1_vault_router(...))`. One router:
service account bindings are a sub-resource of a store, not a resource kind of their own.
There is no FastAPI `Depends` for connectors — the factory closure is the injection seam.

### Two-layer configuration

- **`app/global_conf.py`** (`global_config`) — cross-cutting: `BITBUCKET_*`,
  `HTTP_TIMEOUT_SECONDS`, `VERIFY_SSL`. No CI-server settings: the gates read Bitbucket.
- **`app/v1/vault/conf.py`** (`config`) — the module's own: `API_PREFIX`, `API_TAGS`, the values
  repo layout, PR shaping and the `CI_*` timeouts.

`AUTH_*` / `AUTH_SSO_*` are **not** declared in either — the library reads those from the
environment itself.

### Connectors

| Client | Built on | Why |
|--------|----------|-----|
| `app/clients/bitbucket.py` | library `BaseAPI` | the library's `Git` connector does file CRUD on a default ref but exposes no **pull-request** operations, and PRs are the whole point of this service |

Both normalise every non-2xx into the library's `ExternalServiceError`, which the routes map to
a 502 — the same failure contract the reference API uses. `BaseAPI` defaults `verify=False`;
`create_app()` opts back in via `VERIFY_SSL`.

### Per-module layout (`app/v1/vault/`)

| File | Responsibility |
|------|----------------|
| `conf.py` | the module's `BaseSettings` + `config` singleton (both API prefixes, both branch prefixes) |
| `schemas.py` | pydantic request/response models; `AccessEntry` is a union discriminated on `auth_type`, and one `VaultOperationResponse` covers every mutating route |
| `operations.py` | the create/update/delete chain, the grant sub-resource and the Tashtiot routes, plus every rollback; receives the connector as an argument |
| `routes.py` | `get_v1_vault_router(...)`, prefixed with `config.API_PREFIX`; stores, grants and Tashtiot all hang off it |

`app/helpers.py` holds the pure functions: path and branch naming, both entry shapes, YAML
rendering and comparison. Every transform returns a **new** document and preserves any sibling
top-level key, which is what the no-op short circuit depends on.

All settings added for this format are **defaulted**, so nothing new is required in `.env`
beyond `VAULT_VALUES_DIR=KVs`.

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
  helpers.py         # paths, the access-entry model, YAML rendering (pure)
  clients/
    bitbucket.py     # branch / file / pull-request REST client
    git.py           # deleting a path: the one operation Bitbucket has no REST for
    http.py          # the single funnel that raises ExternalServiceError
  v1/vault/          # conf, schemas, operations, routes
tests/
  test_helpers.py    # the document model and the exact committed bytes
  clients/           # the REST client against its real shapes (respx), and
                     #   test_git.py against a real bare repo on disk
  v1/vault/          # schemas, the chains + rollbacks, routes end-to-end
tools/
  stub_upstreams.py  # local Bitbucket stand-in, build statuses included (see below)
```

### Driving the chain locally

`tools/stub_upstreams.py` impersonates Bitbucket — repo *and* build statuses — so the whole
create chain runs over real HTTP with no Docker, licence or network. It fakes the CI server's
side effect rather than the CI server: opening or merging a pull request attaches `INPROGRESS`
build statuses to the relevant commit, which then settle.

```bash
uv run --no-sync python tools/stub_upstreams.py --port 9000     # terminal 1
# terminal 2: point BITBUCKET_URL at it — full env in the module docstring

curl -X POST 127.0.0.1:9000/__control -d '{"validation":"failure"}'   # success|failure|hang
curl 127.0.0.1:9000/__state                                           # branches, PRs, builds
```

Requires `tashtiot-apis-library >= 1.1.0`.

---

## Extension points

- **Remote Config provider.** Not wired here (nothing in this service is per-environment beyond
  the coordinates already in the request). Add it exactly as `example-api` does — guard on
  `CONFIG_API_URL`, call `enable_remote_config_api`, and pass the provider into the router
  factory — if you later need, say, a different values repo per environment.
- **A third `authType`.** Add the variant to the discriminated union in `schemas.py`, a
  builder in `helpers.py`, and teach `partition_access` / `access_path_for` where it lands.
  Those two functions are the only places that know entries split by kind, so nothing else
  has to change. Keep the "return a new document, preserve every sibling key" contract — it is
  what `yaml_data_equals` and the no-op short circuit depend on.
- **A third layout.** The store directory and the flat Tashtiot file already share one
  document model and one chain; a third would too. What differs between them is only which
  paths a `Change` writes, so it is a `_prepare_*` function and a route, not a package.
- **Formats that are gone, and are not sources to copy from.** A `kvStores` list of stores
  carrying a `roles` mapping with `k8sServiceAccounts` nested inside; before it, a top-level
  `kubernetesAuth` list pointing back at stores; before that, generated `policies` and a
  `groups` endpoint. Policy *generation* stays out of this service permanently.

## Docker

```bash
docker build -t vault-api .
docker run -p 5000:5000 --env-file .env vault-api
```

CI (`.woodpecker/build.yaml`) runs the test suite on every push to `master`, and builds and
pushes the image on git **tags**.
