# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service that **writes a file to a Bitbucket repo and watches the Woodpecker
pipelines that act on it**. A create request opens a pull request, waits for the validating
pipeline, merges, waits for the deploying pipeline, and returns
`Successful creation of <kv name>`.

**Scope boundary — read this first.** The service deliberately knows nothing about Vault. It
commits a file holding two independent **lists**, KV stores and Kubernetes auth roles:

```yaml
kvStores:
  - name: myapp
    description: payments secrets
    roles:
      read:                  # `read` and `write` are separate keys; either, or both
        - app01.corp.example.com
      write:
        - app02.corp.example.com
  - name: billing            # several stores per file is the point
    description: billing secrets
    roles:
      read:
        - app03.corp.example.com

kubernetesAuth:              # second top-level key, same file, edited independently
  - name: myapp-ci
    description: CI deployer for the payments app
    cluster: prod-il-1       # optional
    serviceAccounts:
      - vault-reader
    namespaces:
      - payments
    access:                  # names stores + a capability. NOT a policy.
      read:
        - myapp
    ttl: 24h                 # optional
```

**One file, many entries.** A create *appends* to `{VAULT_VALUES_DIR}/{file}.yaml`, creating
the file only if it is the first entry in it. Nothing else in that file is touched — in
particular a KV create must not erase `kubernetesAuth`, or vice versa; both directions are
pinned by tests in `tests/test_helpers.py`.

Mounts, KV engine versions, policies, HCL — none of it is modelled here. That is the deploy
pipeline's business. `access: {read: [<store>]}` names a store and a capability; the
*pipeline* derives the policy from that pair. Resist adding Vault semantics: an earlier
version generated mounts and policy HCL, and it was removed on purpose — the deleted
`find_policy_name` + `policies: [...]` shape is exactly the line not to cross.

**The `kubernetesAuth` shape is a proposal**, a guess at the pipeline's contract, so every
format decision sits behind one name (see "Format isolation" below).

Built on the internal `tashtiot-apis-library` (base app, `BaseAPI`, `ExternalServiceError`)
and follows the patterns of the reference `example-api` (sibling repo).

## Commands

uv-managed (Python 3.12). Package indexes come from `uv.toml` (internal Artifactory, untracked).

```bash
uv sync --group dev                 # install deps into .venv — ON-PREM ONLY, see below
uv run --no-sync python -m app.main # serves on 0.0.0.0:5000 (--host/--port override)
uv run --no-sync pytest             # everything under tests/ (547 tests, ~7s)
uv run --no-sync pytest tests/v1/vault                                        # one suite
uv run --no-sync pytest tests/v1/vault/test_operations.py::test_happy_path_call_order  # one test
docker build -t vault-api . && docker run -p 5000:5000 --env-file .env vault-api
```

