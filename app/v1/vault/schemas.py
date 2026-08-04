import re
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# A single name segment: lowercase alphanumerics separated by single dashes.
_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"

# The values file a store lives in: `kv/{file}.yaml`. This is the **path-traversal guard**,
# not just a style rule — it is the only request field that reaches the filesystem path. No
# slash, so no directory can be escaped or created, and `..` cannot match.
FILE_PATTERN = rf"^{_SEGMENT}$"

# A store's name. Single-segment because the routes address a store as `{file}/{kv_name}`,
# and a slash here would make that split ambiguous. Unlike `file`, this never reaches a
# path — it is a value inside the document.
KV_NAME_PATTERN = rf"^{_SEGMENT}$"

# Hostnames granted a role. Permissive enough for internal names that carry no dot.
FQDN_PATTERN = r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$"

# The role keys the committed document may carry.
#
# `read` and `write` are separate keys — a store may carry either, or both (a host that only
# reads and a host that also writes are different entries). The `<read/write>` in the format
# spec is a placeholder meaning "one of these", the same as `<name>` and `<FQDN>` around it,
# not a literal key.
#
# Everything outside this module treats `roles` as an opaque mapping of key to host list, so
# this frozenset is the only thing to change if the deploy pipeline ever wants a different
# set of role names.
ALLOWED_ROLE_KEYS = frozenset({"read", "write"})

# --------------------------------------------------------------------------- #
# Kubernetes auth roles
#
# A role's name is addressed as `{file}/{role_name}`, so it is single-segment for the same
# reason `kv_name` is. `K8S_ROLE_NAME_PATTERN` duplicates `KV_NAME_PATTERN`'s value rather
# than aliasing it: they are two independent namespaces that happen to agree today.
# --------------------------------------------------------------------------- #
K8S_ROLE_NAME_PATTERN = rf"^{_SEGMENT}$"
K8S_CLUSTER_PATTERN = rf"^{_SEGMENT}$"

# Kubernetes' own naming rules, not ours. A namespace is an RFC 1123 *label* (no dots, 63
# chars); a ServiceAccount name is an RFC 1123 *subdomain* (dots allowed, 253). Neither may
# contain uppercase — which is why `FQDN_PATTERN` is not reused here: it accepts uppercase,
# so a request would pass validation and then be rejected at admission in the cluster.
K8S_NAMESPACE_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
K8S_NAMESPACE_MAX_LENGTH = 63
K8S_SERVICE_ACCOUNT_PATTERN = (
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)
K8S_SERVICE_ACCOUNT_MAX_LENGTH = 253

# A Vault duration string, passed through untouched.
DURATION_PATTERN = r"^[0-9]+(s|m|h|d)$"

# What a role may ask for over a KV store. Same values as `ALLOWED_ROLE_KEYS` today and
# deliberately a separate frozenset: they are two pipeline contracts that happen to agree,
# and k8s roles could plausibly become read-only without hosts following.
#
# This is also where the scope boundary is drawn. An entry names *stores* and a
# *capability*; deriving the policy from that pair is the deploy pipeline's job. No policy
# name, policy body, mount path or engine version is generated anywhere in this service.
ALLOWED_KV_ACCESS_KEYS = frozenset({"read", "write"})


def _validate_bound_names(
    names: List[str], field: str, pattern: str, max_length: int
) -> List[str]:
    """One list of Kubernetes names: non-blank, unique, no wildcard, RFC 1123."""
    cleaned: List[str] = []
    for name in names:
        stripped = name.strip() if isinstance(name, str) else name
        if not stripped:
            raise ValueError(f"{field} must not contain blank entries")
        if "*" in stripped:
            raise ValueError(
                f"{field} must not contain '*': a wildcard binds every workload in the "
                f"cluster to this role. List each name explicitly, or have a human make "
                f"that grant by editing the values file directly."
            )
        if len(stripped) > max_length:
            raise ValueError(
                f"each {field} entry must be at most {max_length} characters"
            )
        if not re.fullmatch(pattern, stripped):
            raise ValueError(
                f"'{stripped}' is not a valid {field} entry; Kubernetes names are "
                f"lowercase alphanumerics, dashes and (service accounts only) dots"
            )
        cleaned.append(stripped)

    if not cleaned:
        raise ValueError(f"{field} must list at least one name")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"{field} must not repeat a name")
    return cleaned


