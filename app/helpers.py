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

# The document's only top-level key: a *list* of named stores.
KV_STORES_KEY = "kvStores"

# Kubernetes service accounts are **not** a second top-level key, and not a sibling of
# `roles` either. They are a list under `roles`, level with `read`/`write` — two levels
# inside the store. See the k8sServiceAccounts section at the bottom of this module.
ROLES_KEY = "roles"
K8S_SERVICE_ACCOUNTS_KEY = "k8sServiceAccounts"


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

    `roles` is replaced wholesale rather than merged: a caller that wants to drop a
    principal needs to be able to express it, and a merge would make removal impossible.

    **Except for `k8sServiceAccounts`, which is carried across.** The bindings live *under*
    `roles`, level with `read`/`write`, so a wholesale replacement would delete every one of
    them — and the update request body cannot express bindings at all, so there would be no
    way to put them back in the same call. Nothing about editing who may read a secret says
    anything about which workloads are bound to it; those are separate endpoints, and this
    keeps them independent. The bindings are re-appended last, so the committed order is
    unchanged.
    """
    if roles is not None:
        existing = k8s_service_accounts(find_kv_store(values, kv_name))
        if existing:
            roles = {**roles, K8S_SERVICE_ACCOUNTS_KEY: existing}

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
# the `k8sServiceAccounts` list — nested **inside `roles`**, level with read/write
#
# The pipeline's format binds a workload to a secret by listing the workload inside the
# store it reaches, under the same `roles` mapping the principals live in:
#
#     kvStores:
#       - name: athena-passwords
#         description: Passwords for athena
#         roles:
#           write:
#             - CN=<CN>,OU=<OU>,DC=<DC>
#           k8sServiceAccounts:          # <- level with `write`, not with `roles`
#             - serviceAccount: "vault"
#               namespace: "<NAMESPACE>"
#               cluster: dev
#
# That depth has one sharp consequence: `roles` is no longer a homogeneous mapping of
# capability -> principals, and replacing it wholesale (which is what a PATCH does) would
# take every binding with it. `update_kv_store` therefore carries the existing bindings
# across a `roles` replacement — see the note there. The request body cannot express
# bindings at all, so a PATCH that silently dropped them would be pure data loss, and the
# erase would have looked like a legitimate diff in the pull request.
#
# That direction is the whole design. Because the binding lives *in* the store, there is no
# reference to dangle: deleting a store takes its bindings with it, so there is no
# referential check, no cross-file scan and no orphan to refuse. An entry is three scalars
# and carries no capability of its own — what a service account may *do* with the store is
# the deploy pipeline's business, exactly as with `roles`.
#
# An entry has no name, so `(serviceAccount, namespace, cluster)` is its identity. That is
# what `k8s_service_account_identity` exists for: nothing outside this module builds that
# tuple by indexing the document.
# --------------------------------------------------------------------------- #
_K8S_SA_ENTRY_KEYS = {
    "service_account": "serviceAccount",
    "namespace": "namespace",
    "cluster": "cluster",
}

# The identity tuple, in the order `_K8S_SA_ENTRY_KEYS` declares.
K8sServiceAccountIdentity = Tuple[str, str, str]


class K8sServiceAccountNotFound(LookupError):
    """No entry with that `(serviceAccount, namespace, cluster)` in this store."""


def build_k8s_service_account(
    service_account: str, namespace: str, cluster: str
) -> Dict[str, str]:
    """One entry in a store's ``k8sServiceAccounts`` list.

    All three are required and singular — the format repeats the whole triple per binding
    rather than crossing lists of accounts with lists of namespaces, so two namespaces means
    two entries. This is the sole writer of these key names; change it and the pipeline
    together.
    """
    return {
        _K8S_SA_ENTRY_KEYS["service_account"]: service_account,
        _K8S_SA_ENTRY_KEYS["namespace"]: namespace,
        _K8S_SA_ENTRY_KEYS["cluster"]: cluster,
    }


def k8s_service_accounts(store: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The binding list out of one **store entry**, tolerating an absent or empty key.

    Note the argument: a store, not a document — and note that it reaches *through* `roles`,
    which is where the list lives. A store that binds nothing simply has no
    `k8sServiceAccounts` key under `roles`, which is what `build_kv_store` produces. A store
    whose `roles` is missing or malformed reads as no bindings rather than raising.
    """
    if not store:
        return []
    roles = store.get(ROLES_KEY)
    if not isinstance(roles, dict):
        return []
    accounts = roles.get(K8S_SERVICE_ACCOUNTS_KEY)
    return list(accounts) if accounts else []


