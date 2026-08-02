from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# A single name segment: lowercase alphanumerics separated by single dashes.
_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"

# Callers name their own KVs, so a name may be a multi-segment path such as
# `payments/vault-secrets`. Built from _SEGMENT joined by single slashes, which makes this
# the path-traversal guard as well as a style rule: no leading or trailing slash, no empty
# segment, and `..` cannot match. That matters because the name lands in the values file
# path and in the request URL.
KV_NAME_PATTERN = rf"^{_SEGMENT}(?:/{_SEGMENT})*$"


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


class VaultKVUpdate(BaseModel):
    """Editable fields. The name is not one of them — renaming is a Vault migration.

    `kv_name` comes from the URL path; the body carries only the change.
    """

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
