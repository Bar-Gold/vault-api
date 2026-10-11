# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## The docs, and which one owns what

- **`vaultREADME.md` — the format's contract.** The upstream spec: the `oidc-access.yaml` /
  `k8s-access.yaml` templates, every field explained, and the real `cloudops/my-passwords/`
  example. It describes what the deploy pipeline consumes. **It is not ours to edit to match
  the code** — the code changes to match it.
- **`README.md` — human onboarding.** Quick start, project structure, the Artifactory/`uv.toml`
  runbook, extension points, Docker.
- **CLAUDE.md (this file) — decisions and traps.** Only what the code cannot tell you: why
  something is the way it is, and what breaks if you "fix" it. If a fact is discoverable by
  reading one file, it belongs in that file's comments, not here.
- **`todo.txt` — untracked scratch backlog.** Terse and the user's own; read it
  for what is currently open rather than trusting a summary here.

Templates for the two untracked config files are committed: `.env.example` and
`uv.toml.example`. `.claude/agents/` holds two project subagents; `.claude/skills/run-project`
is the verified start/stop/status recipe for the local server — use it rather than improvising.

## What this is

A FastAPI service that **proposes access-file changes as pull requests and confirms CI is
green**. A create request opens a pull request, waits for the validating build, and returns
with the pull request open and waiting for review.

**This service never merges.** Who merges, and when, is a human decision it deliberately does
not make. Nothing it does ever reaches the base branch — which is why every failure is fully
rolled back and there is no point of no return anywhere in this codebase.

**Bitbucket is the only upstream.** The gates are not read from the CI server: a CI server
posts its result *into* Bitbucket against the commit it built, and Bitbucket keeps one result
per key per commit — that store is exactly what a pull request's **Builds** tab renders. So a
gate is "ask Bitbucket which builds it holds for this sha". There is no Woodpecker client, no
CI token, no repo id and no pipeline-matching heuristic; the pipelines still run, they are
simply observed through the forge.

**Scope boundary — read this first.** The service knows nothing about Vault. It commits YAML
holding a list of **grants**, and nothing else. Mounts, KV engine versions, policies, HCL —
all the deploy pipeline's business. Resist adding Vault semantics: an earlier version
generated mounts and policy HCL and it was removed on purpose.

Built on the internal `tashtiot-apis-library` (base app, `BaseAPI`, `ExternalServiceError`),
following the reference `example-api` (sibling repo).

### The document

One top-level key, `access`, holding a list of entries. An entry is one grant: **who**, over
**which path**, with **which capabilities**; `authType` discriminates oidc (`dn`) from k8s
(`name`, `namespace`, `cluster`). **See `vaultREADME.md` for the templates and field
semantics** — that is the spec, and duplicating it here just lets the two drift.

**The model is principal → capabilities.** That is the inverse of the `roles` mapping an
earlier revision wrote (capability → principals). One entry is one principal's grant on one
path, so the same DN on two paths is *two entries* — the intended shape.

> **Two earlier formats are gone; neither is a source to copy from.** The first wrote a
> `kvStores` list of named stores each carrying a `roles` mapping, with Kubernetes bindings
> nested under `roles.k8sServiceAccounts`. The second, before it, modelled bindings as a
> top-level `kubernetesAuth` list pointing back at stores via `access: {read: [<store>]}`.
> Neither `kvStores`, `roles`, `k8sServiceAccounts`, `description` nor `name`-inside-the-
> document has any counterpart in the real format. **Do not go to git history for them.**

**No `description` anywhere** — the format defines no home for it and this service does not
invent keys. **`dn` is deliberately unvalidated** beyond non-blank: any pattern tight enough
to describe an LDAP DN rejects forms this service does not own, and the vocabulary is the
directory's. `capabilities`, by contrast, is a **closed set** (`read`, `write`) — those reach
the pipeline as Vault policy, so an unknown one is a request to refuse rather than pass on.

### Where it goes — two layouts, one document model

- **A KV store is a directory**: `KVs/<team>/<kv>/`, up to two files split by `authType`. The
  split is the format's, not ours. **A store with no Kubernetes grants has no
  `k8s-access.yaml` at all**, rather than one holding `access: []` — that is what a create
  writes, so an edit that empties a kind *removes* its file to match.
- **The Tashtiot per-team file is flat**: `KVs/Tashtiot/<team>.yaml`, one file whose `access`
  list carries **both** kinds mixed. It grants a team access into the shared services store
  (`redis`, `zookeeper`, `kafka`, …), so it is per-team rather than per-KV, has no directory,
  and its paths are usually scoped (`+/cloudops/*`) rather than `*`. Its own routes;
  everything after the commit is the same chain.

`partition_access` in `helpers.py` is the single place that knows entries split by kind, and
`build_access_document` the single place that knows the top-level key.

## Commands

uv-managed (Python 3.12). Package indexes come from `uv.toml` (internal Artifactory, untracked).

```bash
uv sync --group dev                 # install deps into .venv — ON-PREM ONLY, see below
uv run --no-sync python -m app.main # serves on 0.0.0.0:5000 (--host/--port override)
uv run --no-sync pytest             # everything under tests/ (499 tests, ~30s)
uv run --no-sync pytest tests/v1/vault                                            # one suite
uv run --no-sync pytest tests/v1/vault/test_operations.py::test_create_call_order # one test
docker build -t vault-api . && docker run -p 5000:5000 --env-file .env vault-api
```

**`--no-sync` is not optional off-prem.** `pyproject.toml` and the stale `uv.lock` disagree
(below), so a bare `uv run`/`uv sync` re-resolves, tries to reach Artifactory for
`tashtiot-apis-library>=1.1.0`, and fails — public PyPI carries only `0.1.0`. `--no-sync` runs
against the existing `.venv`, which is already correct.

Docs at `/docs`, Prometheus metrics at `/metrics` (both from the library's
`general_create_app`). `asyncio_mode = "auto"`, so async tests need no marker. No
linter/formatter is configured.

