from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from tashtiot_apis_library import InfraOperationRequest

from .conf import config

# Vault mount paths and policy names end up in HCL and in URLs, so keep them to a
# conservative slug: lowercase alphanumerics separated by single dashes.
APP_NAME_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"

# Vault duration strings, e.g. "720h", "30m", "10d".
DURATION_PATTERN = r"^[0-9]+(s|m|h|d)$"


class KVVersion(int, Enum):
    V1 = 1
    V2 = 2


class OperationStatus(str, Enum):
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"


class VaultKVCreateSpec(BaseModel):
    app_name: str = Field(
        ...,
        max_length=40,
        pattern=APP_NAME_PATTERN,
        description="The application the KV mount belongs to. Becomes the last segment of the mount path.",
    )

    owner: str = Field(
        ...,
        max_length=128,
        description="Owner of the mount (team distribution list or user), recorded in the values file.",
    )

    kv_version: KVVersion = Field(
        default=KVVersion.V2,
        description="KV secrets engine version. v2 (versioned) unless you have a reason not to.",
    )

    description: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Free-text description applied to the Vault mount.",
    )

    max_versions: int = Field(
        default=config.DEFAULT_KV_MAX_VERSIONS,
        ge=1,
        le=100,
        description="KV-v2 only: how many versions of a secret Vault retains.",
    )

    delete_version_after: Optional[str] = Field(
        default=config.DEFAULT_DELETE_VERSION_AFTER,
        pattern=DURATION_PATTERN,
        description="KV-v2 only: Vault duration string after which a version is deleted, e.g. '720h'.",
    )

    readers: List[str] = Field(
        default_factory=list,
        description="Identities/groups granted the generated read policy.",
    )

    writers: List[str] = Field(
        default_factory=list,
        description="Identities/groups granted the generated read/write policy.",
    )

    @field_validator("readers", "writers")
    @classmethod
    def no_blank_entities(cls, v: List[str]) -> List[str]:
        """Blank entries would render an empty binding into the policy file."""
        cleaned = [item.strip() for item in v if item and item.strip()]
        if len(cleaned) != len(v):
            raise ValueError("entity lists must not contain empty values")
        return cleaned


class VaultKVCreate(InfraOperationRequest):
    """`metadata` (the six infra coordinates) comes from InfraOperationRequest."""

    spec: VaultKVCreateSpec


# --------------------------------------------------------------------------- #
# edits to an existing mount
#
# `app_name` comes from the URL path on these; the body carries the infra coordinates
# (for `metadata.environment`) plus the change itself.
# --------------------------------------------------------------------------- #
class PolicyCapability(str, Enum):
    """Which of the mount's two generated policies an edit refers to."""

    READ = "read"
    WRITE = "write"


def _clean_entries(values: List[str], label: str) -> List[str]:
    cleaned = [item.strip() for item in values if item and item.strip()]
    if len(cleaned) != len(values):
        raise ValueError(f"{label} must not contain empty values")
    return cleaned


class VaultKVUpdateSpec(BaseModel):
    """Editable labels. The mount path and policy names are deliberately not editable."""

    description: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Replacement free-text description for the Vault mount.",
    )

    owner: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Replacement owner recorded in the values file's metadata.",
    )

    @model_validator(mode="after")
    def at_least_one_field(self) -> "VaultKVUpdateSpec":
        """An empty edit would open a pull request that changes nothing."""
        if self.description is None and self.owner is None:
            raise ValueError("provide at least one of 'description' or 'owner'")
        return self


class VaultKVUpdate(InfraOperationRequest):
    spec: VaultKVUpdateSpec


class VaultKVKubernetesAuthSpec(BaseModel):
    """A Vault Kubernetes auth role bound to one of the mount's policies."""

    role: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=APP_NAME_PATTERN,
        description="Role name. Defaults to '{team}-{environment}-{app}'.",
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
        description="Whether the role gets the mount's read or write policy.",
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


class VaultKVKubernetesAuth(InfraOperationRequest):
    spec: VaultKVKubernetesAuthSpec


class VaultKVGroupBindingSpec(BaseModel):
    """An AD group granted one of the mount's two policies."""

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
        """A whitespace-only group would render an empty binding into the policy file."""
        if not v.strip():
            raise ValueError("group must not be blank")
        return v.strip()


class VaultKVGroupBinding(InfraOperationRequest):
    spec: VaultKVGroupBindingSpec


class PullRequestInfo(BaseModel):
    id: int = Field(..., description="Bitbucket pull-request id.")
    url: Optional[str] = Field(default=None, description="Browser link to the pull request.")
    state: str = Field(..., description="Pull-request state as reported by Bitbucket.")


class PipelineInfo(BaseModel):
    number: int = Field(..., description="Woodpecker pipeline number.")
    status: str = Field(..., description="Terminal Woodpecker status, e.g. success/failure.")
    link: Optional[str] = Field(default=None, description="Browser link to the pipeline.")


class VaultKVOperationResponse(BaseModel):
    """Outcome of the full create flow: PR -> validation CI -> merge -> deploy CI."""

    status: OperationStatus = Field(..., description="Succeeded or Failed.")

    message: str = Field(
        ...,
        description="Human-readable outcome, e.g. 'Successful creation of kingmagen/prod/myapp'.",
    )

    mount_path: str = Field(..., description="The Vault KV mount path this request creates.")

    policies: List[str] = Field(
        default_factory=list, description="Names of the Vault policies created alongside the mount."
    )

    pull_request: Optional[PullRequestInfo] = Field(
        default=None, description="The pull request opened for this change."
    )

    validation_pipeline: Optional[PipelineInfo] = Field(
        default=None, description="The pull-request pipeline that gates the merge."
    )

    deploy_pipeline: Optional[PipelineInfo] = Field(
        default=None, description="The post-merge pipeline that applies the change to Vault."
    )

    error: Optional[str] = Field(
        default=None, description="Failure detail when status is Failed."
    )
