import argparse
import json
from typing import List

import uvicorn
from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from tashtiot_apis_library import general_create_app
from tashtiot_apis_library.fastapi_template._internal.utils import settings as library_settings
from tashtiot_apis_library.fastapi_template.utils import BaseAPI

from .clients.bitbucket import BitbucketClient
from .clients.git import GitFileRemover
from .global_conf import global_config
from .v1.vault.conf import config as vault_config
from .v1.vault.routes import get_v1_vault_router

DOCS_PATH = "/docs"

# How far the grouped sections sit in from the heading drawn over them.
_GROUP_INDENT = "24px"


def _tag_group_css(heading: str, tags: List[str]) -> str:
    """One heading over several Swagger sections, which then read as its sub-headings.

    Swagger UI has no nested tags (and ignores Redoc's `x-tagGroups`), so this is drawn
    rather than declared: a `::before` on the first grouped section carries the heading,
    and every grouped section is indented beneath it with a smaller title. Each section is
    `.opblock-tag-section > h3.opblock-tag[data-tag=…]`, found with `:has()`.

    Pure decoration: a browser without `:has()` simply shows the sections ungrouped, and
    nothing about the spec or the routes depends on it. `json.dumps` quotes the names, which
    is also a valid CSS string for the plain text a tag is.
    """
    sections = [
        f".opblock-tag-section:has(> h3.opblock-tag[data-tag={json.dumps(tag)}])"
        for tag in tags
    ]
    return (
        f"{', '.join(sections)} {{ padding-left: {_GROUP_INDENT}; }}\n"
        f"{', '.join(f'{s} > h3.opblock-tag' for s in sections)} {{ font-size: 20px; }}\n"
        f"{sections[0]}::before {{\n"
        f"  content: {json.dumps(heading)}; display: block;\n"
        f"  margin: 32px 0 8px -{_GROUP_INDENT}; padding-bottom: 8px;\n"
        f"  border-bottom: 1px solid rgba(59, 65, 81, .3);\n"
        f"  font-family: sans-serif; font-size: 30px; font-weight: 700; color: #3b4151;\n"
        f"}}\n"
    )


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

    Being our route, it also carries the stylesheet that groups the two route sections under
    one heading — see `_tag_group_css`. Inline, so the page stays self-contained.
    """
    group_css = _tag_group_css(
        vault_config.API_TAG_GROUP,
        vault_config.API_TAGS + vault_config.API_TASHTIOT_TAGS,
    )

    @app.get(DOCS_PATH, include_in_schema=False)
    async def swagger_ui_without_validator():
        page = get_swagger_ui_html(
            title="Swagger UI",
            swagger_js_url=f"{library_settings.SWAGGER_STATIC_FILES}/swagger-ui-bundle.js",
            swagger_css_url=f"{library_settings.SWAGGER_STATIC_FILES}/swagger-ui.css",
            swagger_favicon_url=f"{library_settings.SWAGGER_STATIC_FILES}/favicon.ico",
            openapi_url=library_settings.SWAGGER_OPENAPI_JSON_URL,
            swagger_ui_parameters={"validatorUrl": None},
        )
        html = page.body.decode().replace(
            "</head>", f"<style>\n{group_css}</style>\n</head>", 1
        )
        return HTMLResponse(html)

    app.router.routes.insert(0, app.router.routes.pop())


def create_app() -> FastAPI:
    # enable_auth wires the library's global AuthMiddleware, which protects every route
    # (except docs/metrics/health/probes). It only activates when AUTH_ENABLED=true and one
    # AUTH_* verification material is set; otherwise the app boots open. See README.
    app = general_create_app(
        enable_auth=True,
        title=global_config.API_TITLE,
        version=global_config.APP_VERSION,
        # Declaring the two sections orders them first, ahead of the library's own
        # `default` (its root route), so the grouped heading sits at the top of the page.
        openapi_tags=[
            {"name": tag}
            for tag in vault_config.API_TAGS + vault_config.API_TASHTIOT_TAGS
        ],
    )

    # Must run after general_create_app, which is what registers the route being replaced.
    _replace_docs_route(app)

    # Configure external services objects. Connectors are built once here, never per-request.
    # Bitbucket is the only upstream: it holds the values repo *and* the build statuses the
    # CI gates read, so there is no second client and no CI-server credentials to carry.
    bitbucket_http = BaseAPI(
        global_config.BITBUCKET_URL,
        headers={"Authorization": f"Bearer {global_config.BITBUCKET_TOKEN}"},
        timeout=global_config.HTTP_TIMEOUT_SECONDS,
        verify=global_config.VERIFY_SSL,
    ).client
    # Deleting a file has no REST equivalent on Bitbucket Server, so the client carries a
    # git collaborator for that one call. Same host, same token — see clients/git.py.
    bitbucket_git = GitFileRemover(
        base_url=global_config.BITBUCKET_URL,
        token=global_config.BITBUCKET_TOKEN,
        project_key=vault_config.VAULT_VALUES_REPO_PROJECT_KEY,
        repo_slug=vault_config.VAULT_VALUES_REPO_SLUG,
        verify_ssl=global_config.VERIFY_SSL,
        author_name=vault_config.GIT_AUTHOR_NAME,
        author_email=vault_config.GIT_AUTHOR_EMAIL,
        timeout=vault_config.GIT_TIMEOUT_SECONDS,
    )
    bitbucket = BitbucketClient(
        bitbucket_http,
        project_key=vault_config.VAULT_VALUES_REPO_PROJECT_KEY,
        repo_slug=vault_config.VAULT_VALUES_REPO_SLUG,
        poll_interval=vault_config.CI_POLL_INTERVAL_SECONDS,
        start_timeout=vault_config.CI_PIPELINE_START_TIMEOUT_SECONDS,
        completion_timeout=vault_config.CI_PIPELINE_TIMEOUT_SECONDS,
        git=bitbucket_git,
    )

    # Add routes to app. One router: Kubernetes service accounts are a sub-resource of a KV
    # store, not a resource kind of their own, so they hang off these same paths.
    app.include_router(get_v1_vault_router(bitbucket))

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Vault API.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=5000, help="Bind port (default: 5000).")
    args = parser.parse_args()

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)
