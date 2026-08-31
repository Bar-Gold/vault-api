"""Pure helpers for the GitOps flow.

Everything here is side-effect free: given a request it produces the committed file's path,
body and any edit applied to it. Keeping it pure means the artefact this service writes is
unit-testable without touching Bitbucket.

Note what is *not* here: nothing models a Vault mount, an engine version or a policy. This
service writes files and watches the pipelines; what the files mean is the deploy
pipeline's business.

## The document

Every file this service writes has the **same** single top-level key, `access`, holding a
list of entries. An entry is one grant: who, over which path, with which capabilities.
`authType` discriminates the two shapes:

    access:
      - path: "*"
        authType: oidc
        dn: "CN=team,OU=Groups,DC=corp,DC=example,DC=com"
        capabilities:
          - read
          - write
      - path: "services/+/tyk/*"
        authType: k8s
        name: argocd
        namespace: perimeter
        cluster: dev
        capabilities:
          - read

**The model is principal → capabilities**, which is the inverse of the `roles` mapping this
service used to write (capability → principals). One entry carries one principal's grant on
one path, so the same DN on two paths is two entries and that is the intended shape.

## Where it goes

Two layouts, one document model:

- **A KV store** is a *directory*, `KVs/<team>/<kv>/`, holding up to two files split by
  `authType` — `oidc-access.yaml` and `k8s-access.yaml`. The split is the format's, not
  ours: each file's `access` list holds only its own kind. A store with no Kubernetes
  grants has no `k8s-access.yaml` at all rather than an empty one.
- **The Tashtiot per-team file** is flat: `KVs/Tashtiot/<team>.yaml`, one file whose
  `access` list carries **both** kinds mixed. It grants a team access into the shared
  services store, so it is per-team rather than per-KV and has no directory of its own.

`partition_access` is the single place that knows entries are split by kind, and
`build_access_document` the single place that knows the top-level key.
"""

import copy
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

# The document's only top-level key, in both layouts.
ACCESS_KEY = "access"

# The discriminator, and its two values. These are the pipeline's vocabulary: change them
# here and in the pipeline together.
AUTH_TYPE_KEY = "authType"
OIDC_AUTH_TYPE = "oidc"
K8S_AUTH_TYPE = "k8s"

CAPABILITIES_KEY = "capabilities"

# The OIDC entry's principal. Named because the dumper quotes it specially, not only
# because the entry builder writes it.
DN_KEY = "dn"

# The two files a KV store directory holds. A store with no grants of one kind simply has
# no file of that kind.
OIDC_ACCESS_FILE = "oidc-access.yaml"
K8S_ACCESS_FILE = "k8s-access.yaml"

# Field order within an entry, which is the committed text's order — `sort_keys=False` in
# `render_values_yaml` is what preserves it. Kept as tuples so the order is stated once.
_OIDC_ENTRY_KEYS = ("path", AUTH_TYPE_KEY, DN_KEY, CAPABILITIES_KEY)
_K8S_ENTRY_KEYS = ("path", AUTH_TYPE_KEY, "name", "namespace", "cluster", CAPABILITIES_KEY)


class AccessEntryNotFound(LookupError):
    """The identified entry is not in this document."""


class DuplicateAccessEntry(LookupError):
    """An entry with this identity is already in this document."""


# --------------------------------------------------------------------------- #
# naming and paths
# --------------------------------------------------------------------------- #
def slugify_mount_path(mount_path: str) -> str:
    """Flatten a path into a single dash-separated token.

    Branch names cannot contain a slash without nesting the ref, and a store's coordinates
    are now two segments (team and name), so this is load-bearing again.
    """
    return mount_path.strip("/").replace("/", "-")


def build_branch_name(team: str, kv_name: str, suffix: str, prefix: str) -> str:
    """Short-lived branch the change is committed to before the PR is opened.

    Carries both coordinates so a reviewer can tell from the branch name alone which team
    and which store a pull request touches — unless they are the same, which would render
    `vault-kv/cloudops-cloudops-abc123`.
    """
    store = slugify_mount_path(kv_name)
    owner = slugify_mount_path(team)
    coordinates = store if owner == store else f"{owner}-{store}"
    return f"{prefix}/{coordinates}-{suffix}"


def kv_store_dir(values_dir: str, team: str, kv_name: str) -> str:
    """Repo-relative directory of one store, e.g. ``KVs/cloudops/my-passwords``.

    A store is a *directory* now, not a file: the two access files live inside it, and a
    delete removes the whole thing.
    """
    return f"{values_dir.strip('/')}/{team.strip('/')}/{kv_name.strip('/')}"


def oidc_access_path(values_dir: str, team: str, kv_name: str) -> str:
    return f"{kv_store_dir(values_dir, team, kv_name)}/{OIDC_ACCESS_FILE}"


