import argparse

import uvicorn
from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from tashtiot_apis_library import general_create_app
from tashtiot_apis_library.fastapi_template._internal.utils import settings as library_settings
from tashtiot_apis_library.fastapi_template.utils import BaseAPI

from .clients.bitbucket import BitbucketClient
from .clients.woodpecker import WoodpeckerClient
from .global_conf import global_config
from .v1.vault.conf import config as vault_config
from .v1.vault.routes import get_v1_vault_router

DOCS_PATH = "/docs"


def _replace_docs_route(app: FastAPI) -> None:
    """Re-register /docs with the spec validator turned off.

    Swagger UI's own default for `validatorUrl` is `https://validator.swagger.io/validator`,
    and the library's /docs route does not override it (neither do FastAPI's defaults). The
    browser therefore calls out to a public host on every page load: it hands our spec URL
    to a third party, and on a network that cannot route there the page hangs until the
    request times out — which is the whole of the "Swagger is slow" symptom.

    The library registers its own /docs, which bypasses FastAPI's `swagger_ui_parameters`,
    so the route has to be swapped rather than configured. The static/openapi URLs are read
    back from the library's settings because `PROXY_LISTEN_PATH` rewrites them at import
    time — hardcoding `/static/swagger` would break the docs behind a proxy.

    The library's route is nested inside an included sub-router rather than sitting at the
    top level, so filtering `app.router.routes` by path does not find it. Starlette matches
    in registration order and takes the first hit, so instead of reaching into the library's
    internals we register ours and move it to the front, where it wins outright. That holds
    however the library chooses to nest things.
    """

    @app.get(DOCS_PATH, include_in_schema=False)
    async def swagger_ui_without_validator():
        return get_swagger_ui_html(
            title="Swagger UI",
            swagger_js_url=f"{library_settings.SWAGGER_STATIC_FILES}/swagger-ui-bundle.js",
            swagger_css_url=f"{library_settings.SWAGGER_STATIC_FILES}/swagger-ui.css",
            swagger_favicon_url=f"{library_settings.SWAGGER_STATIC_FILES}/favicon.ico",
            openapi_url=library_settings.SWAGGER_OPENAPI_JSON_URL,
            swagger_ui_parameters={"validatorUrl": None},
        )

    app.router.routes.insert(0, app.router.routes.pop())


def create_app() -> FastAPI:
    # enable_auth wires the library's global AuthMiddleware, which protects every route
    # (except docs/metrics/health/probes). It only activates when AUTH_ENABLED=true and one
    # AUTH_* verification material is set; otherwise the app boots open. See README.
    app = general_create_app(
        enable_auth=True,
        title=global_config.API_TITLE,
        version=global_config.APP_VERSION,
    )

    # Must run after general_create_app, which is what registers the route being replaced.
    _replace_docs_route(app)

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