**Driving the real chain locally: `tools/stub_upstreams.py`.** One FastAPI process
impersonating Bitbucket Server, so the whole chain runs over real HTTP with no Docker, licence
or network. It fakes the CI server's *side effect* rather than the CI server: opening or
merging a pull request attaches `INPROGRESS` build statuses that then settle. Everything else
is real code — **use it before reaching for more mocks.**

```bash
uv run --no-sync python tools/stub_upstreams.py --port 9000    # terminal 1
# terminal 2: point BITBUCKET_URL at it (full env in the module docstring)
curl -X POST 127.0.0.1:9000/__control -d '{"validation":"failure"}'  # or "hang" -> 504
curl 127.0.0.1:9000/__state       # branches, PRs, build statuses, every commit made
curl -X POST 127.0.0.1:9000/__reset
```

`validation`/`deploy` take `success|failure|hang`; `polls` is how many reads a build stays
`INPROGRESS`; `workflows` is how many statuses one pipeline posts, which makes the "wait for
the slowest one" path reachable.

Three stub traps are deliberate — **keep all three if you extend it**: a PR's `version` is
bumped on every build-status change (so a stale version 409s when declining), a merge lands
the file on the base branch (so a repeat create hits the duplicate guard), and commit ids are
**hex** rather than derived from the branch name — a branch name contains slashes, which would
silently become extra URL path segments in the builds endpoint.

Two more things about the stub:

- **`POST /__merge/{id}` is needed to get past one create.** Since the service never merges and
  every read/edit/delete resolves against the base branch, without playing the reviewer the
  local loop dead-ends: `GET /stores` stays empty and there is nothing to PATCH.
- **It cannot serve anything that removes a path.** A store delete is a `git rm -r`, and so is
  an edit that empties one kind of grant. Both need git's smart HTTP protocol and a real
  repository rather than the stub's in-memory dicts, so they fail there with a git error
  surfaced as a 502. Expected; everything else runs against it unchanged.

`.woodpecker/build.yaml` runs the suite on push to `master` and builds/pushes the image on git
**tags** only, passing `APP_VERSION=${CI_COMMIT_TAG}` as a build arg (it shows in Swagger).

## Where the code is

There are **no `__init__.py` files anywhere** — implicit namespace packages, with
`pythonpath = ["."]` and `package = false` in `pyproject.toml`. Adding empty ones is churn.

| File | Lines | What it owns |
|---|---:|---|
| `app/v1/vault/operations.py` | 1390 | the chain: every `*_operation`, both PR helpers, rollbacks |
| `app/clients/bitbucket.py` | 635 | branch / file / PR REST, and the build-status gate |
| `app/v1/vault/routes.py` | 535 | route decorators, `_execute`/`_read` |
| `app/v1/vault/schemas.py` | 923 | request models (grants, revokes, deletes), every validation pattern |
| `app/helpers.py` | 431 | pure: paths, entries, transforms, YAML rendering |
| `app/clients/git.py` | 208 | the one non-REST operation (deleting a path) |
| `app/main.py` | 112 | `create_app()` — the single wiring point |
| `app/v1/vault/conf.py` / `app/global_conf.py` | 121 / 53 | the two settings layers |
| `app/clients/http.py` | 71 | the single funnel raising `ExternalServiceError` |

`app/v1/vault/` is the same four-file split the reference API uses. **Stores, grants and the
Tashtiot file all live in those four files**; there is no `app/v1/tashtiot/`. They write the
same document shape and share `_open_pull_request` / `_open_and_validate` /
`VaultOperationResponse`, so splitting them would either duplicate that spine or turn it into
a cross-package import.

## Routes

`API_PREFIX` (default `/api/vault/v1/kv`) comes from `app/v1/vault/conf.py`. Each route's
`description=` in `routes.py` is the per-route detail; below is only what spans them.

| Route | Body | Notes |
|-------|------|-------|
| `POST /` | `{metadata, team, kv_name, access}` | the chain below; blocks until the validation build ends; 201 |
| `GET /stores` | — | every store: `{team, kv_name, directory}`. Lists directories, reads no file |
| `GET /{kv_name}` | — | one store: team, directory and every grant, both kinds, oidc first |
| `PATCH /{kv_name}` | `{metadata, access}` | replaces the grant list wholesale; 201 (200 if unchanged) |
| `DELETE /{kv_name}` | `{metadata}` | **removes the store's directory**, both files with it; 201 |
| `POST /{kv_name}/access` | one entry + `metadata` | appends one grant to whichever file its `auth_type` selects; 201 |
| `DELETE /{kv_name}/access` | one identity + `metadata` | revokes one grant; 201 |
| `PUT /tashtiot/{team}` | `{metadata, access}` | create-or-replace a team's shared-services grants; 201 (200 if unchanged) |
| `GET /tashtiot/{team}` | — | that team's file, parsed and returned as-is |
| `DELETE /tashtiot/{team}` | `{metadata}` | removes the team's file; 201 |
| `POST /tashtiot/{team}/access` | one entry + `metadata` | appends one grant to the team's flat file; 201 |
| `DELETE /tashtiot/{team}/access` | one identity + `metadata` | revokes one; removes the file if it was the last; 201 |

**A store is addressed by name alone.** A store name is unique across the whole values
directory — the create enforces exactly that — so the name *is* the address, and a caller
never has to remember which team owns their store. `team` appears on the create and nowhere
else, because that is the one request deciding where the directory goes.

`_resolve_store` walks `KVs` to turn a name into `(team, directory, both documents)`. Unlike
the old flat layout there is **no conventional path to try first**: the team sits between the
values dir and the store, so resolution always costs one directory listing. Bitbucket's
`files/{dir}` resource lists **recursively**, so that is one call, not one per team.

