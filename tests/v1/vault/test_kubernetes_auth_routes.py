"""The Kubernetes auth endpoints end-to-end, and the routing that keeps them reachable.

The prefix is separate from `/kv` precisely so the two can never shadow each other, but
within it `/pull-request` still sits in a `{file}` position — the same trade the KV router
makes, and the same rule pins it here.
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

ROLE = "myapp-ci"
KV = "myapp"
FILE = "payments"
VALUES_PATH = "kv/payments.yaml"
ROLES = {"read": ["app01.corp.example.com"]}

PREFIX = config.API_K8S_AUTH_PREFIX
FILE_URL = f"{PREFIX}/{FILE}"
ROLE_URL = f"{FILE_URL}/{ROLE}"
DELETE_PR_URL = f"{ROLE_URL}/pull-request"
CREATE_PR_URL = f"{PREFIX}/pull-request"

BODY = {
    "file": FILE,
    "role_name": ROLE,
    "role_description": "CI deployer for the payments app",
    "cluster": "prod-il-1",
    "service_accounts": ["vault-reader"],
    "namespaces": ["payments"],
    "access": {"read": [KV]},
}


def _role(name=ROLE, cluster="prod-il-1", access=None):
    return build_kubernetes_auth_role(
        name,
        "CI deployer",
        ["vault-reader"],
        ["payments"],
        access or {"read": [KV]},
        cluster=cluster,
    )


def _seed(bitbucket, *stores, roles=(), path=VALUES_PATH):
    document = build_kv_stores_document(
        [build_kv_store(s, "payments secrets", ROLES) for s in stores]
    )
    for role in roles:
        document = add_kubernetes_auth_role(document, role)
    bitbucket.existing_files[path] = render_values_yaml(document)


def _committed(bitbucket):
    return yaml.safe_load(bitbucket.committed[VALUES_PATH])


def _body(**overrides):
    body = dict(BODY)
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# auth — the middleware is global, so every route must be covered
# --------------------------------------------------------------------------- #
def test_every_route_requires_a_token(client):
    assert client.post(f"{PREFIX}/", json=BODY).status_code == 401
    assert client.post(CREATE_PR_URL, json=BODY).status_code == 401
    assert client.get(FILE_URL).status_code == 401
    assert client.get(ROLE_URL).status_code == 401
    assert client.patch(ROLE_URL, json={"ttl": "1h"}).status_code == 401
    assert client.delete(ROLE_URL).status_code == 401
    assert client.delete(DELETE_PR_URL).status_code == 401


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
def test_create_returns_201_and_the_committed_role(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    response = client.post(f"{PREFIX}/", json=BODY, headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "Succeeded"
    assert body["message"] == f"Successful creation of {ROLE}"
    assert body["role_name"] == ROLE
    assert body["kv_name"] == ""
    assert body["file"] == FILE
    assert body["pull_request"]["state"] == "MERGED"
    assert [r["name"] for r in _committed(bitbucket)["kubernetesAuth"]] == [ROLE]


def test_create_keeps_the_kv_stores_in_the_shared_file(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, "billing")

    assert client.post(f"{PREFIX}/", json=BODY, headers=auth_headers).status_code == 201
    assert [s["name"] for s in _committed(bitbucket)["kvStores"]] == [KV, "billing"]


def test_create_without_a_cluster_is_accepted(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)
    body = _body()
    del body["cluster"]

    response = client.post(f"{PREFIX}/", json=body, headers=auth_headers)

    assert response.status_code == 201
    assert "cluster" not in _committed(bitbucket)["kubernetesAuth"][0]


def test_a_duplicate_identity_returns_409(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, roles=[_role()])

    response = client.post(f"{PREFIX}/", json=BODY, headers=auth_headers)

    assert response.status_code == 409
    assert "already exists" in response.json()["error"]
    assert response.json()["role_name"] == ROLE


def test_access_naming_an_unknown_store_returns_409(client, auth_headers, bitbucket):
    _seed(bitbucket, "something-else")

    response = client.post(f"{PREFIX}/", json=BODY, headers=auth_headers)

    assert response.status_code == 409
    assert "unknown KV store(s)" in response.json()["error"]


def test_failed_validation_returns_502(client, auth_headers, bitbucket, woodpecker):
    _seed(bitbucket, KV)
    woodpecker.results = [make_pipeline(number=2, status="failure", event="pull_request")]

    response = client.post(f"{PREFIX}/", json=BODY, headers=auth_headers)

    assert response.status_code == 502
    assert response.json()["role_name"] == ROLE
    assert "decline_pull_request" in bitbucket.calls


def test_an_upstream_timeout_returns_504(client, auth_headers, bitbucket):
    from tashtiot_apis_library.connectors import ExternalServiceError

    bitbucket.fail_on["list_files"] = ExternalServiceError(
        service_name="bitbucket", detail="Timed out calling bitbucket", status_code=504
    )

    response = client.post(f"{PREFIX}/", json=BODY, headers=auth_headers)

    assert response.status_code == 504


# --------------------------------------------------------------------------- #
# create /pull-request
# --------------------------------------------------------------------------- #
def test_pull_request_only_returns_201_and_leaves_the_pr_open(
    client, auth_headers, bitbucket, woodpecker
):
    _seed(bitbucket, KV)

    response = client.post(CREATE_PR_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["pull_request"]["state"] == "OPEN"
    assert body["validation_pipeline"] is None
    assert body["deploy_pipeline"] is None
    # The fake raises if a pipeline is awaited unscripted; assert Woodpecker was never used.
    assert woodpecker.calls == []


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def test_reading_a_file_returns_the_role_list(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, roles=[_role(), _role(name="other")])

    response = client.get(FILE_URL, headers=auth_headers)

    assert response.status_code == 200
    assert [r["name"] for r in response.json()] == [ROLE, "other"]


def test_a_file_with_no_roles_returns_an_empty_list(client, auth_headers, bitbucket):
    """The file exists; it just declares no roles."""
    _seed(bitbucket, KV)

    response = client.get(FILE_URL, headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == []


def test_reading_a_missing_file_returns_404(client, auth_headers):
    assert client.get(FILE_URL, headers=auth_headers).status_code == 404


def test_reading_one_role_returns_the_entry(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, roles=[_role()])

    response = client.get(ROLE_URL, headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["access"] == {"read": [KV]}


def test_reading_an_unknown_role_returns_404(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    assert client.get(ROLE_URL, headers=auth_headers).status_code == 404


# --------------------------------------------------------------------------- #
# update and delete
# --------------------------------------------------------------------------- #
def test_update_returns_200(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, roles=[_role()])

    response = client.patch(
        ROLE_URL, json={"namespaces": ["payments-staging"]}, headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["role_name"] == ROLE
    assert _committed(bitbucket)["kubernetesAuth"][0]["namespaces"] == [
        "payments-staging"
    ]


def test_a_no_op_update_returns_200_with_no_pull_request(
    client, auth_headers, bitbucket
):
    _seed(bitbucket, KV, roles=[_role()])

    response = client.patch(ROLE_URL, json={"namespaces": ["payments"]}, headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["pull_request"] is None
    assert response.json()["message"] == f"No changes required for {ROLE}"


def test_update_of_an_unknown_role_returns_404(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    response = client.patch(ROLE_URL, json={"ttl": "1h"}, headers=auth_headers)

    assert response.status_code == 404


def test_delete_returns_200(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, roles=[_role()])

    response = client.delete(ROLE_URL, headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["message"] == f"Successful deletion of {ROLE}"
    assert _committed(bitbucket)["kubernetesAuth"] == []


def test_a_repeat_delete_returns_404(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, roles=[_role()])
    assert client.delete(ROLE_URL, headers=auth_headers).status_code == 200
    bitbucket.existing_files[VALUES_PATH] = bitbucket.committed[VALUES_PATH]

    assert client.delete(ROLE_URL, headers=auth_headers).status_code == 404


def test_delete_pull_request_only_returns_201(client, auth_headers, bitbucket, woodpecker):
    _seed(bitbucket, KV, roles=[_role()])

    response = client.delete(DELETE_PR_URL, headers=auth_headers)

    assert response.status_code == 201
    assert response.json()["pull_request"]["state"] == "OPEN"
    assert woodpecker.calls == []


# --------------------------------------------------------------------------- #
# routing
#
# `/pull-request` is the only fixed segment sitting in a variable position, so it has to be
# registered before `/{file}` — otherwise a file with that name shadows the endpoint.
# --------------------------------------------------------------------------- #
def test_the_create_pull_request_route_wins_over_a_file_of_that_name(
    client, auth_headers, bitbucket
):
    _seed(bitbucket, KV)
    _seed(bitbucket, "unrelated", roles=[_role(name="ci")], path="kv/pull-request.yaml")

    response = client.post(CREATE_PR_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 201
    assert response.json()["pull_request"]["state"] == "OPEN"


def test_a_file_named_pull_request_is_still_readable(client, auth_headers, bitbucket):
    """A GET on the same URL is an ordinary read; the two coexist."""
    _seed(bitbucket, KV, roles=[_role(name="ci")], path="kv/pull-request.yaml")

    response = client.get(CREATE_PR_URL, headers=auth_headers)

    assert response.status_code == 200
    assert [r["name"] for r in response.json()] == ["ci"]


def test_the_two_delete_shapes_do_not_shadow_each_other(client, auth_headers, bitbucket):
    """Three segments must reach the PR-only route, two the blocking one."""
    _seed(bitbucket, KV, roles=[_role(), _role(name="pull-request")])

    pr_only = client.delete(DELETE_PR_URL, headers=auth_headers)
    blocking = client.delete(ROLE_URL, headers=auth_headers)

    assert pr_only.status_code == 201
    assert blocking.status_code == 200


def test_a_role_named_pull_request_is_still_addressable(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, roles=[_role(name="pull-request")])

    response = client.delete(f"{FILE_URL}/pull-request", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["role_name"] == "pull-request"


def test_the_two_prefixes_do_not_shadow_each_other(client, auth_headers, bitbucket):
    """A separate prefix is the whole reason this cannot collide with `/kv`."""
    _seed(bitbucket, KV, roles=[_role()])

    kv = client.get(f"{config.API_PREFIX}/{FILE}/{KV}", headers=auth_headers)
    role = client.get(ROLE_URL, headers=auth_headers)

    assert kv.status_code == 200
    assert kv.json()["name"] == KV
    assert role.status_code == 200
    assert role.json()["name"] == ROLE


def test_a_file_named_kubernetes_auth_is_reachable_under_kv(
    client, auth_headers, bitbucket
):
    _seed(bitbucket, KV, path="kv/kubernetes-auth.yaml")

    response = client.get(f"{config.API_PREFIX}/kubernetes-auth", headers=auth_headers)

    assert response.status_code == 200
    assert [s["name"] for s in response.json()["kvStores"]] == [KV]


# --------------------------------------------------------------------------- #
# request validation
# --------------------------------------------------------------------------- #
def test_a_malformed_file_is_422_not_404(client, auth_headers, bitbucket):
    response = client.delete(f"{PREFIX}/Not_A_File/{ROLE}", headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_a_malformed_role_name_is_422(client, auth_headers, bitbucket):
    assert client.get(f"{FILE_URL}/Not_A_Role", headers=auth_headers).status_code == 422
    assert client.delete(f"{FILE_URL}/Not_A_Role", headers=auth_headers).status_code == 422
    assert (
        client.patch(
            f"{FILE_URL}/Not_A_Role", json={"ttl": "1h"}, headers=auth_headers
        ).status_code
        == 422
    )
    assert bitbucket.calls == []


def test_a_traversing_file_never_reaches_bitbucket(client, auth_headers, bitbucket):
    response = client.get(f"{PREFIX}/%2e%2e", headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_a_wildcard_namespace_is_422(client, auth_headers, bitbucket):
    response = client.post(
        f"{PREFIX}/", json=_body(namespaces=["*"]), headers=auth_headers
    )

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_an_unknown_access_key_is_422(client, auth_headers, bitbucket):
    response = client.post(
        f"{PREFIX}/", json=_body(access={"admin": [KV]}), headers=auth_headers
    )

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_an_empty_update_is_422(client, auth_headers, bitbucket):
    response = client.patch(ROLE_URL, json={}, headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


# --------------------------------------------------------------------------- #
# documentation
# --------------------------------------------------------------------------- #
def test_the_openapi_surface_is_the_full_route_table(client):
    paths = client.get("/openapi.json").json()["paths"]
    surface = {
        (method.upper(), path)
        for path, operations in paths.items()
        for method in operations
        if path.startswith(PREFIX)
    }

    assert surface == {
        ("POST", f"{PREFIX}/"),
        ("POST", f"{PREFIX}/pull-request"),
        ("GET", PREFIX + "/{file}"),
        ("GET", PREFIX + "/{file}/{role_name}"),
        ("PATCH", PREFIX + "/{file}/{role_name}"),
        ("DELETE", PREFIX + "/{file}/{role_name}"),
        ("DELETE", PREFIX + "/{file}/{role_name}/pull-request"),
    }


def test_the_create_schema_carries_a_readable_example(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert schemas["VaultKubernetesAuthCreate"]["example"] == {
        "file": "payments",
        "role_name": "myapp-ci",
        "role_description": "CI deployer for the payments app",
        "cluster": "prod-il-1",
        "service_accounts": ["vault-reader"],
        "namespaces": ["payments"],
        "access": {"read": ["myapp"]},
        "ttl": "24h",
    }


def test_the_create_example_is_actually_valid(client):
    """An example the schema would reject is worse than none."""
    from app.v1.vault.schemas import VaultKubernetesAuthCreate

    example = client.get("/openapi.json").json()["components"]["schemas"][
        "VaultKubernetesAuthCreate"
    ]["example"]

    assert VaultKubernetesAuthCreate(**example).role_name == "myapp-ci"


def test_the_response_model_carries_both_coordinates(client):
    """Renamed from VaultKVOperationResponse, and wire-compatible with it."""
    properties = client.get("/openapi.json").json()["components"]["schemas"][
        "VaultOperationResponse"
    ]["properties"]

    assert {"status", "message", "file", "kv_name", "role_name"} <= set(properties)
