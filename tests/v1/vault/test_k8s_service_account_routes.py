"""The service-account sub-resource end-to-end, plus its routing and parameter validation.

Four shapes now share the `/{file}/{kv_name}` prefix — the store itself, its
`/pull-request` delete, `/k8s-service-accounts`, and that one's `/pull-request` — so the
routing tests carry as much weight as the status codes.
"""
import yaml

from app.helpers import (
    build_k8s_service_account,
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

STORE_URL = f"{config.API_PREFIX}/{FILE}/{KV}"
SA_URL = f"{STORE_URL}/k8s-service-accounts"
SA_PR_URL = f"{SA_URL}/pull-request"

BODY = {"service_account": "vault", "namespace": "payments", "cluster": "dev"}
QUERY = {"service_account": "vault", "namespace": "payments", "cluster": "dev"}


def _seed(bitbucket, *names, accounts=None):
    document = build_kv_stores_document(
        [build_kv_store(n, "payments secrets", ROLES) for n in names]
    )
    if accounts is not None:
        for store in document["kvStores"]:
            store["roles"]["k8sServiceAccounts"] = list(accounts)
    bitbucket.existing_files[VALUES_PATH] = render_values_yaml(document)


def _committed(bitbucket):
    return yaml.safe_load(bitbucket.committed[VALUES_PATH])


# --------------------------------------------------------------------------- #
# binding
# --------------------------------------------------------------------------- #
def test_bind_returns_201(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    response = client.post(SA_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 201
    assert response.json()["status"] == "Succeeded"


def test_bind_writes_the_entry_inside_the_store(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    client.post(SA_URL, json=BODY, headers=auth_headers)

    document = _committed(bitbucket)
    assert "k8sServiceAccounts" not in document
    assert "k8sServiceAccounts" not in document["kvStores"][0]
    assert document["kvStores"][0]["roles"]["k8sServiceAccounts"] == [
        {"serviceAccount": "vault", "namespace": "payments", "cluster": "dev"}
    ]


def test_bind_response_carries_the_store_not_a_role_name(client, auth_headers, bitbucket):
    """A binding has no name of its own, so `kv_name` is the coordinate it reports."""
    _seed(bitbucket, KV)

    body = client.post(SA_URL, json=BODY, headers=auth_headers).json()

    assert body["kv_name"] == KV
    assert body["file"] == FILE
    assert "role_name" not in body


def test_bind_to_an_unknown_store_is_404(client, auth_headers, bitbucket):
    _seed(bitbucket, "someone-else")

    response = client.post(SA_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 404


def test_bind_to_an_unknown_file_is_404(client, auth_headers):
    assert client.post(SA_URL, json=BODY, headers=auth_headers).status_code == 404


def test_a_duplicate_binding_is_409(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account("vault", "payments", "dev")])

    response = client.post(SA_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 409
    assert "already bound" in response.json()["error"]


def test_a_wildcard_service_account_is_422(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    response = client.post(
        SA_URL, json={**BODY, "service_account": "*"}, headers=auth_headers
    )

    assert response.status_code == 422
    assert "create_branch" not in bitbucket.calls


def test_an_uppercase_namespace_is_422(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    response = client.post(
        SA_URL, json={**BODY, "namespace": "Payments"}, headers=auth_headers
    )

    assert response.status_code == 422


def test_a_missing_cluster_is_422(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)
    body = {k: v for k, v in BODY.items() if k != "cluster"}

    assert client.post(SA_URL, json=body, headers=auth_headers).status_code == 422


# --------------------------------------------------------------------------- #
# unbinding — the identity travels as query parameters
# --------------------------------------------------------------------------- #
def test_unbind_returns_200(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account("vault", "payments", "dev")])

    response = client.delete(SA_URL, params=QUERY, headers=auth_headers)

    assert response.status_code == 200
    assert "k8sServiceAccounts" not in _committed(bitbucket)["kvStores"][0]["roles"]


def test_unbind_needs_the_whole_triple(client, auth_headers, bitbucket):
    """Two of three cannot identify a binding, so the request is rejected outright."""
    _seed(bitbucket, KV, accounts=[build_k8s_service_account("vault", "payments", "dev")])
    partial = {k: v for k, v in QUERY.items() if k != "cluster"}

    response = client.delete(SA_URL, params=partial, headers=auth_headers)

    assert response.status_code == 422


def test_unbind_validates_the_query_parameters(client, auth_headers, bitbucket):
    """The same patterns the POST body enforces — a 422 beats a 404 two calls later."""
    _seed(bitbucket, KV)

    response = client.delete(
        SA_URL, params={**QUERY, "namespace": "Payments"}, headers=auth_headers
    )

    assert response.status_code == 422


def test_unbinding_something_not_bound_is_404(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    response = client.delete(SA_URL, params=QUERY, headers=auth_headers)

    assert response.status_code == 404
    assert "is not bound to" in response.json()["error"]


def test_a_repeat_unbind_is_404(client, auth_headers, bitbucket, woodpecker):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account("vault", "payments", "dev")])
    assert client.delete(SA_URL, params=QUERY, headers=auth_headers).status_code == 200

    woodpecker.results = [
        make_pipeline(number=4, status="success"),
        make_pipeline(number=5, status="success", event="push", commit="merge-sha-1"),
    ]
    bitbucket.existing_files[VALUES_PATH] = bitbucket.committed[VALUES_PATH]

    assert client.delete(SA_URL, params=QUERY, headers=auth_headers).status_code == 404


# --------------------------------------------------------------------------- #
# the PR-only twins
# --------------------------------------------------------------------------- #
def test_pr_only_bind_returns_201_and_does_not_merge(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    response = client.post(SA_PR_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 201
    assert response.json()["pull_request"]["state"] == "OPEN"
    assert response.json()["validation_pipeline"] is None
    assert "merge_pull_request" not in bitbucket.calls


def test_pr_only_unbind_returns_201_and_does_not_merge(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account("vault", "payments", "dev")])

    response = client.delete(SA_PR_URL, params=QUERY, headers=auth_headers)

    assert response.status_code == 201
    assert response.json()["pull_request"]["state"] == "OPEN"
    assert "merge_pull_request" not in bitbucket.calls


def test_pr_only_bind_shares_the_duplicate_guard(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account("vault", "payments", "dev")])

    assert client.post(SA_PR_URL, json=BODY, headers=auth_headers).status_code == 409


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def test_read_returns_the_bindings(client, auth_headers, bitbucket):
    _seed(bitbucket, KV, accounts=[build_k8s_service_account("vault", "payments", "dev")])

    response = client.get(SA_URL, headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == [
        {"serviceAccount": "vault", "namespace": "payments", "cluster": "dev"}
    ]


def test_read_of_a_store_that_binds_nothing_is_an_empty_list(client, auth_headers, bitbucket):
    _seed(bitbucket, KV)

    response = client.get(SA_URL, headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == []


def test_read_of_an_unknown_file_is_404(client, auth_headers):
    assert client.get(SA_URL, headers=auth_headers).status_code == 404


def test_reads_need_auth(client, bitbucket):
    _seed(bitbucket, KV)

    assert client.get(SA_URL).status_code == 401


# --------------------------------------------------------------------------- #
# routing — the fixed segment must beat the parameterised ones
# --------------------------------------------------------------------------- #
def test_a_store_named_k8s_service_accounts_stays_addressable(client, auth_headers, bitbucket):
    """Three segments against two — `DELETE /{file}/{kv_name}` still wins for this name."""
    _seed(bitbucket, "k8s-service-accounts")

    response = client.delete(
        f"{config.API_PREFIX}/{FILE}/k8s-service-accounts", headers=auth_headers
    )

    assert response.status_code == 200
    assert _committed(bitbucket) == {"kvStores": []}


def test_the_binding_route_wins_over_the_delete_pull_request_route(
    client, auth_headers, bitbucket
):
    """`/{kv}/k8s-service-accounts/pull-request` is four segments; the delete PR route is three."""
    _seed(bitbucket, KV)

    response = client.post(SA_PR_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 201
    assert response.json()["kv_name"] == KV


def test_a_store_named_pull_request_can_still_be_unbound(client, auth_headers, bitbucket):
    """`/{file}/pull-request/k8s-service-accounts` addresses a store, not an endpoint."""
    _seed(
        bitbucket,
        "pull-request",
        accounts=[build_k8s_service_account("vault", "payments", "dev")],
    )

    response = client.delete(
        f"{config.API_PREFIX}/{FILE}/pull-request/k8s-service-accounts",
        params=QUERY,
        headers=auth_headers,
    )

    assert response.status_code == 200


def test_a_malformed_file_is_422_not_404(client, auth_headers):
    response = client.get(
        f"{config.API_PREFIX}/Bad_File/{KV}/k8s-service-accounts", headers=auth_headers
    )

    assert response.status_code == 422
