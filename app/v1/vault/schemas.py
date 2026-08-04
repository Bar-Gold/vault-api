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
# Kubernetes service accounts
#
# A binding is three scalars — `serviceAccount`, `namespace`, `cluster` — living inside the
# store it reaches. It has no name, so those three *are* its identity, and it carries no
# capability: what a bound workload may do with the store is the deploy pipeline's
# business, the same boundary `roles` sits behind. No policy name, policy body, mount path
# or engine version is generated anywhere in this service.
# --------------------------------------------------------------------------- #

# The OS4 cluster suffix the binding belongs to, e.g. `dev`. Required — unlike the rest of
# the triple there is no Kubernetes rule to borrow, so this is our own single-segment shape.
K8S_CLUSTER_PATTERN = rf"^{_SEGMENT}$"
K8S_CLUSTER_MAX_LENGTH = 128

# Kubernetes' own naming rules, not ours. A namespace is an RFC 1123 *label* (no dots, 63
# chars); a ServiceAccount name is an RFC 1123 *subdomain* (dots allowed, 253) — genuinely
# different limits. Neither may contain uppercase, so a permissive hostname-shaped pattern
# would let a request pass here and be rejected at admission in the cluster instead.
K8S_NAMESPACE_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
K8S_NAMESPACE_MAX_LENGTH = 63
K8S_SERVICE_ACCOUNT_PATTERN = (
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)
K8S_SERVICE_ACCOUNT_MAX_LENGTH = 253


def _validate_bound_name(
    name: str, field: str, pattern: str, max_length: int
) -> str:
    """One Kubernetes name: non-blank, no wildcard, RFC 1123.

    Singular, because the format repeats the whole triple per binding rather than crossing
    a list of accounts with a list of namespaces — two namespaces is two entries.
    """
    stripped = name.strip() if isinstance(name, str) else name
    if not stripped:
        raise ValueError(f"{field} must not be blank")
    if "*" in stripped:
        raise ValueError(
            f"{field} must not contain '*': a wildcard binds every workload in the "
            f"cluster to this store. Name it explicitly, or have a human make that grant "
            f"by editing the values file directly."
        )
    if len(stripped) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    if not re.fullmatch(pattern, stripped):
        raise ValueError(
            f"'{stripped}' is not a valid {field}; Kubernetes names are lowercase "
            f"alphanumerics, dashes and (service accounts only) dots"
        )
    return stripped