**`--no-sync` is not optional off-prem.** `pyproject.toml` and the stale `uv.lock` disagree
(see the packaging section), so a bare `uv run`/`uv sync` re-resolves, tries to reach
Artifactory for `tashtiot-apis-library>=1.1.0`, and fails — public PyPI carries only `0.1.0`.
`--no-sync` runs against the existing `.venv`, which is already correct.

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
| `POST /api/vault/v1/kv/` | `{file, kv_name, kv_description, roles}` | the chain below; blocks until the deploy pipeline ends |
| `POST /api/vault/v1/kv/pull-request` | same body | steps 1-3 only: opens the PR and returns. No CI wait, no merge |
| `GET /api/vault/v1/kv/{file}` | — | the whole values file, parsed YAML returned as-is (not a schema) |
| `GET /api/vault/v1/kv/{file}/{kv_name}` | — | one entry out of that file's `kvStores` |
| `PATCH /api/vault/v1/kv/{file}/{kv_name}` | `{kv_description?, roles?}` | edits one store; its siblings are untouched |
| `DELETE /api/vault/v1/kv/{file}/{kv_name}` | — | removes one store, same chain as a create; 200. **409** if a `kubernetesAuth` entry still references it |
| `DELETE /api/vault/v1/kv/{file}/{kv_name}/pull-request` | — | steps 1-3 only: opens the removal PR and returns; 201 |
| `POST /api/vault/v1/kubernetes-auth/` | `{file, role_name, role_description, cluster?, service_accounts, namespaces, access, ttl?}` | same chain; 201 |
| `POST /api/vault/v1/kubernetes-auth/pull-request` | same body | steps 1-3 only; 201 |
| `GET /api/vault/v1/kubernetes-auth/{file}` | — | the file's `kubernetesAuth` list (empty list, not 404, when it has none) |
| `GET /api/vault/v1/kubernetes-auth/{file}/{role_name}` | — | one entry out of that list |
| `PATCH /api/vault/v1/kubernetes-auth/{file}/{role_name}` | `{role_description?, service_accounts?, namespaces?, access?, ttl?}` | edits one role; `role_name` and `cluster` are **not** editable |
| `DELETE /api/vault/v1/kubernetes-auth/{file}/{role_name}` | — | removes one role; 200. No referential check — a store with no role is valid |
| `DELETE /api/vault/v1/kubernetes-auth/{file}/{role_name}/pull-request` | — | steps 1-3 only; 201 |

**Kubernetes auth gets its own prefix, not a segment under `/kv`.** `/kv/{file}/kubernetes-auth`
and `/kv/kubernetes-auth/{file}` both put a fixed segment in a `{file}`/`{kv_name}` position,
so a file or store actually named `kubernetes-auth` fights the endpoint for the URL and only
registration order decides. A separate prefix has **zero** shadowing risk in either direction,
and a test pins that a file named `kubernetes-auth` stays readable under `/kv`.

**`/pull-request` is a fixed segment registered before the `{file}` routes.** There is no POST
on those paths today so nothing is ambiguous, but if one is ever added this route must keep
winning — otherwise a file named `pull-request` shadows the endpoint. A `GET` on the same URL
is still a normal read of a file named `pull-request`; the two coexist, and a test pins it.
The delete PR-only route has no such trap — three segments cannot collide with two — so a
store *named* `pull-request` stays addressable at `DELETE /{file}/pull-request`; a test pins
that too. It is still registered before `/{file}/{kv_name}`, for one rule. **Both rules apply
identically to the `/kubernetes-auth` router**, which has the same two shapes.

**Path parameters are pattern-validated**, not just body fields: `file`, `kv_name` and
`role_name` carry `FILE_PATTERN` / `KV_NAME_PATTERN` / `K8S_ROLE_NAME_PATTERN` via
`Annotated[str, Path(...)]` aliases (`FileParam`, `KVNameParam`, `RoleNameParam` in
`routes.py`) on every `GET`/`PATCH`/`DELETE`. Nothing was exploitable without them —
Starlette's path convertor is `[^/]+` — but a malformed `file` used to surface as an opaque
Bitbucket 404 instead of a 422.

`API_PREFIX` (default `/api/vault/v1/kv`) and `API_K8S_AUTH_PREFIX` (default
`/api/vault/v1/kubernetes-auth`) come from `app/v1/vault/conf.py`. Everything added for
Kubernetes auth is **defaulted**, so `tests/conftest.py` needed no change — a new *required*
setting would break every test at collection, because the config singletons are built at
import time.

**The create request is exactly four fields** and `test_create_takes_exactly_four_fields` pins
that: `file`, `kv_name`, `kv_description`, `roles`. There is no environment and no
infra-coordinates `metadata` block.

**`file` is the path-traversal guard now, not `kv_name`.** That inverted when the format
changed — `kv_name` became a value inside the document while `file` became the thing that
lands in `{VAULT_VALUES_DIR}/{file}.yaml`:

- `FILE_PATTERN` and `KV_NAME_PATTERN` are both a **single segment** of `[a-z0-9]`/dashes. No
  slash, so no directory can be escaped or created, and `..` cannot match.
