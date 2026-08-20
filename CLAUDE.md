# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service that **writes access files to a Bitbucket repo and watches the builds that
act on them**. A create request opens a pull request, waits for the validating build, merges,
waits for the deploying build, and returns `Successful creation of <kv name>`.

**Bitbucket is the only upstream.** The gates are not read from the CI server: a CI server
posts its result *into* Bitbucket against the commit it built, and Bitbucket keeps one result
per key per commit — that store is exactly what a pull request's **Builds** tab renders. So a
gate is "ask Bitbucket which builds it holds for this sha". There is no Woodpecker client, no
CI token, no repo id and no pipeline-matching heuristic; the pipelines still run, they are
simply observed through the forge.

**Scope boundary — read this first.** The service deliberately knows nothing about Vault. It
commits YAML holding a list of **grants**, and nothing else.

### The document

Every file this service writes has the same single top-level key, `access`, holding a list of
entries. An entry is one grant: **who**, over **which path**, with **which capabilities**.
`authType` discriminates the two shapes:

```yaml
access:
  - path: "*"                                   # or "services/+/tyk/*", "+/cloudops/*"
    authType: oidc
    dn: "CN=<CN>,OU=<OU>,DC=<DC>"               # an LDAP DN, deliberately unvalidated
    capabilities:                               # block-list form, per the spec
      - read
      - write
  - path: "services/+/tyk/*"
    authType: k8s
    name: argocd                                # service account name — *not* `serviceAccount`
    namespace: perimeter
    cluster: dev                                # must match a k8sMethods[].name in the pipeline
    capabilities:
      - read
```

**The model is principal → capabilities.** That is the inverse of the `roles` mapping an
earlier revision wrote (capability → principals). One entry carries one principal's grant on
one path, so the same DN on two paths is *two entries* and that is the intended shape.

### Where it goes — two layouts, one document model

- **A KV store is a directory**: `KVs/<team>/<kv>/`, holding up to two files split by
  `authType` — `oidc-access.yaml` and `k8s-access.yaml`. The split is the format's, not ours:
  each file's `access` list holds only its own kind. **A store with no Kubernetes grants has
  no `k8s-access.yaml` at all**, rather than one holding `access: []` — that is the shape a
  create writes, so an edit that empties a kind *removes* its file to match.
- **The Tashtiot per-team file is flat**: `KVs/Tashtiot/<team>.yaml`, one file whose `access`
  list carries **both** kinds mixed. It grants a team access into the shared services store
  (`redis`, `zookeeper`, `kafka`, …), so it is per-team rather than per-KV, has no directory,
  and its paths are usually scoped (`+/cloudops/*`, `services/+/tyk/*`) rather than `*`. It is
  a different resource with its own routes; everything after the commit is the same chain.

`partition_access` in `helpers.py` is the single place that knows entries are split by kind,
and `build_access_document` the single place that knows the top-level key.

> **Two earlier formats are gone; neither is a source to copy from.** The first wrote a
> `kvStores` list of named stores each carrying a `roles` mapping, with Kubernetes bindings
> nested under `roles.k8sServiceAccounts`. The second, before it, modelled bindings as a
> top-level `kubernetesAuth` list pointing back at stores via `access: {read: [<store>]}`.
> Neither `kvStores`, `roles`, `k8sServiceAccounts`, `description` nor `name`-inside-the-
> document has any counterpart in the real format. Do not go to git history for them.

**No `description` anywhere.** The format defines no home for it, and this service does not
invent keys. `kv_description` was a request field under the old format and is gone.

**`dn` values are LDAP distinguished names** and are deliberately **unvalidated** beyond
non-blank: any pattern tight enough to describe a DN rejects forms this service does not own,
and the vocabulary is the directory's. `capabilities`, by contrast, is a **closed set**
(`read`, `write`) — those reach the pipeline as Vault policy, so an unknown one is a request
to refuse rather than pass on.

Mounts, KV engine versions, policies, HCL — none of it is modelled here. That is the deploy
pipeline's business. Resist adding Vault semantics: an earlier version generated mounts and
policy HCL and it was removed on purpose.

Built on the internal `tashtiot-apis-library` (base app, `BaseAPI`, `ExternalServiceError`)
and follows the patterns of the reference `example-api` (sibling repo).

## Commands

uv-managed (Python 3.12). Package indexes come from `uv.toml` (internal Artifactory, untracked).

