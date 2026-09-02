"""Request and response models.

The request shape **mirrors the committed document**: a caller sends the same `access`
entries that land in the file, rather than a shape this service translates. That is the
same boundary the rest of the service keeps — the format is the deploy pipeline's, and
this service is a thin, validating writer of it. The two consequences are that `path` and
`capabilities` are expressible (they have no equivalent in any translated shape), and that
a format change is a change here and in the pipeline, with nothing in between.

What is deliberately absent: there is no `kv_description`. The format defines no home for
it, and this service does not invent keys.

The mirror has exactly **one** documented exception: `metadata`. It is required on every
request that carries a body, and it is the one field that never reaches a file — the
document keeps its single `access` key, and the six coordinates go to the pull-request
description and the service log instead. See `RequestMetadata`.
"""

from enum import Enum
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

# A single name segment: lowercase alphanumerics separated by single dashes.
_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"

# The same segment, case-insensitively. Used **only** on the read routes — see
# `KV_NAME_READ_PATTERN`.
_ANY_CASE_SEGMENT = r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"

# A store's name, and the second segment of its directory (`KVs/<team>/<kv_name>/`). Both
# a **path-traversal guard** and the address every route but the create uses (`/{kv_name}`):
# no slash, so no directory can be escaped or created, and `..` cannot match.
KV_NAME_PATTERN = rf"^{_SEGMENT}$"

# The owning team, and the first segment of the directory. Same guard, same reason. It is
# only ever sent on a create — a store name is unique across the whole values directory, so
# every other route resolves the team rather than being told it.
TEAM_PATTERN = rf"^{_SEGMENT}$"