def _validate_access(access: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """At least one known capability, each naming at least one existing-shaped KV store.

    Whether the stores actually exist is checked against the values repo by the operation;
    this only rejects what cannot be a store name at all.
    """
    if not access:
        raise ValueError("access must not be empty")

    unknown = sorted(set(access) - ALLOWED_KV_ACCESS_KEYS)
    if unknown:
        allowed = ", ".join(sorted(ALLOWED_KV_ACCESS_KEYS))
        raise ValueError(f"unknown access key(s) {unknown}; allowed: {allowed}")

    cleaned: Dict[str, List[str]] = {}
    for capability, stores in access.items():
        stripped = [store.strip() for store in stores if store and store.strip()]
        if not stripped:
            raise ValueError(f"access '{capability}' must list at least one KV store")
        if len(stripped) != len(stores):
            raise ValueError(f"access '{capability}' must not contain blank store names")
        if len(set(stripped)) != len(stripped):
            raise ValueError(f"access '{capability}' must not repeat a KV store")
        for store in stripped:
            if not re.fullmatch(KV_NAME_PATTERN, store):
                raise ValueError(f"'{store}' is not a valid KV store name")
        cleaned[capability] = stripped
    return cleaned


def _validate_roles(roles: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """At least one known role, each with at least one non-blank host."""
    if not roles:
        raise ValueError("roles must not be empty")

    unknown = sorted(set(roles) - ALLOWED_ROLE_KEYS)
    if unknown:
        allowed = ", ".join(sorted(ALLOWED_ROLE_KEYS))
        raise ValueError(f"unknown role(s) {unknown}; allowed: {allowed}")

    cleaned: Dict[str, List[str]] = {}
    for role, hosts in roles.items():
        stripped = [host.strip() for host in hosts if host and host.strip()]
        if not stripped:
            raise ValueError(f"role '{role}' must list at least one host")
        if len(stripped) != len(hosts):
            raise ValueError(f"role '{role}' must not contain blank hosts")
        if len(set(stripped)) != len(stripped):
            raise ValueError(f"role '{role}' must not repeat a host")
        cleaned[role] = stripped
    return cleaned


class OperationStatus(str, Enum):
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"


class VaultKVCreate(BaseModel):
    """A create request: which file, the store's name, what it is for, and who reaches it.

    This service appends an entry to a values file and reports what the pipelines did with
    it. What the KV *means* — mounts, engine version, policies — is the deploy pipeline's
    business, so none of it is modelled here.
    """

    # Without an explicit example, Swagger UI synthesises one from `pattern` and renders an
    # unreadable regex-derived string in the "Example Value" box. These give it something a
    # human can edit.
    model_config = {
        "json_schema_extra": {
            "example": {
                "file": "payments",
                "kv_name": "myapp",
                "kv_description": "payments secrets",
                "roles": {"read": ["app01.corp.example.com"]},
            }
        }
    }

    file: str = Field(
        ...,
        max_length=128,
        pattern=FILE_PATTERN,
        examples=["payments"],
        description=(
            "Values file the store is added to, committed as '<values dir>/<file>.yaml'. "
            "The file may already hold other stores; this one is appended to its kvStores "
            "list. Created if it does not exist yet."
        ),
    )

    kv_name: str = Field(
        ...,
        max_length=128,
        pattern=KV_NAME_PATTERN,
        examples=["myapp"],
        description=(
            "Name of the KV store. Must be unique across every file in the values "
            "directory, and may not contain a slash."
        ),
    )

    kv_description: str = Field(
        ...,
        min_length=1,
        max_length=256,
        examples=["payments secrets"],
        description="What this KV store is for. Recorded in the committed entry.",
    )

    roles: Dict[str, List[str]] = Field(
        ...,
        examples=[{"read": ["app01.corp.example.com"]}],
        description=(
            "Hosts granted each role. At least one role with at least one host is "
            f"required. Allowed roles: {', '.join(sorted(ALLOWED_ROLE_KEYS))}."
        ),
    )

    @field_validator("kv_description")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("kv_description must not be blank")
        return v.strip()

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, v: Dict[str, List[str]]) -> Dict[str, List[str]]:
        return _validate_roles(v)


class VaultKVUpdate(BaseModel):
    """Editable fields. The name is not one of them — renaming is a Vault migration.

    `file` and `kv_name` come from the URL path; the body carries only the change.
    """

    model_config = {
        "json_schema_extra": {
            "example": {
                "kv_description": "payments secrets, rotated quarterly",
                "roles": {"read": ["app01.corp.example.com", "app02.corp.example.com"]},
            }
        }
    }

    kv_description: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=256,
        examples=["payments secrets, rotated quarterly"],
        description="Replacement description.",
    )

    roles: Optional[Dict[str, List[str]]] = Field(
        default=None,
        examples=[{"read": ["app01.corp.example.com"]}],
        description=(
            "Replacement roles. Replaces the existing mapping wholesale rather than "
            "merging into it, so a host can be removed by omitting it."
        ),
    )

    @field_validator("kv_description")
    @classmethod
    def not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("kv_description must not be blank")
        return v.strip() if v is not None else None

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, v: Optional[Dict[str, List[str]]]) -> Optional[Dict[str, List[str]]]:
        return _validate_roles(v) if v is not None else None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "VaultKVUpdate":
        """An empty edit would open a pull request that changes nothing."""
        if self.kv_description is None and self.roles is None:
            raise ValueError("provide at least one of 'kv_description' or 'roles'")
        return self


