"""The /docs page must be fully self-contained.

Swagger UI defaults `validatorUrl` to `https://validator.swagger.io/validator`, and neither
FastAPI's defaults nor the library's /docs route override it. Left alone, every page load
hands our spec URL to a public third party and blocks on a host an air-gapped network
cannot reach — the page appears to hang.

These tests pin the override, and one of them fails on *any* external reference, so a future
library upgrade that re-introduces one is caught here rather than in a browser.
"""
import re

DOCS = "/docs"


def test_docs_is_served(client):
    response = client.get(DOCS)

    assert response.status_code == 200
    assert "swagger-ui" in response.text


def test_docs_disables_the_spec_validator(client):
    """The fix itself: `validatorUrl: null` stops Swagger UI calling validator.swagger.io."""
    body = client.get(DOCS).text

    assert '"validatorUrl": null' in body


def test_docs_never_mentions_the_public_validator(client):
    body = client.get(DOCS).text

    assert "validator.swagger.io" not in body


def test_docs_references_no_external_host(client):
    """Every asset must be same-origin, so the page renders with no egress at all."""
    body = client.get(DOCS).text
    external = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', body)

    assert external == []


def test_docs_assets_are_all_locally_served(client):
    """The referenced asset URLs must actually resolve on this app, not 404."""
    body = client.get(DOCS).text
    referenced = re.findall(r'(?:src|href)\s*=\s*"(/[^"]+)"', body)

    assert referenced, "no local assets referenced — the page shape changed"
    for url in referenced:
        assert client.get(url).status_code == 200, f"{url} does not resolve"


def test_openapi_is_reachable_and_lists_the_routes(client):
    """Swagger fetches this on load; if it 404s the page renders empty."""
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert any(p.endswith("/kv/pull-request") for p in paths)
    assert any(p.endswith("{file}/{kv_name}") for p in paths)


def test_create_schema_carries_a_readable_example(client):
    """Without an explicit example Swagger synthesises one from `pattern`, and renders an
    unreadable regex-derived string in the box a user is meant to edit."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    create = schemas["VaultKVCreate"]

    assert create["example"] == {
        "file": "payments",
        "kv_name": "myapp",
        "kv_description": "payments secrets",
        "roles": {"read": ["app01.corp.example.com"]},
    }
    assert create["properties"]["kv_name"]["examples"] == ["myapp"]
    assert create["properties"]["file"]["examples"] == ["payments"]


def test_update_schema_carries_an_example(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    update = schemas["VaultKVUpdate"]

    assert "example" in update
    assert update["properties"]["roles"]["examples"] == [
        {"read": ["app01.corp.example.com"]}
    ]


def test_the_create_example_is_actually_valid(client):
    """An example that the schema would reject is worse than none."""
    from app.v1.vault.schemas import VaultKVCreate

    example = client.get("/openapi.json").json()["components"]["schemas"]["VaultKVCreate"]["example"]

    assert VaultKVCreate(**example).kv_name == "myapp"


def test_docs_needs_no_auth(client):
    """The auth middleware excludes the docs; a login wall would also look like a hang."""
    assert client.get(DOCS).status_code == 200
    assert client.get("/openapi.json").status_code == 200
