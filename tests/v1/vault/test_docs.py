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
    # A store is addressed by name alone; only the whole-file read still names a file.
    assert any(p.endswith("/kv/{kv_name}") for p in paths)
    assert any(p.endswith("/kv/files/{file}") for p in paths)
    assert not any("{file}/{kv_name}" in p for p in paths)


def test_create_schema_carries_a_readable_example(client):
    """Without an explicit example Swagger synthesises one from `pattern`, and renders an
    unreadable regex-derived string in the box a user is meant to edit."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    create = schemas["VaultKVCreate"]

    # No `file`: the example shows the normal three-field request, since a caller should
    # not have to know which file their store lands in.
    assert create["example"] == {
        "kv_name": "athena-passwords",
        "kv_description": "Passwords for athena",
        "roles": {
            "write": ["CN=svc-athena,OU=ServiceAccounts,DC=corp,DC=example,DC=com"]
        },
    }
    assert create["properties"]["kv_name"]["examples"] == ["myapp"]
    assert "file" not in create["required"]


def test_the_role_example_is_a_distinguished_name(client):
    """The values files bind LDAP DNs, not hostnames — the example has to show the real thing."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert schemas["VaultKVCreate"]["example"]["roles"]["write"][0].startswith("CN=")


def test_update_schema_carries_an_example(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    update = schemas["VaultKVUpdate"]

    assert "example" in update
    assert update["properties"]["roles"]["examples"] == [
        {"write": ["CN=svc-athena,OU=ServiceAccounts,DC=corp,DC=example,DC=com"]}
    ]


def test_the_create_example_is_actually_valid(client):
    """An example that the schema would reject is worse than none."""
    from app.v1.vault.schemas import VaultKVCreate

    example = client.get("/openapi.json").json()["components"]["schemas"]["VaultKVCreate"]["example"]

    assert VaultKVCreate(**example).kv_name == "athena-passwords"


def test_the_binding_example_is_actually_valid(client):
    """Same rule for the sub-resource: the example must survive its own validators."""
    from app.v1.vault.schemas import K8sServiceAccountCreate

    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    example = schemas["K8sServiceAccountCreate"]["example"]

    assert K8sServiceAccountCreate(**example).cluster == "dev"


def test_docs_needs_no_auth(client):
    """The auth middleware excludes the docs; a login wall would also look like a hang."""
    assert client.get(DOCS).status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_the_create_example_omitting_file_still_lands_somewhere(client):
    """`file` defaults to the store's own name, so the three-field example is complete."""
    from app.v1.vault.schemas import VaultKVCreate

    example = client.get("/openapi.json").json()["components"]["schemas"]["VaultKVCreate"]["example"]

    assert VaultKVCreate(**example).file == "athena-passwords"
