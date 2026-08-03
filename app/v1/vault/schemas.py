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


class VaultKVOperationResponse(BaseModel):
    """Outcome of the chain: pull request -> validation CI -> merge -> deploy CI."""

    status: OperationStatus = Field(..., description="Succeeded or Failed.")

    message: str = Field(
        ...,
        description="Human-readable outcome, e.g. 'Successful creation of myapp'.",
    )

    file: str = Field(
        default="", description="The values file this request acted on."
    )

    kv_name: str = Field(..., description="The KV store this request acted on.")

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
