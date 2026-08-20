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
        default="KVs",
        description="Directory inside the values repo holding the KV store directories.",
    )

    TASHTIOT_DIR: str = Field(
        default="Tashtiot",
        description="Sub-directory of the values dir holding the per-team access files for "
        "the shared services store. One flat '<team>.yaml' each, not a store directory.",
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

    # --- Git (deleting a file, the one operation Bitbucket has no REST endpoint for) ---
    # All defaulted: the config singletons are built at import time, so a new *required*
    # setting would break every test at collection.
    GIT_AUTHOR_NAME: str = Field(
        default="vault-api",
        description="Author/committer name on the commit that removes a values file.",
    )

    GIT_AUTHOR_EMAIL: str = Field(
        default="vault-api@noreply.invalid",
        description="Author/committer email on the commit that removes a values file.",
    )

    GIT_TIMEOUT_SECONDS: float = Field(
        default=120.0,
        description="Budget for a single git invocation (fetch, commit, push). Bounds one "
        "command, not the whole delete — the CI_* settings still bound the waiting.",
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