def _validate_roles(roles: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """At least one known role, each naming at least one non-blank principal.

    A principal is whatever the deploy pipeline binds to a capability — the values files
    carry LDAP distinguished names (`CN=…,OU=…,DC=…`) as well as bare hostnames, so the
    entries are deliberately **unvalidated beyond non-blank and unique**. Any pattern
    tight enough to describe one form rejects the other, and this service does not own
    that vocabulary.
    """
    if not roles:
        raise ValueError("roles must not be empty")

    unknown = sorted(set(roles) - ALLOWED_ROLE_KEYS)
    if unknown:
        allowed = ", ".join(sorted(ALLOWED_ROLE_KEYS))
        raise ValueError(f"unknown role(s) {unknown}; allowed: {allowed}")

    cleaned: Dict[str, List[str]] = {}
    for role, principals in roles.items():
        stripped = [
            principal.strip() for principal in principals if principal and principal.strip()
        ]
        if not stripped:
            raise ValueError(f"role '{role}' must list at least one principal")
        if len(stripped) != len(principals):
            raise ValueError(f"role '{role}' must not contain blank principals")
        if len(set(stripped)) != len(stripped):
            raise ValueError(f"role '{role}' must not repeat a principal")
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
                "file": "athena",
                "kv_name": "athena-passwords",
                "kv_description": "Passwords for athena",
                "roles": {
                    "write": ["CN=svc-athena,OU=ServiceAccounts,DC=corp,DC=example,DC=com"]
                },
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
        examples=[{"write": ["CN=svc-athena,OU=ServiceAccounts,DC=corp,DC=example,DC=com"]}],
        description=(
            "Principals granted each role — an LDAP distinguished name or a hostname, "
            "whichever the deploy pipeline expects. At least one role with at least one "
            f"principal is required. Allowed roles: {', '.join(sorted(ALLOWED_ROLE_KEYS))}."
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
                "kv_description": "Passwords for athena, rotated quarterly",
                "roles": {
                    "write": ["CN=svc-athena,OU=ServiceAccounts,DC=corp,DC=example,DC=com"]
                },
            }
        }
    }

    kv_description: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=256,
        examples=["Passwords for athena, rotated quarterly"],
        description="Replacement description.",
    )

    roles: Optional[Dict[str, List[str]]] = Field(
        default=None,
        examples=[
            {"write": ["CN=svc-athena,OU=ServiceAccounts,DC=corp,DC=example,DC=com"]}
        ],
        description=(
            "Replacement roles. Replaces the existing mapping wholesale rather than "
            "merging into it, so a principal can be removed by omitting it. Does not "
            "touch the store's k8sServiceAccounts."
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
# Kubernetes service accounts
#
# A binding is added to a store that already exists, so this request carries no `file` or
# `kv_name` — both come from the URL path. There is no update model: an entry is three
# scalars that together *are* its identity, so "editing" one is removing it and adding
# another, and a PATCH would have nothing left to change.
# --------------------------------------------------------------------------- #
class K8sServiceAccountCreate(BaseModel):
    """One `(serviceAccount, namespace, cluster)` binding on a KV store.

    All three are required and singular. The format repeats the whole triple per binding
    rather than crossing lists, so binding one service account in two namespaces is two
    requests — which also makes each one individually removable.

    There is no capability field: listing a service account inside a store *is* the grant,
    and what it may do with the store is the deploy pipeline's business.
    """

    model_config = {
        "json_schema_extra": {
            "example": {
                "service_account": "vault",
                "namespace": "athena",
                "cluster": "dev",
            }
        }
    }

    service_account: str = Field(
        ...,
        max_length=K8S_SERVICE_ACCOUNT_MAX_LENGTH,
        examples=["vault"],
        description=(
            "ServiceAccount name allowed to reach this store. An RFC 1123 subdomain "
            "(lowercase, dots allowed); '*' is rejected."
        ),
    )

    namespace: str = Field(
        ...,
        max_length=K8S_NAMESPACE_MAX_LENGTH,
        examples=["athena"],
        description=(
            "Namespace that service account comes from. An RFC 1123 label (lowercase, no "
            "dots); '*' is rejected."
        ),
    )

    cluster: str = Field(
        ...,
        max_length=K8S_CLUSTER_MAX_LENGTH,
        pattern=K8S_CLUSTER_PATTERN,
        examples=["dev"],
        description=(
            "OS4 cluster suffix the binding belongs to. Required — the same service "
            "account and namespace in two clusters are two distinct bindings."
        ),
    )

    @field_validator("service_account")
    @classmethod
    def valid_service_account(cls, v: str) -> str:
        return _validate_bound_name(
            v,
            "service_account",
            K8S_SERVICE_ACCOUNT_PATTERN,
            K8S_SERVICE_ACCOUNT_MAX_LENGTH,
        )

    @field_validator("namespace")
    @classmethod
    def valid_namespace(cls, v: str) -> str:
        return _validate_bound_name(
            v, "namespace", K8S_NAMESPACE_PATTERN, K8S_NAMESPACE_MAX_LENGTH
        )


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

    validation_pipeline: Optional[PipelineInfo] = Field(
        default=None, description="The pull-request pipeline that gates the merge."
    )

    deploy_pipeline: Optional[PipelineInfo] = Field(
        default=None, description="The post-merge pipeline that applies the change."
    )

    error: Optional[str] = Field(
        default=None, description="Failure detail when status is Failed."
    )
