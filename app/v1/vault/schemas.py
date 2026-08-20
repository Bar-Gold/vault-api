"""Request and response models.

The request shape **mirrors the committed document**: a caller sends the same `access`
entries that land in the file, rather than a shape this service translates. That is the
same boundary the rest of the service keeps — the format is the deploy pipeline's, and
this service is a thin, validating writer of it. The two consequences are that `path` and
`capabilities` are expressible (they have no equivalent in any translated shape), and that
a format change is a change here and in the pipeline, with nothing in between.

What is deliberately absent: there is no `kv_description`. The format defines no home for
it, and this service does not invent keys.
"""

from enum import Enum
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

# A single name segment: lowercase alphanumerics separated by single dashes.
_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"

# A store's name, and the second segment of its directory (`KVs/<team>/<kv_name>/`). Both
# a **path-traversal guard** and the address every route but the create uses (`/{kv_name}`):
# no slash, so no directory can be escaped or created, and `..` cannot match.
KV_NAME_PATTERN = rf"^{_SEGMENT}$"

# The owning team, and the first segment of the directory. Same guard, same reason. It is
# only ever sent on a create — a store name is unique across the whole values directory, so
# every other route resolves the team rather than being told it.
TEAM_PATTERN = rf"^{_SEGMENT}$"

# A Vault secret path pattern, e.g. `*`, `services/+/tyk/*`, `+/cloudops/*`.
#
# This is a *Vault* path, not a filesystem one — it never reaches a repo path, so traversal
# is not the risk here and the pattern can be permissive about slashes. `+` (single
# segment) and `*` (any suffix) are Vault's own wildcards. What it still refuses is an
# empty string and anything outside the character set the pipeline can parse; `..` is
# rejected by `no_parent_segments` below, because pydantic-core's regex engine has no
# look-ahead and the pattern cannot express it.
VAULT_PATH_PATTERN = r"^[A-Za-z0-9_*+][A-Za-z0-9_*+/.-]*$"

VAULT_PATH_MAX_LENGTH = 256


class Capability(str, Enum):
    """What a grant permits.

    A closed set, unlike the DN values beside it: these reach the pipeline as Vault policy
    capabilities, so an unknown one is a request this service should refuse rather than
    pass on. Add to it only alongside the pipeline.
    """

    READ = "read"
    WRITE = "write"


class AuthType(str, Enum):
    """Which identity provider a grant is for — and which file it lands in.

    `oidc` entries go to `oidc-access.yaml`, `k8s` entries to `k8s-access.yaml`. The
    Tashtiot per-team file mixes both in one list.
    """

    OIDC = "oidc"
    K8S = "k8s"


def _validate_capabilities(values: List[Capability]) -> List[Capability]:
    """At least one, and no duplicates.

    An entry granting nothing is not a grant, and the same capability twice is a diff a
    reviewer has to think about for no reason.
    """
    if not values:
        raise ValueError("capabilities must not be empty")
    seen = [value.value for value in values]
    if len(set(seen)) != len(seen):
        raise ValueError("capabilities must not repeat")
    return values


class _AccessEntryBase(BaseModel):
    """Fields both kinds of grant carry."""

    model_config = {"extra": "forbid"}

    path: str = Field(
        ...,
        max_length=VAULT_PATH_MAX_LENGTH,
        pattern=VAULT_PATH_PATTERN,
        examples=["*"],
        description=(
            "Secret path pattern this grant covers. '*' means every secret in the store; "
            "'services/+/tyk/*' scopes it, where '+' matches one segment."
        ),
    )

    capabilities: List[Capability] = Field(
        ...,
        examples=[["read", "write"]],
        description="What the principal may do on that path. At least one, no repeats.",
    )

    @field_validator("capabilities")
    @classmethod
    def valid_capabilities(cls, v: List[Capability]) -> List[Capability]:
        return _validate_capabilities(v)

    @field_validator("path")
    @classmethod
    def no_parent_segments(cls, v: str) -> str:
        """`..` is refused here rather than in the pattern.

        A Vault path never reaches a repo path, so this is not the traversal guard — that
        is `TEAM_PATTERN`/`KV_NAME_PATTERN`. It is refused because it is meaningless to
        Vault and almost certainly a mistake. It lives in a validator because
        pydantic-core's regex engine does not support the look-ahead the pattern would
        need.
        """
        if ".." in v:
            raise ValueError("path must not contain '..'")
        return v


class OidcAccessEntry(_AccessEntryBase):
    """A group's grant, written to `oidc-access.yaml`.

    `dn` is an LDAP distinguished name and is deliberately **unvalidated** beyond non-blank:
    any pattern tight enough to describe a DN rejects the forms this service does not own,
    and the vocabulary is the directory's, not ours.
    """

    auth_type: Literal[AuthType.OIDC] = Field(
        ...,
        description="oidc — a group or user from the directory.",
    )

    dn: str = Field(
        ...,
        min_length=1,
        max_length=512,
        examples=["CN=svc-athena,OU=ServiceAccounts,DC=corp,DC=example,DC=com"],
        description="Distinguished Name of the group being granted access.",
    )

    @field_validator("dn")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("dn must not be blank")
        return v.strip()