def k8s_access_path(values_dir: str, team: str, kv_name: str) -> str:
    return f"{kv_store_dir(values_dir, team, kv_name)}/{K8S_ACCESS_FILE}"


def access_path_for(values_dir: str, team: str, kv_name: str, auth_type: str) -> str:
    """The file an entry of this `authType` belongs in.

    The one place that maps a kind to a file, so a caller never picks the filename itself.
    """
    if auth_type == K8S_AUTH_TYPE:
        return k8s_access_path(values_dir, team, kv_name)
    return oidc_access_path(values_dir, team, kv_name)


def tashtiot_access_path(values_dir: str, tashtiot_dir: str, team: str) -> str:
    """The flat per-team file, e.g. ``KVs/Tashtiot/cloudops.yaml``.

    Deliberately not a `kv_store_dir`: this layout is one file holding both kinds, so it
    has no directory and no oidc/k8s split.
    """
    return f"{values_dir.strip('/')}/{tashtiot_dir.strip('/')}/{team.strip('/')}.yaml"


# --------------------------------------------------------------------------- #
# building entries
#
# These are the *sole* writers of this format's key names. Change one and change the deploy
# pipeline with it.
# --------------------------------------------------------------------------- #
def build_oidc_access_entry(
    path: str, dn: str, capabilities: Iterable[str]
) -> Dict[str, Any]:
    """One OIDC grant: a group DN over a path."""
    return {
        "path": path,
        AUTH_TYPE_KEY: OIDC_AUTH_TYPE,
        DN_KEY: dn,
        CAPABILITIES_KEY: list(capabilities),
    }


def build_k8s_access_entry(
    path: str,
    name: str,
    namespace: str,
    cluster: str,
    capabilities: Iterable[str],
) -> Dict[str, Any]:
    """One Kubernetes grant: a service account triple over a path.

    `name` rather than `serviceAccount` — that is the format's key, and it is not the same
    name the previous revision of this service wrote.
    """
    return {
        "path": path,
        AUTH_TYPE_KEY: K8S_AUTH_TYPE,
        "name": name,
        "namespace": namespace,
        "cluster": cluster,
        CAPABILITIES_KEY: list(capabilities),
    }