**A store is any path of exactly three segments** under the values dir whose last is one of the
two access files. That rule also keeps the Tashtiot layout out of the store walk:
`Tashtiot/cloudops.yaml` is two segments. Anything else the walk sees is recorded and named in
the 404, so a values directory in an unexpected shape does not read as a deleted store.

**Registration order is the whole collision guard.** Fixed segments (`/stores`, `/tashtiot`)
are registered before `/{kv_name}` or a store literally named one of those would shadow them.
Tests pin each case: a store named `access` stays readable at `GET /{name}` (one segment
against two), `/tashtiot/{team}` cannot collide with a store read for the same reason, and the
one genuine casualty is a store named `stores` — the listing wins.

**Path parameters are pattern-validated**, not just body fields: `KVNameParam` / `TeamParam`
in `routes.py`, and `KVNameReadParam` / `TeamReadParam` on the two `GET`s, which accept
capitals as well (see the validation section). Starlette's convertor is `[^/]+` so nothing was exploitable without them, but a
malformed name would surface as an opaque Bitbucket 404 two calls later instead of a 422.

**No route takes query parameters; every route but the three `GET`s takes a required JSON
body carrying `metadata` — the `DELETE`s included.** A revoke's identity is its body,
`AccessRevokeRequest` (a discriminated union over `OidcAccessRevoke` / `K8sAccessRevoke`); a
store or Tashtiot delete sends `DeleteRequest`, which is `{metadata}` alone.
`test_no_mutating_route_takes_a_query_parameter` reads the rule off the OpenAPI document, so a
new route is covered the moment it is registered.

This **reverses a deliberate earlier choice**: revokes used query parameters because DELETE
bodies are sometimes dropped by proxies and HTTP clients. That risk is now accepted. Its
symptom is a 422 saying the body is missing, so if one appears, check what sits in front of
the service before the code. Python's httpx `.delete()` cannot send a body at all; use
`.request("DELETE", url, json=…)`, as the tests do.

**Which fields an `auth_type` needs lives in the schema**, not in code: the discriminator, the
required fields and `extra: "forbid"` make a partial or mixed identity (oidc needs
`path`+`dn`, k8s needs `path`+`name`+`namespace`+`cluster`) a **422** that names the field,
not a wrong-grant revoke. That 422 is FastAPI's standard `{"detail": […]}`, like every other
body error. Capabilities are refused the same way: not part of identity, and accepting them
would suggest a partial revoke is possible.

Four routes that no longer exist, so you do not re-add them:

- **No `/k8s-service-accounts` prefix.** Under this format a Kubernetes binding *is* an access
  entry, so it goes through the same `/{kv_name}/access` pair and `auth_type` picks the file.
- **No `GET /{kv_name}/access`.** It returned exactly `GET /{kv_name}`'s `access` key,
  unwrapped, for identical upstream work. **The path still exists**, so a `GET` there is a
  **405**, not a 404 — `test_reading_the_access_list_alone_is_gone` pins it. Also no
  `GET /files/{file}`: a file is not an address, a store is.
- **No `PATCH` on a single grant.** An entry's identity is everything but its capabilities, so
  "editing" one is a revoke plus a grant — and `PATCH /{kv_name}` replaces the whole list.
- **No `/pull-request` segment anywhere.** The four routes that opened a PR *without* waiting
  for CI are gone: without the wait a caller could not know the change was green, which is the
  one guarantee this service still offers now that it does not merge. Tests pin 404/405.

**Every route declares its own `tags=`, and the router declares none.** FastAPI
**concatenates** a router's tags with a route's, so a router default would put every Tashtiot
route in both documentation sections. `test_no_route_appears_in_both_sections` pins that no
operation has more than one tag. Docs grouping only — registration order is untouched.

### The request mirrors the document

A caller sends the same `access` entries that land in the file. Same boundary the rest of the
service keeps, and it is what makes `path` and `capabilities` expressible at all — a
translated shape (the old `roles` mapping) could express neither.

**The create request is exactly four required fields**: `team`, `kv_name`, `access`,
`metadata` (`test_create_takes_exactly_four_fields`). No `file` and no `kv_description`.
**A single grant is the request body on its own**, no wrapper, discriminated on `auth_type`.

**`metadata` is the one field that never reaches a file.** Six required non-blank strings —
`project`, `network`, `region`, `space`, `environment`, `island` — demanded by the deploying
team on every route but the reads. They are free-form on purpose, exactly like `dn`:
the vocabulary is the deploying team's, so any closed set or pattern here would reject a
value this service does not own. `RequestMetadata` in `schemas.py` is the only place they
are described.

It is **request-only**, and that is the whole point of it: the committed document keeps its
single `access` key, and the six coordinates go to the **pull-request description** (via
`_describe`, between `- change:` and `- grants:`) and the service log instead. Nothing else
carries them — they are not echoed in `VaultOperationResponse`, and `to_document_entry` /
`to_revoked_entry` build from named fields so there is no path by which one can reach an
access file. `test_metadata_never_reaches_the_committed_yaml` drives the seven routes that
commit a file and pins that; `tests/test_helpers.py` needing an edit is the signal that
something leaked.

It is required on **all nine routes that are not reads**: the create, both list-replacing
writes, the two one-grant adds, the two revokes and the two whole-resource deletes. A delete
names everything it needs in its path, so its body is the metadata block and nothing else;
that body exists so a deletion is audited in its pull request like any other change. The
three `GET`s take none, since a read changes nothing. `_describe` takes `metadata` as a
**required** keyword, so an operation that forgot to pass it fails with a `TypeError` instead
of opening an unaudited PR.

**Metadata alone is not a change.** `PATCH /{kv_name}` and `PUT /tashtiot/{team}` still
short-circuit to 200 with a null `pull_request` when the grants already match, because
`_unchanged` compares committed documents and metadata is not in one. Sending new
coordinates with an identical access list opens no pull request. That is a consequence of
metadata being request-only, not an oversight.