# Kubernetes' own rules, not ours — a looser pattern would let a request pass here and fail
# at admission long after the pull request merged.
#
# A namespace is an RFC 1123 *label* (no dots, <=63) and a service account name an RFC 1123
# *subdomain* (dots allowed, <=253). Genuinely different limits: do not collapse them.
K8S_SERVICE_ACCOUNT_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
K8S_SERVICE_ACCOUNT_MAX_LENGTH = 253
K8S_NAMESPACE_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
K8S_NAMESPACE_MAX_LENGTH = 63

# The OpenShift cluster suffix. No Kubernetes rule to borrow, so this is our own
# single-segment shape. The pipeline matches it against a k8sMethods[].name.
K8S_CLUSTER_PATTERN = rf"^{_SEGMENT}$"
K8S_CLUSTER_MAX_LENGTH = 64


class K8sAccessEntry(_AccessEntryBase):
    """A service account's grant, written to `k8s-access.yaml`.

    The three scalars together are the workload's identity — the same account in two
    namespaces or two clusters is two different principals.

    `*` is **rejected** in `name` and `namespace`: it would grant every workload in the
    cluster, and that escalation should need a human editing the YAML, not an API call.
    """

    auth_type: Literal[AuthType.K8S] = Field(
        ...,
        description="k8s — a Kubernetes service account.",
    )

    name: str = Field(
        ...,
        max_length=K8S_SERVICE_ACCOUNT_MAX_LENGTH,
        pattern=K8S_SERVICE_ACCOUNT_PATTERN,
        examples=["cloudops-external-secrets"],
        description="Service account name (RFC 1123 subdomain). No wildcards.",
    )

    namespace: str = Field(
        ...,
        max_length=K8S_NAMESPACE_MAX_LENGTH,
        pattern=K8S_NAMESPACE_PATTERN,
        examples=["cloudops"],
        description="Namespace the service account lives in (RFC 1123 label). No wildcards.",
    )

    cluster: str = Field(
        ...,
        max_length=K8S_CLUSTER_MAX_LENGTH,
        pattern=K8S_CLUSTER_PATTERN,
        examples=["dev"],
        description="OpenShift cluster suffix; must match a k8sMethods[].name in the pipeline.",
    )


# Discriminated on `auth_type`, so a malformed entry reports the field that is wrong rather
# than "did not match any variant". An entry with no `auth_type` is rejected rather than
# defaulted, because the two shapes carry different required fields and guessing between
# them turns a typo into the wrong file.
AccessEntry = Annotated[
    Union[OidcAccessEntry, K8sAccessEntry],
    Field(discriminator="auth_type"),
]
"""One grant, either kind.

Used both inside `access` lists and **as a request body on its own** — the sub-resource
form of an edit posts a single entry rather than the whole list, and needs no wrapper
object to do it.
"""


def _validate_access(entries: List[AccessEntry]) -> List[AccessEntry]:
    """At least one grant, and no two grants of the same identity.

    Identity is everything but the capabilities: the same principal on the same path twice
    is a contradiction, not an addition. This mirrors `access_entry_identity` in
    `helpers.py`; that one works on committed dicts, this one on the request.
    """
    if not entries:
        raise ValueError("access must not be empty")

    seen = set()
    for entry in entries:
        if isinstance(entry, K8sAccessEntry):
            identity = ("k8s", entry.path, entry.name, entry.namespace, entry.cluster)
        else:
            identity = ("oidc", entry.path, entry.dn)
        if identity in seen:
            raise ValueError(f"duplicate access entry: {identity}")
        seen.add(identity)
    return entries


class OperationStatus(str, Enum):
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"


class VaultKVCreate(BaseModel):
    """A create request: who owns the store, what it is called, and who reaches it.

    The store becomes a **directory**, `KVs/<team>/<kv_name>/`, holding up to two files
    split by `auth_type`. The caller never names a file: the split is the format's and this
    service applies it.

    `kv_name` is unique across the *whole* values directory, not just within the team, which
    is what lets every other route address a store by name alone.
    """

    model_config = {
        "extra": "forbid",
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "team": "cloudops",
                "kv_name": "my-passwords",
                "access": [
                    {
                        "path": "*",
                        "auth_type": "oidc",
                        "dn": "CN=cloudops,OU=Groups,DC=corp,DC=example,DC=com",
                        "capabilities": ["read", "write"],
                    },
                    {
                        "path": "*",
                        "auth_type": "k8s",
                        "name": "cloudops-external-secrets",
                        "namespace": "cloudops",
                        "cluster": "dev",
                        "capabilities": ["read"],
                    },
                ],
            }
        },
    }

    team: str = Field(
        ...,
        max_length=128,
        pattern=TEAM_PATTERN,
        examples=["cloudops"],
        description=(
            "Team that owns the store. Becomes the first directory segment, "
            "'<values dir>/<team>/<kv_name>/'."
        ),
    )

    kv_name: str = Field(
        ...,
        max_length=128,
        pattern=KV_NAME_PATTERN,
        examples=["my-passwords"],
        description=(
            "Name of the KV store, and its directory. Must be unique across every team in "
            "the values directory, and may not contain a slash."
        ),
    )

    access: List[AccessEntry] = Field(
        ...,
        description=(
            "The grants this store starts with. oidc entries are written to "
            "oidc-access.yaml and k8s entries to k8s-access.yaml; a store with no k8s "
            "grants gets no k8s-access.yaml at all."
        ),
    )

    @field_validator("access")
    @classmethod
    def valid_access(cls, v: List[AccessEntry]) -> List[AccessEntry]:
        return _validate_access(v)


