"""The three edit endpoints end-to-end through the app: auth, routing, status codes.

Also the place multi-segment names are exercised through HTTP, since `/{kv_name:path}` has
to bind the name without swallowing a `/groups` or `/kubernetes-auth` suffix.
"""
import yaml

from app.helpers import build_kv_values, render_values_yaml
from app.v1.vault.conf import config

KV = "myapp"
VALUES_PATH = "kv/myapp.yaml"
READ_POLICY = "myapp-read"
WRITE_POLICY = "myapp-write"

UPDATE_URL = f"{config.API_PREFIX}/{KV}"
K8S_URL = f"{config.API_PREFIX}/{KV}/kubernetes-auth"
GROUPS_URL = f"{config.API_PREFIX}/{KV}/groups"

NESTED = "payments/vault-secrets"
NESTED_PATH = "kv/payments/vault-secrets.yaml"


def _seed_simple(bitbucket, kv_name=KV, path=VALUES_PATH):
    values = build_kv_values(kv_name, "payments secrets")
    bitbucket.existing_files[path] = render_values_yaml(values)
    return values


def _seed_with_policies(bitbucket, kv_name=KV, path=VALUES_PATH):
    values = {
        "kvname": kv_name,
        "description": "payments secrets",
        "policies": [
            {"name": f"{kv_name.replace('/', '-')}-read", "entities": ["group/readers"]},
            {"name": f"{kv_name.replace('/', '-')}-write", "entities": ["group/writers"]},
        ],
    }
    bitbucket.existing_files[path] = render_values_yaml(values)
    return values


def _committed(bitbucket, path=VALUES_PATH):
    return yaml.safe_load(bitbucket.committed[path])


# --------------------------------------------------------------------------- #
# auth — the middleware is global, so every route must be covered
# --------------------------------------------------------------------------- #
def test_update_requires_a_token(client):
    assert client.patch(UPDATE_URL, json={"kv_description": "x"}).status_code == 401


def test_kubernetes_auth_requires_a_token(client):
    body = {"service_accounts": ["sa"], "namespaces": ["ns"]}
    assert client.post(K8S_URL, json=body).status_code == 401


def test_group_binding_requires_a_token(client):
    body = {"group": "AD\\x", "capability": "read"}
    assert client.post(GROUPS_URL, json=body).status_code == 401


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
# kubernetes auth
# --------------------------------------------------------------------------- #
def test_kubernetes_auth_returns_200_and_commits_the_role(
    client, auth_headers, bitbucket
):
    _seed_with_policies(bitbucket)

    response = client.post(
        K8S_URL,
        json={
            "service_accounts": ["myapp"],
            "namespaces": ["payments-prod"],
            "capability": "write",
            "ttl": "24h",
        },
        headers=auth_headers,
    )

    assert response.status_code == 200
    role = _committed(bitbucket)["kubernetes_auth"][0]
    assert role["policies"] == [WRITE_POLICY]
    assert role["namespaces"] == ["payments-prod"]
    assert role["ttl"] == "24h"


def test_kubernetes_auth_requires_service_accounts(client, auth_headers, bitbucket):
    response = client.post(K8S_URL, json={"namespaces": ["ns"]}, headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_kubernetes_auth_on_a_missing_kv_returns_404(client, auth_headers):
    response = client.post(
        K8S_URL,
        json={"service_accounts": ["sa"], "namespaces": ["ns"]},
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_kubernetes_auth_without_policies_returns_422(client, auth_headers, bitbucket):
    _seed_simple(bitbucket)

    response = client.post(
        K8S_URL,
        json={"service_accounts": ["sa"], "namespaces": ["ns"]},
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert "no 'read' policy" in response.json()["error"]


# --------------------------------------------------------------------------- #
# AD group bindings
# --------------------------------------------------------------------------- #
def test_group_binding_returns_200_and_commits_the_entity(
    client, auth_headers, bitbucket
):
    _seed_with_policies(bitbucket)

    response = client.post(
        GROUPS_URL,
        json={"group": "AD\\payments-ro", "capability": "read"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    policies = {p["name"]: p for p in _committed(bitbucket)["policies"]}
    assert "AD\\payments-ro" in policies[READ_POLICY]["entities"]
    assert "AD\\payments-ro" not in policies[WRITE_POLICY]["entities"]


def test_group_binding_rejects_an_unknown_capability(client, auth_headers):
    response = client.post(
        GROUPS_URL, json={"group": "AD\\x", "capability": "admin"}, headers=auth_headers
    )

    assert response.status_code == 422


def test_group_binding_rejects_a_blank_group(client, auth_headers):
    response = client.post(
        GROUPS_URL, json={"group": "   ", "capability": "read"}, headers=auth_headers
    )

    assert response.status_code == 422


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


def test_groups_on_a_multi_segment_name(client, auth_headers, bitbucket):
    """`/{kv_name:path}/groups` must bind the name without eating '/groups'."""
    _seed_with_policies(bitbucket, NESTED, NESTED_PATH)

    response = client.post(
        f"{config.API_PREFIX}/{NESTED}/groups",
        json={"group": "AD\\payments-ro", "capability": "read"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["kv_name"] == NESTED
    policies = {p["name"]: p for p in _committed(bitbucket, NESTED_PATH)["policies"]}
    assert "AD\\payments-ro" in policies["payments-vault-secrets-read"]["entities"]


def test_kubernetes_auth_on_a_multi_segment_name(client, auth_headers, bitbucket):
    _seed_with_policies(bitbucket, NESTED, NESTED_PATH)

    response = client.post(
        f"{config.API_PREFIX}/{NESTED}/kubernetes-auth",
        json={"service_accounts": ["sa"], "namespaces": ["ns"]},
        headers=auth_headers,
    )

    assert response.status_code == 200
    role = _committed(bitbucket, NESTED_PATH)["kubernetes_auth"][0]
    # The role name is flattened; so is the policy it binds.
    assert role["role"] == "payments-vault-secrets"
    assert role["policies"] == ["payments-vault-secrets-read"]


def test_a_name_ending_in_groups_still_resolves(client, auth_headers, bitbucket):
    """The converter backtracks to the anchored suffix, so 'team/groups' is usable."""
    _seed_with_policies(bitbucket, "team/groups", "kv/team/groups.yaml")

    response = client.post(
        f"{config.API_PREFIX}/team/groups/groups",
        json={"group": "AD\\x", "capability": "read"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["kv_name"] == "team/groups"