- `kv_name` is single-segment for a second reason: the routes address a store as
  `{file}/{kv_name}`, and a slash would make that split ambiguous. Multi-segment names were
  supported under the old one-file-per-KV layout and are not any more.
- `roles` is **required**, at least one key with at least one host. `ALLOWED_ROLE_KEYS` in
  `schemas.py` holds `read` and `write` as **separate** keys — a store may carry either or
  both. The combined string `read/write` is *not* a role and is rejected; `<read/write>` in
  the format spec was a placeholder meaning "one of these", like `<name>` and `<FQDN>`
  beside it. That frozenset is the only thing to change if the pipeline ever wants a
  different set; everything else treats `roles` as an opaque mapping.

**Kubernetes auth validation is Kubernetes' rules, not ours.** `K8S_NAMESPACE_PATTERN` is an
RFC 1123 *label* (no dots, ≤63) and `K8S_SERVICE_ACCOUNT_PATTERN` an RFC 1123 *subdomain*
(dots allowed, ≤253) — genuinely different limits. **Do not reuse `FQDN_PATTERN`**: it
accepts uppercase, which the cluster rejects at admission, so a request would pass here and
fail long after the PR merged. `*` in `service_accounts`/`namespaces` is **rejected** —
`["*"]` binds every workload in the cluster, and that escalation should need a human editing
the YAML, not an API call. `ALLOWED_KV_ACCESS_KEYS` is a separate frozenset from
`ALLOWED_ROLE_KEYS` on purpose: two pipeline contracts that agree today and may not later.

**Uniqueness differs between the two kinds.** A store name is global (unique across the
whole values dir). A role is keyed on **`(cluster, name)`** across the dir, plus `name` alone
within one file — a Vault k8s role is scoped to its auth mount, so `deployer` in two clusters
is legitimate, but two in one file would make `{file}/{role_name}` unaddressable. `cluster` is
**optional** (an estate with a single auth mount has none to name), so uniqueness keys on
`(cluster or "", name)` — an absent cluster is its own coordinate, not a wildcard.

### Format isolation — the one-line-edit inventory

The `kubernetesAuth` shape is a guess, so each guess has exactly one home:

| What could be wrong | Single place to change |
|---|---|
| the top-level key name | `K8S_AUTH_KEY` in `helpers.py` |
| any entry field name, and camelCase vs snake_case | `_K8S_AUTH_ENTRY_KEYS` + `build_kubernetes_auth_role` in `helpers.py` |
| the capability key set, the `access` semantics | `ALLOWED_KV_ACCESS_KEYS` in `schemas.py` |
| whether `cluster` exists at all | `build_kubernetes_auth_role` + one optional schema field |
| the k8s name patterns | the four `K8S_*_PATTERN` constants in `schemas.py` |
| the branch prefix | `K8S_AUTH_BRANCH_PREFIX` in `conf.py` |

**Nothing outside `helpers.py`/`schemas.py` knows a single document key name of this format** —
`operations.py` reaches identities and referrers through `kubernetes_auth_role_identities` /
`kv_store_referrers` rather than indexing `entry["cluster"]`. Keep it that way.

## Architecture

**App composition (`app/main.py`).** `create_app()` is the single wiring point: it calls
`general_create_app(enable_auth=True)`, constructs the Bitbucket and Woodpecker clients **once**
from config, and injects them into `get_v1_vault_router(bitbucket, woodpecker)` **and**
`get_v1_kubernetes_auth_router(bitbucket, woodpecker)`. Both routers share the same two
connectors — the values file is shared, so the two resource kinds are two views of one repo,
not two services. Connectors are never built per-request; the router factory closure is the
injection seam (no FastAPI `Depends` for connectors).

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
the same four-file split the reference API uses. **Both resource kinds live in those same four
files**; there is no `app/v1/kubernetes_auth/`. That is deliberate — the two kinds write the
same document, share `_open_pull_request`/`_commit_via_pull_request`, `_walk_values_files` and
`VaultOperationResponse`, and splitting them into packages would either duplicate that spine or
turn it into a cross-package import. A third kind goes in the same way: a second router factory
in `routes.py`, a `_prepare_*` pair in `operations.py`, a key in `helpers.py`.

