# ─────────────────────────────────────────────────────────────────────────────
#   Settings for the Vault KV v1 API.
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class VaultV1StaticSettings(BaseSettings):
    """Settings for the Vault KV v1 API (GitOps via Bitbucket pull requests)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    API_PREFIX: str = Field(
        description="Root path under which the API is served",
        default="/api/vault/v1/kv",
    )

    API_TAGS: List[str] = Field(
        description="Tags used for OpenAPI documentation grouping",
        default_factory=lambda: ["v1 - Vault KV Operations"],
    )

    # --- The GitOps repo holding the Vault values files ---
    VAULT_VALUES_REPO_PROJECT_KEY: str = Field(
        ..., description="Bitbucket project key of the Vault values repo."
    )

    VAULT_VALUES_REPO_SLUG: str = Field(
        ..., description="Bitbucket repository slug of the Vault values repo."
    )

    VAULT_VALUES_REPO_BASE_BRANCH: str = Field(
        default="master",
        description="Branch the pull requests target and the deploy pipeline runs on.",
    )

    VAULT_VALUES_DIR: str = Field(
        default="kv",
        description="Directory inside the values repo holding the KV mount definitions.",
    )

    # --- Pull request shaping ---
    BRANCH_PREFIX: str = Field(
        default="vault-kv",
        description="Prefix for the short-lived branch a create request commits to.",
    )

    # A separate prefix so a reviewer can tell the kind of change from the branch name
    # alone, before opening the diff — the file and store are the same either way, so
    # without it a binding change and a store change are indistinguishable.
    K8S_SA_BRANCH_PREFIX: str = Field(
        default="vault-k8s-sa",
        description="Prefix for the branch a Kubernetes service account change commits to.",
    )

    PR_REVIEWERS: List[str] = Field(
        default_factory=list,
        description="Bitbucket usernames added as reviewers on every opened pull request.",
    )

    # --- CI gates ---
    # Read from Bitbucket's build-status store (the pull request's Builds tab), not from
    # the CI server, so there is no CI repo id or token to configure. The names still say
    # PIPELINE because that is what is being waited on; only the place we ask has changed.
    CI_POLL_INTERVAL_SECONDS: float = Field(
        default=5.0,
        description="How often to re-read a commit's build statuses from Bitbucket.",
    )

    CI_PIPELINE_START_TIMEOUT_SECONDS: float = Field(
        default=120.0,
        description="How long to wait for the first build to be reported against a commit.",
    )

    CI_PIPELINE_TIMEOUT_SECONDS: float = Field(
        default=900.0,
        description="How long to wait for a commit's builds to leave INPROGRESS.",
    )


config = VaultV1StaticSettings()
