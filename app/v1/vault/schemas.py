from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# A single name segment: lowercase alphanumerics separated by single dashes.
_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"

# Callers name their own KVs, so a name may be a multi-segment path such as
# `payments/vault-secrets`. Built from _SEGMENT joined by single slashes, which makes this
# the path-traversal guard as well as a style rule: no leading or trailing slash, no empty
# segment, and `..` cannot match. That matters because the name lands in the values file
# path and in the request URL.
KV_NAME_PATTERN = rf"^{_SEGMENT}(?:/{_SEGMENT})*$"

# Vault role names cannot contain slashes, so they stay a single segment.
ROLE_NAME_PATTERN = rf"^{_SEGMENT}$"

# Vault duration strings, e.g. "720h", "30m", "10d".
DURATION_PATTERN = r"^[0-9]+(s|m|h|d)$"


class OperationStatus(str, Enum):
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"


class VaultKVCreate(BaseModel):
    """A create request: the name and what it is for. Nothing else.

    This service writes a file to the values repo and reports what the pipelines did with
    it. What the KV *means* — mounts, policies, engine version — is the deploy pipeline's
    business, so none of it is modelled here.
    """

    kv_name: str = Field(
        ...,
        max_length=128,
        pattern=KV_NAME_PATTERN,
        description=(
            "Name of the KV. Used verbatim, and may be a multi-segment path such as "
            "'payments/vault-secrets'. Becomes the committed file name."
        ),
    )

    kv_description: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="What this KV is for. Recorded in the committed file.",
    )

    @field_validator("kv_description")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("kv_description must not be blank")
        return v.strip()


# --------------------------------------------------------------------------- #
# edits to an existing KV
#
# `kv_name` comes from the URL path on these; the body carries only the change.
# --------------------------------------------------------------------------- #
class PolicyCapability(str, Enum):
    """Which of a KV's two policies an edit refers to, when the file has policies."""

    READ = "read"
    WRITE = "write"


def _clean_entries(values: List[str], label: str) -> List[str]:
    cleaned = [item.strip() for item in values if item and item.strip()]
    if len(cleaned) != len(values):
        raise ValueError(f"{label} must not contain empty values")
    return cleaned


class VaultKVUpdate(BaseModel):
    """Editable fields. The name is not one of them — renaming is a Vault migration."""

    kv_description: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Replacement description.",
    )

    owner: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Owner recorded in the file, if the file carries one.",
    )

    @model_validator(mode="after")
    def at_least_one_field(self) -> "VaultKVUpdate":
        """An empty edit would open a pull request that changes nothing."""
        if self.kv_description is None and self.owner is None:
            raise ValueError("provide at least one of 'kv_description' or 'owner'")
        return self


class VaultKVKubernetesAuth(BaseModel):
    """A Vault Kubernetes auth role bound to one of the KV's policies."""

    role: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=ROLE_NAME_PATTERN,
        description="Role name. Defaults to the KV name flattened to a single token.",
    )

    service_accounts: List[str] = Field(
        ...,
        min_length=1,
        description="Kubernetes service account names allowed to assume the role.",
    )

    namespaces: List[str] = Field(
        ...,
        min_length=1,
        description="Kubernetes namespaces the service accounts must live in.",
    )

    capability: PolicyCapability = Field(
        default=PolicyCapability.READ,
        description="Whether the role gets the read or the write policy.",
    )

    ttl: Optional[str] = Field(
        default=None,
        pattern=DURATION_PATTERN,
        description="Token TTL for the role, e.g. '24h'.",
    )

    @field_validator("service_accounts", "namespaces")
    @classmethod
    def no_blank_entries(cls, v: List[str]) -> List[str]:
        return _clean_entries(v, "service_accounts/namespaces")


class VaultKVGroupBinding(BaseModel):
    """An AD group granted one of the KV's policies."""

    group: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="AD group as Vault records it, e.g. 'AD\\\\payments-readers'.",
    )

    capability: PolicyCapability = Field(
        ...,
        description="Whether the group is granted read or read/write access.",
    )

    @field_validator("group")
    @classmethod
    def not_blank(cls, v: str) -> str:
        """A whitespace-only group would render an empty binding into the file."""
        if not v.strip():
            raise ValueError("group must not be blank")
        return v.strip()


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

    kv_name: str = Field(..., description="The KV this request acted on.")

    policies: List[str] = Field(
        default_factory=list,
        description="Policy names found in the committed file, when it has any.",
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