# --------------------------------------------------------------------------- #
# Kubernetes auth roles
#
# A role lives under a second top-level key in the *same* values file as the stores it
# reaches, so the two are edited together: one file, one optimistic-lock token, one pull
# request, one deploy diff per app.
# --------------------------------------------------------------------------- #
class VaultKubernetesAuthCreate(BaseModel):
    """A create request: which file, the role's name, which workloads, and what they reach.

    `access` names KV stores and a capability. It is not a policy: what
    `(store, capability)` means in Vault — the mount, the HCL, the policy name — is the
    deploy pipeline's business, and none of it is modelled here.
    """

    model_config = {
        "json_schema_extra": {
            "example": {
                "file": "payments",
                "role_name": "myapp-ci",
                "role_description": "CI deployer for the payments app",
                "cluster": "prod-il-1",
                "service_accounts": ["vault-reader"],
                "namespaces": ["payments"],
                "access": {"read": ["myapp"]},
                "ttl": "24h",
            }
        }
    }

    file: str = Field(
        ...,
        max_length=128,
        pattern=FILE_PATTERN,
        examples=["payments"],
        description=(
            "Values file the role is added to, committed as '<values dir>/<file>.yaml' — "
            "the same file the KV stores live in. Created if it does not exist yet."
        ),
    )

    role_name: str = Field(
        ...,
        max_length=128,
        pattern=K8S_ROLE_NAME_PATTERN,
        examples=["myapp-ci"],
        description=(
            "Name of the Kubernetes auth role. Unique within its file, and unique per "
            "cluster across the values directory. May not contain a slash."
        ),
    )

    role_description: str = Field(
        ...,
        min_length=1,
        max_length=256,
        examples=["CI deployer for the payments app"],
        description="What this role is for. Recorded in the committed entry.",
    )

    cluster: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=K8S_CLUSTER_PATTERN,
        examples=["prod-il-1"],
        description=(
            "Which Kubernetes auth mount the role belongs to. Optional: an estate with a "
            "single mount has no cluster to name. Omitted from the committed entry when "
            "absent, and not editable afterwards."
        ),
    )

    service_accounts: List[str] = Field(
        ...,
        examples=[["vault-reader"]],
        description=(
            "ServiceAccount names allowed to assume this role. At least one is required; "
            "'*' is rejected."
        ),
    )

    namespaces: List[str] = Field(
        ...,
        examples=[["payments"]],
        description=(
            "Namespaces those service accounts may come from. At least one is required; "
            "'*' is rejected."
        ),
    )

    access: Dict[str, List[str]] = Field(
        ...,
        examples=[{"read": ["myapp"]}],
        description=(
            "KV stores this role reaches, per capability. Every store named must already "
            "exist in the values directory. Allowed capabilities: "
            f"{', '.join(sorted(ALLOWED_KV_ACCESS_KEYS))}."
        ),
    )

    ttl: Optional[str] = Field(
        default=None,
        max_length=32,
        pattern=DURATION_PATTERN,
        examples=["24h"],
        description="Optional token TTL, as a Vault duration string such as '24h'.",
    )

    @field_validator("role_description")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("role_description must not be blank")
        return v.strip()

    @field_validator("service_accounts")
    @classmethod
    def valid_service_accounts(cls, v: List[str]) -> List[str]:
        return _validate_bound_names(
            v,
            "service_accounts",
            K8S_SERVICE_ACCOUNT_PATTERN,
            K8S_SERVICE_ACCOUNT_MAX_LENGTH,
        )

    @field_validator("namespaces")
    @classmethod
    def valid_namespaces(cls, v: List[str]) -> List[str]:
        return _validate_bound_names(
            v, "namespaces", K8S_NAMESPACE_PATTERN, K8S_NAMESPACE_MAX_LENGTH
        )

    @field_validator("access")
    @classmethod
    def valid_access(cls, v: Dict[str, List[str]]) -> Dict[str, List[str]]:
        return _validate_access(v)