```bash
uv sync --group dev                 # install deps into .venv — ON-PREM ONLY, see below
uv run --no-sync python -m app.main # serves on 0.0.0.0:5000 (--host/--port override)
uv run --no-sync pytest             # everything under tests/ (305 tests, ~18s)
uv run --no-sync pytest tests/v1/vault                                        # one suite
uv run --no-sync pytest tests/v1/vault/test_operations.py::test_create_call_order      # one test
docker build -t vault-api . && docker run -p 5000:5000 --env-file .env vault-api
```

**`--no-sync` is not optional off-prem.** `pyproject.toml` and the stale `uv.lock` disagree
(see the packaging section), so a bare `uv run`/`uv sync` re-resolves, tries to reach
Artifactory for `tashtiot-apis-library>=1.1.0`, and fails — public PyPI carries only `0.1.0`.
`--no-sync` runs against the existing `.venv`, which is already correct.

Docs at `http://localhost:5000/docs`, Prometheus metrics at `/metrics` (both provided by the
library's `general_create_app`). `asyncio_mode = "auto"` is set in `pyproject.toml`, so async
tests need no `@pytest.mark.asyncio`. There is no linter/formatter configured.

**Driving the real chain locally: `tools/stub_upstreams.py`.** A single FastAPI process
impersonating Bitbucket Server, so the whole create chain runs over real HTTP with no
Docker, licence or network. It fakes the CI server's *side effect* rather than the CI
server: opening or merging a pull request attaches `INPROGRESS` build statuses to the
relevant commit, which then settle. Everything except that upstream is real code — use it
before reaching for more mocks.

```bash
uv run --no-sync python tools/stub_upstreams.py --port 9000     # terminal 1
# terminal 2: point BITBUCKET_URL at it (full env in the module docstring)
curl -X POST 127.0.0.1:9000/__control -d '{"validation":"failure"}'  # or "hang" -> 504
curl 127.0.0.1:9000/__state       # branches, PRs, build statuses, every commit made
curl -X POST 127.0.0.1:9000/__reset
```

`validation`/`deploy` take `success` | `failure` | `hang`; `polls` is how many reads a build
stays `INPROGRESS`; `workflows` is how many build statuses one pipeline posts against a
commit, which is what makes the "wait for the slowest one" path reachable. The stub
reproduces three traps on purpose: a PR's `version` is bumped on every build-status change
(so a stale version 409s at merge), a merge lands the file on the base branch (so a repeat
create hits the duplicate guard), and commit ids are **hex** rather than derived from the
branch name — a branch name contains slashes, which would silently become extra URL path
segments in the builds endpoint. Keep all three if you extend it.

`.woodpecker/build.yaml` runs the suite on push to `master` and builds/pushes the image on git
**tags** only, passing `APP_VERSION=${CI_COMMIT_TAG}` as a build arg (it surfaces in the Swagger UI).

## Routes

| Route | Body | Notes |
|-------|------|-------|
| `POST /api/vault/v1/kv/` | `{team, kv_name, access}` | the chain below; blocks until the deploy build ends; 201 |
| `POST /api/vault/v1/kv/pull-request` | same body | steps 1-3 only: opens the PR and returns. No CI wait, no merge; 201 |
| `GET /api/vault/v1/kv/stores` | — | every store: `{team, kv_name, directory}`. Lists directories, reads no file |
| `GET /api/vault/v1/kv/{kv_name}` | — | one store: its team, directory and every grant, both kinds, oidc first |
| `PATCH /api/vault/v1/kv/{kv_name}` | `{access}` | replaces the grant list wholesale; 200 |
| `DELETE /api/vault/v1/kv/{kv_name}` | — | **removes the store's directory**, both files with it; 200 |
| `DELETE /api/vault/v1/kv/{kv_name}/pull-request` | — | steps 1-3 only for a removal; 201 |
| `POST /api/vault/v1/kv/{kv_name}/access` | one entry | appends one grant to whichever file its `auth_type` selects; 201 |
| `POST /api/vault/v1/kv/{kv_name}/access/pull-request` | same body | steps 1-3 only; 201 |
| `GET /api/vault/v1/kv/{kv_name}/access` | — | that store's grants (empty list, not 404, when it grants none) |
| `DELETE /api/vault/v1/kv/{kv_name}/access` | `?auth_type=&path=&…` | revokes one grant; 200 |
| `DELETE /api/vault/v1/kv/{kv_name}/access/pull-request` | same query | steps 1-3 only; 201 |
| `PUT /api/vault/v1/kv/tashtiot/{team}` | `{access}` | create-or-replace one team's shared-services grants; 200 |
| `GET /api/vault/v1/kv/tashtiot/{team}` | — | that team's file, parsed and returned as-is |
| `DELETE /api/vault/v1/kv/tashtiot/{team}` | — | removes the team's file; 200 |

**A store is addressed by name alone.** A caller should not have to remember which team owns
their store, and does not have to: a store name is unique across the whole values directory —
the create enforces exactly that — so the name *is* the address. `team` appears on the create
and nowhere else, because that is the one request deciding where the directory goes.

`_resolve_store` walks `KVs` to turn a name into `(team, directory, both documents)`. Unlike
the old flat layout there is **no conventional path to try first**: the team sits between the
values dir and the store, so resolution always costs one directory listing. Bitbucket's
`files/{dir}` resource lists **recursively**, so that is one call, not one per team.

**A store is any path of exactly three segments** under the values dir whose last is one of
the two access files. That rule is also what keeps the Tashtiot layout out of the store walk:
`Tashtiot/cloudops.yaml` is two segments. Anything else the walk sees is recorded and named in
the 404, so a values directory in an unexpected shape does not read as a deleted store.

**There is no `/k8s-service-accounts` prefix any more.** Under this format a Kubernetes
binding *is* an access entry, so it goes through the same `/{kv_name}/access` pair as an OIDC
grant and the `auth_type` picks the file. One endpoint, one list, no second route to keep in
step. There is also no `GET /files/{file}`: a file is not an address, a store is.

**There is no `PATCH` on a single grant.** An entry's identity is everything but its
capabilities, so "editing" one is a revoke plus a grant — and `PATCH /{kv_name}` already
replaces the whole list when that is what you want.

**The revoke identity travels as query parameters**, not a request body: DELETE bodies are
widely dropped by proxies and clients. Which parameters are required depends on `auth_type` —
oidc needs `path` and `dn`, k8s needs `path`, `name`, `namespace` and `cluster` — and a
mismatched set is a **422**, not a wrong-grant revoke. `_revoked_entry` in `routes.py` is the
single place that rule lives. Capabilities are not taken: they are not part of identity, and
asking for them would suggest a partial revoke is possible.

**Fixed segments are registered before `/{kv_name}`.** `/pull-request`, `/stores` and
`/tashtiot` would otherwise be shadowed by a store literally named one of those. Registration
order is the whole guard, and tests pin each case: a store named `pull-request` or `access`
stays readable at `GET /{name}` (one segment against two), and `/tashtiot/{team}` cannot
collide with a store read for the same reason. The one genuine casualty is a store named
`stores` — `GET /stores` is the listing and wins; a test pins that too.

**Path parameters are pattern-validated**, not just body fields: `kv_name` carries
`KV_NAME_PATTERN` and `team` carries `TEAM_PATTERN`, via `Annotated[str, Path(...)]` aliases
(`KVNameParam`, `TeamParam` in `routes.py`). Starlette's path convertor is `[^/]+`, so nothing
was exploitable without them, but a malformed name would surface as an opaque Bitbucket 404
two calls later instead of a 422.

`API_PREFIX` (default `/api/vault/v1/kv`) comes from `app/v1/vault/conf.py`.

### The request mirrors the document

A caller sends the same `access` entries that land in the file. That is the same boundary the
rest of the service keeps — the format is the pipeline's and this service is a thin,
validating writer of it — and it is what makes `path` and `capabilities` expressible at all.
A translated shape (the old `roles` mapping) could express neither.

**The create request is exactly three required fields**: `team`, `kv_name`, `access`.
`test_create_takes_exactly_three_fields` pins the set. There is no `file`, no
`kv_description`, no `environment` and no `metadata` block.

**Every request model sets `extra: "forbid"`.** That is load-bearing rather than tidy: a
caller migrating from the old shape who still sends `file` or `roles` gets a **422**, not a
201 for a request whose fields were silently dropped.

**A single grant is the request body on its own**, with no wrapper object —
`POST /{kv_name}/access` takes the entry directly, discriminated on `auth_type`.

**Validation lives where the rule comes from:**

- `KV_NAME_PATTERN` / `TEAM_PATTERN` are a **single segment** of `[a-z0-9]`/dashes. Both land
  in the committed path, so no slash means no directory can be escaped or created and `..`
  cannot match. They are the path-traversal guard.
- `VAULT_PATH_PATTERN` is deliberately **looser** — it allows `/`, `*` and `+`, because a
  Vault path is not a filesystem path and never reaches one. `..` is refused in a
  `field_validator` rather than the pattern, because pydantic-core's regex engine has no
  look-ahead.
- `K8S_NAMESPACE_PATTERN` is an RFC 1123 *label* (no dots, ≤63) and
  `K8S_SERVICE_ACCOUNT_PATTERN` an RFC 1123 *subdomain* (dots allowed, ≤253) — genuinely
  different limits, so do not collapse them. Neither accepts uppercase, which the cluster
  rejects at admission. `*` in `name`/`namespace` is **rejected**: it would grant every
  workload in the cluster, and that escalation should need a human editing the YAML.
- `K8S_CLUSTER_PATTERN` is our own single-segment shape — there is no Kubernetes rule to
  borrow, and the pipeline matches it against a `k8sMethods[].name`.

**Uniqueness.** A **store name** is global — unique across the whole values dir, which is why
a create walks every team, and what makes the name a sufficient address everywhere else. A
**grant** is unique within its file, on its identity: `(authType, path, dn)` for oidc and
`(authType, path, name, namespace, cluster)` for k8s. **Capabilities are not part of
identity** — granting `read` and later `read`+`write` to the same principal on the same path
is an edit of one entry, not a second entry, so a duplicate identity is a 409 and a revoke
does not need to know what the grant currently permits.

### Format isolation — the one-line-edit inventory

The format is the pipeline's, so each decision has exactly one home:

| What could be wrong | Single place to change |
|---|---|
| the top-level key | `ACCESS_KEY` in `helpers.py` |
| the discriminator key and its two values | `AUTH_TYPE_KEY` / `OIDC_AUTH_TYPE` / `K8S_AUTH_TYPE` in `helpers.py` |
| any entry field name, and camelCase vs snake_case | `build_oidc_access_entry` / `build_k8s_access_entry` in `helpers.py` |
| entry field **order** in the committed text | `_OIDC_ENTRY_KEYS` / `_K8S_ENTRY_KEYS` in `helpers.py` |
| which fields identify a grant | `access_entry_identity` in `helpers.py` |
| the two filenames, and which kind goes in which | `OIDC_ACCESS_FILE` / `K8S_ACCESS_FILE` / `access_path_for` in `helpers.py` |
| the directory shape | `kv_store_dir` / `tashtiot_access_path` in `helpers.py` |
| whether an emptied list is dropped or kept as `[]` | `remove_access_entry` (keeps) and `_store_writes` (drops the file) |
| sequence indentation and block-scalar style | `_BlockStyleDumper` in `helpers.py` |
| the allowed capabilities | `Capability` in `schemas.py` |
| the k8s name patterns and limits | the `K8S_*_PATTERN` / `K8S_*_MAX_LENGTH` constants in `schemas.py` |
| the branch prefix | `BRANCH_PREFIX` in `conf.py` |
| the values dir and the Tashtiot sub-dir | `VAULT_VALUES_DIR` / `TASHTIOT_DIR` in `conf.py` |
| how a directory is deleted | `GitFileRemover` in `clients/git.py` |

**Nothing outside `helpers.py`/`schemas.py` knows a document key name** — `operations.py`
reaches a grant's identity through `access_entry_identity` and its kind through
`auth_type_of` rather than indexing `entry["authType"]`. `to_document_entry` in
`operations.py` is the single bridge from a validated request model to a document dict. Keep
it that way.

## Architecture

**App composition (`app/main.py`).** `create_app()` is the single wiring point: it calls
`general_create_app(enable_auth=True)`, constructs the Bitbucket client **once** from config
— handing it a `GitFileRemover` for the one non-REST operation — and injects it into
`get_v1_vault_router(bitbucket)`. Connectors are never built per-request; the router factory
closure is the injection seam (no FastAPI `Depends` for connectors).

**Config is two-layered, all sourced from `.env`** (pydantic-settings `BaseSettings`):
- `app/global_conf.py` — `BITBUCKET_*`, `HTTP_TIMEOUT_SECONDS`, `VERIFY_SSL`. There are
  deliberately no CI-server settings: the `CI_*` knobs below bound the **build-status
  polling**. Their names still say PIPELINE because a pipeline is still what is waited on;
  only the place we ask changed.
- `app/v1/vault/conf.py` — `API_PREFIX`, `API_TAGS`, values-repo layout (`VAULT_VALUES_DIR`
  defaults to `KVs`, `TASHTIOT_DIR` to `Tashtiot`), PR shaping, `CI_*` timeouts, and
  `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/`GIT_TIMEOUT_SECONDS` for the delete commit.
- `AUTH_*` / `AUTH_SSO_*` are **not** declared here — the library reads them from the
  environment.

Both singletons are built at **import time**, so a missing required setting is an import
error, not a request error. Everything added for this format is **defaulted**, so a new
*required* setting would break every test at collection — add it to `tests/conftest.py` too.

**Per-module layout** (`app/v1/vault/`): `conf.py`, `schemas.py`, `operations.py`, `routes.py`
— the same four-file split the reference API uses. **Stores, grants and the Tashtiot file all
live in those same four files**; there is no `app/v1/tashtiot/`. They write the same document
shape, share `_open_pull_request`/`_commit_via_pull_request` and `VaultOperationResponse`, and
splitting them would either duplicate that spine or turn it into a cross-package import.

There are **no `__init__.py` files anywhere** — implicit namespace packages, with
`pythonpath = ["."]` and `package = false` in `pyproject.toml`. Adding empty ones is churn.

**Clients (`app/clients/`).** `BitbucketClient` — the only one — wraps the library's `BaseAPI`
async client. It is hand-rolled rather than using the library's `Git` connector because `Git`
exposes no pull-request operations, and PRs are the entire point here. It goes
through **`app/clients/http.py`** — `request()` is the single funnel that raises the library's
`ExternalServiceError` (`ExternalServiceError(service_name, status_code, detail=None)`) for
three cases:

| Cause | `status_code` carried |
|-------|----------------------|
| `httpx.TimeoutException` | `504` |
| any other `httpx.RequestError` (DNS, connect, TLS) | `502` |
| non-2xx response | the upstream's own status |

Never call `self._client.request` directly — a bare `httpx.RequestError` escapes past the
routes' `except ExternalServiceError` and the caller gets an opaque 500. The `504` is
load-bearing: `routes.py` keys off `external_error.status_code == 504` to answer 504 instead
of 502. The client passes its own `detail_from_response` (digging through
`{"errors": [{"message": ...}]}`); `default_detail` is the fallback.

**`app/clients/git.py` is the one operation that is not REST.** Bitbucket Server has **no
endpoint that deletes a file** — `browse/{path}` is `GET` and `PUT` only, Atlassian have said
outright it is unsupported, and the `-F files=<path>` workaround every search result offers
is the **Bitbucket Cloud** `src` resource, which does not exist on Server. So `delete_file`
on `BitbucketClient` delegates to `GitFileRemover`, which pushes a deleting commit over
**HTTPS** using the same `BITBUCKET_TOKEN` as a bearer header. No SSH key, no second secret.
Four things about it are deliberate:

- **The token goes in through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n` environment variables**,
  not `-c http.extraHeader=...`, because argv is readable through `ps`. `_scrub` also strips
  it from any stderr that reaches an error body, and `GIT_TERMINAL_PROMPT=0` stops a
  credential prompt hanging the request.
- **It runs in `asyncio.to_thread`.** A subprocess plus two network round-trips on the event
  loop would stall every other in-flight request.
- **It produces one commit on one topic branch and stops.** It never pushes to the base
  branch, so the pull request, both gates, the merge and every rollback are unchanged — the
  branch is deleted over REST exactly as if `put_file` had made it.
- **Every failure leaves as `ExternalServiceError`**, matching `http.py`, so the routes map
  it without knowing a subprocess was involved. A git timeout is a **504** for the same
  reason a read timeout is.

It needs the **git binary in the image**. The Dockerfile asserts `git --version` at build
time rather than letting a missing binary surface as a 502 on the first delete. `tests/clients/test_git.py`
runs real git against a real bare repo in a temp dir — no mocks, deliberately: a fake here
would only prove the fake agrees with itself, which is exactly how the build-status bug below
survived a full suite.

**The build-status half of the client is the CI gate.** `get_build_statuses(commit_id)` reads
`GET /rest/build-status/1.0/commits/{commitId}` — the **only** endpoint here that is not
repo-scoped, and deliberately the deprecated one. **Do not "modernise" it.** The repo-scoped
7.4+ replacement, `GET {api}/commits/{commitId}/builds`, takes a **required `key` query
parameter** and returns the single build carrying that key. A bare call is rejected with
*"Please specify a non-blank key smaller than 255 characters"*, which surfaces as a 502 on
every gate; a keyed call cannot enumerate. Enumeration is the whole requirement — the gate
asks "which builds exist on this sha" before any key is known. This client called the
repo-scoped resource for a while and no test caught it, because the fakes, the respx mocks
*and* `tools/stub_upstreams.py` all implemented the invented contract. `test_get_build_statuses_asks_the_unscoped_resource_and_sends_no_key`
now pins the path and the absence of `key`. If the old resource is ever withdrawn, the
replacement is to make the expected build keys configuration and fetch each by key — which
would silently pass a gate for any workflow nobody listed, so it is not the default.

`await_builds` is two-phase, and the split is not cosmetic:

- **phase 1, `find_builds`** — wait for *at least one* build. The forge webhook is
  asynchronous, so a freshly pushed commit has no builds for a few seconds; an empty list can
  never mean "finished".
- **phase 2, `wait_for_builds`** — wait until every build has left `INPROGRESS`.

`PENDING_BUILD_STATES` is `{INPROGRESS}` and `SUCCESSFUL_BUILD_STATE` is `SUCCESSFUL`. Those
are separate constants on purpose: `CANCELLED` and `UNKNOWN` are **terminal but not success**,
so a gate keyed off "not FAILED" would merge both. A gate passes only when *every* build
succeeded — Bitbucket stores one result per key per commit, so a pipeline with several
workflows lands several rows.

**Known consequence, documented rather than worked around:** a CI server that posts one
status per workflow may not post them all at once, so a gate that sees a single finished
build while a sibling has yet to appear returns early. Bitbucket's own required-builds merge
check is the backstop if that matters for a given repo.

**Shared helpers (`app/helpers.py`)** are pure. Paths: `kv_store_dir`, `oidc_access_path`,
`k8s_access_path`, `access_path_for`, `tashtiot_access_path`, `build_branch_name`,
`slugify_mount_path`. Entries: `build_oidc_access_entry`, `build_k8s_access_entry`,
`build_access_document`. Reading: `read_access_entries`, `auth_type_of`,
`access_entry_identity`, `access_entry_identities`, `find_access_entry`, `partition_access`.
Transforms: `add_access_entry`, `remove_access_entry`, `replace_access_entries`. Rendering:
`render_values_yaml`, `yaml_data_equals`.

**Every transform deep-copies the whole document and replaces only `access`**, so a sibling
key this service has never heard of survives. That is not hypothetical tidiness: an earlier
revision rebuilt the document from its own list alone, which silently dropped every other key
— and the erase would have looked like a legitimate PR diff. The `SIBLING_KEY` tests in
`tests/test_helpers.py` pin it with a key name the code has never seen.

They return a **new** document and never mutate the input. That is what lets an update diff
old against new with `yaml_data_equals` and skip a no-op commit. Any helper added later must
keep both properties. `add_access_entry` accepts `None`, so "the file does not exist yet" and
"append to an existing file" are one code path; it raises `DuplicateAccessEntry` for a
repeated identity, which the operations turn into a 409. `remove_access_entry` raises
`AccessEntryNotFound` → 404, and **keeps an emptied list as `access: []`** — dropping the file
is the *caller's* decision, made in `_store_writes` and `_prepare_access_change`.

`build_branch_name` collapses its two coordinates when they match, so a store whose name
equals its team renders `vault-kv/cloudops-abc123` rather than `vault-kv/cloudops-cloudops-…`.

`render_values_yaml` uses a `SafeDumper` subclass, `_BlockStyleDumper`, doing **two** things
PyYAML does not, both about the committed text rather than its meaning:

- **`increase_indent` forces block sequences to indent under their key** — `  - path:` and
  not the flush `- path:` PyYAML emits. Both parse identically, but the indented form is the
  pipeline's format, and it is what the spec's templates show. `capabilities` gets the same
  treatment, which is the spec's "block-list format" requirement.
  `test_oidc_file_has_the_expected_shape` pins the exact bytes.
- **multi-line strings become `|` blocks** — the default representer emits one escaped,
  width-wrapped scalar, unreadable in the pull request diff a human is supposed to review.

`sort_keys=False` is what preserves entry field order; alphabetical would reorder every entry
away from the spec's shape. Note also that `*` is emitted as `'*'` — a bare `*` is a YAML
alias and would not round-trip.

## The create chain — read this before editing `operations.py`

**Two shared helpers, nested.** `_open_pull_request` owns branch → commit(s) → PR and both of
their rollbacks (either failure deletes the branch). `_commit_via_pull_request` calls it and
then adds the gates and the merge. **Every mutating operation goes through them** — the
blocking ones via `_commit_via_pull_request`, the PR-only variants stopping at
`_open_pull_request`. One implementation each means the rollback shape cannot drift.

**A change is a `Change`, not a path.** `Change(writes=[FileWrite…], removes=[path…])` is what
the chain commits. That is what lets a create write **two files on one branch** (Bitbucket's
browse resource writes one path per call, so a store costs two `put_file`s), and what lets an
update rewrite one file and remove another in the same pull request. `removes` holds paths
*or directories*; a store delete passes the directory. `FileWrite.source_commit_id` is
per-file because Bitbucket's optimistic-lock token is: it is required when editing a path that
exists and **refused** when creating one, and a store's two files are frequently in different
states — adding the first Kubernetes grant edits `oidc-access.yaml` and creates
`k8s-access.yaml` in one commit.

`create_kv_mount_operation` runs, in order:

0. **name scan** — `_assert_name_is_free` walks every store directory; the name appearing
   under any team is a 409. Store names are global to Vault, so this cannot be scoped to the
   target team. It is a check, not a lock: two creates in flight both pass and the second PR
   conflicts at merge, same as any other GitOps race.
1. `_store_writes` partitions the grants and renders one file per non-empty kind. Nothing is
   read: the store is new, so neither file exists and no lock token applies.
2. `create_branch` → `put_file` per file
3. `create_pull_request`
4. **gate 1**: `await_builds` on `pull_request.from_commit` (`fromRef.latestCommit`) — the sha
   the PR's Builds tab shows
5. `get_pull_request` (re-read for the current `version`) → `merge_pull_request`
6. **gate 2**: `await_builds` on the merge commit (`properties.mergeCommit.id`, falling back
   to `get_branch_head(base)` — not every merge strategy reports one)

There is **no transaction**; each step hand-rolls its own rollback:

| Failure | Rollback |
|---------|----------|
| any `put_file` / `delete_file` | delete the branch |
| `create_pull_request` | delete the branch |
| gate 1 fails or times out | decline the PR **and** delete the branch |
| merge fails | **nothing** — leave the PR open for a human |
| gate 2 fails or times out | **nothing** — already merged; the message says a revert is needed |

**Step 5 is the point of no return.** Preserve that asymmetry when editing. Rollback failures
are logged and never raised over the original error.

Three details that are easy to break:
- **The version re-read before merging/declining.** Bitbucket's `version` is an optimistic
  lock that CI status updates bump; merging with the stale one from `create_pull_request`
  409s.
- **`exclude_keys` on gate 2.** A **fast-forward merge** leaves the base branch pointing at
  the very commit the pull request built, so gate 1's results are already sitting on gate 2's
  sha; without excluding the keys gate 1 saw, gate 2 would open on its predecessor's answer
  and call the deploy a success before it started. Applied *only* when
  `merge_commit == from_commit`, so a normal merge commit excludes nothing.
- **Gate 1 needs `from_commit`.** If Bitbucket reports no `fromRef.latestCommit` there is no
  sha to watch, and merging unvalidated is not the fallback — the chain declines the PR and
  deletes the branch, exactly as a red build would.

### Update, delete and the sub-resource

**Update** resolves the store, converts the request, and **short-circuits when nothing
changed** — returning success with `pull_request: null` rather than opening an empty PR.
`_unchanged` compares parsed documents, so a re-render or a reordering is not a change. Then
`_store_writes` decides the whole `Change`: a kind that has grants is written, a kind that had
a file and now has none is **removed**. That is the one place the "no empty file" rule is
applied, and it is why an update can delete a file.

**Delete** removes the store's **directory**, both files with it. `_prepare_delete` resolves
the store and returns it — no document to render, nothing to diff, no lock token to fetch,
because git resolves the path from the branch it fetched. There is no referential scan: a
grant lives inside the store it reaches, so the directory takes every grant with it and
nothing is left dangling. There is no `yaml_data_equals` short circuit either — a removal is
never a no-op.

**One grant at a time** (`_prepare_access_change`) reads only the file its `auth_type`
selects, so a Kubernetes grant never appears in the diff of an OIDC change. An add rejects a
duplicate identity (409); a remove 404s when the identity is not there. Removing the last
grant of a kind **removes that file**, for the same reason an update does.

**The Tashtiot operations are the same chain minus the store parts**: no name scan (a team
owns exactly one file, and its name is the address), no two-file partition, no directory. The
upsert is create-or-replace in one call because the file's identity *is* the team, so a 409
would have nothing to describe; it short-circuits on no change like an update does.

**Ordering consequence worth knowing** (documented, not worked around): a caller who creates a
store via `POST /pull-request` and then immediately adds a grant to it gets a 404 — the store
is on an unmerged branch and every scan reads the base branch. Same base-branch-only
visibility that makes a repeat `/pull-request` open a second PR.

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
  is the escape hatch when that is unacceptable: it answers in one round-trip and hands the
  merge to a human.
- Status codes: **201** creates, grants and every PR-only route; **200** edits, deletes, the
  Tashtiot upsert and reads; **409** duplicate store name or duplicate grant; **404** unknown
  store, grant or Tashtiot file; **502** build/transport failure; **504** build or upstream
  timeout; **422** request, path-parameter or query-parameter validation. The rule: an
  endpoint that opens a PR and stops answers 201; one that runs the full chain answers 201 for
  a create or a grant and 200 for an edit, a delete or a revoke.
- **`routes.py` has two funnels, not one.** `_execute` is for mutations; the `GET`s go through
  **`_read`**, which differs in both directions: an upstream **404 stays a 404** (a missing
  store is the caller's mistake, not a bad gateway), and its failure body is a bare
  `{"status", "error"}` dict rather than a `VaultOperationResponse` — reads return raw parsed
  YAML on success, so there is no response model for a failure to match. Putting a read
  through `_execute` turns every unknown store into a 502.
- **One response model for every mutating route**, `VaultOperationResponse`. `(file, kv_name)`
  addresses stores, grants and Tashtiot files alike — `file` carries the *directory* for a
  store and the file path for a Tashtiot upsert.
- **The CI outcome is reported as `validation_builds` / `deploy_builds`** — lists of
  `BuildInfo{key, state, name, url}`. Lists because Bitbucket stores one result per key per
  commit, so a multi-workflow pipeline lands several; `key` rather than `number` because the
  key is what identifies a build to Bitbucket and what tells two workflows apart.
- **Failures are `return`ed as a `JSONResponse`, not raised.** So the decorator's
  `status_code=` and `response_model=` describe the success path only — FastAPI does not
  validate or document the failure bodies. Adding a field to `VaultOperationResponse` means
  the failure path silently omits it unless `to_response()` is updated too.
- The committed entries are the contract with the deploy pipeline — change
  `build_oidc_access_entry` / `build_k8s_access_entry` and the pipeline together.
- Tests: 305 of them. `tests/test_helpers.py` (pure, no library import — the document model
  and the exact committed bytes), `tests/clients/` (the Bitbucket client and `http.py`'s error
  mapping against the real REST shapes via respx, plus `test_git.py` running **real git
  against a real bare repo** in a temp dir), `tests/v1/vault/` (schemas, the chain + rollbacks
  with duck-typed fakes from `tests/fakes.py`, and routes end-to-end through `TestClient`).
  Three conftest details to respect:
  - `tests/conftest.py` sets `os.environ` **before** any `app.*` import (its own `import jwt` /
    `import pytest` sit below the env block behind `# noqa: E402`) because the config
    singletons are built at import time. It pins `VAULT_VALUES_DIR`/`TASHTIOT_DIR` so a
    developer `.env` cannot change every expected path, blanks the optional `AUTH_*` knobs,
    and zeroes the `CI_*` timeouts to keep the suite fast.
  - It also provides the shared `auth_headers` fixture (with an `exp` claim). Do not shadow it
    in a test module — a token without `exp` is a 401 and looks like a routing bug.
  - `tests/v1/vault/conftest.py`'s `client` fixture monkeypatches `app.main.BitbucketClient` —
    the class, not an instance. That works only because `create_app()` is the sole
    construction site; keep it that way. `seed_store()` there writes only the files a store
    actually has grants for, mirroring what a create commits.

  **Steering the fakes** (`tests/fakes.py`):
  - `FakeBitbucket(existing_files={path: yaml})` is the repo's whole state, keyed on the full
    repo-relative path; `list_files` derives the recursive listing from it, exactly as the real
    resource does, and an absent path raises a 404 `ExternalServiceError`.
  - `fail_on={"put_file": exc}` injects a failure into one method by name. Rollback tests
    assert against `bitbucket.calls`, an ordered list of every method reached — that list is
    what pins the call *order*, not just the outcome.
  - `bitbucket.committed` is paths written, `bitbucket.deleted` is paths removed. `delete_file`
    deliberately does **not** drop the path from `existing_files`: that is the base branch, and
    only a merge would remove it there, so a test wanting the merged state says so.
  - `FakeBitbucket.get_pull_request` bumps `version` on every call, reproducing the real
    optimistic-lock trap.
  - `FakeBitbucket(builds=[...])` is a **queue** popped once per `await_builds`, so element 0
    is the validation gate and element 1 the deploy gate; an `Exception` in the list is raised
    instead of returned. Running dry is an `AssertionError`, so a test running two full chains
    must script four entries.
- **`tools/stub_upstreams.py` cannot serve a delete.** A store is a directory and removing one
  is a `git rm -r`, which needs git's smart HTTP protocol and a real repository rather than the
  stub's in-memory dicts. A `DELETE` fails there with a git error surfaced as a 502; that is
  expected. Everything else — create, update, both PR-only variants, adding a grant and the
  Tashtiot routes — runs against it unchanged.
- Commits are expected to be **Conventional Commits** (`feat:`, `fix:`, `test:`, `feat!:` for
  a breaking change) — follow the existing `git log`. There is no changelog tooling. The
  version is a literal in `pyproject.toml` plus the `APP_VERSION` build arg.
