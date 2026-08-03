"""Pure helpers for the GitOps flow.

Everything here is side-effect free: given a request it produces the committed file's path,
body and any edit applied to it. Keeping it pure means the artefact this service writes is
unit-testable without touching Bitbucket or Woodpecker.

Note what is *not* here: nothing models a Vault mount, an engine version or a policy. This
service writes a file and watches the pipelines; what the file means is the deploy
pipeline's business.
"""

import copy
from typing import Any, Dict, List, Optional

import yaml

# Top-level key of the committed document. A file holds a *list* of KV stores under it.
KV_STORES_KEY = "kvStores"


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
    """A whole values file: the ``kvStores`` list and nothing else."""
    return {KV_STORES_KEY: list(stores)}


def read_kv_stores(values: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The store list out of a parsed document, tolerating an empty or absent key.

    A file that exists but has ``kvStores:`` with nothing under it parses to ``None``, which
    is a legitimate empty file rather than a corrupt one.
    """
    if not values:
        return []
    stores = values.get(KV_STORES_KEY)
    return list(stores) if stores else []


def find_kv_store(values: Dict[str, Any], kv_name: str) -> Optional[Dict[str, Any]]:
    """The entry with this name, or None. Names are unique within a file."""
    for store in read_kv_stores(values):
        if isinstance(store, dict) and store.get("name") == kv_name:
            return store
    return None


def kv_store_names(values: Optional[Dict[str, Any]]) -> List[str]:
    """Every store name in a document, for the cross-file duplicate scan."""
    return [
        store["name"]
        for store in read_kv_stores(values)
        if isinstance(store, dict) and store.get("name")
    ]


def add_kv_store(
    values: Optional[Dict[str, Any]], store: Dict[str, Any]
) -> Dict[str, Any]:
    """Append a store to a document, returning a **new** one.

    Accepts None so the caller can treat "the file does not exist yet" and "the file exists
    and we are appending" as the same code path.
    """
    stores = [copy.deepcopy(existing) for existing in read_kv_stores(values)]
    stores.append(copy.deepcopy(store))
    return build_kv_stores_document(stores)


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
# Takes the parsed document and returns a *new* one, leaving the input alone so the caller
# can compare the two with `yaml_data_equals` and skip a no-op commit. It never touches
# `name`: renaming means migrating the secrets in Vault, not editing a field.
# --------------------------------------------------------------------------- #
class KVStoreNotFound(LookupError):
    """The named store is not in this file."""


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
    updated = copy.deepcopy(values)
    stores = updated.get(KV_STORES_KEY) or []

    for store in stores:
        if not isinstance(store, dict) or store.get("name") != kv_name:
            continue
        if description is not None:
            store["description"] = description
        if roles is not None:
            store["roles"] = {role: list(hosts) for role, hosts in roles.items()}
        updated[KV_STORES_KEY] = stores
        return updated

    raise KVStoreNotFound(kv_name)
