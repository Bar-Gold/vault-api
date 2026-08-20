"""The HTTP surface, end-to-end through TestClient.

Three things live here that nowhere else covers: the status-code table, the routing
collisions between fixed segments and `/{kv_name}`, and the two failure funnels (`_execute`
for mutations, `_read` for GETs — they map an upstream 404 differently on purpose).
"""

import pytest
import yaml

from app.v1.vault.conf import config
from tests.fakes import failing, passing
from tests.v1.vault.conftest import (
    DN,
    K8S_PATH,
    KV,
    OIDC_PATH,
    OTHER_DN,
    STORE_DIR,
    TEAM,
    k8s_entry,
    oidc_entry,
    seed_store,
)

CREATE_URL = f"{config.API_PREFIX}/"
PR_URL = f"{config.API_PREFIX}/pull-request"
STORE_URL = f"{config.API_PREFIX}/{KV}"
ACCESS_URL = f"{STORE_URL}/access"
STORES_URL = f"{config.API_PREFIX}/stores"
TASHTIOT_URL = f"{config.API_PREFIX}/tashtiot/{TEAM}"

BODY = {
    "team": TEAM,
    "kv_name": KV,
    "access": [
        {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read", "write"]}
    ],
}


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def test_create_requires_a_token(client):
    assert client.post(CREATE_URL, json=BODY).status_code == 401


def test_create_rejects_a_bad_token(client):
    response = client.post(
        CREATE_URL, json=BODY, headers={"Authorization": "Bearer nonsense"}
    )
    assert response.status_code == 401


def test_docs_need_no_token(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
def test_create_returns_201_and_leaves_the_pull_request_open(
    client, auth_headers, bitbucket
):
    response = client.post(CREATE_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "Succeeded"
    assert f"to create {KV}" in body["message"]
    assert body["kv_name"] == KV
    assert body["file"] == STORE_DIR
    # Open, not merged: this service proposes and a human merges.
    assert body["pull_request"]["state"] == "OPEN"
    assert body["validation_builds"][0]["state"] == "SUCCESSFUL"


def test_create_commits_the_access_file(client, auth_headers, bitbucket):
    client.post(CREATE_URL, json=BODY, headers=auth_headers)

    committed = yaml.safe_load(bitbucket.committed[OIDC_PATH])
    assert committed["access"][0]["dn"] == DN
    assert committed["access"][0]["authType"] == "oidc"


def test_a_duplicate_store_returns_409(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    response = client.post(CREATE_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["status"] == "Failed"


def test_a_failed_validation_build_returns_502(client, auth_headers, bitbucket):
    bitbucket.builds = [failing()]

    response = client.post(CREATE_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 502
    assert "Validation did not pass" in response.json()["error"]


def test_a_build_timeout_returns_504(client, auth_headers, bitbucket):
    from app.clients.bitbucket import BuildTimeoutError

    bitbucket.builds = [BuildTimeoutError("no builds reported", [])]

    response = client.post(CREATE_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 504


def test_there_is_no_deploy_build_field(client, auth_headers, bitbucket):
    """Nothing waits for a deploy any more, so the field would always be null."""
    body = client.post(CREATE_URL, json=BODY, headers=auth_headers).json()

    assert "deploy_builds" not in body


@pytest.mark.parametrize("field,value", [
    ("kv_name", "Bad_Name"),
    ("team", "../etc"),
    ("access", []),
])
def test_a_malformed_body_is_422_before_any_call(
    client, auth_headers, bitbucket, field, value
):
    response = client.post(
        CREATE_URL, json={**BODY, field: value}, headers=auth_headers
    )

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_the_pull_request_only_route_returns_201_and_leaves_it_open(
    client, auth_headers, bitbucket
):
    response = client.post(PR_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["pull_request"]["state"] == "OPEN"
    assert body["validation_builds"] is None


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def test_get_a_store_returns_its_location_and_grants(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()], k8s=[k8s_entry()])

    body = client.get(STORE_URL, headers=auth_headers).json()

    assert body["team"] == TEAM
    assert body["directory"] == STORE_DIR
    assert [entry["authType"] for entry in body["access"]] == ["oidc", "k8s"]


def test_get_an_unknown_store_is_404(client, auth_headers):
    """`_read` maps a missing store to 404, not the 502 `_execute` would give."""
    response = client.get(STORE_URL, headers=auth_headers)

    assert response.status_code == 404


def test_get_the_access_list_alone(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    body = client.get(ACCESS_URL, headers=auth_headers).json()

    assert [entry["dn"] for entry in body] == [DN]


def test_listing_stores(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    body = client.get(STORES_URL, headers=auth_headers).json()

    assert body == [{"team": TEAM, "kv_name": KV, "directory": STORE_DIR}]


# --------------------------------------------------------------------------- #
# edits
# --------------------------------------------------------------------------- #
def test_patch_replaces_the_grants(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    response = client.patch(
        STORE_URL,
        json={"access": [
            {"path": "*", "auth_type": "oidc", "dn": OTHER_DN, "capabilities": ["read"]}
        ]},
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert yaml.safe_load(bitbucket.committed[OIDC_PATH])["access"][0]["dn"] == OTHER_DN


def test_a_no_op_patch_returns_200_with_no_pull_request(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    response = client.patch(
        STORE_URL,
        json={"access": [
            {"path": "*", "auth_type": "oidc", "dn": DN,
             "capabilities": ["read", "write"]}
        ]},
        headers=auth_headers,
    )

    # 200, not 201: no pull request was opened, so 201 would be a lie.
    assert response.status_code == 200
    assert response.json()["pull_request"] is None


def test_patch_on_an_unknown_store_is_404(client, auth_headers):
    response = client.patch(
        STORE_URL,
        json={"access": [
            {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read"]}
        ]},
        headers=auth_headers,
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# grants
# --------------------------------------------------------------------------- #
def test_adding_a_grant_returns_201(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    response = client.post(
        ACCESS_URL,
        json={"path": "*", "auth_type": "oidc", "dn": OTHER_DN, "capabilities": ["read"]},
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert len(yaml.safe_load(bitbucket.committed[OIDC_PATH])["access"]) == 2


def test_adding_a_duplicate_grant_returns_409(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    response = client.post(
        ACCESS_URL,
        json={"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read"]},
        headers=auth_headers,
    )

    assert response.status_code == 409


def test_revoking_a_grant_returns_201(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry(), oidc_entry(dn=OTHER_DN)])

    response = client.delete(
        f"{ACCESS_URL}?auth_type=oidc&path=*&dn={OTHER_DN}", headers=auth_headers
    )

    assert response.status_code == 201
    assert yaml.safe_load(bitbucket.committed[OIDC_PATH])["access"][0]["dn"] == DN


def test_revoking_a_k8s_grant_takes_all_three_scalars(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()], k8s=[k8s_entry()])

    response = client.delete(
        f"{ACCESS_URL}?auth_type=k8s&path=*&name=cloudops-external-secrets"
        f"&namespace=cloudops&cluster=dev",
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert bitbucket.deleted == [K8S_PATH]


def test_revoking_a_grant_that_is_not_there_is_404(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    response = client.delete(
        f"{ACCESS_URL}?auth_type=oidc&path=*&dn={OTHER_DN}", headers=auth_headers
    )

    assert response.status_code == 404


def test_a_k8s_revoke_missing_a_scalar_is_422(client, auth_headers, bitbucket):
    """A partial identity would resolve to *some* entry; revoking the wrong one is worse."""
    seed_store(bitbucket, oidc=[oidc_entry()], k8s=[k8s_entry()])

    response = client.delete(
        f"{ACCESS_URL}?auth_type=k8s&path=*&name=sa", headers=auth_headers
    )

    assert response.status_code == 422
    assert "create_branch" not in bitbucket.calls


def test_an_oidc_revoke_without_a_dn_is_422(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    response = client.delete(f"{ACCESS_URL}?auth_type=oidc&path=*", headers=auth_headers)

    assert response.status_code == 422


def test_mixing_the_two_identities_is_422(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    response = client.delete(
        f"{ACCESS_URL}?auth_type=oidc&path=*&dn={DN}&namespace=cloudops",
        headers=auth_headers,
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
def test_delete_returns_201_and_removes_the_directory(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()], k8s=[k8s_entry()])

    response = client.delete(STORE_URL, headers=auth_headers)

    assert response.status_code == 201
    assert bitbucket.deleted == [STORE_DIR]


def test_delete_of_an_unknown_store_is_404(client, auth_headers):
    assert client.delete(STORE_URL, headers=auth_headers).status_code == 404


def test_the_delete_pull_request_route_returns_201(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    response = client.delete(f"{STORE_URL}/pull-request", headers=auth_headers)

    assert response.status_code == 201
    assert response.json()["pull_request"]["state"] == "OPEN"


# --------------------------------------------------------------------------- #
# the Tashtiot layout
#
# A flat file per team, both kinds in one list, no directory and no oidc/k8s split.
# --------------------------------------------------------------------------- #
TASHTIOT_BODY = {
    "access": [
        {"path": "+/cloudops/*", "auth_type": "oidc", "dn": DN, "capabilities": ["read"]},
        {"path": "services/+/tyk/*", "auth_type": "k8s", "name": "argocd",
         "namespace": "perimeter", "cluster": "dev", "capabilities": ["read", "write"]},
    ]
}
TASHTIOT_PATH = f"KVs/Tashtiot/{TEAM}.yaml"


def test_tashtiot_upsert_writes_one_flat_file(client, auth_headers, bitbucket):
    response = client.put(TASHTIOT_URL, json=TASHTIOT_BODY, headers=auth_headers)

    assert response.status_code == 201
    assert list(bitbucket.committed) == [TASHTIOT_PATH]


def test_tashtiot_mixes_both_kinds_in_one_list(client, auth_headers, bitbucket):
    """The whole difference from a store: no split by authType."""
    client.put(TASHTIOT_URL, json=TASHTIOT_BODY, headers=auth_headers)

    committed = yaml.safe_load(bitbucket.committed[TASHTIOT_PATH])
    assert [entry["authType"] for entry in committed["access"]] == ["oidc", "k8s"]


def test_tashtiot_keeps_the_scoped_paths(client, auth_headers, bitbucket):
    client.put(TASHTIOT_URL, json=TASHTIOT_BODY, headers=auth_headers)

    committed = yaml.safe_load(bitbucket.committed[TASHTIOT_PATH])
    assert committed["access"][0]["path"] == "+/cloudops/*"


def test_a_second_tashtiot_upsert_replaces(client, auth_headers, bitbucket):
    # Two full chains, so two gates each: the fake's queue is popped once per gate.
    bitbucket.builds = [passing(), passing("ci/woodpecker/push/deploy")] * 2
    client.put(TASHTIOT_URL, json=TASHTIOT_BODY, headers=auth_headers)
    bitbucket.existing_files[TASHTIOT_PATH] = bitbucket.committed[TASHTIOT_PATH]
    bitbucket.committed.clear()

    response = client.put(
        TASHTIOT_URL,
        json={"access": [
            {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read"]}
        ]},
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert len(yaml.safe_load(bitbucket.committed[TASHTIOT_PATH])["access"]) == 1


def test_an_unchanged_tashtiot_upsert_opens_no_pull_request(client, auth_headers, bitbucket):
    client.put(TASHTIOT_URL, json=TASHTIOT_BODY, headers=auth_headers)
    bitbucket.existing_files[TASHTIOT_PATH] = bitbucket.committed[TASHTIOT_PATH]

    response = client.put(TASHTIOT_URL, json=TASHTIOT_BODY, headers=auth_headers)

    assert response.json()["pull_request"] is None


def test_reading_tashtiot_returns_the_parsed_file(client, auth_headers, bitbucket):
    bitbucket.existing_files[TASHTIOT_PATH] = (
        "access:\n  - path: '*'\n    authType: oidc\n    dn: x\n"
        "    capabilities:\n      - read\n"
    )

    body = client.get(TASHTIOT_URL, headers=auth_headers).json()

    assert body["access"][0]["dn"] == "x"


def test_reading_a_missing_tashtiot_file_is_404(client, auth_headers):
    assert client.get(TASHTIOT_URL, headers=auth_headers).status_code == 404


def test_deleting_tashtiot_removes_the_file(client, auth_headers, bitbucket):
    bitbucket.existing_files[TASHTIOT_PATH] = "access: []\n"

    response = client.delete(TASHTIOT_URL, headers=auth_headers)

    assert response.status_code == 201
    assert bitbucket.deleted == [TASHTIOT_PATH]


def test_deleting_a_missing_tashtiot_file_is_404(client, auth_headers):
    assert client.delete(TASHTIOT_URL, headers=auth_headers).status_code == 404


def test_the_tashtiot_file_is_not_a_store(client, auth_headers, bitbucket):
    """Two segments, not three — the store walk must not pick it up."""
    bitbucket.existing_files[TASHTIOT_PATH] = "access: []\n"

    assert client.get(STORES_URL, headers=auth_headers).json() == []


# --------------------------------------------------------------------------- #
# routing collisions
#
# Every fixed segment is registered before `/{kv_name}`. Without that a store named after
# one of them would shadow the endpoint.
# --------------------------------------------------------------------------- #
def test_the_create_pull_request_segment_is_not_swallowed(client, auth_headers, bitbucket):
    response = client.post(PR_URL, json=BODY, headers=auth_headers)

    assert response.status_code == 201


def test_a_store_named_pull_request_is_still_readable(client, auth_headers, bitbucket):
    """`GET /pull-request` is a normal store read — the two coexist."""
    seed_store(bitbucket, kv_name="pull-request", oidc=[oidc_entry()])

    response = client.get(f"{config.API_PREFIX}/pull-request", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["kv_name"] == "pull-request"


def test_a_store_named_stores_is_shadowed_by_the_listing(client, auth_headers, bitbucket):
    """A known trade-off: `/stores` is registered first and wins."""
    seed_store(bitbucket, kv_name="stores", oidc=[oidc_entry()])

    body = client.get(STORES_URL, headers=auth_headers).json()

    assert isinstance(body, list)


def test_a_store_named_access_stays_addressable(client, auth_headers, bitbucket):
    """One segment against two — `/{kv_name}` still wins for a bare name."""
    seed_store(bitbucket, kv_name="access", oidc=[oidc_entry()])

    response = client.get(f"{config.API_PREFIX}/access", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["kv_name"] == "access"


def test_a_store_named_tashtiot_does_not_shadow_the_team_routes(
    client, auth_headers, bitbucket
):
    """`/tashtiot/{team}` is two segments; a store read is one."""
    seed_store(bitbucket, kv_name="tashtiot", oidc=[oidc_entry()])

    assert client.get(f"{config.API_PREFIX}/tashtiot", headers=auth_headers).status_code == 200
    assert client.get(TASHTIOT_URL, headers=auth_headers).status_code == 404


def test_a_malformed_store_name_in_the_path_is_422(client, auth_headers, bitbucket):
    """Starlette's convertor is `[^/]+`, so the pattern is what makes this a 422."""
    response = client.get(f"{config.API_PREFIX}/Bad_Name", headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []
