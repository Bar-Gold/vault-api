"""The three edit endpoints end-to-end through the app: auth, routing, status codes.

Uses the same `client` fixture as `test_routes.py`, so the real app is exercised with both
connectors replaced by fakes.
"""
import yaml
from fastapi.encoders import jsonable_encoder

from app.helpers import build_values_data, render_values_yaml
from app.v1.vault.conf import config
from tests.fakes import make_pipeline

VALUES_PATH = "kv/prod/myapp.yaml"
MOUNT_PATH = "kingmagen/prod/myapp"
READ_POLICY = "kingmagen-prod-myapp-read"
WRITE_POLICY = "kingmagen-prod-myapp-write"

UPDATE_URL = f"{config.API_PREFIX}/myapp"
K8S_URL = f"{config.API_PREFIX}/myapp/kubernetes-auth"
GROUPS_URL = f"{config.API_PREFIX}/myapp/groups"


def _seed(bitbucket, payload):
    _, values = build_values_data(payload, "kingmagen", "prod")
    bitbucket.existing_files[VALUES_PATH] = render_values_yaml(values)
    return values


def _body(metadata, spec):
    return jsonable_encoder({"metadata": metadata, "spec": spec})


# --------------------------------------------------------------------------- #
# auth — the middleware is global, so every new route must be covered
# --------------------------------------------------------------------------- #
def test_update_requires_a_token(client, metadata):
    body = _body(metadata, {"description": "x"})
    assert client.patch(UPDATE_URL, json=body).status_code == 401


def test_kubernetes_auth_requires_a_token(client, metadata):
    body = _body(metadata, {"service_accounts": ["sa"], "namespaces": ["ns"]})
    assert client.post(K8S_URL, json=body).status_code == 401


