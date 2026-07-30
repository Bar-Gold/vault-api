"""Route behaviour end-to-end through the app: auth, status codes and response bodies.

The `client` fixture builds the real app with both connectors replaced by fakes, so these
tests exercise routing, the global AuthMiddleware, validation and the error mapping.
"""
import yaml
from fastapi.encoders import jsonable_encoder

from app.clients.woodpecker import PipelineTimeoutError
from app.v1.vault.conf import config
from tests.fakes import make_pipeline

CREATE_URL = f"{config.API_PREFIX}/"
MOUNT_PATH = "myapp"
VALUES_PATH = "kv/prod/myapp.yaml"


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
    assert client.get(f"{config.API_PREFIX}/myapp?environment=prod").status_code == 401


# --------------------------------------------------------------------------- #
# create — success
# --------------------------------------------------------------------------- #
def test_create_returns_201_and_the_success_message(client, payload, auth_headers):
    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "Succeeded"
    assert body["message"] == f"Successful creation of {MOUNT_PATH}"
    assert body["mount_path"] == MOUNT_PATH
    assert body["pull_request"]["id"] == 101
    assert body["validation_pipeline"]["status"] == "success"
    assert body["deploy_pipeline"]["status"] == "success"
    assert body["error"] is None


def test_create_merges_the_pull_request(client, payload, auth_headers, bitbucket):
    client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert "merge_pull_request" in bitbucket.calls
    assert bitbucket.pull_requests[101].state == "MERGED"


# --------------------------------------------------------------------------- #
# create — failures
# --------------------------------------------------------------------------- #
def test_failed_validation_pipeline_returns_502_with_the_error(
    client, payload, auth_headers, woodpecker, bitbucket
):
    woodpecker.results = [make_pipeline(number=2, status="failure", event="pull_request")]

    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "Failed"
    assert "Validation pipeline #2 finished with status 'failure'" in body["error"]
    assert body["mount_path"] == MOUNT_PATH
    assert "decline_pull_request" in bitbucket.calls


def test_failed_deploy_pipeline_returns_502_and_says_it_is_merged(
    client, payload, auth_headers, woodpecker
):
    woodpecker.results = [
        make_pipeline(number=2, status="success", event="pull_request"),
        make_pipeline(number=3, status="error", event="push"),
    ]

    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 502
    body = response.json()
    assert "Deploy pipeline #3 finished with status 'error'" in body["error"]
    assert "already merged" in body["error"]
    assert body["deploy_pipeline"]["number"] == 3


def test_pipeline_timeout_returns_504(client, payload, auth_headers, woodpecker):
    woodpecker.results = [PipelineTimeoutError("No matching Woodpecker pipeline appeared")]

    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 504
    assert "did not complete" in response.json()["error"]


def test_duplicate_mount_returns_409(client, payload, auth_headers, bitbucket):
    bitbucket.existing_files[VALUES_PATH] = "mount: {}\n"

    response = client.post(CREATE_URL, json=_body(payload), headers=auth_headers)

    assert response.status_code == 409
    assert "already exists" in response.json()["error"]


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
def test_invalid_app_name_is_rejected_before_any_call(
    client, payload, auth_headers, bitbucket
):
    body = _body(payload)
    body["spec"]["app_name"] = "Not_A_Valid_Name"

    response = client.post(CREATE_URL, json=body, headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_missing_spec_is_rejected(client, payload, auth_headers):
    body = _body(payload)
    body.pop("spec")

    assert client.post(CREATE_URL, json=body, headers=auth_headers).status_code == 422


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def test_read_returns_the_committed_values(client, auth_headers, bitbucket):
    bitbucket.existing_files[VALUES_PATH] = yaml.safe_dump({"mount": {"path": MOUNT_PATH}})

    response = client.get(f"{config.API_PREFIX}/myapp?environment=prod", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["mount"]["path"] == MOUNT_PATH


def test_read_corrupt_values_file_returns_502_not_500(client, auth_headers, bitbucket):
    bitbucket.existing_files[VALUES_PATH] = "mount: [unclosed\n  ::: bad"

    response = client.get(f"{config.API_PREFIX}/myapp?environment=prod", headers=auth_headers)

    assert response.status_code == 502
    assert "not valid YAML" in response.json()["error"]


def test_read_unknown_mount_returns_404(client, auth_headers):
    response = client.get(f"{config.API_PREFIX}/nope?environment=prod", headers=auth_headers)
    assert response.status_code == 404


def test_read_requires_the_environment_query_param(client, auth_headers):
    assert client.get(f"{config.API_PREFIX}/myapp", headers=auth_headers).status_code == 422
