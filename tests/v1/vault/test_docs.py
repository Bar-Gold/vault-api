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

from app.v1.vault.conf import config

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
    # A store is addressed by name alone; the team appears only on a create.
    assert any(p.endswith("/kv/{kv_name}") for p in paths)
    assert any(p.endswith("/kv/{kv_name}/access") for p in paths)
    assert any(p.endswith("/kv/tashtiot/{team}") for p in paths)
    # The retired shapes: a file was never an address, bindings are not their own kind,
    # and the routes that skipped CI are gone.
    assert not any("{file}" in p for p in paths)
    assert not any("k8s-service-accounts" in p for p in paths)
    assert not any(p.endswith("/pull-request") for p in paths)


def test_create_schema_carries_a_readable_example(client):
    """Without an explicit example Swagger synthesises one from `pattern`, and renders an
    unreadable regex-derived string in the box a user is meant to edit."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    create = schemas["VaultKVCreate"]

    assert set(create["example"]) == {"team", "kv_name", "access", "metadata"}
    assert create["properties"]["kv_name"]["examples"] == ["my-passwords"]
    assert "kv_description" not in create["properties"]


def test_the_example_shows_both_kinds_of_grant(client):
    """The two shapes differ in their required fields, so one of each earns its place."""
    example = client.get("/openapi.json").json()["components"]["schemas"][
        "VaultKVCreate"
    ]["example"]

    assert [entry["auth_type"] for entry in example["access"]] == ["oidc", "k8s"]


def test_the_tashtiot_example_shows_both_kinds_of_grant(client):
    """The flat file mixes both kinds in one list, so an oidc-only example reads as if
    a Kubernetes grant belonged somewhere else."""
    example = client.get("/openapi.json").json()["components"]["schemas"][
        "TashtiotAccessUpsert"
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


# The four entry schemas: the list-element pair and the request-body pair. The bodies of
# the two `.../access` POSTs are the Request ones; the plain pair only ever appears
# inside an `access` list.
ENTRY_SCHEMAS = ("OidcAccessEntry", "K8sAccessEntry",
                 "OidcAccessEntryRequest", "K8sAccessEntryRequest")


def test_the_grant_body_carries_an_example_per_branch(client):
    """`POST /{kv_name}/access` takes one entry as the whole body — the *Request* pair,
    since the body carries the metadata block too. Without a model-level example Swagger
    builds the box from the field schemas and fills every string with "string" — the
    field-level `examples` do not reach it."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert schemas["OidcAccessEntryRequest"]["example"]["auth_type"] == "oidc"
    assert schemas["K8sAccessEntryRequest"]["example"]["auth_type"] == "k8s"
    # The list-element pair keeps its own, for the grants inside an `access` list.
    assert schemas["OidcAccessEntry"]["example"]["auth_type"] == "oidc"
    assert schemas["K8sAccessEntry"]["example"]["auth_type"] == "k8s"