**Metadata is on the request body, not the entry.** `AccessEntry` is the **list-element**
type and carries none — a list element that sends one is a 422. The two `.../access` POST
bodies are `AccessEntryRequest`, a second discriminated union over
`OidcAccessEntryRequest` / `K8sAccessEntryRequest`, which subclass the entry models to add
the block. Subclasses rather than a `{metadata, access}` wrapper for three reasons: the
`oneOf` stays at the top of the body, which is the only place Swagger's branch picker and
the two per-branch examples work; `K8sAccessEntryRequest` *is* a `K8sAccessEntry`, so
`to_document_entry`'s `isinstance` dispatch and `_validate_access` keep working with no
unwrapping step for metadata to leak through; and it keeps the documented "no wrapper
object" shape for the two routes a wrapper would have repealed it for.

**Every request model sets `extra: "forbid"`.** Load-bearing rather than tidy: a caller
migrating from the old shape who still sends `file` or `roles` gets a **422**, not a 201 for a
request whose fields were silently dropped.

**Both entry models carry a model-level example** in `model_config["json_schema_extra"]`,
because the entry *is* the body and Swagger builds the editable box from the model alone.
Field-level `examples=[…]` do not reach it — without one, every string in the box reads
`"string"`. Two, because the body is a `oneOf`, and the picker is how a caller switches. Note
the config is **merged** with the base's (`**_AccessEntryBase.model_config`), not replaced:
`extra: "forbid"` lives there too, and replacing it would silently reopen both models
(`test_the_entry_models_still_forbid_extra_fields`).

**One pair of models, two routes, two prefills.** The models' own example sets `path` to
`"*"`, which is what `POST /{kv_name}/access` shows — a store grant covering that whole
store is the usual case. `POST /tashtiot/{team}/access` overrides it to `"+/team/*"`,
because that file grants a team access *into* the shared services store and its paths are
scoped in practice, so a prefilled `*` nudges towards the wrong shape. `team` there is a
literal placeholder: an OpenAPI example is static and cannot be templated from the URL.
**Examples, not defaults** — `path` stays required on both routes.

The override is `openapi_extra=` on the route decorator, and that spelling is
load-bearing. The obvious alternative, `Body(openapi_examples=…)` on the payload, replaces
the discriminated union's generated schema with a bare `anyOf` — which loses the `oneOf`
and the discriminator that Swagger's branch picker is built from, invisibly unless you go
looking for the dropdown. FastAPI **deep-merges** `openapi_extra` (`deep_dict_update`, in
`fastapi/openapi/utils.py`), so it adds the examples and leaves the schema untouched.
`test_the_tashtiot_examples_did_not_cost_the_branch_picker` pins the difference.

**Validation lives where the rule comes from** — the patterns are in `schemas.py` with
comments; what matters across them:

- `KV_NAME_PATTERN` / `TEAM_PATTERN` are a **single segment**. Both land in the committed path,
  so no slash means no directory can be escaped or created and `..` cannot match. **They are
  the path-traversal guard.**
- `KV_NAME_READ_PATTERN` / `TEAM_READ_PATTERN` are the same segment **plus capitals**, and are
  used only by the two `GET`s. The values directory is not written only by this service, so it
  may already hold a capitalised store or team: `_walk_store_dirs` finds those and
  `_resolve_store` matches names exactly, which left the lowercase-only pattern as the one
  thing making them unreachable — and it failed the request as *malformed* before the walk
  ever ran. Case never had any bearing on traversal (still one segment, no slash, no dot), it
  only decided which existing names were addressable. **Accepting capitals is not
  case-insensitive resolution**: the match stays exact, so wrong casing is a 404, not a
  different store. The writes deliberately keep the lowercase patterns — writing to a name
  this service would never itself create is a separate decision.
- `VAULT_PATH_PATTERN` is deliberately **looser** (`/`, `*`, `+` allowed) because a Vault path
  is not a filesystem path and never reaches one. `..` is refused in a `field_validator` rather
  than the pattern, because pydantic-core's regex engine has no look-ahead.
- `K8S_NAMESPACE_PATTERN` is an RFC 1123 *label* (no dots, ≤63), `K8S_SERVICE_ACCOUNT_PATTERN`
  a *subdomain* (dots, ≤253) — genuinely different limits, **do not collapse them**. Neither
  accepts uppercase, which the cluster rejects at admission. `*` in `name`/`namespace` is
  **rejected**: it would grant every workload in the cluster, and that escalation should need a
  human editing the YAML.
- `K8S_CLUSTER_PATTERN` is our own shape — no Kubernetes rule to borrow; the pipeline matches
  it against a `k8sMethods[].name`.

**Uniqueness.** A **store name** is global across the values dir (which is why a create walks
every team, and what makes the name a sufficient address). A **grant** is unique within its
file on its identity: `(authType, path, dn)` for oidc, `(authType, path, name, namespace,
cluster)` for k8s. **Capabilities are not part of identity** — granting `read` and later
`read`+`write` to the same principal on the same path is an edit of one entry, not a second, so
a duplicate identity is a 409 and a revoke need not know what the grant currently permits.

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
| indentation, quote character and block-scalar style | `_BlockStyleDumper` in `helpers.py` |
| the allowed capabilities | `Capability` in `schemas.py` |
| the k8s name patterns and limits | the `K8S_*_PATTERN` / `K8S_*_MAX_LENGTH` constants in `schemas.py` |
| the branch prefix | `BRANCH_PREFIX` in `conf.py` |
| the values dir and the Tashtiot sub-dir | `VAULT_VALUES_DIR` / `TASHTIOT_DIR` in `conf.py` |
| how a directory is deleted | `GitFileRemover` in `clients/git.py` |