def k8s_service_account_identity(entry: Dict[str, Any]) -> K8sServiceAccountIdentity:
    """`(serviceAccount, namespace, cluster)` for one entry, missing keys as `""`.

    The only place the identity is assembled, so "which fields identify a binding" stays a
    format decision changeable in one place.
    """
    return (
        str(entry.get(_K8S_SA_ENTRY_KEYS["service_account"]) or ""),
        str(entry.get(_K8S_SA_ENTRY_KEYS["namespace"]) or ""),
        str(entry.get(_K8S_SA_ENTRY_KEYS["cluster"]) or ""),
    )


def k8s_service_account_identities(
    store: Optional[Dict[str, Any]],
) -> List[K8sServiceAccountIdentity]:
    """Every binding's identity in one store. Malformed entries are skipped."""
    return [
        k8s_service_account_identity(entry)
        for entry in k8s_service_accounts(store)
        if isinstance(entry, dict)
    ]


def find_k8s_service_account(
    store: Optional[Dict[str, Any]], identity: K8sServiceAccountIdentity
) -> Optional[Dict[str, Any]]:
    """The binding with that identity, or None."""
    for entry in k8s_service_accounts(store):
        if isinstance(entry, dict) and k8s_service_account_identity(entry) == identity:
            return entry
    return None


def _mutate_store(
    values: Dict[str, Any], kv_name: str, mutate: Any
) -> Dict[str, Any]:
    """Apply `mutate` to one store inside a deep copy of the document, and return it.

    The nested counterpart of `_update_entry`: the same "copy the whole document, touch one
    entry, never mutate the input" contract, so a caller can still diff old against new with
    `yaml_data_equals`.
    """
    document = copy.deepcopy(values)
    entries = _entries(document, KV_STORES_KEY)

    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == kv_name:
            mutate(entry)
            document[KV_STORES_KEY] = entries
            return document

    raise KVStoreNotFound(kv_name)


def add_k8s_service_account(
    values: Dict[str, Any], kv_name: str, account: Dict[str, str]
) -> Dict[str, Any]:
    """Append a binding to one store, returning a **new** document.

    Raises `KVStoreNotFound` when the store is not in the file — unlike `add_kv_store` this
    takes no `None`, because a binding cannot create the store it lives in.
    """

    def mutate(store: Dict[str, Any]) -> None:
        roles = store.get(ROLES_KEY)
        if not isinstance(roles, dict):
            roles = {}
            store[ROLES_KEY] = roles
        # Appended after the capability keys, so the entry reads in the pull request diff
        # the way the format spec writes it: principals first, then the workloads.
        roles[K8S_SERVICE_ACCOUNTS_KEY] = k8s_service_accounts(store) + [
            copy.deepcopy(account)
        ]

    return _mutate_store(values, kv_name, mutate)


def remove_k8s_service_account(
    values: Dict[str, Any], kv_name: str, identity: K8sServiceAccountIdentity
) -> Dict[str, Any]:
    """Drop one binding from one store, returning a **new** document.

    Removing the last one **drops the key from `roles`** rather than leaving
    `k8sServiceAccounts: []`. That is deliberately the opposite of `remove_kv_store`, and for
    the opposite reason: `kvStores: []` is a meaningful statement by a file ("declare
    nothing"), whereas a store with no bindings is just a store — exactly what
    `build_kv_store` writes — so keeping an empty list would leave a diff a fresh create
    would never produce. The `roles` mapping itself always stays, empty or not: it is a
    required field of a store.
    """

    def mutate(store: Dict[str, Any]) -> None:
        existing = k8s_service_accounts(store)
        remaining = [
            entry
            for entry in existing
            if not (
                isinstance(entry, dict)
                and k8s_service_account_identity(entry) == identity
            )
        ]
        if len(remaining) == len(existing):
            raise K8sServiceAccountNotFound(identity)

        roles = store[ROLES_KEY]
        if remaining:
            roles[K8S_SERVICE_ACCOUNTS_KEY] = remaining
        else:
            roles.pop(K8S_SERVICE_ACCOUNTS_KEY, None)

    return _mutate_store(values, kv_name, mutate)
