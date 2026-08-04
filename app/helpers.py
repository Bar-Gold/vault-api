"""Pure helpers for the GitOps flow.

Everything here is side-effect free: given a request it produces the committed file's path,
body and any edit applied to it. Keeping it pure means the artefact this service writes is
unit-testable without touching Bitbucket or Woodpecker.

Note what is *not* here: nothing models a Vault mount, an engine version or a policy. This
service writes a file and watches the pipelines; what the file means is the deploy
pipeline's business.
"""

import copy
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Top-level keys of the committed document. A file holds a *list* of named entries under
# each of them, and the two are edited independently — one file, one optimistic-lock token,
# one pull request per app.
KV_STORES_KEY = "kvStores"
K8S_AUTH_KEY = "kubernetesAuth"


def slugify_mount_path(mount_path: str) -> str:
    """Flatten a path into a single dash-separated token.

    Branch names cannot contain a slash without nesting the ref. Names and files are
    single-segment now, so this is belt-and-braces rather than load-bearing.
    """
    return mount_path.strip("/").replace("/", "-")


def build_branch_name(file: str, kv_name: str, suffix: str, prefix: str) -> str:
    """Short-lived branch the change is committed to before the PR is opened.

    Carries both coordinates so a reviewer can tell from the branch name alone which file
    and which store a pull request touches.
    """
    return f"{prefix}/{slugify_mount_path(file)}-{slugify_mount_path(kv_name)}-{suffix}"


def values_file_path(values_dir: str, file: str) -> str:
    """Repo-relative path of the committed file, e.g. ``kv/payments.yaml``.

    Keyed on the *file*, not the store name: one file holds many stores.
    """
    return f"{values_dir.strip('/')}/{file.strip('/')}.yaml"


def build_kv_store(
    kv_name: str, kv_description: str, roles: Dict[str, List[str]]
) -> Dict[str, Any]:
    """One entry in the ``kvStores`` list.

    This is the contract with the deploy pipeline. What the KV means in Vault — the mount,
    the engine version, the policies — is the pipeline's business, not this service's, so
    none of it is written here. Change this dict and the pipeline together.
    """
    return {
        "name": kv_name,
        "description": kv_description,
        "roles": {role: list(hosts) for role, hosts in roles.items()},
    }