There are **no `__init__.py` files anywhere** — implicit namespace packages, with
`pythonpath = ["."]` and `package = false` in `pyproject.toml`. A new module needs no
`__init__.py`; adding empty ones is churn.

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

**Shared helpers (`app/helpers.py`)** are pure. For `kvStores`: `build_kv_store` (one entry),
`build_kv_stores_document`, `add_kv_store`, `read_kv_stores`, `find_kv_store`,
`kv_store_names`, `update_kv_store`, `remove_kv_store`. For `kubernetesAuth`:
`build_kubernetes_auth_role`, `add_kubernetes_auth_role`, `kubernetes_auth_roles`,
`find_kubernetes_auth_role`, `kubernetes_auth_role_names`, `kubernetes_auth_role_identities`,
`kubernetes_auth_role_stores`, `update_kubernetes_auth_role`, `remove_kubernetes_auth_role`,
plus `kv_store_referrers` (the referential rule). Shared: `slugify_mount_path`,
`build_branch_name`, `values_file_path`, `render_values_yaml`, `yaml_data_equals`.

**The public names are thin wrappers over one key-parameterised private family** —
`_entries` / `_find_entry` / `_entry_names` / `_add_entry` / `_update_entry` /
`_remove_entry`, each taking the top-level key to act on (`KV_STORES_KEY` or
`K8S_AUTH_KEY`). Every transform deepcopies the **whole document** and replaces only its own
key, so the sibling key survives. That is not hypothetical tidiness: `add_kv_store` used to
rebuild the document from the store list alone via `build_kv_stores_document`, which silently
dropped every other key — data loss that became real the moment `kubernetesAuth` arrived, and
the erase would have looked like a legitimate PR diff. Tests pin **both directions**: a k8s
create must not erase `kvStores`, and a KV create must not erase `kubernetesAuth`. A third
key goes in by parameterising, never by copy-pasting the family.

`add_kv_store`, `update_kv_store` and `remove_kv_store` take the parsed document and return
a **new** one, never mutating the input. That is what lets the update operation diff old
against new with `yaml_data_equals` and skip a no-op commit. Any helper added later must
keep that contract. `add_kv_store` accepts `None` so "the file does not exist yet" and
"append to an existing file" are one code path. `update_kv_store` and `remove_kv_store`
raise `KVStoreNotFound` when the named store is not in the file, which the operations turn
into a 404. `remove_kv_store` leaves `kvStores: []` behind rather than dropping the key —
`render_values_yaml` emits exactly `kvStores: []\n` and `read_kv_stores` reads it back as
an empty file. `build_kv_stores_document` is a *constructor* for a fresh file, not a
transform; nothing that starts from a parsed document should call it.

`read_kv_stores` tolerates `kvStores:` with nothing under it (parses to `None`) — an empty
file, not a corrupt one. `kv_store_names` skips malformed entries so a hand-edited file
cannot break the duplicate scan. The `kubernetesAuth` wrappers inherit all of this, plus:
`add_kubernetes_auth_role(None, ...)` emits **only** `kubernetesAuth` — a file started by a
role gets no `kvStores: []` sibling it never asked for; `build_kubernetes_auth_role` omits
`cluster`/`ttl` entirely when absent rather than writing null; and `kv_store_referrers`
skips malformed entries and matches whole store names, so `myapp-two` being referenced does
not block deleting `myapp`.

`render_values_yaml` uses a `SafeDumper` subclass that writes multi-line strings as `|`
blocks — the default representer emits one escaped, width-wrapped scalar, which is
unreadable in the pull request diff a human is supposed to review. Only a multi-line
`description` can trigger it today.

## The create chain — read this before editing `operations.py`

