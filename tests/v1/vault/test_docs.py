"""The /docs page must be fully self-contained.

Swagger UI defaults `validatorUrl` to `https://validator.swagger.io/validator`, and neither
FastAPI's defaults nor the library's /docs route override it. Left alone, every page load
hands our spec URL to a public third party and blocks on a host an air-gapped network
cannot reach — the page appears to hang.

These tests pin the override, and one of them fails on *any* external reference, so a future
library upgrade that re-introduces one is caught here rather than in a browser.
"""
import re

import pytest

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
    # A store is addressed by name alone; the team appears only on a create.
    assert any(p.endswith("/kv/{kv_name}") for p in paths)
    assert any(p.endswith("/kv/{kv_name}/access") for p in paths)
    assert any(p.endswith("/kv/tashtiot/{team}") for p in paths)
    # The retired shapes: a file was never an address, and bindings are not their own kind.
    assert not any("{file}" in p for p in paths)
    assert not any("k8s-service-accounts" in p for p in paths)


def test_create_schema_carries_a_readable_example(client):
    """Without an explicit example Swagger synthesises one from `pattern`, and renders an
    unreadable regex-derived string in the box a user is meant to edit."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    create = schemas["VaultKVCreate"]

    assert set(create["example"]) == {"team", "kv_name", "access"}
    assert create["properties"]["kv_name"]["examples"] == ["my-passwords"]
    assert "kv_description" not in create["properties"]


def test_the_example_shows_both_kinds_of_grant(client):
    """The two shapes differ in their required fields, so one of each earns its place."""
    example = client.get("/openapi.json").json()["components"]["schemas"][
        "VaultKVCreate"
    ]["example"]

    assert [entry["auth_type"] for entry in example["access"]] == ["oidc", "k8s"]


def test_the_dn_example_is_a_distinguished_name(client):
    """The files carry LDAP DNs, not hostnames — the example has to show the real thing."""
    example = client.get("/openapi.json").json()["components"]["schemas"][
        "VaultKVCreate"
    ]["example"]

    assert example["access"][0]["dn"].startswith("CN=")


def test_the_create_example_is_actually_valid(client):
    """An example the schema would reject is worse than none."""
    from app.v1.vault.schemas import VaultKVCreate

    example = client.get("/openapi.json").json()["components"]["schemas"][
        "VaultKVCreate"
    ]["example"]

    assert VaultKVCreate(**example).kv_name == "my-passwords"


def test_every_documented_example_survives_its_own_validators(client):
    """Same rule for every request model, not just the create."""
    from app.v1.vault.schemas import TashtiotAccessUpsert, VaultKVUpdate

    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    for name, model in (
        ("VaultKVUpdate", VaultKVUpdate),
        ("TashtiotAccessUpsert", TashtiotAccessUpsert),
    ):
        assert model(**schemas[name]["example"]), name


def test_docs_needs_no_auth(client):
    """The auth middleware excludes the docs; a login wall would also look like a hang."""
    assert client.get(DOCS).status_code == 200
    assert client.get("/openapi.json").status_code == 200


# --------------------------------------------------------------------------- #
# the route documentation is part of the contract
#
# Swagger renders `summary` and `description` verbatim, so a stale one is a lie told to
# every caller who reads the docs page. Nothing pinned them before, and they went stale the
# moment the chain stopped merging: the create still advertised "wait for both pipelines"
# long after the second pipeline was gone.
# --------------------------------------------------------------------------- #
RETIRED_CLAIMS = (
    "both pipelines",
    "deploy build",
    "merges, then",
    "runs the full chain",
    "point of no return",
)


def _documented_text(client):
    """Every summary and description the OpenAPI document exposes, per operation."""
    spec = client.get("/openapi.json").json()
    for path, operations in spec["paths"].items():
        if "vault" not in path:
            continue
        for method, operation in operations.items():
            yield (
                f"{method.upper()} {path}",
                f"{operation.get('summary', '')} {operation.get('description', '')}",
            )


@pytest.mark.parametrize("claim", RETIRED_CLAIMS)
def test_no_route_advertises_a_retired_behaviour(client, claim):
    offenders = [name for name, text in _documented_text(client) if claim in text.lower()]

    assert not offenders, f"{offenders} still document {claim!r}"


def test_every_mutating_route_says_a_pull_request_is_opened(client):
    """The one thing a caller must not miss: nothing is applied when the call returns."""
    mutating = [
        (name, text)
        for name, text in _documented_text(client)
        if not name.startswith("GET")
    ]

    assert mutating, "no mutating routes found — the shape changed"
    for name, text in mutating:
        assert "pull request" in text.lower(), f"{name} does not mention the pull request"


def test_the_blocking_routes_say_they_wait_and_the_others_say_they_do_not(client):
    """The only difference between a route and its /pull-request twin is the wait."""
    for name, text in _documented_text(client):
        if name.endswith("/pull-request"):
            assert "without waiting" in text.lower(), f"{name} should say it does not wait"
