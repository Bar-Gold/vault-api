# ─────────────────────────────────────────────────────────────────────────────
#   Settings for the Vault KV v1 API.
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class VaultV1StaticSettings(BaseSettings):
    """Settings for the Vault KV v1 API (GitOps via Bitbucket pull requests)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    API_DESCRIPTION: str = Field(
        description="Human-readable description of the Vault KV API",
        default="Create and read HashiCorp Vault KV mounts and their policies via GitOps.",
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

    PR_REVIEWERS: List[str] = Field(
        default_factory=list,
        description="Bitbucket usernames added as reviewers on every opened pull request.",
    )

    # --- Woodpecker CI ---
    WOODPECKER_REPO_ID: str = Field(
        ...,
        description="Woodpecker's numeric repo id (or owner/name) for the Vault values repo.",
    )

    CI_POLL_INTERVAL_SECONDS: float = Field(
        default=5.0,
        description="How often to poll Woodpecker for pipeline state.",
    )

    CI_PIPELINE_START_TIMEOUT_SECONDS: float = Field(
        default=120.0,
        description="How long to wait for a pipeline to appear after opening/merging the PR.",
    )

    CI_PIPELINE_TIMEOUT_SECONDS: float = Field(
        default=900.0,
        description="How long to wait for a running pipeline to reach a terminal status.",
    )

    # --- Defaults for the created mount ---
    DEFAULT_KV_MAX_VERSIONS: int = Field(
        default=10,
        description="Default max_versions on a new KV-v2 mount when the request omits it.",
    )

    DEFAULT_DELETE_VERSION_AFTER: Optional[str] = Field(
        default=None,
        description="Default Vault duration string (e.g. '720h') after which versions are deleted.",
    )


config = VaultV1StaticSettings()
