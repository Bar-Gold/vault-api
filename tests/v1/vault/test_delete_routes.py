"""The delete endpoints end-to-end, plus the path-parameter validation they share.

Two shapes live on the same prefix here — `/{file}/{kv_name}` and
`/{file}/{kv_name}/pull-request` — so the routing tests matter as much as the status codes.
"""
import yaml

from app.helpers import (
    add_kubernetes_auth_role,
    build_kubernetes_auth_role,
    build_kv_store,
    build_kv_stores_document,
    render_values_yaml,
)
from app.v1.vault.conf import config
from tests.fakes import make_pipeline

KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
ROLES = {"read": ["app01.corp.example.com"]}

FILE_URL = f"{config.API_PREFIX}/{FILE}"
STORE_URL = f"{config.API_PREFIX}/{FILE}/{KV}"
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


def test_delete_of_a_missing_file_returns_404(client, auth_headers):
    response = client.delete(STORE_URL, headers=auth_headers)

    assert response.status_code == 404
    assert "does not exist" in response.json()["error"]


def test_delete_of_a_missing_store_returns_404(client, auth_headers, bitbucket):
    _seed(bitbucket, "someone-else")

    response = client.delete(STORE_URL, headers=auth_headers)

    assert response.status_code == 404
    assert "not defined in" in response.json()["error"]


def test_deleting_a_referenced_store_returns_409(client, auth_headers, bitbucket):
    """Refusing beats orphaning the binding — and the message says how to unblock it."""
    _seed(bitbucket, KV)
    bitbucket.existing_files["kv/platform.yaml"] = render_values_yaml(
        add_kubernetes_auth_role(
            build_kv_stores_document([]),
            build_kubernetes_auth_role(
                "myapp-ci", "CI deployer", ["vault-reader"], ["payments"], {"read": [KV]}
            ),
        )
    )

    response = client.delete(STORE_URL, headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["error"] == (
        f"{KV} is referenced by kubernetesAuth role(s) myapp-ci (kv/platform.yaml); "
        f"delete those first"
    )
    assert "create_branch" not in bitbucket.calls


def test_delete_failed_validation_returns_502(client, auth_headers, bitbucket, woodpecker):
    _seed(bitbucket, KV)
    woodpecker.results = [make_pipeline(number=2, status="failure", event="pull_request")]

    response = client.delete(STORE_URL, headers=auth_headers)

    assert response.status_code == 502
    assert "Validation pipeline #2" in response.json()["error"]
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
    client, auth_headers, bitbucket, woodpecker
):
    _seed(bitbucket, KV)

    response = client.delete(DELETE_PR_URL, headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "Succeeded"
    assert body["pull_request"]["state"] == "OPEN"
    assert body["validation_pipeline"] is None
    assert body["deploy_pipeline"] is None
    # The fake raises if a pipeline is awaited unscripted; assert Woodpecker was never used.
    assert woodpecker.calls == []


def test_delete_pull_request_only_of_a_missing_store_returns_404(
    client, auth_headers, bitbucket
):
    _seed(bitbucket, "someone-else")

    assert client.delete(DELETE_PR_URL, headers=auth_headers).status_code == 404


def test_the_two_delete_shapes_do_not_shadow_each_other(client, auth_headers, bitbucket):
    """Three segments must reach the PR-only route, two the blocking one."""
    _seed(bitbucket, KV, "pull-request")

    pr_only = client.delete(DELETE_PR_URL, headers=auth_headers)
    blocking = client.delete(STORE_URL, headers=auth_headers)

    assert pr_only.status_code == 201
    assert blocking.status_code == 200


def test_a_store_named_pull_request_is_still_addressable(client, auth_headers, bitbucket):
    """`pull-request` is a legal store name; only the third segment is fixed."""
    _seed(bitbucket, "pull-request")

    response = client.delete(f"{FILE_URL}/pull-request", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["kv_name"] == "pull-request"


# --------------------------------------------------------------------------- #
# path parameters
#
# `file` and `kv_name` are pattern-checked in the URI exactly as they are in a create body.
# Nothing here is currently exploitable — Starlette's path convertor already refuses a
# slash — but an unchecked `file` surfaces as an opaque Bitbucket 404 instead of a 422.
# --------------------------------------------------------------------------- #
def test_a_malformed_file_is_422_not_404(client, auth_headers, bitbucket):
    response = client.delete(f"{config.API_PREFIX}/Not_A_File/{KV}", headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_a_malformed_kv_name_is_422(client, auth_headers, bitbucket):
    response = client.delete(f"{FILE_URL}/Not_A_Name", headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_a_traversing_file_never_reaches_bitbucket(client, auth_headers, bitbucket):
    """`file` is the only path parameter that reaches a filesystem path.

    Percent-encoded because an HTTP client collapses a literal `..` out of the URL before
    it is ever sent — this is the form that actually arrives at the route.
    """
    response = client.get(f"{config.API_PREFIX}/%2e%2e", headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_the_read_routes_are_validated_too(client, auth_headers, bitbucket):
    assert client.get(f"{config.API_PREFIX}/UPPER", headers=auth_headers).status_code == 422
    assert client.get(f"{FILE_URL}/UPPER", headers=auth_headers).status_code == 422
    assert bitbucket.calls == []


def test_the_update_route_is_validated_too(client, auth_headers, bitbucket):
    response = client.patch(
        f"{FILE_URL}/Not_A_Name", json={"kv_description": "x"}, headers=auth_headers
    )

    assert response.status_code == 422
    assert bitbucket.calls == []