class VaultKVUpdate(BaseModel):
    """The replacement grant list for an existing store.

    Wholesale, not merged: a caller that wants to drop a grant sends the list without it,
    and nothing here has to guess at intent. The name and team are not editable — a rename
    means migrating secrets in Vault, not editing a field, so it is a delete plus a create.
    """

    model_config = {
        "extra": "forbid",
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "access": [
                    {
                        "path": "*",
                        "auth_type": "oidc",
                        "dn": "CN=cloudops,OU=Groups,DC=corp,DC=example,DC=com",
                        "capabilities": ["read"],
                    }
                ]
            }
        },
    }

    access: List[AccessEntry] = Field(
        ...,
        description="The full grant list after the edit. Replaces what is there.",
    )

    @field_validator("access")
    @classmethod
    def valid_access(cls, v: List[AccessEntry]) -> List[AccessEntry]:
        return _validate_access(v)


class TashtiotAccessUpsert(BaseModel):
    """The whole grant list for one team's Tashtiot file.

    A different resource from a KV store: it is per-team, lands in a single flat file
    (`<values dir>/Tashtiot/<team>.yaml`) and mixes both `auth_type`s in one list rather
    than splitting them. It grants a team access into the shared services store, which is
    why its paths are usually scoped (`services/+/redis/*`) rather than `*`.
    """

    model_config = {
        "extra": "forbid",
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "access": [
                    {
                        "path": "+/cloudops/*",
                        "auth_type": "oidc",
                        "dn": "CN=cloudops,OU=Groups,DC=corp,DC=example,DC=com",
                        "capabilities": ["read"],
                    }
                ]
            }
        },
    }

    access: List[AccessEntry] = Field(
        ...,
        description="Every grant this team has into the shared services store.",
    )

    @field_validator("access")
    @classmethod
    def valid_access(cls, v: List[AccessEntry]) -> List[AccessEntry]:
        return _validate_access(v)


# --------------------------------------------------------------------------- #
# responses
# --------------------------------------------------------------------------- #
class PullRequestInfo(BaseModel):
    id: int = Field(..., description="Bitbucket pull-request id.")
    url: Optional[str] = Field(default=None, description="Browser link to the pull request.")
    state: str = Field(..., description="Pull-request state as reported by Bitbucket.")


class BuildInfo(BaseModel):
    """One row of the pull request's Builds tab, as Bitbucket reports it.

    `key` is what identifies a build to Bitbucket — it stores one result per key per
    commit — so it is the field that tells two workflows apart, not a number.
    """

    key: str = Field(..., description="Bitbucket build-status key, e.g. ci/woodpecker/pr/build.")
    state: str = Field(
        ...,
        description="Bitbucket build state: SUCCESSFUL, FAILED, CANCELLED, UNKNOWN or INPROGRESS.",
    )
    name: Optional[str] = Field(default=None, description="Display name reported by the CI server.")
    url: Optional[str] = Field(default=None, description="Browser link to the build.")


class VaultOperationResponse(BaseModel):
    """Outcome of the chain: pull request -> validation CI -> merge -> deploy CI.

    One model for every mutating route, stores and bindings alike. `(file, kv_name)`
    addresses both: a binding is part of a store, so the store is the subject of a binding
    operation too, and the message names the triple. There is no `role_name` — an earlier
    revision modelled bindings as independently-named top-level roles, which the pipeline's
    format does not have.

    The trap to remember: a field added here is silently omitted from every failure body
    unless `VaultOperationError.to_response()` is updated too, because failures are
    *returned* as a JSONResponse rather than validated against this model.
    """

    status: OperationStatus = Field(..., description="Succeeded or Failed.")

    message: str = Field(
        ...,
        description="Human-readable outcome, e.g. 'Successful creation of myapp'.",
    )

    file: str = Field(
        default="", description="The values file this request acted on."
    )

    kv_name: str = Field(
        default="", description="The KV store this request acted on, if any."
    )

    pull_request: Optional[PullRequestInfo] = Field(
        default=None, description="The pull request opened for this change."
    )

    validation_builds: Optional[List[BuildInfo]] = Field(
        default=None,
        description=(
            "Builds Bitbucket reported against the pull request's commit. A list because a "
            "CI server may report one build per workflow, and all of them must be green. "
            "Null on the routes that open a pull request without waiting for CI."
        ),
    )

    error: Optional[str] = Field(
        default=None, description="Failure detail when status is Failed."
    )