class VaultKubernetesAuthUpdate(BaseModel):
    """Editable fields. Neither the name nor the cluster is one of them.

    Both are part of the role's identity in Vault, so changing either is a delete plus a
    create — the same reasoning that makes `kv_name` immutable. `file` and `role_name` come
    from the URL path; the body carries only the change.
    """

    model_config = {
        "json_schema_extra": {
            "example": {
                "namespaces": ["payments", "payments-staging"],
                "access": {"read": ["myapp"]},
            }
        }
    }

    role_description: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=256,
        examples=["CI deployer for the payments app"],
        description="Replacement description.",
    )

    service_accounts: Optional[List[str]] = Field(
        default=None,
        examples=[["vault-reader"]],
        description=(
            "Replacement service accounts. Replaces the existing list wholesale rather "
            "than merging into it, so one can be removed by omitting it."
        ),
    )

    namespaces: Optional[List[str]] = Field(
        default=None,
        examples=[["payments", "payments-staging"]],
        description="Replacement namespaces. Replaces the existing list wholesale.",
    )

    access: Optional[Dict[str, List[str]]] = Field(
        default=None,
        examples=[{"read": ["myapp"]}],
        description=(
            "Replacement KV store access. Replaces the existing mapping wholesale, and "
            "every store named must exist."
        ),
    )

    ttl: Optional[str] = Field(
        default=None,
        max_length=32,
        pattern=DURATION_PATTERN,
        examples=["24h"],
        description="Replacement token TTL.",
    )

    @field_validator("role_description")
    @classmethod
    def not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("role_description must not be blank")
        return v.strip() if v is not None else None

    @field_validator("service_accounts")
    @classmethod
    def valid_service_accounts(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        return _validate_bound_names(
            v,
            "service_accounts",
            K8S_SERVICE_ACCOUNT_PATTERN,
            K8S_SERVICE_ACCOUNT_MAX_LENGTH,
        )

    @field_validator("namespaces")
    @classmethod
    def valid_namespaces(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        return _validate_bound_names(
            v, "namespaces", K8S_NAMESPACE_PATTERN, K8S_NAMESPACE_MAX_LENGTH
        )

    @field_validator("access")
    @classmethod
    def valid_access(cls, v: Optional[Dict[str, List[str]]]) -> Optional[Dict[str, List[str]]]:
        return _validate_access(v) if v is not None else None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "VaultKubernetesAuthUpdate":
        """An empty edit would open a pull request that changes nothing."""
        if all(
            value is None
            for value in (
                self.role_description,
                self.service_accounts,
                self.namespaces,
                self.access,
                self.ttl,
            )
        ):
            raise ValueError(
                "provide at least one of 'role_description', 'service_accounts', "
                "'namespaces', 'access' or 'ttl'"
            )
        return self


# --------------------------------------------------------------------------- #
# responses
# --------------------------------------------------------------------------- #
class PullRequestInfo(BaseModel):
    id: int = Field(..., description="Bitbucket pull-request id.")
    url: Optional[str] = Field(default=None, description="Browser link to the pull request.")
    state: str = Field(..., description="Pull-request state as reported by Bitbucket.")


class PipelineInfo(BaseModel):
    number: int = Field(..., description="Woodpecker pipeline number.")
    status: str = Field(..., description="Terminal Woodpecker status, e.g. success/failure.")
    link: Optional[str] = Field(default=None, description="Browser link to the pipeline.")


class VaultOperationResponse(BaseModel):
    """Outcome of the chain: pull request -> validation CI -> merge -> deploy CI.

    One model for both resource kinds. A second one would double the trap below: a field
    added here is silently omitted from every failure body unless
    `VaultOperationError.to_response()` is updated too, because failures are *returned* as
    a JSONResponse rather than validated against this model.

    `kv_name` and `role_name` both default to `""` so each route fills in the coordinate it
    has. Putting a role name in `kv_name` would be a lie every consumer had to learn.
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

    role_name: str = Field(
        default="", description="The Kubernetes auth role this request acted on, if any."
    )

    pull_request: Optional[PullRequestInfo] = Field(
        default=None, description="The pull request opened for this change."
    )

    validation_pipeline: Optional[PipelineInfo] = Field(
        default=None, description="The pull-request pipeline that gates the merge."
    )

    deploy_pipeline: Optional[PipelineInfo] = Field(
        default=None, description="The post-merge pipeline that applies the change."
    )

    error: Optional[str] = Field(
        default=None, description="Failure detail when status is Failed."
    )