# The store name and team as accepted on the **read** routes (`GET /{kv_name}` and
# `GET /tashtiot/{team}`), which differ from the patterns above in one way: they accept
# capitals.
#
# A create still writes a lowercase name — `KV_NAME_PATTERN` above is unchanged — but the
# values directory is not written only by this service, and it may already hold a
# capitalised store or team. `_walk_store_dirs` finds those perfectly well and
# `_resolve_store` compares names exactly, so the only thing standing between a caller and
# a capitalised store was this pattern: the request was rejected as a 422 before the walk
# ever ran, which read as "malformed name" when the truth was "your store is unreachable".
#
# **Still the same path-traversal guard.** One segment, no slash and no dot, so no
# directory can be escaped or created and `..` cannot match. Case has no bearing on that;
# it only ever decided which existing names were addressable.
KV_NAME_READ_PATTERN = rf"^{_ANY_CASE_SEGMENT}$"
TEAM_READ_PATTERN = rf"^{_ANY_CASE_SEGMENT}$"

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

    # A **model-level** example, not just per-field ones. Without one Swagger synthesises
    # the shape from the field schemas — which fills every string with `"string"` rather
    # than the field's own example, so the box a user is meant to edit shows nothing usable.
    #
    # It carries **no** `metadata`, deliberately: this model is what goes inside an `access`
    # list, and a list element carrying metadata is a 422. The body of
    # `POST /{kv_name}/access` is `OidcAccessEntryRequest`, whose example does carry it.
    model_config = {
        **_AccessEntryBase.model_config,
        "json_schema_extra": {
            "example": {
                "path": "*",
                "auth_type": "oidc",
                "dn": "CN=cloudops,OU=Groups,DC=corp,DC=example,DC=com",
                "capabilities": ["read", "write"],
            }
        },
    }

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

    # See `OidcAccessEntry.model_config` — the same reason, and the reason there are two:
    # the body is a `oneOf` on `auth_type`, so Swagger shows one example per branch and the
    # picker is how a caller switches between them. No `metadata` here either: this is the
    # list-element shape, and `K8sAccessEntryRequest` is the body one.
    model_config = {
        **_AccessEntryBase.model_config,
        "json_schema_extra": {
            "example": {
                "path": "*",
                "auth_type": "k8s",
                "name": "cloudops-external-secrets",
                "namespace": "cloudops",
                "cluster": "dev",
                "capabilities": ["read"],
            }
        },
    }

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
"""One grant, either kind — the **list-element** type.

This is what goes inside an `access` list, and nothing else: it carries no `metadata`, so
a list element that sends one is a 422. The request body of the sub-resource form is
`AccessEntryRequest` below — the same grant plus the metadata block, still with no
wrapper object around it.
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


# --------------------------------------------------------------------------- #
# request-only data
#
# Everything above this point mirrors the committed document. `metadata` is the single
# documented exception: required on every request that carries a body, and never written.
# --------------------------------------------------------------------------- #

# Deployment coordinates the organisation requires on every mutating request. Free-form on
# purpose, exactly like `dn`: the vocabulary is the deploying team's, not this service's,
# so any closed set or pattern here would reject a value we do not own. 253 is the same
# number as K8S_SERVICE_ACCOUNT_MAX_LENGTH and is **not** the same rule — do not collapse
# them.
METADATA_MAX_LENGTH = 253

# One dict, spliced into all five body examples. Retyping it would let the docs drift.
_METADATA_EXAMPLE = {
    "project": "cloudops",
    "network": "internal",
    "region": "il-central",
    "space": "shared",
    "environment": "production",
    "island": "island-a",
}


class RequestMetadata(BaseModel):
    """Where the caller's deployment lives — audit only, never committed.

    Required on every request that carries a body, and **dropped before anything is
    written**: `to_document_entry` builds a document entry from named fields, so there is
    no path by which one of these values can reach an access file. What it does reach is
    the pull-request description, where a reviewer sees it, and the service log.

    All six values are free-form non-blank strings. That mirrors how `dn` is handled: any
    pattern tight enough to describe the organisation's vocabulary would reject forms this
    service does not own. What is refused is only blank/whitespace-only and over-long.
    """

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"example": _METADATA_EXAMPLE},
    }

    project: str = Field(
        ...,
        min_length=1,
        max_length=METADATA_MAX_LENGTH,
        examples=["cloudops"],
        description="Project the deployment belongs to.",
    )

    network: str = Field(
        ...,
        min_length=1,
        max_length=METADATA_MAX_LENGTH,
        examples=["internal"],
        description="Network the deployment sits on.",
    )

    region: str = Field(
        ...,
        min_length=1,
        max_length=METADATA_MAX_LENGTH,
        examples=["il-central"],
        description="Region the deployment sits in.",
    )

    space: str = Field(
        ...,
        min_length=1,
        max_length=METADATA_MAX_LENGTH,
        examples=["shared"],
        description="Space the deployment belongs to.",
    )

    environment: str = Field(
        ...,
        min_length=1,
        max_length=METADATA_MAX_LENGTH,
        examples=["production"],
        description="Environment the deployment targets.",
    )

    island: str = Field(
        ...,
        min_length=1,
        max_length=METADATA_MAX_LENGTH,
        examples=["island-a"],
        description="Island the deployment lands on.",
    )

    # The six names are spelled out rather than `"*"`: the explicit list survives someone
    # later adding a field that is not a string.
    @field_validator("project", "network", "region", "space", "environment", "island")
    @classmethod
    def not_blank(cls, v: str) -> str:
        """Same rule, same shape, as `OidcAccessEntry.dn`: non-blank, and stripped."""
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    def as_log_text(self) -> str:
        """One line for the service log: `project=... network=... ...`, declaration order."""
        return " ".join(f"{name}={value}" for name, value in self.model_dump().items())


_METADATA_FIELD_DESCRIPTION = (
    "Deployment coordinates, for audit. Recorded in the pull-request description and "
    "never written to the access files."
)


class OidcAccessEntryRequest(OidcAccessEntry):
    """`OidcAccessEntry` as a request body on its own: the same grant plus the metadata
    block.

    The list forms (`access: [...]`) keep the plain entry — metadata is a property of the
    *request*, not of a grant, so it must not become required on every list element. This
    subclasses rather than wraps, so a request entry still **is** the entry it extends and
    `to_document_entry`'s dispatch keeps working untouched.
    """

    # Merged, not replaced: `extra: "forbid"` lives in the parent's config too, and
    # overwriting the whole dict would silently reopen the model.
    model_config = {
        **OidcAccessEntry.model_config,
        "json_schema_extra": {
            "example": {
                "path": "*",
                "auth_type": "oidc",
                "dn": "CN=cloudops,OU=Groups,DC=corp,DC=example,DC=com",
                "capabilities": ["read", "write"],
                "metadata": _METADATA_EXAMPLE,
            }
        },
    }

    metadata: RequestMetadata = Field(..., description=_METADATA_FIELD_DESCRIPTION)


class K8sAccessEntryRequest(K8sAccessEntry):
    """The Kubernetes branch of the same body. See `OidcAccessEntryRequest`."""

    model_config = {
        **K8sAccessEntry.model_config,
        "json_schema_extra": {
            "example": {
                "path": "*",
                "auth_type": "k8s",
                "name": "cloudops-external-secrets",
                "namespace": "cloudops",
                "cluster": "dev",
                "capabilities": ["read"],
                "metadata": _METADATA_EXAMPLE,
            }
        },
    }

    metadata: RequestMetadata = Field(..., description=_METADATA_FIELD_DESCRIPTION)


# The union stays at the **top** of the body, which is the only place Swagger's branch
# picker and the two model-level examples work — a wrapper object would push it under a key
# and leave one example to describe two shapes.
AccessEntryRequest = Annotated[
    Union[OidcAccessEntryRequest, K8sAccessEntryRequest],
    Field(discriminator="auth_type"),
]
"""One grant **as a whole request body**, with its metadata.

`AccessEntry` (no metadata) remains what goes inside an `access` list.
"""


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
                "metadata": _METADATA_EXAMPLE,
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

    # Declared last, so all five bodies read the same way. Request-only: nothing past
    # `to_document_entry` ever sees it.
    metadata: RequestMetadata = Field(..., description=_METADATA_FIELD_DESCRIPTION)

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
                ],
                "metadata": _METADATA_EXAMPLE,
            }
        },
    }

    access: List[AccessEntry] = Field(
        ...,
        description="The full grant list after the edit. Replaces what is there.",
    )

    # Declared last, so all five bodies read the same way. Request-only: nothing past
    # `to_document_entry` ever sees it.
    metadata: RequestMetadata = Field(..., description=_METADATA_FIELD_DESCRIPTION)

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
                    },
                    {
                        "path": "services/+/redis/*",
                        "auth_type": "k8s",
                        "name": "cloudops-external-secrets",
                        "namespace": "cloudops",
                        "cluster": "dev",
                        "capabilities": ["read"],
                    },
                ],
                "metadata": _METADATA_EXAMPLE,
            }
        },
    }

    access: List[AccessEntry] = Field(
        ...,
        description="Every grant this team has into the shared services store.",
    )

    # Declared last, so all five bodies read the same way. Request-only: nothing past
    # `to_document_entry` ever sees it.
    metadata: RequestMetadata = Field(..., description=_METADATA_FIELD_DESCRIPTION)

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
