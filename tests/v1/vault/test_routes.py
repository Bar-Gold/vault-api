"""Route behaviour end-to-end through the app: auth, status codes and response bodies.

The `client` fixture builds the real app with both connectors replaced by fakes, so these
tests exercise routing, the global AuthMiddleware, validation and the error mapping.
"""
from fastapi.encoders import jsonable_encoder

from app.clients.bitbucket import BuildTimeoutError
from app.helpers import build_kv_store, build_kv_stores_document, render_values_yaml
from app.v1.vault.conf import config
from app.clients.bitbucket import BuildTimeoutError
from tests.fakes import failing, passing

CREATE_URL = f"{config.API_PREFIX}/"
KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
ROLES = {"read": ["app01.corp.example.com"]}


def _file_with(*names):
    return render_values_yaml(
        build_kv_stores_document(
            [build_kv_store(n, "payments secrets", ROLES) for n in names]
        )
    )


def _body(payload):
    return jsonable_encoder(payload)


# --------------------------------------------------------------------------- #
# auth (global AuthMiddleware)
# --------------------------------------------------------------------------- #
def test_create_requires_a_token(client, payload):
    assert client.post(CREATE_URL, json=_body(payload)).status_code == 401


def test_create_rejects_a_bad_token(client, payload):
    response = client.post(
        CREATE_URL, json=_body(payload), headers={"Authorization": "Bearer garbage"}
    )
    assert response.status_code == 401


def test_read_requires_a_token(client):
    assert client.get(f"{config.API_PREFIX}/myapp").status_code == 401


# --------------------------------------------------------------------------- #
# create — success
# --------------------------------------------------------------------------- #
def test_create_returns_201_and_the_success_message(client, payload, auth_headers):
    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "Succeeded"
    assert body["message"] == f"Successful creation of {KV}"
    assert body["kv_name"] == KV
    assert body["pull_request"]["id"] == 101
    assert body["validation_builds"][0]["state"] == "SUCCESSFUL"
    assert body["deploy_builds"][0]["state"] == "SUCCESSFUL"
    assert body["error"] is None


def test_create_merges_the_pull_request(client, payload, auth_headers, bitbucket):
    client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert "merge_pull_request" in bitbucket.calls
    assert bitbucket.pull_requests[101].state == "MERGED"


# --------------------------------------------------------------------------- #
# create — failures
# --------------------------------------------------------------------------- #
def test_failed_validation_builds_returns_502_with_the_error(
    client, payload, auth_headers, bitbucket
):
    bitbucket.builds = [failing()]

    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "Failed"
    assert "Validation did not pass: ci/woodpecker/pr/build [FAILED]" in body["error"]
    assert body["kv_name"] == KV
    assert "decline_pull_request" in bitbucket.calls


def test_failed_deploy_builds_returns_502_and_says_it_is_merged(
    client, payload, auth_headers, bitbucket
):
    bitbucket.builds = [passing(), failing("ci/woodpecker/push/deploy")]

    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 502
    body = response.json()
    assert "Deploy did not pass" in body["error"]
    assert "already merged" in body["error"]
    assert body["deploy_builds"][0]["key"] == "ci/woodpecker/push/deploy"


def test_build_timeout_returns_504(client, payload, auth_headers, bitbucket):
    bitbucket.builds = [BuildTimeoutError("No build was reported against sha-x within 120s")]

    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 504
    assert "did not complete" in response.json()["error"]


def test_duplicate_store_returns_409(client, payload, auth_headers, bitbucket):
    bitbucket.existing_files[VALUES_PATH] = _file_with(KV)

    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 409
    assert "already exists" in response.json()["error"]


def test_a_name_taken_in_another_file_returns_409(client, payload, auth_headers, bitbucket):
    """Store names are global to Vault, so the scan covers the whole values directory."""
    bitbucket.existing_files["kv/other-team.yaml"] = _file_with(KV)

    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 409
    assert "kv/other-team.yaml" in response.json()["error"]


def test_upstream_timeout_returns_504(client, payload, auth_headers, bitbucket):
    """A timed-out upstream is a gateway timeout, matching how a pipeline timeout reports."""
    from tashtiot_apis_library.connectors import ExternalServiceError

    bitbucket.fail_on["get_file_content"] = ExternalServiceError(
        service_name="bitbucket", detail="Timed out calling bitbucket", status_code=504
    )

    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 504
    assert "Timed out" in response.json()["error"]


def test_bitbucket_outage_returns_502(client, payload, auth_headers, bitbucket):
    from tashtiot_apis_library.connectors import ExternalServiceError

    bitbucket.fail_on["get_file_content"] = ExternalServiceError(
        service_name="bitbucket", detail="service unavailable", status_code=503
    )

    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 502
    assert "bitbucket" in response.json()["error"]


# --------------------------------------------------------------------------- #
# create — validation
# --------------------------------------------------------------------------- #
def test_invalid_kv_name_is_rejected_before_any_call(
    client, payload, auth_headers, bitbucket
):
    body = _body(payload)
    body["kv_name"] = "Not_A_Valid_Name"

    response = client.post(CREATE_URL, json=body, headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_missing_description_is_rejected(client, payload, auth_headers):
    body = _body(payload)
    body.pop("kv_description")

    assert client.post(CREATE_URL, json=body, headers=auth_headers).status_code == 422


def test_traversal_in_the_file_is_rejected(client, payload, auth_headers, bitbucket):
    """`file` is the only request field that reaches a path, so '..' must not get through."""
    body = _body(payload)
    body["file"] = "../../etc/passwd"

    assert client.post(CREATE_URL, json=body, headers=auth_headers).status_code == 422
    assert bitbucket.calls == []


def test_missing_roles_is_rejected(client, payload, auth_headers, bitbucket):
    body = _body(payload)
    body.pop("roles")

    assert client.post(CREATE_URL, json=body, headers=auth_headers).status_code == 422
    assert bitbucket.calls == []


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def test_read_returns_the_committed_file(client, auth_headers, bitbucket):
    bitbucket.existing_files[VALUES_PATH] = _file_with(KV)

    response = client.get(f"{config.API_PREFIX}/files/{FILE}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["kvStores"][0]["name"] == KV


def test_read_corrupt_values_file_returns_502_not_500(client, auth_headers, bitbucket):
    bitbucket.existing_files[VALUES_PATH] = "kvStores: [unclosed\n  ::: bad"

    response = client.get(f"{config.API_PREFIX}/files/{FILE}", headers=auth_headers)

    assert response.status_code == 502
    assert "not valid YAML" in response.json()["error"]


def test_read_unknown_file_returns_404(client, auth_headers):
    response = client.get(f"{config.API_PREFIX}/nope", headers=auth_headers)
    assert response.status_code == 404


def test_read_takes_no_query_parameters(client, auth_headers, bitbucket):
    """The file name alone identifies the document — nothing to disambiguate."""
    bitbucket.existing_files[VALUES_PATH] = _file_with(KV)

    assert client.get(f"{config.API_PREFIX}/files/{FILE}", headers=auth_headers).status_code == 200
