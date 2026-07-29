import argparse

import uvicorn
from fastapi import FastAPI
from tashtiot_apis_library import general_create_app
from tashtiot_apis_library.fastapi_template.utils import BaseAPI

from .clients.bitbucket import BitbucketClient
from .clients.woodpecker import WoodpeckerClient
from .global_conf import global_config
from .v1.vault.conf import config as vault_config
from .v1.vault.routes import get_v1_vault_router


def create_app() -> FastAPI:
    # enable_auth wires the library's global AuthMiddleware, which protects every route
    # (except docs/metrics/health/probes). It only activates when AUTH_ENABLED=true and one
    # AUTH_* verification material is set; otherwise the app boots open. See README.
    app = general_create_app(
        enable_auth=True,
        title=global_config.API_TITLE,
        version=global_config.APP_VERSION,
    )

    # Configure external services objects. Connectors are built once here, never per-request.
    bitbucket_http = BaseAPI(
        global_config.BITBUCKET_URL,
        headers={"Authorization": f"Bearer {global_config.BITBUCKET_TOKEN}"},
        timeout=global_config.HTTP_TIMEOUT_SECONDS,
        verify=global_config.VERIFY_SSL,
    ).client
    bitbucket = BitbucketClient(
        bitbucket_http,
        project_key=vault_config.VAULT_VALUES_REPO_PROJECT_KEY,
        repo_slug=vault_config.VAULT_VALUES_REPO_SLUG,
    )

    woodpecker_http = BaseAPI(
        global_config.WOODPECKER_URL,
        headers={"Authorization": f"Bearer {global_config.WOODPECKER_TOKEN}"},
        timeout=global_config.HTTP_TIMEOUT_SECONDS,
        verify=global_config.VERIFY_SSL,
    ).client
    woodpecker = WoodpeckerClient(
        woodpecker_http,
        repo_id=vault_config.WOODPECKER_REPO_ID,
        poll_interval=vault_config.CI_POLL_INTERVAL_SECONDS,
        start_timeout=vault_config.CI_PIPELINE_START_TIMEOUT_SECONDS,
        completion_timeout=vault_config.CI_PIPELINE_TIMEOUT_SECONDS,
    )

    # Add routes to app
    app.include_router(get_v1_vault_router(bitbucket, woodpecker))

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Vault API.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=5000, help="Bind port (default: 5000).")
    args = parser.parse_args()

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)
