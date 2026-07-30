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


def slugify_mount_path(mount_path: str) -> str:
    """Flatten a mount path into a single dash-separated token.

    Policy names and Kubernetes role names cannot contain slashes, so a mount path of
    ``payments/vault-secrets`` becomes ``payments-vault-secrets``.
    """
    return mount_path.strip("/").replace("/", "-")


def build_branch_name(kv_name: str, suffix: str, prefix: str) -> str:
    """Short-lived branch the file is committed to before the PR is opened.

    The name is flattened: a slash in `kv_name` would nest the ref, and git cannot hold
    both ``vault-kv/payments`` and ``vault-kv/payments/secrets-abc``.
    """
    return f"{prefix}/{slugify_mount_path(kv_name)}-{suffix}"


def values_file_path(values_dir: str, kv_name: str) -> str:
    """Repo-relative path of the committed file, e.g. ``kv/myapp.yaml``."""
    return f"{values_dir.strip('/')}/{kv_name.strip('/')}.yaml"


def build_kv_values(kv_name: str, kv_description: str) -> Dict[str, Any]:
    """The committed document: the name and what it is for.

    This is the contract with the deploy pipeline. Everything the KV means in Vault - the
    mount, the engine version, the policies - is the pipeline's business, not this
    service's, so none of it is written here. Change this dict and the pipeline together.
    """
    return {"kvname": kv_name, "description": kv_description}


class _BlockStyleDumper(yaml.SafeDumper):
    """SafeDumper that writes multi-line strings as block scalars."""


def _represent_str(dumper: yaml.Dumper, data: str) -> Any:
    """Render multi-line strings as ``|`` blocks, everything else normally.

    The policy HCL is multi-line. Left to the default representer it becomes a single
    escaped, width-wrapped double-quoted scalar (``"path \\"…\\" {\\n  capabilities…"``),
    which parses fine but is unreadable in a pull request diff — and a human reviewing
    that diff is the whole point of the GitOps flow.
    """
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BlockStyleDumper.add_representer(str, _represent_str)


def render_values_yaml(values: Dict[str, Any]) -> str:
    """Serialise the values dict for committing. Key order is preserved for readable diffs."""
    # width: PyYAML wraps at 80 columns by default, which splits long policy paths
    # mid-token and undoes the readability the block style buys.
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
# All of these take the parsed document and return a *new* one, leaving the input alone so
# the caller can compare the two with `yaml_data_equals` and skip a no-op commit. None of
# them touch `kvname`: renaming means migrating the secrets in Vault, not editing a field.
# --------------------------------------------------------------------------- #
class ValuesEditError(ValueError):
    """The requested edit does not apply to this file."""


def update_kv_metadata(
    values: Dict[str, Any],
    description: Optional[str] = None,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    """Replace the description and/or the recorded owner."""
    updated = copy.deepcopy(values)
    if description is not None:
        updated["description"] = description
    if owner is not None:
        updated["owner"] = owner
    return updated


def build_kubernetes_role_name(kv_name: str) -> str:
    """Default Vault Kubernetes auth role name, e.g. ``payments-vault-secrets``."""
    return slugify_mount_path(kv_name)


def find_policy_name(values: Dict[str, Any], capability: str) -> str:
    """The read or write policy name recorded in the file.

    Read out of the file rather than rebuilt from the coordinates, so an edit can never
    invent a policy name that does not exist in the committed document.
    """
    for policy in values.get("policies") or []:
        name = policy.get("name", "")
        if name.endswith(f"-{capability}"):
            return name
    raise ValuesEditError(f"no '{capability}' policy found in the values file")


def add_kubernetes_auth(
    values: Dict[str, Any],
    role: str,
    service_accounts: List[str],
    namespaces: List[str],
    capability: str,
    ttl: Optional[str] = None,
) -> Dict[str, Any]:
    """Add (or replace) a Kubernetes auth role bound to one of the mount's policies.

    Upsert rather than append-only: re-adding an identical role leaves the document
    untouched, so the caller's no-op check turns a repeat request into "nothing to do"
    instead of a duplicate entry.
    """
    updated = copy.deepcopy(values)
    role_entry: Dict[str, Any] = {
        "role": role,
        "service_accounts": list(service_accounts),
        "namespaces": list(namespaces),
        "policies": [find_policy_name(values, capability)],
    }
    if ttl:
        role_entry["ttl"] = ttl

    roles = updated.setdefault("kubernetes_auth", [])
    for index, existing in enumerate(roles):
        if existing.get("role") == role:
            roles[index] = role_entry
            break
    else:
        roles.append(role_entry)
    return updated


def add_group_binding(
    values: Dict[str, Any], group: str, capability: str
) -> Dict[str, Any]:
    """Grant an AD group the mount's read or write policy.

    Adding a group that is already bound leaves the document untouched.
    """
    updated = copy.deepcopy(values)
    policy_name = find_policy_name(values, capability)
    for policy in updated["policies"]:
        if policy["name"] != policy_name:
            continue
        entities = policy.setdefault("entities", [])
        if group not in entities:
            entities.append(group)
        return updated
    raise ValuesEditError(f"policy {policy_name} disappeared while editing")
