"""Pure helpers for the Vault GitOps flow.

Everything here is side-effect free: given a request payload it produces the mount path,
the policy names/HCL, the values-file path and the YAML body that gets committed. Keeping
it pure means the naming convention and the rendered artefact are unit-testable without
touching Bitbucket, Woodpecker or Vault.
"""

import copy
from typing import Any, Dict, List, Optional, Tuple

import yaml


def build_mount_path(team: str, environment: str, app_name: str) -> str:
    """The KV mount path in Vault: ``{team}/{environment}/{app}``.

    `TEAM_NAME` is the team's mount prefix, so every mount this API creates is namespaced
    under the owning team and then per environment.
    """
    return f"{team}/{environment}/{app_name}"


def break_mount_path(mount_path: str) -> Tuple[str, str, str]:
    """Inverse of :func:`build_mount_path` -> ``(team, environment, app_name)``."""
    team, environment, app_name = mount_path.split("/", 2)
    return team, environment, app_name


def build_policy_name(team: str, environment: str, app_name: str, capability: str) -> str:
    """Vault policy name, e.g. ``kingmagen-prod-myapp-read``."""
    return f"{team}-{environment}-{app_name}-{capability}"


def build_branch_name(environment: str, app_name: str, suffix: str, prefix: str) -> str:
    """Short-lived branch the values file is committed to before the PR is opened."""
    return f"{prefix}/{environment}-{app_name}-{suffix}"


def values_file_path(values_dir: str, environment: str, app_name: str) -> str:
    """Repo-relative path of the values file, e.g. ``kv/prod/myapp.yaml``."""
    return f"{values_dir.strip('/')}/{environment}/{app_name}.yaml"


def _kv_paths(mount_path: str, kv_version: int) -> Tuple[str, str]:
    """The (data, metadata) policy path globs for a KV mount.

    KV v2 splits the API surface into ``<mount>/data/*`` and ``<mount>/metadata/*``;
    KV v1 has a single flat ``<mount>/*``.
    """
    if kv_version == 2:
        return f"{mount_path}/data/*", f"{mount_path}/metadata/*"
    return f"{mount_path}/*", ""


def _policy_stanza(path: str, capabilities: List[str]) -> str:
    caps = ", ".join(f'"{c}"' for c in capabilities)
    return f'path "{path}" {{\n  capabilities = [{caps}]\n}}\n'


def render_read_policy(mount_path: str, kv_version: int) -> str:
    """HCL granting read-only access to the mount."""
    data_path, metadata_path = _kv_paths(mount_path, kv_version)
    policy = _policy_stanza(data_path, ["read", "list"])
    if metadata_path:
        policy += "\n" + _policy_stanza(metadata_path, ["read", "list"])
    return policy


def render_write_policy(mount_path: str, kv_version: int) -> str:
    """HCL granting full read/write access to the mount (including soft-delete)."""
    data_path, metadata_path = _kv_paths(mount_path, kv_version)
    policy = _policy_stanza(data_path, ["create", "read", "update", "delete", "list"])
    if metadata_path:
        policy += "\n" + _policy_stanza(metadata_path, ["read", "list", "delete"])
    return policy


def build_values_data(
    payload,
    team: str,
    environment: str,
) -> Tuple[str, Dict[str, Any]]:
    """Turn a validated request into ``(mount_path, values_dict)``.

    The dict is the contract with the downstream pipeline — the deploy pipeline reads this
    file and applies the mount and the two policies to Vault. Change it in lockstep with
    the pipeline.
    """
    spec = payload.spec
    mount_path = build_mount_path(team, environment, spec.app_name)
    kv_version = int(spec.kv_version)

    mount: Dict[str, Any] = {
        "path": mount_path,
        "type": "kv",
        "options": {"version": str(kv_version)},
        "description": spec.description or f"KV store for {spec.app_name} ({environment})",
    }

    # KV v2 is the only version with versioned-secret tuning knobs.
    if kv_version == 2:
        config: Dict[str, Any] = {"max_versions": spec.max_versions}
        if spec.delete_version_after:
            config["delete_version_after"] = spec.delete_version_after
        mount["config"] = config

    policies = [
        {
            "name": build_policy_name(team, environment, spec.app_name, "read"),
            "rules": render_read_policy(mount_path, kv_version),
            "entities": list(spec.readers),
        },
        {
            "name": build_policy_name(team, environment, spec.app_name, "write"),
            "rules": render_write_policy(mount_path, kv_version),
            "entities": list(spec.writers),
        },
    ]

    values = {
        "mount": mount,
        "policies": policies,
        "metadata": {
            "app": spec.app_name,
            "team": team,
            "environment": environment,
            "owner": spec.owner,
        },
    }
    return mount_path, values


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
# edits to an existing values file
#
# All of these take the parsed values dict and return a *new* one, leaving the input
# alone so the caller can compare the two with `yaml_data_equals` and skip a no-op
# commit. None of them touch the mount path or the policy names: renaming a Vault mount
# is a data migration, not an edit.
# --------------------------------------------------------------------------- #
class ValuesEditError(ValueError):
    """The requested edit does not apply to this values file."""


def update_mount_metadata(
    values: Dict[str, Any],
    description: Optional[str] = None,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    """Replace the mount description and/or the recorded owner."""
    updated = copy.deepcopy(values)
    if description is not None:
        updated.setdefault("mount", {})["description"] = description
    if owner is not None:
        updated.setdefault("metadata", {})["owner"] = owner
    return updated


def build_kubernetes_role_name(team: str, environment: str, app_name: str) -> str:
    """Default Vault Kubernetes auth role name, e.g. ``kingmagen-prod-myapp``."""
    return f"{team}-{environment}-{app_name}"


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