**Two shared helpers, nested.** `_open_pull_request` owns branch → commit → PR and both of
their rollbacks (either failure deletes the branch). `_commit_via_pull_request` calls it and
then adds the gates and the merge. **All eight mutating operations across both resource kinds
go through them** — create/update/delete via `_commit_via_pull_request`, the four PR-only
variants stopping at `_open_pull_request`. One implementation each means the rollback shape
cannot drift between resource kinds, let alone between operations.

Both carry `kv_name` **and** `role_name`, defaulted to `""`; exactly one is set, by whichever
kind is being written, and it is threaded into every `VaultOperationError` the chain raises
so a failure names its subject in the right field.

The baseline watermark is taken **before** `_open_pull_request` and deliberately not folded
into it — it has to precede the branch creation, and the PR-only paths have no use for it.

`_prepare_create`, `_prepare_delete`, `_prepare_k8s_auth_create` and `_prepare_k8s_auth_delete`
are the per-operation shared pieces: each blocking path and its PR-only twin call the same one,
which is why they cannot diverge on what is a 409 or a 404.

`_walk_values_files(bitbucket)` is the single "read and parse every yaml under
`VAULT_VALUES_DIR`, skipping unparseable ones with a warning" async generator. Three scans use
it — the store-name scan, the role uniqueness scan and the delete's referential check — and
each takes everything it needs from **one** pass (the k8s create answers both uniqueness and
store-existence from the same walk).

Update reads the file, applies `update_kv_store`, and **short-circuits when nothing changed**
— returning success with `pull_request: null` rather than opening an empty PR. It always
passes a `source_commit_id`; without that optimistic-lock token Bitbucket rejects an edit to
an existing path as an attempted create.

