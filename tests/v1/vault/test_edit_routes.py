"""The update endpoint end-to-end through the app: auth, routing, status codes.

Also the place multi-segment names are exercised through HTTP, since `/{kv_name:path}` has
to bind a name that contains slashes.
"""
import yaml

from app.helpers import build_kv_values, render_values_yaml
from app.v1.vault.conf import config

KV = "myapp"
VALUES_PATH = "kv/myapp.yaml"

UPDATE_URL = f"{config.API_PREFIX}/{KV}"

NESTED = "payments/vault-secrets"
NESTED_PATH = "kv/payments/vault-secrets.yaml"


def _seed_simple(bitbucket, kv_name=KV, path=VALUES_PATH):
    values = build_kv_values(kv_name, "payments secrets")
    bitbucket.existing_files[path] = render_values_yaml(values)
    return values


def _committed(bitbucket, path=VALUES_PATH):
    return yaml.safe_load(bitbucket.committed[path])


# --------------------------------------------------------------------------- #
# auth — the middleware is global, so every route must be covered
# --------------------------------------------------------------------------- #
def test_update_requires_a_token(client):
    assert client.patch(UPDATE_URL, json={"kv_description": "x"}).status_code == 401


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #
def test_update_returns_200_and_the_new_description(client, auth_headers, bitbucket):
    _seed_simple(bitbucket)

    response = client.patch(
        UPDATE_URL, json={"kv_description": "new text"}, headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "Succeeded"
    assert body["message"] == f"Successful update of {KV}"
    assert body["kv_name"] == KV
    assert _committed(bitbucket)["description"] == "new text"


def test_update_of_a_missing_kv_returns_404(client, auth_headers):
    response = client.patch(
        UPDATE_URL, json={"kv_description": "x"}, headers=auth_headers
    )

    assert response.status_code == 404
    assert "does not exist" in response.json()["error"]


def test_empty_update_is_422(client, auth_headers, bitbucket):
    response = client.patch(UPDATE_URL, json={}, headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_update_no_op_returns_200_without_a_pull_request(client, auth_headers, bitbucket):
    values = _seed_simple(bitbucket)

    response = client.patch(
        UPDATE_URL, json={"kv_description": values["description"]}, headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["message"] == f"No changes required for {KV}"
    assert response.json()["pull_request"] is None


def test_update_failed_validation_returns_502(
    client, auth_headers, bitbucket, woodpecker
):
    from tests.fakes import make_pipeline

    _seed_simple(bitbucket)
    woodpecker.results = [make_pipeline(number=2, status="failure", event="pull_request")]

    response = client.patch(
        UPDATE_URL, json={"kv_description": "new"}, headers=auth_headers
    )

    assert response.status_code == 502
    assert "Validation pipeline #2" in response.json()["error"]
    assert "decline_pull_request" in bitbucket.calls


# --------------------------------------------------------------------------- #
# multi-segment names through HTTP
# --------------------------------------------------------------------------- #
def test_read_a_multi_segment_name(client, auth_headers, bitbucket):
    _seed_simple(bitbucket, NESTED, NESTED_PATH)

    response = client.get(f"{config.API_PREFIX}/{NESTED}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["kvname"] == NESTED


def test_patch_a_multi_segment_name(client, auth_headers, bitbucket):
    _seed_simple(bitbucket, NESTED, NESTED_PATH)

    response = client.patch(
        f"{config.API_PREFIX}/{NESTED}",
        json={"kv_description": "new text"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["kv_name"] == NESTED
    assert _committed(bitbucket, NESTED_PATH)["description"] == "new text"
