"""The delete endpoints end-to-end, plus the path-parameter validation they share.

Two shapes live on the same prefix here — `/{kv_name}` and `/{kv_name}/pull-request` — so
the routing tests matter as much as the status codes.
"""
import yaml

from app.helpers import (
    build_k8s_service_account,
    build_kv_store,
    build_kv_stores_document,
    render_values_yaml,
)
from app.v1.vault.conf import config
from tests.fakes import failing

KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
ROLES = {"read": ["app01.corp.example.com"]}

FILE_URL = f"{config.API_PREFIX}/files/{FILE}"
STORE_URL = f"{config.API_PREFIX}/{KV}"
DELETE_PR_URL = f"{STORE_URL}/pull-request"


def _seed(bitbucket, *names):
    bitbucket.existing_files[VALUES_PATH] = render_values_yaml(
        build_kv_stores_document(
            [build_kv_store(n, "payments secrets", ROLES) for n in names]
        )
    )


def _committed(bitbucket):
    return yaml.safe_load(bitbucket.committed[VALUES_PATH])


# --------------------------------------------------------------------------- #
# auth — the middleware is global, so every route must be covered
# --------------------------------------------------------------------------- #
def test_delete_requires_a_token(client):
    assert client.delete(STORE_URL).status_code == 401


def test_delete_pull_request_only_requires_a_token(client):
    assert client.delete(DELETE_PR_URL).status_code == 401


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
def test_delete_returns_200_and_the_success_message(client, auth_headers, bitbucket):
    _seed(bitbucket, "sibling", KV)

    response = client.delete(STORE_URL, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "Succeeded"
    assert body["message"] == f"Successful deletion of {KV}"
    assert body["kv_name"] == KV
    assert body["file"] == FILE
    assert body["pull_request"]["state"] == "MERGED"
    assert [s["name"] for s in _committed(bitbucket)["kvStores"]] == ["sibling"]


def test_deleting_the_last_store_leaves_the_file_with_an_empty_list(
    client, auth_headers, bitbucket
):
    _seed(bitbucket, KV)

    assert client.delete(STORE_URL, headers=auth_headers).status_code == 200
    assert _committed(bitbucket) == {"kvStores": []}


def test_delete_of_a_store_in_no_file_returns_404(client, auth_headers):
    response = client.delete(STORE_URL, headers=auth_headers)

    assert response.status_code == 404
    assert "is not defined in any file" in response.json()["error"]


def test_delete_of_a_missing_store_returns_404(client, auth_headers, bitbucket):
    _seed(bitbucket, "someone-else")

    response = client.delete(STORE_URL, headers=auth_headers)

    assert response.status_code == 404
    assert "not defined in" in response.json()["error"]


def test_deleting_a_bound_store_succeeds(client, auth_headers, bitbucket):
    """Bindings are nested in the store, so there is nothing left behind to refuse over."""
    document = build_kv_stores_document([build_kv_store(KV, "payments secrets", ROLES)])
    document["kvStores"][0]["roles"]["k8sServiceAccounts"] = [
        build_k8s_service_account("vault", "payments", "dev")
    ]
    bitbucket.existing_files[VALUES_PATH] = render_values_yaml(document)

    response = client.delete(STORE_URL, headers=auth_headers)

    assert response.status_code == 200
    assert _committed(bitbucket) == {"kvStores": []}


def test_delete_failed_validation_returns_502(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)
    bitbucket.builds = [failing()]

    response = client.delete(STORE_URL, headers=auth_headers)

    assert response.status_code == 502
    assert "Validation did not pass" in response.json()["error"]
    assert "decline_pull_request" in bitbucket.calls


def test_delete_upstream_timeout_returns_504(client, auth_headers, bitbucket):
    from tashtiot_apis_library.connectors import ExternalServiceError

    bitbucket.fail_on["get_file_content"] = ExternalServiceError(
        service_name="bitbucket", detail="Timed out calling bitbucket", status_code=504
    )

    response = client.delete(STORE_URL, headers=auth_headers)

    assert response.status_code == 504


# --------------------------------------------------------------------------- #
# delete /pull-request
# --------------------------------------------------------------------------- #
def test_delete_pull_request_only_returns_201_and_leaves_the_pr_open(
    client, auth_headers, bitbucket
):
    _seed(bitbucket, KV)

    response = client.delete(DELETE_PR_URL, headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "Succeeded"
    assert body["pull_request"]["state"] == "OPEN"
    assert body["validation_builds"] is None
    assert body["deploy_builds"] is None
    # Not "the gate passed" — the gate was never opened at all.
    assert "await_builds" not in bitbucket.calls


def test_delete_pull_request_only_of_a_missing_store_returns_404(
    client, auth_headers, bitbucket
):
    _seed(bitbucket, "someone-else")

    assert client.delete(DELETE_PR_URL, headers=auth_headers).status_code == 404


def test_the_two_delete_shapes_do_not_shadow_each_other(client, auth_headers, bitbucket):
    """Two segments must reach the PR-only route, one the blocking one."""
    _seed(bitbucket, KV, "pull-request")

    pr_only = client.delete(DELETE_PR_URL, headers=auth_headers)
    blocking = client.delete(STORE_URL, headers=auth_headers)

    assert pr_only.status_code == 201
    assert blocking.status_code == 200


def test_a_store_named_pull_request_is_still_addressable(client, auth_headers, bitbucket):
    """`pull-request` is a legal store name, and one segment beats two.

    `DELETE /pull-request` is the store; `DELETE /pull-request/pull-request` would be the
    PR-only removal of it.
    """
    _seed(bitbucket, "pull-request")

    response = client.delete(f"{config.API_PREFIX}/pull-request", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["kv_name"] == "pull-request"


# --------------------------------------------------------------------------- #
# path parameters
#
# `kv_name` is pattern-checked in the URI exactly as it is in a create body, and so is
# `file` on the one route that still takes one. Nothing here is currently exploitable —
# Starlette's path convertor already refuses a slash — but an unchecked `file` surfaces
# as an opaque Bitbucket 404 instead of a 422.
# --------------------------------------------------------------------------- #
def test_a_malformed_file_is_422_not_404(client, auth_headers, bitbucket):
    response = client.get(f"{config.API_PREFIX}/files/Not_A_File", headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_a_malformed_kv_name_is_422(client, auth_headers, bitbucket):
    response = client.delete(f"{config.API_PREFIX}/Not_A_Name", headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_a_traversing_file_never_reaches_bitbucket(client, auth_headers, bitbucket):
    """`file` is still the path parameter that reaches a filesystem path.

    Percent-encoded because an HTTP client collapses a literal `..` out of the URL before
    it is ever sent — this is the form that actually arrives at the route.
    """
    response = client.get(f"{config.API_PREFIX}/files/%2e%2e", headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_the_read_routes_are_validated_too(client, auth_headers, bitbucket):
    assert client.get(f"{config.API_PREFIX}/UPPER", headers=auth_headers).status_code == 422
    assert (
        client.get(f"{config.API_PREFIX}/files/UPPER", headers=auth_headers).status_code
        == 422
    )
    assert bitbucket.calls == []


def test_the_update_route_is_validated_too(client, auth_headers, bitbucket):
    response = client.patch(
        f"{config.API_PREFIX}/Not_A_Name",
        json={"kv_description": "x"},
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert bitbucket.calls == []