Delete applies `remove_kv_store` and takes the same chain. Two deliberate asymmetries with
its neighbours: **no uniqueness scan** (that is a create-only concern — it walks the values
dir, but for the referential check below, asking the opposite question) and **no
`yaml_data_equals` short circuit** — if `remove_kv_store` did not raise, the document
changed, so a removal is never a no-op. It always passes a `source_commit_id`; the file
exists by definition. It is a **content edit, not a file removal**: the last store out leaves
`kvStores: []` and the file in place, which keeps `GET /{file}` answering 200, lets a later
create append normally, and needs no `delete_file` on the Bitbucket client (there isn't one).

**Referential integrity (`_assert_no_referrers`).** A KV delete walks the whole values dir and
returns **409** if any `kubernetesAuth` entry's `access` still names the store, with a message
listing the roles and their file: `myapp is referenced by kubernetesAuth role(s) myapp-ci
(kv/platform.yaml); delete those first`. Dir-wide, not file-scoped — a role in
`kv/platform.yaml` may reach a store in `kv/payments.yaml`. Rejected alternatives: cascading
into those roles (a multi-resource mutation hidden behind a single-resource URI, blast radius
invisible in the request) and doing nothing (silent orphan). **The reverse direction has no
check**: deleting a *role* never touches the stores, because a store with nothing pointing at
it is perfectly valid.

### The Kubernetes auth chain

`create_kubernetes_auth_operation` differs from a KV create only in step 0, and its PR-only
twin shares `_prepare_k8s_auth_create` the way both KV create paths share `_prepare_create`:

0. **one walk, two questions** — `(cluster, name)` taken anywhere → 409; `name` taken in the
   *target file* whatever the cluster → 409 (`{file}/{role_name}` would be ambiguous); and
   every store named in `access` must already exist somewhere in the dir → 409. Same "check,
   not a lock" semantics as the KV scan.
1-6. identical to the KV create, with `K8S_AUTH_BRANCH_PREFIX` on the branch
   (`vault-k8s-auth/payments-myapp-ci-3f2a1b09`) so a reviewer sees the change kind first.

Update short-circuits on `yaml_data_equals` like the KV one, and **re-runs the store-existence
check whenever `access` changes** — otherwise an edit could introduce the dangling reference a
create refuses. It skips the walk entirely when `access` is untouched. Delete is the plain
chain with no scan at all.

**Ordering consequence worth knowing** (documented, not worked around): a caller who creates a
store via `POST /kv/pull-request` and then immediately creates a role referencing it gets a
409 — the store is on an unmerged branch and every scan reads the base branch. Same
base-branch-only visibility that makes a repeat `/pull-request` open a second PR.

`create_kv_mount_operation` runs, in order:

0. **name scan** — `list_files` on `VAULT_VALUES_DIR`, then `get_file_content` + parse for
   each `.yaml`; the name appearing anywhere is a 409. Store names are global to Vault, so
   this cannot be scoped to the target file. It is a check, not a lock: two creates in flight
   both pass and the second PR conflicts at merge, same as any other GitOps race. A file that
   will not parse is logged and skipped rather than blocking an unrelated create.
1. read the target file — absent (404) means this is its first store, so `source_commit_id`
   stays `None` and Bitbucket treats the write as a create; present means append, and
   `get_last_commit` supplies the optimistic-lock token
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

`create_kv_pull_request_operation` runs steps 0-3, then returns 201 with the PR `OPEN`. It
takes **no Woodpecker client** — the argument would be dead weight. Two consequences, both
deliberate and both pinned by tests:

- **Opening a PR still triggers CI in the forge.** The endpoint does not suppress the
  validation pipeline; it just does not wait for it. The response's pipeline fields are `null`
  because nothing was *observed*, not because nothing ran.
- **A repeat call opens a second PR.** The name scan reads the base branch, and an unmerged
  PR is not there, so it cannot see the first one. Catching it would mean listing open PRs —
  a different and inherently racy check. Reviewers close the loser.

`delete_kv_store_pull_request_operation` is the same shape for a removal, with both
consequences intact: CI still runs, and a repeat call opens a second removal PR because
nothing was merged, so the store is still on the base branch and still deletable. The escape
hatch matters more here than for a create — delete is the most destructive operation, and
this is the variant that hands the decision to a reviewer. The two
`*_kubernetes_auth_pull_request_operation` twins are the same shape again, with the same two
consequences and the same "no Woodpecker client" rule.

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
- Status codes: 201 creates and all four PR-only routes, 200 edits, deletes and reads,
  409 duplicate / dangling `access` reference / still-referenced store, 404 unknown file,
  store or role, 502 pipeline/transport failure, 504 pipeline or upstream timeout, 422 request
  **or path parameter** validation. The rule across the whole table: an endpoint that opens a
  PR and stops answers 201; one that runs the full chain answers 201 for a create and 200 for
  an edit or a delete.
  Failures reuse `VaultOperationResponse` via `VaultOperationError.to_response()`; every
  mutating route on **both** routers funnels through `_execute` in `routes.py`, which is the
  single place that mapping lives.
- **`routes.py` has two funnels, not one.** `_execute` is for mutations; the `GET`s go through
  **`_read`**, which differs in both directions: an upstream **404 stays a 404** (a missing
  file is the caller's mistake, not a bad gateway), and its failure body is a bare
  `{"status", "error"}` dict rather than a `VaultOperationResponse` — the reads return raw
  parsed YAML on success, so there is no response model for a failure to match. Adding a read
  means using `_read`; putting a read through `_execute` turns every unknown file into a 502.
- **One response model for both kinds**, `VaultOperationResponse` (renamed from
  `VaultKVOperationResponse` — wire-compatible; only the OpenAPI schema title changed). It
  carries `kv_name` **and** `role_name`, both `""`-defaulted, so each route fills in the
  coordinate it has. Two models would have doubled the trap below; putting a role name in
  `kv_name` would have been a lie every consumer had to learn.
- **Failures are `return`ed as a `JSONResponse`, not raised.** So the POST decorator's
  `status_code=201` and `response_model=` describe the success path only — FastAPI does not
  validate or document the failure bodies. Adding a field to `VaultOperationResponse` means
  the failure path silently omits it unless `to_response()` is updated too — that is exactly
  what `role_name` would have hit.
- The committed entries are the contract with the deploy pipeline — change `build_kv_store` /
  `build_kubernetes_auth_role` and the pipeline together. Both are the *sole* writers of their
  own key names.
- Tests: 547 of them. `tests/test_helpers.py` (pure, no library import), `tests/clients/` (both
  clients plus `http.py`'s error mapping, against their real REST shapes via respx),
  `tests/v1/vault/` (schemas, the chain + rollbacks with duck-typed fakes from `tests/fakes.py`,
  and routes end-to-end through `TestClient`; `test_kubernetes_auth_*.py` mirror the KV files
  one-for-one). Two conftest details to respect:
  - `tests/conftest.py` sets `os.environ` **before** any `app.*` import (its own `import jwt` /
    `import pytest` sit below the env block behind `# noqa: E402`) because the config singletons
    are built at import time. Adding a required setting means adding it there, above that line.
    It also blanks the optional `AUTH_*` knobs so a developer `.env` cannot change outcomes, and
    zeroes the `CI_*` timeouts to keep the suite fast.
  - `tests/v1/vault/conftest.py`'s `client` fixture monkeypatches `app.main.BitbucketClient` and
    `app.main.WoodpeckerClient` — the classes, not instances. That works only because
    `create_app()` is the sole construction site; keep it that way.

  **Steering the fakes** (`tests/fakes.py`) — this is how a new operation test is written:
  - `FakeBitbucket(existing_files={path: yaml})` is the repo's whole state, keyed on the
    full repo-relative path (`kv/payments.yaml`); `list_files` derives the values-dir listing
    from it, and an absent path raises a 404 `ExternalServiceError`, which is what a "first
    store in a new file" test relies on.
  - `fail_on={"put_file": exc}` injects a failure into one method by name. Rollback tests
    assert against `bitbucket.calls`, an ordered list of every method reached — that list is
    what pins the call *order*, not just the outcome.
  - `FakeBitbucket.get_pull_request` bumps `version` on every call, reproducing the real
    optimistic-lock trap. A test that skips the re-read has to be seen to break.
  - `FakeWoodpecker(results=[...])` is a **queue** popped once per `await_pipeline`, so
    element 0 is the validation gate and element 1 the deploy gate; an `Exception` in the
    list is raised instead of returned, which is how a timeout or a red pipeline is staged.
    Running dry is an `AssertionError`, so an unexpected extra gate fails loudly.
- **Delete is a content edit, not a file removal**, and it ships in two variants
  (`delete_kv_store_operation`, `delete_kv_store_pull_request_operation`) that reuse
  `_commit_via_pull_request` / `_open_pull_request` unchanged. Removing the last store leaves
  `kvStores: []` and the file on the base branch; a repeat delete is a 404, which is how
  `GET`/`PATCH` already answer for a store that is not there. **This depends on the deploy
  pipeline reading an empty list as "prune everything this file declared"** — if it cannot,
  the Bitbucket client needs a `delete_file` it does not have today.
- **The `kubernetes-auth` endpoints that were deleted are not the ones that exist now.** The
  old pair (plus `groups`) edited a `policies` list that a create never writes, so they
  returned 422 unconditionally against every file this service produces. The current ones bind
  by `access: {read: [<store>]}` — a *coordinate*, not a generated policy — which is why they
  could be built rather than reinstated. Do not go back to git history for them, and the rule
  stands: no policy *generation* in this service, ever. If the pipeline ever wants policies in
  the committed file, that is still the pipeline's output, not this service's.
- **`groups` is still gone** and has no replacement.
- Commits are expected to be **Conventional Commits** (`feat:`, `fix:`, `test:`, `feat!:` for a
  breaking change) — follow the existing `git log`. There is no changelog tooling: a `cliff.toml`
  was removed because nothing ran it (no tags, no `CHANGELOG.md`, and its header described GitHub
  Actions + setuptools-scm, neither of which exists here). The version is a literal in
  `pyproject.toml` plus the `APP_VERSION` build arg.