def test_both_grant_examples_default_the_path_to_the_whole_store(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    for name in ENTRY_SCHEMAS:
        assert schemas[name]["example"]["path"] == "*", name


def test_the_grant_examples_are_actually_valid(client):
    """An example the schema would reject is worse than none — and these are two branches
    of a discriminated union, so each has to validate as its own kind."""
    from app.v1.vault.schemas import (
        K8sAccessEntry,
        K8sAccessEntryRequest,
        OidcAccessEntry,
        OidcAccessEntryRequest,
    )

    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    models = {
        "OidcAccessEntry": OidcAccessEntry,
        "K8sAccessEntry": K8sAccessEntry,
        "OidcAccessEntryRequest": OidcAccessEntryRequest,
        "K8sAccessEntryRequest": K8sAccessEntryRequest,
    }

    for name in ENTRY_SCHEMAS:
        assert models[name](**schemas[name]["example"]), name


def test_the_entry_models_still_forbid_extra_fields(client):
    """The examples arrive via `model_config`, which is where `extra: forbid` lives too —
    replacing rather than merging it would silently reopen the models. The Request pair
    re-merges the parent's config for exactly that reason."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    for name in ENTRY_SCHEMAS + ("RequestMetadata",):
        assert schemas[name]["additionalProperties"] is False, name


# --------------------------------------------------------------------------- #
# the metadata block in the documented shapes
# --------------------------------------------------------------------------- #
METADATA_NAMES = {"project", "network", "region", "space", "environment", "island"}

BODY_SCHEMAS = (
    "VaultKVCreate",
    "VaultKVUpdate",
    "TashtiotAccessUpsert",
    "OidcAccessEntryRequest",
    "K8sAccessEntryRequest",
    "OidcAccessRevoke",
    "K8sAccessRevoke",
    "DeleteRequest",
)


@pytest.mark.parametrize("name", BODY_SCHEMAS)
def test_every_body_example_carries_the_full_metadata_block(client, name):
    """One shared `_METADATA_EXAMPLE`, so a divergent example is a test failure rather
    than a lie left in the docs."""
    example = client.get("/openapi.json").json()["components"]["schemas"][name]["example"]

    assert set(example["metadata"]) == METADATA_NAMES, name


@pytest.mark.parametrize("path", ["/{kv_name}/access", "/tashtiot/{team}/access"])
def test_the_grant_body_schema_is_the_request_entry_pair(client, path):
    """Not the base pair: reverting to `AccessEntry` would make metadata optional in
    practice, since the body would no longer declare it."""
    spec = client.get("/openapi.json").json()
    body = spec["paths"][f"{config.API_PREFIX}{path}"]["post"]["requestBody"]
    schema = body["content"]["application/json"]["schema"]

    assert [ref["$ref"].rsplit("/", 1)[-1] for ref in schema["oneOf"]] == [
        "OidcAccessEntryRequest",
        "K8sAccessEntryRequest",
    ]
    assert schema["discriminator"]["propertyName"] == "auth_type"


def test_the_tashtiot_grant_box_is_prefilled_with_a_scoped_path(client):
    """The Tashtiot sub-resource prefills `+/team/*`, not the models' own `*`.

    That file grants a team access *into* the shared services store, so its paths are
    scoped in practice and a prefilled `*` nudges towards the wrong shape.
    """
    spec = client.get("/openapi.json").json()
    body = spec["paths"][f"{config.API_PREFIX}/tashtiot/{{team}}/access"]["post"]
    examples = body["requestBody"]["content"]["application/json"]["examples"]

    assert {name: value["value"]["path"] for name, value in examples.items()} == {
        "oidc": "+/team/*",
        "k8s": "+/team/*",
    }
    assert examples["oidc"]["value"]["auth_type"] == "oidc"
    assert examples["k8s"]["value"]["auth_type"] == "k8s"


def test_the_tashtiot_examples_are_actually_valid(client):
    """Same rule the model-level examples follow: an example the schema would reject is
    worse than none. `+/team/*` has to survive VAULT_PATH_PATTERN and `no_parent_segments`,
    and each branch has to validate as its own kind."""
    from app.v1.vault.schemas import K8sAccessEntryRequest, OidcAccessEntryRequest

    body = client.get("/openapi.json").json()["paths"][
        f"{config.API_PREFIX}/tashtiot/{{team}}/access"
    ]["post"]
    examples = body["requestBody"]["content"]["application/json"]["examples"]

    assert OidcAccessEntryRequest(**examples["oidc"]["value"]).path == "+/team/*"
    assert K8sAccessEntryRequest(**examples["k8s"]["value"]).path == "+/team/*"


def test_the_store_grant_box_still_prefills_a_bare_star(client):
    """Only the Tashtiot route was given its own examples. The store's `/access` keeps the
    models' `*` — a store grant covers that store, so everything in it is the usual case."""
    spec = client.get("/openapi.json").json()
    body = spec["paths"][f"{config.API_PREFIX}/{{kv_name}}/access"]["post"]

    assert "examples" not in body["requestBody"]["content"]["application/json"]
    for name in ("OidcAccessEntryRequest", "K8sAccessEntryRequest"):
        assert spec["components"]["schemas"][name]["example"]["path"] == "*"


def test_the_tashtiot_examples_did_not_cost_the_branch_picker(client):
    """`Body(openapi_examples=...)` on the payload would have replaced the discriminated
    union's schema with a bare `anyOf`, losing the picker. `openapi_extra` deep-merges, so
    the examples arrive and the schema does not move — pinned because the failure is
    invisible in the rendered box until you go looking for the dropdown."""
    spec = client.get("/openapi.json").json()
    schema = spec["paths"][f"{config.API_PREFIX}/tashtiot/{{team}}/access"]["post"][
        "requestBody"
    ]["content"]["application/json"]["schema"]

    assert "anyOf" not in schema
    assert schema["discriminator"]["propertyName"] == "auth_type"


@pytest.mark.parametrize("name", ["OidcAccessEntry", "K8sAccessEntry"])
def test_the_list_element_examples_carry_no_metadata(client, name):
    """A list element that carries metadata is a 422, so its example must not show one."""
    example = client.get("/openapi.json").json()["components"]["schemas"][name]["example"]

    assert "metadata" not in example, name


# --------------------------------------------------------------------------- #
# JSON bodies, never query parameters
#
# Only a GET may take query parameters, and none does today. Every other route takes a
# required JSON body carrying the metadata block — the DELETEs included, which is why a
# revoke's identity is in its body and a store delete sends `{"metadata": {...}}`.
# --------------------------------------------------------------------------- #
def _body_properties(spec, body):
    """Every property a request body can carry, across a `oneOf`'s branches too."""
    schema = body["content"]["application/json"]["schema"]
    refs = [branch["$ref"] for branch in schema.get("oneOf", [schema])]
    schemas = spec["components"]["schemas"]
    return [set(schemas[ref.rsplit("/", 1)[-1]]["properties"]) for ref in refs]


def test_no_mutating_route_takes_a_query_parameter(client):
    """The rule itself, read off the whole OpenAPI document so a new route is covered the
    moment it is registered."""
    spec = client.get("/openapi.json").json()
    checked = []

    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method == "get":
                continue
            name = f"{method.upper()} {path}"
            checked.append(name)

            query = [p["name"] for p in operation.get("parameters", []) if p["in"] == "query"]
            assert not query, f"{name} takes query parameters {query}"

            body = operation.get("requestBody")
            assert body and body.get("required"), f"{name} has no required JSON body"
            for properties in _body_properties(spec, body):
                assert "metadata" in properties, f"{name} does not carry metadata"

    assert len(checked) == 9, checked


@pytest.mark.parametrize("path", ["/{kv_name}/access", "/tashtiot/{team}/access"])
def test_the_revoke_body_schema_is_the_revoke_pair(client, path):
    """A discriminated `oneOf`, like the grant's, so Swagger offers the same branch picker."""
    spec = client.get("/openapi.json").json()
    body = spec["paths"][f"{config.API_PREFIX}{path}"]["delete"]["requestBody"]
    schema = body["content"]["application/json"]["schema"]

    assert [ref["$ref"].rsplit("/", 1)[-1] for ref in schema["oneOf"]] == [
        "OidcAccessRevoke",
        "K8sAccessRevoke",
    ]
    assert schema["discriminator"]["propertyName"] == "auth_type"


def test_the_tashtiot_revoke_box_is_prefilled_with_a_scoped_path(client):
    """Same prefill as the Tashtiot add, minus capabilities — and still valid bodies."""
    from app.v1.vault.schemas import K8sAccessRevoke, OidcAccessRevoke

    operation = client.get("/openapi.json").json()["paths"][
        f"{config.API_PREFIX}/tashtiot/{{team}}/access"
    ]["delete"]
    examples = operation["requestBody"]["content"]["application/json"]["examples"]

    assert {name: value["value"]["path"] for name, value in examples.items()} == {
        "oidc": "+/team/*",
        "k8s": "+/team/*",
    }
    assert OidcAccessRevoke(**examples["oidc"]["value"]).path == "+/team/*"
    assert K8sAccessRevoke(**examples["k8s"]["value"]).path == "+/team/*"


# --------------------------------------------------------------------------- #
# documentation sections
# --------------------------------------------------------------------------- #
def test_tashtiot_has_its_own_section(client):
    """A different resource — per-team, one flat file, no directory — so it reads as one."""
    paths = client.get("/openapi.json").json()["paths"]
    tashtiot = f"{config.API_PREFIX}/tashtiot/{{team}}"

    for method in ("get", "put", "delete"):
        assert paths[tashtiot][method]["tags"] == config.API_TASHTIOT_TAGS, method


def test_no_route_appears_in_both_sections(client):
    """FastAPI concatenates a router's tags with a route's, so a router-level default puts
    every Tashtiot route in both lists. That is why the router carries none."""
    paths = client.get("/openapi.json").json()["paths"]

    for path, operations in paths.items():
        if config.API_PREFIX not in path:
            continue
        for method, operation in operations.items():
            assert len(operation["tags"]) == 1, f"{method.upper()} {path}"


def test_the_store_routes_keep_the_original_section(client):
    paths = client.get("/openapi.json").json()["paths"]

    assert paths[f"{config.API_PREFIX}/"]["post"]["tags"] == config.API_TAGS
    assert paths[f"{config.API_PREFIX}/{{kv_name}}"]["get"]["tags"] == config.API_TAGS


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
    "query parameter",
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


def test_every_route_waits_for_ci(client):
    """There is no longer a variant that skips the build, so none may claim to.

    The `/pull-request` twins were removed: without the wait a caller could not know the
    change was green, which is the one guarantee this service still offers.
    """
    for name, text in _documented_text(client):
        assert "without waiting" not in text.lower(), f"{name} claims to skip CI"


