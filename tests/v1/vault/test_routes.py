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
    METADATA,
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
    "metadata": METADATA,
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
    # An empty or absent metadata block is the same class of error, and must be caught
    # in the same place: before anything upstream is touched.
    ("metadata", {}),
    ("metadata", None),
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
        ], "metadata": METADATA},
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
        ], "metadata": METADATA},
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
        ], "metadata": METADATA},
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
        json={"path": "*", "auth_type": "oidc", "dn": OTHER_DN,
              "capabilities": ["read"], "metadata": METADATA},
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert len(yaml.safe_load(bitbucket.committed[OIDC_PATH])["access"]) == 2


def test_adding_a_duplicate_grant_returns_409(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])

    response = client.post(
        ACCESS_URL,
        json={"path": "*", "auth_type": "oidc", "dn": DN,
              "capabilities": ["read"], "metadata": METADATA},
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
    ],
    "metadata": METADATA,
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
        ], "metadata": METADATA},
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
        ], "metadata": METADATA},
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


# --------------------------------------------------------------------------- #
# one Tashtiot grant at a time
#
# The same pair the store sub-resource has, on a three-segment path. The rules are the
# document's, so they match the store's: duplicate identity 409, unknown identity 404,
# and the file goes when its last grant does.
# --------------------------------------------------------------------------- #
TASHTIOT_ACCESS_URL = f"{TASHTIOT_URL}/access"


def _seed_tashtiot(bitbucket, entries):
    from app.helpers import build_access_document, render_values_yaml

    bitbucket.existing_files[TASHTIOT_PATH] = render_values_yaml(
        build_access_document(list(entries))
    )


