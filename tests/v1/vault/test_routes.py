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


def test_reading_the_access_list_alone_is_gone(client, auth_headers, bitbucket):
    """It answered `GET /{kv_name}`'s `access` list unwrapped — the same upstream work for
    strictly less. POST and DELETE still live on the path, so this is a 405, not a 404."""
    seed_store(bitbucket, oidc=[oidc_entry()])

    assert client.get(ACCESS_URL, headers=auth_headers).status_code == 405


def test_the_store_read_is_where_the_grants_are_now(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    body = client.get(STORE_URL, headers=auth_headers).json()

    assert [entry["dn"] for entry in body["access"]] == [DN]


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


def test_the_tashtiot_message_reads_as_an_intent_not_a_past_act(
    client, auth_headers, bitbucket
):
    """"to create", never "to created".

    The summary and the message need different tenses off the same create/update
    decision, and sharing one verb between them produced "to created".
    """
    created = client.put(TASHTIOT_URL, json=TASHTIOT_BODY, headers=auth_headers)
    assert "to create " in created.json()["message"]
    assert "to created" not in created.json()["message"]

    # ...and the update branch of the same decision.
    bitbucket.existing_files[TASHTIOT_PATH] = bitbucket.committed[TASHTIOT_PATH]
    bitbucket.builds = [passing()]
    updated = client.put(
        TASHTIOT_URL,
        json={"access": [
            {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read"]}
        ]},
        headers=auth_headers,
    )
    assert "to update " in updated.json()["message"]
    assert "to updated" not in updated.json()["message"]


def test_the_tashtiot_pull_request_title_stays_past_tense(client, auth_headers, bitbucket):
    """The summary is the other half of that split — it must not follow the message."""
    client.put(TASHTIOT_URL, json=TASHTIOT_BODY, headers=auth_headers)

    assert bitbucket.pull_requests[101].title.startswith("Created ")


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


def test_a_store_named_pull_request_has_nothing_to_collide_with(
    client, auth_headers, bitbucket
):
    """The routes that owned that segment are gone, so the name is now unremarkable."""
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


# --------------------------------------------------------------------------- #
# the routes that skipped CI are gone
#
# They opened a pull request and returned without watching the build. Without that wait a
# caller could not know the change was green, which is the one guarantee this service still
# offers now that it does not merge. Pinned at the HTTP level so re-adding one is a
# deliberate act rather than an accident.
# --------------------------------------------------------------------------- #
def test_the_create_pull_request_route_is_gone(client, auth_headers, bitbucket):
    """`/pull-request` now falls through to `/{kv_name}`, which has no POST."""
    response = client.post(
        f"{config.API_PREFIX}/pull-request", json=BODY, headers=auth_headers
    )

    assert response.status_code == 405
    assert bitbucket.calls == []


@pytest.mark.parametrize("path", [
    "/{kv}/pull-request",
    "/{kv}/access/pull-request",
])
def test_the_sub_resource_pull_request_routes_are_gone(
    client, auth_headers, bitbucket, path
):
    url = f"{config.API_PREFIX}{path.format(kv=KV)}"

    assert client.post(url, json={}, headers=auth_headers).status_code == 404
    assert client.delete(url, headers=auth_headers).status_code == 404
    assert bitbucket.calls == []


# --------------------------------------------------------------------------- #
# committed style, every writing route
#
# `render_values_yaml` is the only serializer, so the style rules hold everywhere by
# construction. These pin it end-to-end anyway: the guarantee is about the bytes a route
# commits, and a second render site added later would satisfy the helper tests while
# quietly writing a different shape here.
# --------------------------------------------------------------------------- #
def _write_via_create(client, auth_headers, bitbucket):
    client.post(
        CREATE_URL,
        json={"team": TEAM, "kv_name": KV, "access": [
            {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read"]},
            {"path": "*", "auth_type": "k8s", "name": "argocd",
             "namespace": "perimeter", "cluster": "dev", "capabilities": ["read"]},
        ]},
        headers=auth_headers,
    )


def _write_via_patch(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    client.patch(
        STORE_URL,
        json={"access": [
            {"path": "*", "auth_type": "oidc", "dn": OTHER_DN, "capabilities": ["read"]}
        ]},
        headers=auth_headers,
    )


def _write_via_grant(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    client.post(
        ACCESS_URL,
        json={"path": "*", "auth_type": "oidc", "dn": OTHER_DN, "capabilities": ["read"]},
        headers=auth_headers,
    )


def _write_via_revoke(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry(), oidc_entry(dn=OTHER_DN)])
    client.delete(
        f"{ACCESS_URL}?auth_type=oidc&path=*&dn={OTHER_DN}", headers=auth_headers
    )


def _write_via_tashtiot(client, auth_headers, bitbucket):
    client.put(TASHTIOT_URL, json=TASHTIOT_BODY, headers=auth_headers)


WRITING_ROUTES = [
    ("POST /", _write_via_create),
    ("PATCH /{kv_name}", _write_via_patch),
    ("POST /{kv_name}/access", _write_via_grant),
    ("DELETE /{kv_name}/access", _write_via_revoke),
    ("PUT /tashtiot/{team}", _write_via_tashtiot),
]


@pytest.mark.parametrize("route,write", WRITING_ROUTES, ids=[r for r, _ in WRITING_ROUTES])
def test_every_writing_route_commits_double_quotes_only(
    client, auth_headers, bitbucket, route, write
):
    """PyYAML prefers `'*'`; every route commits `"*"`."""
    write(client, auth_headers, bitbucket)

    assert bitbucket.committed, route
    for path, content in bitbucket.committed.items():
        assert "'" not in content, f"{route} -> {path}"


@pytest.mark.parametrize("route,write", WRITING_ROUTES, ids=[r for r, _ in WRITING_ROUTES])
def test_every_writing_route_quotes_the_dn(
    client, auth_headers, bitbucket, route, write
):
    """A DN is plain-safe, so nothing but the dumper's rule keeps it quoted."""
    write(client, auth_headers, bitbucket)

    for path, content in bitbucket.committed.items():
        for entry in yaml.safe_load(content)["access"]:
            if "dn" in entry:
                assert f'dn: "{entry["dn"]}"' in content, f"{route} -> {path}"
