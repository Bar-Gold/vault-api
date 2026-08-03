"""The update and read endpoints end-to-end: auth, routing, status codes.

Also where the two-segment `{file}/{kv_name}` routing is exercised over HTTP, since a
single-segment read (`/{file}`) and a two-segment one must not shadow each other.
"""
import yaml

from app.helpers import build_kv_store, build_kv_stores_document, render_values_yaml
from app.v1.vault.conf import config

KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
ROLES = {"read": ["app01.corp.example.com"]}

FILE_URL = f"{config.API_PREFIX}/{FILE}"
STORE_URL = f"{config.API_PREFIX}/{FILE}/{KV}"


def _seed(bitbucket, *names, path=VALUES_PATH, description="payments secrets"):
    bitbucket.existing_files[path] = render_values_yaml(
        build_kv_stores_document([build_kv_store(n, description, ROLES) for n in names])
    )


def _committed(bitbucket, path=VALUES_PATH):
    return yaml.safe_load(bitbucket.committed[path])


# --------------------------------------------------------------------------- #
# auth — the middleware is global, so every route must be covered
# --------------------------------------------------------------------------- #
def test_update_requires_a_token(client):
    assert client.patch(STORE_URL, json={"kv_description": "x"}).status_code == 401


def test_read_file_requires_a_token(client):
    assert client.get(FILE_URL).status_code == 401


def test_read_store_requires_a_token(client):
    assert client.get(STORE_URL).status_code == 401


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #
def test_update_returns_200_and_the_new_description(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    response = client.patch(
        STORE_URL, json={"kv_description": "new text"}, headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "Succeeded"
    assert body["message"] == f"Successful update of {KV}"
    assert body["kv_name"] == KV
    assert body["file"] == FILE
    assert _committed(bitbucket)["kvStores"][0]["description"] == "new text"


def test_update_can_replace_roles(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    response = client.patch(
        STORE_URL,
        json={"roles": {"read": ["new-host.corp.example.com"]}},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert _committed(bitbucket)["kvStores"][0]["roles"] == {
        "read": ["new-host.corp.example.com"]
    }


def test_update_of_a_missing_file_returns_404(client, auth_headers):
    response = client.patch(
        STORE_URL, json={"kv_description": "x"}, headers=auth_headers
    )

    assert response.status_code == 404
    assert "does not exist" in response.json()["error"]


def test_update_of_a_missing_store_returns_404(client, auth_headers, bitbucket):
    _seed(bitbucket, "someone-else")

    response = client.patch(
        STORE_URL, json={"kv_description": "x"}, headers=auth_headers
    )

    assert response.status_code == 404
    assert "not defined in" in response.json()["error"]


def test_empty_update_is_422(client, auth_headers, bitbucket):
    response = client.patch(STORE_URL, json={}, headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_update_rejects_an_unknown_role(client, auth_headers, bitbucket):
    response = client.patch(
        STORE_URL, json={"roles": {"admin": ["h.example.com"]}}, headers=auth_headers
    )

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_update_no_op_returns_200_without_a_pull_request(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    response = client.patch(
        STORE_URL, json={"kv_description": "payments secrets"}, headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["message"] == f"No changes required for {KV}"
    assert response.json()["pull_request"] is None


def test_update_failed_validation_returns_502(client, auth_headers, bitbucket, woodpecker):
    from tests.fakes import make_pipeline

    _seed(bitbucket, KV)
    woodpecker.results = [make_pipeline(number=2, status="failure", event="pull_request")]

    response = client.patch(
        STORE_URL, json={"kv_description": "new"}, headers=auth_headers
    )

    assert response.status_code == 502
    assert "Validation pipeline #2" in response.json()["error"]
    assert "decline_pull_request" in bitbucket.calls


# --------------------------------------------------------------------------- #
# reads — one segment is a file, two is a store
# --------------------------------------------------------------------------- #
def test_reading_a_file_returns_every_store(client, auth_headers, bitbucket):
    _seed(bitbucket, "one", "two")

    response = client.get(FILE_URL, headers=auth_headers)

    assert response.status_code == 200
    assert [s["name"] for s in response.json()["kvStores"]] == ["one", "two"]


def test_reading_a_store_returns_just_that_entry(client, auth_headers, bitbucket):
    _seed(bitbucket, "one", KV)

    response = client.get(STORE_URL, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == KV
    assert body["roles"] == ROLES
    assert "kvStores" not in body


def test_reading_a_missing_file_returns_404(client, auth_headers):
    assert client.get(f"{config.API_PREFIX}/nope", headers=auth_headers).status_code == 404


def test_reading_a_missing_store_returns_404(client, auth_headers, bitbucket):
    _seed(bitbucket, "one")

    assert client.get(STORE_URL, headers=auth_headers).status_code == 404


def test_the_two_route_shapes_do_not_shadow_each_other(client, auth_headers, bitbucket):
    """`/{file}` and `/{file}/{kv_name}` must both resolve to their own handler."""
    _seed(bitbucket, KV)

    whole_file = client.get(FILE_URL, headers=auth_headers).json()
    single = client.get(STORE_URL, headers=auth_headers).json()

    assert "kvStores" in whole_file
    assert single["name"] == KV