**Nothing outside `helpers.py`/`schemas.py` knows a document key name** — `operations.py`
reaches identity through `access_entry_identity` and kind through `auth_type_of` rather than
indexing `entry["authType"]`. `to_document_entry` (a grant) and `to_revoked_entry` (a revoke's
identity, with `[]` capabilities) are the only bridges from a validated request model to a
document dict. Both call the `build_*_access_entry` builders. **Keep it that way.**

## Architecture

**Composition.** `create_app()` in `app/main.py` is the single wiring point: it calls
`general_create_app(enable_auth=True)`, constructs the Bitbucket client **once** from config —
handing it a `GitFileRemover` for the one non-REST operation — and injects it into
`get_v1_vault_router(bitbucket)`. Connectors are never built per-request; the router factory
closure is the injection seam (no FastAPI `Depends` for connectors). **`create_app()` being the
sole construction site is what lets the test fixture monkeypatch the client class.**

**Config is two layers, both from `.env`**, both built at **import time** — so a missing
required setting is an import error, not a request error, and a new *required* setting would
break every test at collection. Everything added for this format is defaulted; **add new
settings to `tests/conftest.py` too.** `app/global_conf.py` holds `BITBUCKET_*`,
`HTTP_TIMEOUT_SECONDS`, `VERIFY_SSL`; `app/v1/vault/conf.py` the prefix, tags, values-repo
layout, PR shaping, `CI_*` timeouts and `GIT_*`. There are deliberately **no CI-server
settings** — the `CI_*` knobs bound the **build-status polling**; their names still say PIPELINE
because a pipeline is still what is waited on, only the place we ask changed. `AUTH_*` is
**not** declared here: the library reads it from the environment.

### The HTTP funnel

`BitbucketClient` is the only client. It is hand-rolled rather than using the library's `Git`
connector because `Git` exposes no pull-request operations, and PRs are the entire point.
Everything goes through **`app/clients/http.py`** — `request()` is the single funnel raising
`ExternalServiceError(service_name, status_code, detail=None)`:

| Cause | `status_code` carried |
|-------|----------------------|
| `httpx.TimeoutException` | `504` |
| any other `httpx.RequestError` (DNS, connect, TLS) | `502` |
| non-2xx response | the upstream's own status |

**Never call `self._client.request` directly** — a bare `httpx.RequestError` escapes past the
routes' `except ExternalServiceError` and the caller gets an opaque 500. The `504` is
load-bearing: `routes.py` keys off `status_code == 504` to answer 504 instead of 502.

### `app/clients/git.py` — the one operation that is not REST

Bitbucket Server has **no endpoint that deletes a file**: `browse/{path}` is `GET` and `PUT`
only, Atlassian have said outright it is unsupported, and the `-F files=<path>` workaround
every search result offers is the **Bitbucket Cloud** `src` resource, which does not exist on
Server. So `delete_file` delegates to `GitFileRemover`, which pushes a deleting commit over
**HTTPS** with the same `BITBUCKET_TOKEN` as a bearer header. No SSH key, no second secret.
Four deliberate things:

- **The token goes in through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`**, not
  `-c http.extraHeader=…`, because argv is readable through `ps`. `_scrub` also strips it from
  any stderr reaching an error body, and `GIT_TERMINAL_PROMPT=0` stops a credential prompt
  hanging the request.
- **It runs in `asyncio.to_thread`** — a subprocess plus two round-trips on the event loop
  would stall every other in-flight request.
- **One commit on one topic branch, and stop.** It never pushes to the base branch, so the PR,
  the gate and every rollback are unchanged.
- **Every failure leaves as `ExternalServiceError`**, matching `http.py`, so the routes map it
  without knowing a subprocess was involved. A git timeout is a **504**, same as a read timeout.

It needs the **git binary in the image** — the Dockerfile asserts `git --version` at build time
rather than letting a missing binary surface as a 502 on the first delete.

### The build-status gate

`get_build_statuses(commit_id)` reads `GET /rest/build-status/1.0/commits/{commitId}` — the
**only** endpoint here that is not repo-scoped, and deliberately the deprecated one. **Do not
"modernise" it.** The repo-scoped 7.4+ replacement, `GET {api}/commits/{commitId}/builds`,
takes a **required `key` query parameter** and returns the single build carrying that key. A
bare call is rejected with *"Please specify a non-blank key smaller than 255 characters"*,
which surfaces as a 502 on every gate; a keyed call cannot enumerate. **Enumeration is the
whole requirement** — the gate asks "which builds exist on this sha" before any key is known.
This client called the repo-scoped resource for a while and no test caught it, because the
fakes, the respx mocks *and* the stub all implemented the invented contract.
`test_get_build_statuses_asks_the_unscoped_resource_and_sends_no_key` now pins the path and the
absence of `key`. If the old resource is ever withdrawn, the replacement is to make the
expected build keys configuration and fetch each by key — which would silently pass a gate for
any workflow nobody listed, so it is not the default.

`await_builds` is two-phase, and the split is not cosmetic:

- **phase 1, `find_builds`** — wait for *at least one* build. The forge webhook is asynchronous,
  so a freshly pushed commit has no builds for a few seconds; an empty list can never mean
  "finished".
- **phase 2, `wait_for_builds`** — wait until every build has left `INPROGRESS`.

`PENDING_BUILD_STATES` is `{INPROGRESS}` and `SUCCESSFUL_BUILD_STATE` is `SUCCESSFUL` —
separate constants on purpose: `CANCELLED` and `UNKNOWN` are **terminal but not success**, so a
gate keyed off "not FAILED" would pass both. The gate passes only when *every* build succeeded.

**Known consequence, documented rather than worked around:** a CI server that posts one status
per workflow may not post them all at once, so a gate seeing a single finished build while a
sibling has yet to appear returns early. Bitbucket's own required-builds merge check is the
backstop — and it matters less now, since a human reviews the PR before merging it anyway.

### `app/helpers.py` is pure

**Every transform deep-copies the whole document and replaces only `access`**, so a sibling key
this service has never heard of survives. Not hypothetical tidiness: an earlier revision
rebuilt the document from its own list alone, which silently dropped every other key — and the
erase would have looked like a legitimate PR diff. The `SIBLING_KEY` tests in
`tests/test_helpers.py` pin it with a key name the code has never seen.

They return a **new** document and never mutate the input. That is what lets an update diff old
against new with `yaml_data_equals` and skip a no-op commit. **Any helper added later must keep
both properties.** `add_access_entry` accepts `None`, so "file does not exist yet" and "append
to an existing file" are one code path; it raises `DuplicateAccessEntry` (→409).
`remove_access_entry` raises `AccessEntryNotFound` (→404) and **keeps an emptied list as
`access: []`** — dropping the file is the *caller's* decision, in `_store_writes` and
`_prepare_access_change`.

`build_branch_name` collapses its two coordinates when they match, so a store whose name equals
its team renders `vault-kv/cloudops-abc123`, not `vault-kv/cloudops-cloudops-…`.

`render_values_yaml` uses `_BlockStyleDumper`, doing **four** things PyYAML does not, all about
the committed text rather than its meaning: block sequences indented under their key
(`test_oidc_file_has_the_expected_shape` pins the exact bytes); multi-line strings as `|`
blocks, since the default representer emits one escaped, width-wrapped scalar that is
unreadable in the PR diff a human is supposed to review; **every single quote turned into a
double one** (`test_nothing_is_single_quoted`), rewriting only the quote *character* a quoted
scalar would have got, so a plain-safe value stays bare; and `dn` quoted even though it need
not be, because unquoted a DN reads as structure rather than one value (`DN_KEY` is where that
is stated). `sort_keys=False` preserves entry field order — alphabetical would reorder every
entry away from the spec's shape. `*` is emitted as `"*"`: a bare `*` is a YAML alias and would
not round-trip.

**Reformatting on first touch is expected, not a bug.** Every edit is a full round-trip, so a
file this service has not written before comes back in the dumper's style: comments and blank
lines dropped, flow lists expanded, quoting normalised. Only the *text* changes; values and
existing key order survive. After that first PR the file is canonical. Preserving comments
would mean a round-trip loader (`ruamel.yaml`) replacing `render_values_yaml` and `safe_load`
both.

## The chain — read this before editing `operations.py`

**Two shared helpers, nested.** `_open_pull_request` owns branch → commit(s) → PR and both of
their rollbacks (either failure deletes the branch). `_open_and_validate` calls it and then
waits for the validation build. **Every mutating operation goes through them** — the blocking
ones via `_open_and_validate`, which is now `_open_pull_request`'s **only** caller. It stays a
separate function because the two rollback stages differ (the inner deletes the branch, the
outer also declines the PR), and folding them together would put both stories in one place.

**Nothing merges.** What went with that decision:

- `merge_pull_request` and `get_branch_head` on the client have **no caller**. Both are kept —
  they are the client's REST surface, they are tested, and merging is the obvious thing to
  re-add — but nothing in `app/v1/` reaches them.
- `await_builds(..., exclude_keys=…)` is unused for the same reason. It existed only so a
  deploy gate on a **fast-forward merge** would not re-read the validation gate's results, and
  there is no second gate to protect.
- `deploy_builds` is gone from `VaultOperationResponse` — nothing could populate it.
- **There is no point of no return.** Every failure is fully rolled back, and that is a
  consequence of not merging rather than an effort.

**A change is a `Change`, not a path.** `Change(writes=[FileWrite…], removes=[path…])` is what
the chain commits. That is what lets a create write **two files on one branch** (Bitbucket's
browse resource writes one path per call, so a store costs two `put_file`s), and what lets an
update rewrite one file and remove another in the same PR. `removes` holds paths *or
directories*; a store delete passes the directory. `FileWrite.source_commit_id` is per-file
because Bitbucket's optimistic-lock token is: required when editing a path that exists,
**refused** when creating one, and a store's two files are frequently in different states —
adding the first Kubernetes grant edits `oidc-access.yaml` and creates `k8s-access.yaml` in one
commit.

`create_kv_mount_operation` runs, in order:

0. **name scan** — `_assert_name_is_free`, in **two halves**. First it walks every store
   directory on the base branch; the name under any team is a 409 (store names are global, so
   this cannot be scoped to the target team). Then, because nothing here merges, it asks
   whether an **open pull request** already creates that name —
   `_name_claimed_by_open_pull_request`, also a 409, naming the PR to merge or decline. Without
   the second half a repeat create just opens a second PR, which is the *common* case now
   rather than a rare one. Still a check, not a lock: two creates racing inside the same second
   both pass and the second PR conflicts at merge, like any other GitOps race.

   The second half costs one `list_pull_requests` per create, plus one
   `get_pull_request_changes` per open PR **whose branch starts with `BRANCH_PREFIX`** — the
   prefix filter is what keeps a busy repo from costing a request per open PR
   (`test_a_pull_request_outside_the_branch_prefix_is_not_even_read`). The branch *name* cannot
   answer the question: `build_branch_name` joins team and store with a dash and both may
   contain dashes, so `vault-kv/cloudops-my-passwords-…` parses two ways. The changed paths are
   unambiguous, and `_store_name_in` applies the same three-segments rule the store walk uses.
   The base-branch half runs first because it is the cheaper answer.
1. `_store_writes` partitions the grants and renders one file per non-empty kind. Nothing is
   read: the store is new, so neither file exists and no lock token applies.
2. `create_branch` → `put_file` per file
3. `create_pull_request`
4. **the gate**: `await_builds` on `pull_request.from_commit` (`fromRef.latestCommit`) — the sha
   the PR's Builds tab shows
5. return. The pull request is open, green, and a human's to merge.

There is **no transaction**; each step hand-rolls its own rollback:

| Failure | Rollback |
|---------|----------|
| any `put_file` / `delete_file` | delete the branch |
| `create_pull_request` | delete the branch |
| the validation build fails or times out | decline the PR **and** delete the branch |

Rollback failures are logged and never raised over the original error.

Three details that are easy to break:

- **A rolled-back failure reports the pull request as `DECLINED`, not `OPEN`.**
  `_decline_and_cleanup` **returns** the declined PR and the three raise sites in
  `_open_and_validate` report *that*, not the pre-decline object they were holding — reporting
  the stale one sends a caller to review a PR that is closed and whose branch is gone. The
  inverse matters too: a decline that itself failed hands back the original, because the PR
  really is still open then. `test_a_failed_decline_still_reports_the_pull_request_as_open`
  pins that direction so the fix cannot become "always say DECLINED".
- **The version is re-read before declining.** Bitbucket's `version` is an optimistic lock that
  CI status updates bump, so `_decline_and_cleanup` re-reads the PR rather than declining with
  the stale version `create_pull_request` returned.
- **The gate needs `from_commit`.** If Bitbucket reports no `fromRef.latestCommit` there is no
  sha to watch, and reporting an unvalidated PR as validated is not the fallback — the chain
  declines the PR and deletes the branch, exactly as a red build would.

### Update, delete and the sub-resource

**Update** resolves the store, converts the request, and **short-circuits when nothing changed**
— success with `pull_request: null` rather than an empty PR. `_unchanged` compares parsed
documents, so a re-render or a reordering is not a change. Then `_store_writes` decides the
whole `Change`: a kind that has grants is written, a kind that had a file and now has none is
**removed**. That is the one place the "no empty file" rule is applied, and it is why an update
can delete a file.

**Delete** removes the store's **directory**, both files with it. `_prepare_delete` resolves the
store and returns it — no document to render, nothing to diff, no lock token to fetch, because
git resolves the path from the branch it fetched. No referential scan: a grant lives inside the
store it reaches. No `yaml_data_equals` short circuit either — a removal is never a no-op.

**The team directory goes too when that store was the team's last, and no code decides that.**
Git stores a tree of blobs and has no concept of an empty directory — one exists exactly as
long as something is under it — so `git rm -r KVs/<team>/<kv>` takes the store directory with
the files, and `KVs/<team>` disappears from the commit once nothing else is under it. A team
with another store keeps its directory, for the same reason. `tests/clients/test_git.py` pins
all three against a real repo, because this is the obvious thing to "fix" by hand and a
`.gitkeep` or a per-file removal would quietly break it. **There is nothing to add here**: git
cannot be asked to delete a directory that holds no files, because for git it does not exist.
If an empty directory ever appears it is in a *working clone* — `git pull` does not prune
directories its merge emptied — not in the repository.

**One grant at a time** (`_prepare_access_change`) reads only the file its `auth_type` selects,
so a Kubernetes grant never appears in the diff of an OIDC change. An add rejects a duplicate
identity (409); a remove 404s when the identity is not there. Removing the last grant of a kind
**removes that file**, for the same reason an update does.

**The Tashtiot operations are the same chain minus the store parts**: no name scan (a team owns
exactly one file and its name is the address), no two-file partition, no directory. The upsert
is create-or-replace in one call because the file's identity *is* the team, so a 409 would have
nothing to describe; it short-circuits on no change like an update does.
`_prepare_tashtiot_access_change` is the store version minus the file choice — one file holding
both kinds, so `auth_type` selects nothing. The rules match because they are the document's
rather than the layout's: duplicate identity 409, unknown identity 404, and the file is
**removed** when its last grant goes, since the upsert refuses an empty list and so no other
route could write one. A team with no file yet is a **404**, not an implicit create: `PUT` is
what creates a team's file, and appending to nothing behind a route that reads as an edit would
hide that.

**Ordering consequence worth knowing** (documented, not worked around): a caller who creates a
store and then immediately adds a grant to it gets a 404 — the store is on an unmerged branch
and every scan reads the base branch. **This is the normal case now**, not an edge one. A
repeat *create* is the one case no longer left to the merge conflict, since the name scan reads
open pull requests too.

## Tests

499 of them. `tests/test_helpers.py` (pure, no library import — the document model and the
exact committed bytes), `tests/clients/` (the Bitbucket client and `http.py`'s error mapping
against the real REST shapes via respx, plus `test_git.py` running **real git against a real
bare repo** in a temp dir), `tests/v1/vault/` (schemas, the chain + rollbacks with duck-typed
fakes, routes end-to-end through `TestClient`).

`test_git.py` uses no mocks deliberately: a fake there would only prove the fake agrees with
itself, which is exactly how the build-status bug survived a full suite.

Three conftest details to respect:

- `tests/conftest.py` sets `os.environ` **before** any `app.*` import (its own imports sit below
  the env block behind `# noqa: E402`) because the config singletons are built at import time.
  It pins `VAULT_VALUES_DIR`/`TASHTIOT_DIR` so a developer `.env` cannot change every expected
  path, blanks the optional `AUTH_*` knobs, and zeroes the `CI_*` timeouts.