def build_access_document(entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """A whole access file from scratch: the `access` list and nothing else.

    A constructor, not a transform — the transforms below start from a parsed document and
    keep whatever else it holds.
    """
    return {ACCESS_KEY: list(entries)}


# --------------------------------------------------------------------------- #
# reading and identifying
# --------------------------------------------------------------------------- #
def read_access_entries(document: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The `access` list, tolerating an absent key and an empty one.

    `access:` with nothing under it parses to None — an empty file, not a corrupt one.
    Non-dict entries are skipped so a hand-edited file cannot wedge an unrelated edit.
    """
    if not isinstance(document, dict):
        return []
    entries = document.get(ACCESS_KEY)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def auth_type_of(entry: Dict[str, Any]) -> str:
    """An entry's kind, defaulting to `oidc` when the key is missing.

    The format's own default: an entry carrying a `dn` and no `authType` is an OIDC grant.
    """
    value = entry.get(AUTH_TYPE_KEY)
    return value if isinstance(value, str) and value else OIDC_AUTH_TYPE


def access_entry_identity(entry: Dict[str, Any]) -> Tuple[str, ...]:
    """What makes an entry unique — everything except its capabilities.

    Capabilities are the *mutable* part: granting `read` and later `read`+`write` to the
    same principal on the same path is an edit of one entry, not a second entry. Missing
    keys become "" so a hand-edited file cannot wedge an unrelated removal.
    """
    kind = auth_type_of(entry)
    keys = _K8S_ENTRY_KEYS if kind == K8S_AUTH_TYPE else _OIDC_ENTRY_KEYS
    return tuple(
        str(entry.get(key, "") or "") if key != AUTH_TYPE_KEY else kind
        for key in keys
        if key != CAPABILITIES_KEY
    )


def access_entry_identities(
    document: Optional[Dict[str, Any]],
) -> List[Tuple[str, ...]]:
    return [access_entry_identity(entry) for entry in read_access_entries(document)]


def find_access_entry(
    document: Optional[Dict[str, Any]], identity: Tuple[str, ...]
) -> Optional[Dict[str, Any]]:
    for entry in read_access_entries(document):
        if access_entry_identity(entry) == identity:
            return entry
    return None


def partition_access(
    entries: Iterable[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split entries into (oidc, k8s) — the one place that knows the files are split.

    Order within each kind is preserved, so the committed lists read in the order the
    caller sent them.
    """
    oidc: List[Dict[str, Any]] = []
    k8s: List[Dict[str, Any]] = []
    for entry in entries:
        (k8s if auth_type_of(entry) == K8S_AUTH_TYPE else oidc).append(entry)
    return oidc, k8s


# --------------------------------------------------------------------------- #
# transforms
#
# Every transform deep-copies the whole document and replaces only the `access` key, so a
# sibling key this service knows nothing about survives. It returns a **new** document and
# never mutates its input — that is what lets an update diff old against new with
# `yaml_data_equals` and skip a no-op commit. Anything added here must keep both properties.
# --------------------------------------------------------------------------- #
def _with_entries(
    document: Optional[Dict[str, Any]], entries: List[Dict[str, Any]]
) -> Dict[str, Any]:
    updated = copy.deepcopy(document) if isinstance(document, dict) else {}
    updated[ACCESS_KEY] = entries
    return updated


def add_access_entry(
    document: Optional[Dict[str, Any]], entry: Dict[str, Any]
) -> Dict[str, Any]:
    """Append one grant. `None` is accepted: the file may not exist yet.

    Raises `DuplicateAccessEntry` when the identity is already present — the caller turns
    that into a 409. Two entries differing only in capabilities are the same grant stated
    twice, which is contradictory rather than additive.
    """
    entries = read_access_entries(document)
    identity = access_entry_identity(entry)
    if any(access_entry_identity(existing) == identity for existing in entries):
        raise DuplicateAccessEntry(str(identity))
    return _with_entries(document, [*copy.deepcopy(entries), copy.deepcopy(entry)])


def remove_access_entry(
    document: Optional[Dict[str, Any]], identity: Tuple[str, ...]
) -> Dict[str, Any]:
    """Drop one grant, raising `AccessEntryNotFound` when it is not there.

    Unlike a store delete, an emptied list is **kept** as `access: []` rather than dropping
    the key: a file that declares nothing is a meaningful statement, and the caller decides
    separately whether an empty file should be removed.
    """
    entries = read_access_entries(document)
    remaining = [
        entry for entry in entries if access_entry_identity(entry) != identity
    ]
    if len(remaining) == len(entries):
        raise AccessEntryNotFound(str(identity))
    return _with_entries(document, copy.deepcopy(remaining))


def replace_access_entries(
    document: Optional[Dict[str, Any]], entries: Iterable[Dict[str, Any]]
) -> Dict[str, Any]:
    """Swap the whole `access` list, keeping every sibling key.

    What a `PATCH` applies. Wholesale rather than merged: a caller that wants to drop a
    grant sends the list without it, and nothing here has to guess at intent.
    """
    return _with_entries(document, copy.deepcopy(list(entries)))


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
class _BlockStyleDumper(yaml.SafeDumper):
    """SafeDumper that indents block sequences and writes multi-line strings as blocks."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        """Indent list items under their parent key, instead of flush with it.

        PyYAML defaults to the "indentless" form, which is valid YAML and parses
        identically::

            access:
            - path: "*"

        but the deploy pipeline's format (and every hand-written file) puts the dash under
        the key::

            access:
              - path: "*"

        Passing `indentless=False` unconditionally is the documented way to get that. It
        applies to every sequence, so `capabilities` indents the same way — the committed
        file has one style throughout.
        """
        return super().increase_indent(flow=flow, indentless=False)

    def choose_scalar_style(self) -> Any:
        """Double quotes wherever PyYAML would have reached for single ones.

        PyYAML prefers `'*'`; the pipeline's files are written with `"*"`. The two parse
        identically — this is about the committed text matching the format's shape, like
        the sequence indentation above. Only *quoted* scalars are affected: a value that
        needs no quoting stays plain, so this does not quote the whole document.
        """
        style = super().choose_scalar_style()
        return '"' if style == "'" else style

    def represent_mapping(self, tag: str, mapping: Any, flow_style: Any = None) -> Any:
        """Quote `dn` even though it needs no quoting.

        A distinguished name is plain-safe, so PyYAML would emit it bare. The format's
        templates show it quoted, and the quotes are load-bearing for a reader: a DN is
        full of `=` and `,` and reads as structure otherwise.
        """
        node = super().represent_mapping(tag, mapping, flow_style)
        for key_node, value_node in node.value:
            if (
                key_node.value == DN_KEY
                and isinstance(value_node, yaml.ScalarNode)
                and value_node.style not in ("|", ">")
            ):
                value_node.style = '"'
        return node


def _represent_str(dumper: yaml.Dumper, data: str) -> Any:
    """Multi-line strings as `|` blocks.

    The default representer emits one escaped, width-wrapped scalar, which is unreadable in
    the pull request diff a human is supposed to review.
    """
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BlockStyleDumper.add_representer(str, _represent_str)


def render_values_yaml(values: Dict[str, Any]) -> str:
    """Serialise the document for committing. Key order is preserved for readable diffs."""
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