def test_group_binding_requires_a_token(client, metadata):
    body = _body(metadata, {"group": "AD\\x", "capability": "read"})
    assert client.post(GROUPS_URL, json=body).status_code == 401


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #
def test_update_returns_200_and_the_new_description(
    client, payload, metadata, auth_headers, bitbucket
):
    _seed(bitbucket, payload)

    response = client.patch(
        UPDATE_URL, json=_body(metadata, {"description": "payments secrets"}), headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "Succeeded"
    assert body["message"] == f"Successful update of {MOUNT_PATH}"
    committed = yaml.safe_load(bitbucket.committed[VALUES_PATH])
    assert committed["mount"]["description"] == "payments secrets"


def test_update_of_a_missing_mount_returns_404(client, metadata, auth_headers):
    response = client.patch(
        UPDATE_URL, json=_body(metadata, {"description": "x"}), headers=auth_headers
    )

    assert response.status_code == 404
    assert "does not exist" in response.json()["error"]


def test_update_with_an_empty_spec_is_422(client, metadata, auth_headers, bitbucket):
    """An edit that specifies nothing would open a pull request that changes nothing."""
    response = client.patch(UPDATE_URL, json=_body(metadata, {}), headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_update_no_op_returns_200_without_a_pull_request(
    client, payload, metadata, auth_headers, bitbucket
):
    values = _seed(bitbucket, payload)

    response = client.patch(
        UPDATE_URL,
        json=_body(metadata, {"description": values["mount"]["description"]}),
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["message"] == f"No changes required for {MOUNT_PATH}"
    assert response.json()["pull_request"] is None


def test_update_failed_validation_returns_502(
    client, payload, metadata, auth_headers, bitbucket, woodpecker
):
    _seed(bitbucket, payload)
    woodpecker.results = [make_pipeline(number=2, status="failure", event="pull_request")]

    response = client.patch(
        UPDATE_URL, json=_body(metadata, {"description": "new"}), headers=auth_headers
    )

    assert response.status_code == 502
    assert "Validation pipeline #2" in response.json()["error"]
    assert "decline_pull_request" in bitbucket.calls


# --------------------------------------------------------------------------- #
# kubernetes auth
# --------------------------------------------------------------------------- #
def test_kubernetes_auth_returns_200_and_commits_the_role(
    client, payload, metadata, auth_headers, bitbucket
):
    _seed(bitbucket, payload)

    response = client.post(
        K8S_URL,
        json=_body(
            metadata,
            {
                "service_accounts": ["myapp"],
                "namespaces": ["payments-prod"],
                "capability": "write",
                "ttl": "24h",
            },
        ),
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert "kingmagen-prod-myapp" in response.json()["message"]
    role = yaml.safe_load(bitbucket.committed[VALUES_PATH])["kubernetes_auth"][0]
    assert role["policies"] == [WRITE_POLICY]
    assert role["namespaces"] == ["payments-prod"]
    assert role["ttl"] == "24h"


def test_kubernetes_auth_requires_service_accounts(
    client, metadata, auth_headers, bitbucket
):
    response = client.post(
        K8S_URL, json=_body(metadata, {"namespaces": ["ns"]}), headers=auth_headers
    )

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_kubernetes_auth_rejects_empty_lists(client, metadata, auth_headers):
    response = client.post(
        K8S_URL,
        json=_body(metadata, {"service_accounts": [], "namespaces": ["ns"]}),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_kubernetes_auth_rejects_a_bad_ttl(client, metadata, auth_headers):
    response = client.post(
        K8S_URL,
        json=_body(
            metadata, {"service_accounts": ["sa"], "namespaces": ["ns"], "ttl": "forever"}
        ),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_kubernetes_auth_on_a_missing_mount_returns_404(client, metadata, auth_headers):
    response = client.post(
        K8S_URL,
        json=_body(metadata, {"service_accounts": ["sa"], "namespaces": ["ns"]}),
        headers=auth_headers,
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# AD group bindings
# --------------------------------------------------------------------------- #
def test_group_binding_returns_200_and_commits_the_entity(
    client, payload, metadata, auth_headers, bitbucket
):
    _seed(bitbucket, payload)

    response = client.post(
        GROUPS_URL,
        json=_body(metadata, {"group": "AD\\payments-ro", "capability": "read"}),
        headers=auth_headers,
    )

    assert response.status_code == 200
    policies = {
        p["name"]: p for p in yaml.safe_load(bitbucket.committed[VALUES_PATH])["policies"]
    }
    assert "AD\\payments-ro" in policies[READ_POLICY]["entities"]
    assert "AD\\payments-ro" not in policies[WRITE_POLICY]["entities"]


def test_group_binding_write_capability(
    client, payload, metadata, auth_headers, bitbucket
):
    _seed(bitbucket, payload)

    client.post(
        GROUPS_URL,
        json=_body(metadata, {"group": "AD\\payments-rw", "capability": "write"}),
        headers=auth_headers,
    )

    policies = {
        p["name"]: p for p in yaml.safe_load(bitbucket.committed[VALUES_PATH])["policies"]
    }
    assert "AD\\payments-rw" in policies[WRITE_POLICY]["entities"]


def test_group_binding_rejects_an_unknown_capability(client, metadata, auth_headers):
    response = client.post(
        GROUPS_URL,
        json=_body(metadata, {"group": "AD\\x", "capability": "admin"}),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_group_binding_rejects_a_blank_group(client, metadata, auth_headers):
    response = client.post(
        GROUPS_URL,
        json=_body(metadata, {"group": "   ", "capability": "read"}),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_group_binding_requires_a_capability(client, metadata, auth_headers):
    response = client.post(
        GROUPS_URL, json=_body(metadata, {"group": "AD\\x"}), headers=auth_headers
    )

    assert response.status_code == 422


def test_group_binding_on_a_file_without_policies_returns_422(
    client, metadata, auth_headers, bitbucket
):
    bitbucket.existing_files[VALUES_PATH] = yaml.safe_dump({"mount": {"path": MOUNT_PATH}})

    response = client.post(
        GROUPS_URL,
        json=_body(metadata, {"group": "AD\\x", "capability": "read"}),
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert "no 'read' policy" in response.json()["error"]