- It provides the shared `auth_headers` fixture (with an `exp` claim). **Do not shadow it in a
  test module** — a token without `exp` is a 401 and looks like a routing bug.
- `tests/v1/vault/conftest.py`'s `client` fixture monkeypatches `app.main.BitbucketClient` — the
  class, not an instance. `seed_store()` there writes only the files a store actually has grants
  for, mirroring what a create commits.

**Steering the fakes** (`tests/fakes.py`):

- `FakeBitbucket(existing_files={path: yaml})` is the repo's whole state, keyed on the full
  repo-relative path; `list_files` derives the recursive listing from it exactly as the real
  resource does, and an absent path raises a 404 `ExternalServiceError`.
- `fail_on={"put_file": exc}` injects a failure into one method by name. Rollback tests assert
  against `bitbucket.calls`, an ordered list of every method reached — that list is what pins
  the call *order*, not just the outcome.
- `bitbucket.committed` is paths written, `bitbucket.deleted` paths removed. `delete_file`
  deliberately does **not** drop the path from `existing_files`: that is the base branch, and
  only a merge would remove it there, so a test wanting the merged state says so.
- `get_pull_request` bumps `version` on every call, reproducing the optimistic-lock trap.
- **Every pull-request method returns a `replace()` copy, never the stored object.** The real
  client parses a fresh `PullRequest` out of each response, so an object a caller already holds
  cannot be edited later by the server. Returning the stored instance made the fake
  *structurally unable* to catch a caller reporting a stale PR: a decline mutated the very
  object the caller held, so the wrong answer corrected itself. Same shape of fake that let the
  build-status bug through a green suite. **If you add a method returning a `PullRequest`,
  return a copy.**
