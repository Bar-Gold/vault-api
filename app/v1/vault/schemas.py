from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator
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
