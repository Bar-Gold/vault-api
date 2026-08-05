from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class VaultApiStaticSettings(BaseSettings):
    """Cross-cutting settings shared by the whole app.

    Per-module knobs (routing prefix, repo layout, CI timeouts) live in
    `app/v{n}/<service>/conf.py`. Auth settings (`AUTH_*`, `AUTH_SSO_*`) are not declared
    here — the library reads those straight from the environment.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    API_TITLE: str = Field(
        description="API title for the swagger",
        default="Vault API",
    )

    APP_VERSION: str = Field(
        default="v1.0.0",
        description="Release version shown in the Swagger UI; injected at image build time from the git tag (see README).",
    )

    # --- Bitbucket (the GitOps repo that holds the Vault values files) ---
    BITBUCKET_URL: str = Field(
        description="Base URL of Bitbucket Server / Data Center (no trailing path).",
    )

    BITBUCKET_TOKEN: str = Field(
        description="HTTP access token for the Bitbucket REST API (branch, file, pull-request "
        "and build-status operations).",
    )

    # There are deliberately no CI-server settings here. The CI gates read the build
    # statuses the pipelines post *into Bitbucket* — the pull request's Builds tab — so
    # Bitbucket is the only upstream this service talks to.

    # --- Outbound HTTP ---
    HTTP_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        description="Per-request timeout for the Bitbucket client. This bounds a single call, "
        "not the overall flow — the CI_* settings bound the waiting.",
    )

    VERIFY_SSL: bool = Field(
        default=True,
        description="Verify TLS certificates on outbound calls. BaseAPI defaults this to False; "
        "we opt back in. Set false only for local dev against self-signed certs.",
    )


global_config = VaultApiStaticSettings()