- `FakeBitbucket(builds=[...])` is a **queue** popped once per `await_builds`; an `Exception` in
  the list is raised instead of returned. Running dry is an `AssertionError`, so a test running
  two full chains must script the entries.

## Conventions

- **Status codes, one line: 201 when a pull request was opened, 200 when none was.** Every
  mutating route declares 201; the only 200s are the no-op short circuits and the reads.
  `_execute` owns that downgrade, so no route has to know it. Then **409** duplicate store name
  or grant; **404** unknown store, grant or Tashtiot file; **502** build/transport failure;
  **504** build or upstream timeout; **422** request-body or path-parameter validation.
- **`routes.py` has two funnels, not one.** `_execute` is for mutations; the `GET`s go through
  **`_read`**, which differs in both directions: an upstream **404 stays a 404** (a missing
  store is the caller's mistake, not a bad gateway), and its failure body is a bare
  `{"status", "error"}` dict rather than a `VaultOperationResponse`, since reads return raw
  parsed YAML on success. **Putting a read through `_execute` turns every unknown store into a
  502.**
- **One response model for every mutating route**, `VaultOperationResponse`. `(file, kv_name)`
  addresses stores, grants and Tashtiot files alike — `file` carries the *directory* for a store
  and the file path for a Tashtiot upsert.
- **The CI outcome is `validation_builds`**, a list of `BuildInfo{key, state, name, url}`. A
  list because Bitbucket stores one result per key per commit; `key` rather than `number`
  because the key is what identifies a build to Bitbucket and tells two workflows apart.
- **Failures are `return`ed as a `JSONResponse`, not raised.** So the decorator's `status_code=`
  and `response_model=` describe the success path only — FastAPI does not validate or document
  the failure bodies. **Adding a field to `VaultOperationResponse` means the failure path
  silently omits it unless `to_response()` is updated too.**
- **The mutating endpoints block** until the validation build finishes. Any proxy in front needs
  a read timeout above `CI_PIPELINE_START_TIMEOUT_SECONDS + CI_PIPELINE_TIMEOUT_SECONDS` — one
  gate's worth, not two. **There is no escape hatch**; if that budget is not workable for some
  client, the answer is a shorter `CI_PIPELINE_TIMEOUT_SECONDS`, not a route that reports
  success without knowing the build passed.
- The committed entries are the contract with the deploy pipeline — change
  `build_oidc_access_entry` / `build_k8s_access_entry` and the pipeline together.
- **Auth is global and no route reads the caller's claims.**
  `general_create_app(enable_auth=True)` wires the library's `AuthMiddleware` over every route
  except docs/metrics/health/probes. It activates only when `AUTH_ENABLED=true` **and** one
  `AUTH_*` verification material is set; turning it on with none set fails app creation, and
  setting two makes the library's mode selection ambiguous — which is why `tests/conftest.py`
  configures HS256 only. Nothing here is per-identity, so there is no `get_current_claims`
  dependency; the middleware alone is the authorization boundary. The Remote Config provider is
  deliberately not wired (see README's extension points).
- Commits are **Conventional Commits** (`feat:`, `fix:`, `test:`, `feat!:` for a breaking
  change) — follow the existing `git log`. No changelog tooling. The version is a literal in
  `pyproject.toml` plus the `APP_VERSION` build arg.

## uv / packaging

`uv.toml` (untracked, Artifactory index + creds) is the uv equivalent of `pip.ini`; **when both
a `uv.toml` and a `[tool.uv]` section exist, uv.toml wins** — so uv settings go there, not in
`pyproject.toml`. `[project.dependencies]` lists **only what this service imports directly**;
`prometheus-client`, `aiocache`, `cryptography` and `python-dotenv` arrive transitively via the
library, and re-declaring them just lets the two drift.

**Known gap — the committed `uv.lock` is stale.** `[tool.uv.sources]` has been removed from
`pyproject.toml`, but `uv.lock` still records `tashtiot-apis-library` as
`source = { editable = "../apis-library" }`, so `uv sync --frozen` still fails in the Dockerfile
and the `.woodpecker` Test step. Regenerating it needs Artifactory, reachable **only on-prem** —
runbook in README's "Package index" section. Until then: **never run a bare `uv run`/`uv sync`
off-prem**, and to develop against the local library overlay it rather than re-adding a sources
block (uv refuses `sources` in `uv.toml`):
`uv sync --group dev && uv pip install -e ../apis-library`.

**A clean lock alone does not fix CI.** `uv.toml` is gitignored, so nothing committed declares
the `pypi-local`/`pypi` indexes; `.woodpecker/build.yaml` injects credentials
(`UV_INDEX_PYPI_LOCAL_*`) for an index name that is never defined, and the Dockerfile copies no
index config. If the base image does not supply it, declare the URLs (credentials stay in
secrets) as `[[tool.uv.index]]` in `pyproject.toml`.

Without `tashtiot-apis-library>=1.1.0`, `tests/v1/` cannot even be collected
(`tests/test_helpers.py` and `tests/clients/` still run).