def build_kv_stores_document(stores: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A whole values file from scratch: the ``kvStores`` list and nothing else.

    A constructor, not a transform — the transforms below start from a parsed document and
    keep whatever else it holds.
    """
    return {KV_STORES_KEY: list(stores)}


# --------------------------------------------------------------------------- #
# entries under one top-level key
#
# A values file is a mapping whose top-level keys each hold a list of named entries;
# `kvStores` is the only one today. Every transform here deep-copies the *whole* document
# and replaces only its own key, so a sibling key it knows nothing about survives. An
# earlier version rebuilt the document from the store list alone, which silently dropped
# every other key — and the erase would have looked like a legitimate diff in the pull
# request a human is supposed to review.
#
# Returning a new document rather than mutating the input is also what lets the update
# operation diff old against new with `yaml_data_equals` and skip a no-op commit. Anything
# added here has to keep both properties.
# --------------------------------------------------------------------------- #
class KVStoreNotFound(LookupError):
    """The named entry is not in this file."""


def _entries(values: Optional[Dict[str, Any]], key: str) -> List[Any]:
    """The list under one top-level key, tolerating an empty or absent key.

    A file that exists but has ``kvStores:`` with nothing under it parses to ``None``, which
    is a legitimate empty file rather than a corrupt one.
    """
    if not values:
        return []
    entries = values.get(key)
    return list(entries) if entries else []


def _find_entry(
    values: Optional[Dict[str, Any]], key: str, name: str
) -> Optional[Dict[str, Any]]:
    for entry in _entries(values, key):
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _entry_names(values: Optional[Dict[str, Any]], key: str) -> List[str]:
    """Malformed entries are skipped so a hand-edited file cannot break a scan."""
    return [
        entry["name"]
        for entry in _entries(values, key)
        if isinstance(entry, dict) and entry.get("name")
    ]


def _add_entry(
    values: Optional[Dict[str, Any]], key: str, entry: Dict[str, Any]
) -> Dict[str, Any]:
    """Append an entry, returning a **new** document.

    Accepts None so the caller can treat "the file does not exist yet" and "the file exists
    and we are appending" as the same code path.
    """
    document = copy.deepcopy(values) if values else {}
    document[key] = _entries(document, key) + [copy.deepcopy(entry)]
    return document


def _update_entry(
    values: Dict[str, Any], key: str, name: str, **fields: Any
) -> Dict[str, Any]:
    """Replace the given fields on one entry, leaving its siblings alone.

    A field passed as None is left untouched — that is how a partial edit says "not this
    one". Values are copied in, so a caller that later mutates what it passed cannot reach
    into the returned document.
    """
    document = copy.deepcopy(values)
    entries = _entries(document, key)

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("name") != name:
            continue
        for field, value in fields.items():
            if value is not None:
                entry[field] = copy.deepcopy(value)
        document[key] = entries
        return document

    raise KVStoreNotFound(name)


def _remove_entry(values: Dict[str, Any], key: str, name: str) -> Dict[str, Any]:
    """Drop one entry, returning a **new** document.

    The key stays, holding a possibly empty list: an empty list is a file that declares
    nothing, which the pipeline can act on, whereas a missing key is ambiguous.
    """
    document = copy.deepcopy(values)
    entries = _entries(document, key)
    remaining = [
        entry
        for entry in entries
        if not (isinstance(entry, dict) and entry.get("name") == name)
    ]

    if len(remaining) == len(entries):
        raise KVStoreNotFound(name)

    document[key] = remaining
    return document


# --------------------------------------------------------------------------- #
# the `kvStores` key — thin wrappers so callers name the store, not the key
# --------------------------------------------------------------------------- #
def read_kv_stores(values: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The store list out of a parsed document, tolerating an empty or absent key."""
    return _entries(values, KV_STORES_KEY)


def find_kv_store(values: Dict[str, Any], kv_name: str) -> Optional[Dict[str, Any]]:
    """The entry with this name, or None. Names are unique within a file."""
    return _find_entry(values, KV_STORES_KEY, kv_name)


def kv_store_names(values: Optional[Dict[str, Any]]) -> List[str]:
    """Every store name in a document, for the cross-file duplicate scan."""
    return _entry_names(values, KV_STORES_KEY)


def add_kv_store(
    values: Optional[Dict[str, Any]], store: Dict[str, Any]
) -> Dict[str, Any]:
    """Append a store to a document, returning a **new** one."""
    return _add_entry(values, KV_STORES_KEY, store)


class _BlockStyleDumper(yaml.SafeDumper):
    """SafeDumper that writes multi-line strings as block scalars."""


def _represent_str(dumper: yaml.Dumper, data: str) -> Any:
    """Render multi-line strings as ``|`` blocks, everything else normally.

    A description containing a newline would otherwise become a single escaped,
    width-wrapped double-quoted scalar (``"line one\\nline two"``), which parses fine but
    is unreadable in a pull request diff — and a human reviewing that diff is the whole
    point of the GitOps flow.
    """
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BlockStyleDumper.add_representer(str, _represent_str)


def render_values_yaml(values: Dict[str, Any]) -> str:
    """Serialise the values dict for committing. Key order is preserved for readable diffs."""
    # width: PyYAML wraps at 80 columns by default, which splits long values mid-token and
    # undoes the readability the block style buys.
    return yaml.dump(
        values,
        Dumper=_BlockStyleDumper,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )


def _normalize(data):
    if isinstance(data, dict):
        return {k: _normalize(v) for k, v in sorted(data.items())}
    if isinstance(data, list):
        return sorted((_normalize(i) for i in data), key=lambda x: str(x))
    return data


def yaml_data_equals(yaml_data_1, yaml_data_2) -> bool:
    """Order-insensitive YAML comparison, used to skip no-op commits."""
    if isinstance(yaml_data_1, str):
        yaml_data_1 = yaml.safe_load(yaml_data_1)
    if isinstance(yaml_data_2, str):
        yaml_data_2 = yaml.safe_load(yaml_data_2)
    return _normalize(yaml_data_1) == _normalize(yaml_data_2)


# --------------------------------------------------------------------------- #
# edits to an existing file
#
# Neither of these touches `name`: renaming means migrating the secrets in Vault, not
# editing a field, so a rename is a delete plus a create.
# --------------------------------------------------------------------------- #
def update_kv_store(
    values: Dict[str, Any],
    kv_name: str,
    description: Optional[str] = None,
    roles: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Replace the description and/or roles of one store, leaving its siblings alone.

    `roles` is replaced wholesale rather than merged: a caller that wants to drop a host
    needs to be able to express it, and a merge would make removal impossible.
    """
    return _update_entry(
        values, KV_STORES_KEY, kv_name, description=description, roles=roles
    )


def remove_kv_store(values: Dict[str, Any], kv_name: str) -> Dict[str, Any]:
    """Drop one store from a document, leaving its siblings alone.

    Removing the last store empties the list rather than dropping the key or the file:
    `kvStores: []` still parses, still reads back as an empty file, and a later create
    appends to it normally.
    """
    return _remove_entry(values, KV_STORES_KEY, kv_name)


# --------------------------------------------------------------------------- #
# the `kubernetesAuth` key — the second kind of entry a values file can hold
#
# The shape below is a **proposal**: it is a guess at what the deploy pipeline wants, so
# every guess sits behind exactly one name. `K8S_AUTH_KEY` is the top-level key,
# `_K8S_AUTH_ENTRY_KEYS` is the request-field -> document-key table (including snake_case ->
# camelCase), and `build_kubernetes_auth_role` is the only function that writes an entry.
# Nothing outside this module knows a single key name of the format, so correcting it is a
# one-line edit here — the same property `ALLOWED_ROLE_KEYS` has in `schemas.py`.
#
# What is deliberately *not* here: a mount path, an engine version, a policy name or any
# HCL. An entry names KV stores and a capability; deriving a policy from that pair is the
# pipeline's business, and generating one here is the boundary an earlier version crossed.
# --------------------------------------------------------------------------- #
_K8S_AUTH_ENTRY_KEYS = {
    "role_description": "description",
    "cluster": "cluster",
    "service_accounts": "serviceAccounts",
    "namespaces": "namespaces",
    "access": "access",
    "ttl": "ttl",
}


def _kubernetes_auth_fields(**fields: Any) -> Dict[str, Any]:
    """Translate request fields to document keys, dropping the ones not supplied.

    Insertion order is the caller's, and `render_values_yaml` preserves it, so the caller
    decides how the entry reads in the pull request diff.
    """
    return {
        _K8S_AUTH_ENTRY_KEYS[field]: copy.deepcopy(value)
        for field, value in fields.items()
        if value is not None
    }


def build_kubernetes_auth_role(
    role_name: str,
    role_description: str,
    service_accounts: List[str],
    namespaces: List[str],
    access: Dict[str, List[str]],
    cluster: Optional[str] = None,
    ttl: Optional[str] = None,
) -> Dict[str, Any]:
    """One entry in the ``kubernetesAuth`` list.

    `cluster` and `ttl` are omitted entirely when absent rather than written as null — an
    estate with a single Kubernetes auth mount has no cluster to name, and a key holding
    null is harder for a pipeline to treat as "not specified" than a missing one.
    """
    return {
        "name": role_name,
        **_kubernetes_auth_fields(
            role_description=role_description,
            cluster=cluster,
            service_accounts=service_accounts,
            namespaces=namespaces,
            access=access,
            ttl=ttl,
        ),
    }


def kubernetes_auth_roles(values: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The role list out of a parsed document, tolerating an empty or absent key."""
    return _entries(values, K8S_AUTH_KEY)


def find_kubernetes_auth_role(
    values: Dict[str, Any], role_name: str
) -> Optional[Dict[str, Any]]:
    """The role with this name, or None. Names are unique within a file."""
    return _find_entry(values, K8S_AUTH_KEY, role_name)


def kubernetes_auth_role_names(values: Optional[Dict[str, Any]]) -> List[str]:
    """Every role name in a document, for the within-file uniqueness check."""
    return _entry_names(values, K8S_AUTH_KEY)


def kubernetes_auth_role_identities(
    values: Optional[Dict[str, Any]],
) -> List[Tuple[str, str]]:
    """`(cluster, name)` for every role, with `""` standing in for an absent cluster.

    The cross-file uniqueness key. It lives here rather than in the operation so that
    "which fields identify a role" stays a format decision, changeable in one place.
    """
    return [
        (str(entry.get(_K8S_AUTH_ENTRY_KEYS["cluster"]) or ""), entry["name"])
        for entry in _entries(values, K8S_AUTH_KEY)
        if isinstance(entry, dict) and entry.get("name")
    ]


def kubernetes_auth_role_stores(entry: Dict[str, Any]) -> List[str]:
    """Every KV store name one role's `access` mentions, whatever the capability."""
    access = entry.get(_K8S_AUTH_ENTRY_KEYS["access"])
    if not isinstance(access, dict):
        return []
    return [
        store
        for stores in access.values()
        if isinstance(stores, list)
        for store in stores
        if isinstance(store, str)
    ]


def kv_store_referrers(values: Optional[Dict[str, Any]], kv_name: str) -> List[str]:
    """Roles in this document whose `access` still names the given store.

    The only place the rule lives, so deleting a store can refuse with a 409 that names the
    blockers rather than silently orphaning a binding. Malformed entries are skipped for
    the same reason the name scan skips them: a hand-edited file must not wedge an
    unrelated delete.
    """
    return [
        entry["name"]
        for entry in _entries(values, K8S_AUTH_KEY)
        if isinstance(entry, dict)
        and entry.get("name")
        and kv_name in kubernetes_auth_role_stores(entry)
    ]


def add_kubernetes_auth_role(
    values: Optional[Dict[str, Any]], role: Dict[str, Any]
) -> Dict[str, Any]:
    """Append a role to a document, returning a **new** one.

    Accepts None so the first role in a file creates it. A file started this way carries
    only `kubernetesAuth` — no empty `kvStores: []` sibling it never asked for.
    """
    return _add_entry(values, K8S_AUTH_KEY, role)


def update_kubernetes_auth_role(
    values: Dict[str, Any],
    role_name: str,
    role_description: Optional[str] = None,
    service_accounts: Optional[List[str]] = None,
    namespaces: Optional[List[str]] = None,
    access: Optional[Dict[str, List[str]]] = None,
    ttl: Optional[str] = None,
) -> Dict[str, Any]:
    """Replace the given fields on one role, leaving its siblings alone.

    Neither `name` nor `cluster` is editable: in Vault both are part of the role's identity
    — a rename or a cluster move is a delete plus a create, not a field edit. The three
    list/mapping fields are replaced wholesale rather than merged, for the same reason
    `roles` is on a KV store: a merge makes removal impossible.
    """
    return _update_entry(
        values,
        K8S_AUTH_KEY,
        role_name,
        **_kubernetes_auth_fields(
            role_description=role_description,
            service_accounts=service_accounts,
            namespaces=namespaces,
            access=access,
            ttl=ttl,
        ),
    )


def remove_kubernetes_auth_role(values: Dict[str, Any], role_name: str) -> Dict[str, Any]:
    """Drop one role from a document, leaving its siblings alone."""
    return _remove_entry(values, K8S_AUTH_KEY, role_name)