def test_adding_a_tashtiot_grant_appends_to_the_flat_file(client, auth_headers, bitbucket):
    _seed_tashtiot(bitbucket, [oidc_entry(path="+/cloudops/*")])

    response = client.post(
        TASHTIOT_ACCESS_URL,
        json={"path": "services/+/redis/*", "auth_type": "oidc", "dn": OTHER_DN,
              "capabilities": ["read"], "metadata": METADATA},
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert len(yaml.safe_load(bitbucket.committed[TASHTIOT_PATH])["access"]) == 2


def test_a_tashtiot_grant_can_be_either_kind(client, auth_headers, bitbucket):
    """The flat file mixes both, so a k8s grant goes in the same list as the oidc one."""
    _seed_tashtiot(bitbucket, [oidc_entry(path="+/cloudops/*")])

    response = client.post(
        TASHTIOT_ACCESS_URL,
        json={"path": "services/+/redis/*", "auth_type": "k8s", "name": "argocd",
              "namespace": "perimeter", "cluster": "dev", "capabilities": ["read"],
              "metadata": METADATA},
        headers=auth_headers,
    )

    assert response.status_code == 201
    kinds = [e["authType"] for e in yaml.safe_load(bitbucket.committed[TASHTIOT_PATH])["access"]]
    assert kinds == ["oidc", "k8s"]


def test_adding_a_duplicate_tashtiot_grant_is_409(client, auth_headers, bitbucket):
    _seed_tashtiot(bitbucket, [oidc_entry(path="+/cloudops/*")])

    response = client.post(
        TASHTIOT_ACCESS_URL,
        json={"path": "+/cloudops/*", "auth_type": "oidc", "dn": DN,
              "capabilities": ["read"], "metadata": METADATA},
        headers=auth_headers,
    )

    assert response.status_code == 409


def test_adding_a_grant_to_a_team_with_no_file_is_404(client, auth_headers, bitbucket):
    """PUT creates a team's file; this route edits one. Appending to nothing would create
    a file behind a route that reads as an edit."""
    response = client.post(
        TASHTIOT_ACCESS_URL,
        json={"path": "*", "auth_type": "oidc", "dn": DN,
              "capabilities": ["read"], "metadata": METADATA},
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert bitbucket.committed == {}


def test_revoking_a_tashtiot_grant_leaves_the_others(client, auth_headers, bitbucket):
    _seed_tashtiot(
        bitbucket,
        [oidc_entry(path="+/cloudops/*"), oidc_entry(path="+/cloudops/*", dn=OTHER_DN)],
    )

    response = client.delete(
        f"{TASHTIOT_ACCESS_URL}?auth_type=oidc&path=%2B/cloudops/*&dn={OTHER_DN}",
        headers=auth_headers,
    )

    assert response.status_code == 201
    remaining = yaml.safe_load(bitbucket.committed[TASHTIOT_PATH])["access"]
    assert [entry["dn"] for entry in remaining] == [DN]


def test_revoking_the_last_tashtiot_grant_removes_the_file(client, auth_headers, bitbucket):
    """The upsert refuses an empty list, so a file saying nothing is a shape nothing can
    write — the same rule the store's access files follow."""
    _seed_tashtiot(bitbucket, [oidc_entry(path="+/cloudops/*")])

    response = client.delete(
        f"{TASHTIOT_ACCESS_URL}?auth_type=oidc&path=%2B/cloudops/*&dn={DN}",
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert bitbucket.deleted == [TASHTIOT_PATH]
    assert bitbucket.committed == {}


def test_revoking_a_tashtiot_grant_that_is_not_there_is_404(client, auth_headers, bitbucket):
    _seed_tashtiot(bitbucket, [oidc_entry(path="+/cloudops/*")])

    response = client.delete(
        f"{TASHTIOT_ACCESS_URL}?auth_type=oidc&path=%2B/cloudops/*&dn={OTHER_DN}",
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_a_mismatched_tashtiot_revoke_identity_is_422(client, auth_headers, bitbucket):
    """`_revoked_entry` is shared with the store route, so the rule cannot drift apart."""
    _seed_tashtiot(bitbucket, [oidc_entry(path="+/cloudops/*")])

    response = client.delete(
        f"{TASHTIOT_ACCESS_URL}?auth_type=k8s&path=*&name=argocd", headers=auth_headers
    )

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_the_tashtiot_grant_routes_do_not_shadow_a_store(client, auth_headers, bitbucket):
    """Three segments against two — a store named `tashtiot` is a different address, and
    `/{kv_name}/access` is unaffected."""
    seed_store(bitbucket, oidc=[oidc_entry()])

    response = client.post(
        ACCESS_URL,
        json={"path": "*", "auth_type": "oidc", "dn": OTHER_DN,
              "capabilities": ["read"], "metadata": METADATA},
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert list(bitbucket.committed) == [OIDC_PATH]


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
# capitals on the read routes
#
# The values directory is not written only by this service, so it may already hold a
# capitalised store or team. `_walk_store_dirs` finds those and `_resolve_store` matches
# names exactly — the lowercase-only path pattern was the only thing making them
# unreachable, and it failed the request as "malformed" before the walk ever ran.
# --------------------------------------------------------------------------- #
def test_a_capitalised_store_is_readable(client, auth_headers, bitbucket):
    seed_store(bitbucket, team="CloudOps", kv_name="My-Passwords", oidc=[oidc_entry()])

    response = client.get(f"{config.API_PREFIX}/My-Passwords", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["kv_name"] == "My-Passwords"
    assert body["team"] == "CloudOps"
    assert [entry["dn"] for entry in body["access"]] == [DN]


def test_a_capitalised_team_is_readable(client, auth_headers, bitbucket):
    from app.helpers import build_access_document, render_values_yaml

    bitbucket.existing_files["KVs/Tashtiot/CloudOps.yaml"] = render_values_yaml(
        build_access_document([oidc_entry(path="+/cloudops/*")])
    )

    response = client.get(
        f"{config.API_PREFIX}/tashtiot/CloudOps", headers=auth_headers
    )

    assert response.status_code == 200
    assert [entry["dn"] for entry in response.json()["access"]] == [DN]


def test_the_name_is_still_matched_exactly(client, auth_headers, bitbucket):
    """Accepting capitals is not case-insensitive resolution. `_resolve_store` compares
    names exactly, so the wrong casing is a 404 — the store really is not there under that
    spelling — rather than a silently different store."""
    seed_store(bitbucket, team="CloudOps", kv_name="My-Passwords", oidc=[oidc_entry()])

    response = client.get(f"{config.API_PREFIX}/my-passwords", headers=auth_headers)

    assert response.status_code == 404


@pytest.mark.parametrize(
    "method, url",
    [
        ("patch", f"{config.API_PREFIX}/My-Passwords"),
        ("delete", f"{config.API_PREFIX}/My-Passwords"),
        ("post", f"{config.API_PREFIX}/My-Passwords/access"),
        ("delete", f"{config.API_PREFIX}/My-Passwords/access"),
        ("put", f"{config.API_PREFIX}/tashtiot/CloudOps"),
        ("delete", f"{config.API_PREFIX}/tashtiot/CloudOps"),
        ("post", f"{config.API_PREFIX}/tashtiot/CloudOps/access"),
        ("delete", f"{config.API_PREFIX}/tashtiot/CloudOps/access"),
    ],
)
def test_the_write_routes_still_refuse_a_capitalised_name(
    client, auth_headers, bitbucket, method, url
):
    """Only the reads were widened. Writing to a name this service would never itself
    create is a separate decision, so these stay 422 and reach no upstream."""
    response = client.request(method, url, json=BODY, headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


def test_an_underscore_is_still_rejected_on_a_read(client, auth_headers, bitbucket):
    """Widened to capitals, not opened up. The segment still admits no underscore, no dot
    and no slash — which is what keeps it a path-traversal guard."""
    for name in ("Bad_Name", "has.dot", "has space"):
        response = client.get(f"{config.API_PREFIX}/{name}", headers=auth_headers)
        assert response.status_code == 422, name
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
        ], "metadata": METADATA},
        headers=auth_headers,
    )


def _write_via_patch(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    client.patch(
        STORE_URL,
        json={"access": [
            {"path": "*", "auth_type": "oidc", "dn": OTHER_DN, "capabilities": ["read"]}
        ], "metadata": METADATA},
        headers=auth_headers,
    )


def _write_via_grant(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    client.post(
        ACCESS_URL,
        json={"path": "*", "auth_type": "oidc", "dn": OTHER_DN,
              "capabilities": ["read"], "metadata": METADATA},
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


# --------------------------------------------------------------------------- #
# metadata
#
# Required on every body-carrying route, recorded in the pull-request description, and
# never written to a file. The last of those is the whole point of the block.
# --------------------------------------------------------------------------- #
METADATA_NAMES = ("project", "network", "region", "space", "environment", "island")

OIDC_GRANT = {"path": "*", "auth_type": "oidc", "dn": OTHER_DN, "capabilities": ["read"]}
TASHTIOT_GRANT = {
    "path": "services/+/redis/*",
    "auth_type": "oidc",
    "dn": OTHER_DN,
    "capabilities": ["read"],
}

# (id, method, url, body-without-metadata) for each of the five body-carrying routes.
BODY_ROUTES = [
    ("POST /", "post", CREATE_URL,
     {"team": TEAM, "kv_name": KV, "access": [dict(OIDC_GRANT)]}),
    ("PATCH /{kv_name}", "patch", STORE_URL, {"access": [dict(OIDC_GRANT)]}),
    ("POST /{kv_name}/access", "post", ACCESS_URL, dict(OIDC_GRANT)),
    ("PUT /tashtiot/{team}", "put", TASHTIOT_URL, {"access": [dict(TASHTIOT_GRANT)]}),
    ("POST /tashtiot/{team}/access", "post", TASHTIOT_ACCESS_URL, dict(TASHTIOT_GRANT)),
]


@pytest.mark.parametrize(
    "method,url,body",
    [route[1:] for route in BODY_ROUTES],
    ids=[route[0] for route in BODY_ROUTES],
)
def test_every_body_route_is_422_without_metadata(
    client, auth_headers, bitbucket, method, url, body
):
    """All five, one rule — and refused before a single upstream call is made."""
    response = client.request(method, url, json=body, headers=auth_headers)

    assert response.status_code == 422
    assert bitbucket.calls == []


@pytest.mark.parametrize("name", METADATA_NAMES)
@pytest.mark.parametrize("route", ["create", "grant"], ids=["POST /", "POST /{kv}/access"])
def test_an_incomplete_metadata_block_is_422(client, auth_headers, bitbucket, route, name):
    """Five coordinates do not locate a deployment; a partial block is not a partial answer."""
    block = {key: value for key, value in METADATA.items() if key != name}
    if route == "create":
        response = client.post(
            CREATE_URL, json={**BODY, "metadata": block}, headers=auth_headers
        )
    else:
        response = client.post(
            ACCESS_URL, json={**OIDC_GRANT, "metadata": block}, headers=auth_headers
        )

    assert response.status_code == 422
    assert bitbucket.calls == []


def _drive_create(client, auth_headers, bitbucket):
    return client.post(CREATE_URL, json=BODY, headers=auth_headers)


def _drive_patch(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    return client.patch(
        STORE_URL,
        json={"access": [dict(OIDC_GRANT)], "metadata": METADATA},
        headers=auth_headers,
    )


def _drive_grant(client, auth_headers, bitbucket):
    seed_store(bitbucket, oidc=[oidc_entry()])
    return client.post(
        ACCESS_URL, json={**OIDC_GRANT, "metadata": METADATA}, headers=auth_headers
    )


def _drive_tashtiot_upsert(client, auth_headers, bitbucket):
    return client.put(TASHTIOT_URL, json=TASHTIOT_BODY, headers=auth_headers)


def _drive_tashtiot_grant(client, auth_headers, bitbucket):
    _seed_tashtiot(bitbucket, [oidc_entry(path="+/cloudops/*")])
    return client.post(
        TASHTIOT_ACCESS_URL,
        json={**TASHTIOT_GRANT, "metadata": METADATA},
        headers=auth_headers,
    )


# The create has to run first when they are driven in one sequence: it is the only route
# that scans for a duplicate name, and seeding a store for the others would collide.
BODY_DRIVERS = [
    ("POST /", _drive_create),
    ("PATCH /{kv_name}", _drive_patch),
    ("POST /{kv_name}/access", _drive_grant),
    ("PUT /tashtiot/{team}", _drive_tashtiot_upsert),
    ("POST /tashtiot/{team}/access", _drive_tashtiot_grant),
]


def test_metadata_never_reaches_the_committed_yaml(client, auth_headers, bitbucket):
    """The load-bearing test of the whole change.

    Every body-carrying route is driven with a full metadata block, and every file any of
    them commits must still be the document the deploy pipeline expects: one top-level
    `access` key, and entries carrying none of the six coordinate names.
    """
    # One gate per chain, five chains — the fake's build queue is popped once each.
    bitbucket.builds = [passing() for _ in BODY_DRIVERS]

    # `committed` is keyed by path, so two routes writing the same file would hide one of
    # them. Snapshot after each drive instead, and check every route's own bytes.
    written = []
    for name, drive in BODY_DRIVERS:
        bitbucket.committed.clear()
        assert drive(client, auth_headers, bitbucket).status_code == 201, name
        assert bitbucket.committed, name
        written += [(name, path, text) for path, text in bitbucket.committed.items()]

    assert len(written) >= len(BODY_DRIVERS)
    for name, path, content in written:
        document = yaml.safe_load(content)
        assert set(document) == {"access"}, f"{name} -> {path}"
        for entry in document["access"]:
            assert "metadata" not in entry, f"{name} -> {path}"
            assert not set(entry) & set(METADATA_NAMES), f"{name} -> {path}"
        # And nothing survives as loose text either — these two values appear nowhere in
        # a legitimate grant, so a leak anywhere in the file shows up here.
        assert "il-central" not in content, f"{name} -> {path}"
        assert "island-a" not in content, f"{name} -> {path}"


@pytest.mark.parametrize(
    "drive", [drive for _, drive in BODY_DRIVERS], ids=[name for name, _ in BODY_DRIVERS]
)
def test_metadata_appears_in_the_pull_request_description(
    client, auth_headers, bitbucket, drive
):
    """Its only destination besides the log: the pull request a human reviews."""
    assert drive(client, auth_headers, bitbucket).status_code == 201

    description = bitbucket.descriptions[-1]

    assert "- metadata:" in description
    for name in METADATA_NAMES:
        assert f"  - {name}: {METADATA[name]}" in description


def test_a_metadata_only_difference_does_not_open_a_pull_request(
    client, auth_headers, bitbucket
):
    """Documented, not a bug: metadata is not state.

    `_unchanged` compares parsed *documents*, and metadata is not in one, so a repeat
    request whose grants already match still short-circuits. Opening an empty pull request
    to record a different project name would defeat the guard that keeps the values repo
    free of empty diffs.
    """
    other = {**METADATA, "project": "platform", "island": "island-b"}
    bitbucket.builds = [passing(), passing()]

    # PATCH: open a real pull request, play the reviewer, then repeat with new metadata.
    seed_store(bitbucket, oidc=[oidc_entry()])
    first = client.patch(
        STORE_URL,
        json={"access": [dict(OIDC_GRANT)], "metadata": METADATA},
        headers=auth_headers,
    )
    assert first.status_code == 201
    bitbucket.existing_files[OIDC_PATH] = bitbucket.committed[OIDC_PATH]

    repeat = client.patch(
        STORE_URL,
        json={"access": [dict(OIDC_GRANT)], "metadata": other},
        headers=auth_headers,
    )

    assert repeat.status_code == 200
    assert repeat.json()["pull_request"] is None

    # ...and the upsert, which shares the short circuit.
    assert client.put(
        TASHTIOT_URL, json=TASHTIOT_BODY, headers=auth_headers
    ).status_code == 201
    bitbucket.existing_files[TASHTIOT_PATH] = bitbucket.committed[TASHTIOT_PATH]

    repeat_upsert = client.put(
        TASHTIOT_URL,
        json={**TASHTIOT_BODY, "metadata": other},
        headers=auth_headers,
    )

    assert repeat_upsert.status_code == 200
    assert repeat_upsert.json()["pull_request"] is None
