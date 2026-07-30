"""Multi-segment mount names, end to end.

Names are used verbatim as the Vault mount path, so `payments/vault-secrets` has to survive
four different encodings: the mount path, the flattened policy/role names, a nested values
file path, and a URL path segment that must not be swallowed by the `:path` converter.
"""
import yaml
from fastapi.encoders import jsonable_encoder
from tashtiot_apis_library.fastapi_template.config_api import InfraMetadata

from app.helpers import build_values_data, render_values_yaml
from app.v1.vault.conf import config
from app.v1.vault.operations import create_kv_mount_operation
from app.v1.vault.schemas import VaultKVCreate, VaultKVCreateSpec

NAME = "payments/vault-secrets"
VALUES_PATH = "kv/prod/payments/vault-secrets.yaml"
READ_POLICY = "payments-vault-secrets-read"
WRITE_POLICY = "payments-vault-secrets-write"


def _payload(metadata, **spec):
    values = {"app_name": NAME, "owner": "team-dl@example.com"}
    values.update(spec)
    return VaultKVCreate(spec=VaultKVCreateSpec(**values), metadata=metadata)


def _body(metadata, spec):
    return jsonable_encoder({"metadata": metadata, "spec": spec})


def _seed(bitbucket, metadata):
    _, values = build_values_data(_payload(metadata), "kingmagen", "prod")
    bitbucket.existing_files[VALUES_PATH] = render_values_yaml(values)
    return values


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
async def test_create_uses_the_name_as_the_mount_path(bitbucket, woodpecker, metadata):
    result = await create_kv_mount_operation(bitbucket, woodpecker, _payload(metadata))

    assert result.mount_path == NAME
    assert result.message == f"Successful creation of {NAME}"
    assert yaml.safe_load(bitbucket.committed[VALUES_PATH])["mount"]["path"] == NAME


async def test_create_nests_the_values_file(bitbucket, woodpecker, metadata):
    await create_kv_mount_operation(bitbucket, woodpecker, _payload(metadata))

    assert VALUES_PATH in bitbucket.committed


async def test_create_flattens_the_policy_names(bitbucket, woodpecker, metadata):
    """Vault policy names cannot contain slashes."""
    result = await create_kv_mount_operation(bitbucket, woodpecker, _payload(metadata))

    assert result.policies == [READ_POLICY, WRITE_POLICY]


async def test_policy_rules_use_the_unflattened_path(bitbucket, woodpecker, metadata):
    """The HCL paths must be the real mount path, slashes intact."""
    await create_kv_mount_operation(bitbucket, woodpecker, _payload(metadata))

    committed = yaml.safe_load(bitbucket.committed[VALUES_PATH])
    assert f'path "{NAME}/data/*"' in committed["policies"][0]["rules"]


async def test_create_flattens_the_branch_name(bitbucket, woodpecker, metadata):
    """A slash in the branch would nest the ref and collide with sibling branches."""
    await create_kv_mount_operation(
        bitbucket, woodpecker, _payload(metadata), branch_suffix="ab12cd34"
    )

    pull_request = bitbucket.pull_requests[101]
    assert pull_request.from_branch == "vault-kv/prod-payments-vault-secrets-ab12cd34"
    assert pull_request.from_branch.count("/") == 1


# --------------------------------------------------------------------------- #
# routes — the `:path` converter must not swallow the trailing segment
# --------------------------------------------------------------------------- #
def test_read_a_multi_segment_name(client, auth_headers, bitbucket, metadata):
    _seed(bitbucket, metadata)

    response = client.get(
        f"{config.API_PREFIX}/{NAME}?environment=prod", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["mount"]["path"] == NAME


def test_patch_a_multi_segment_name(client, auth_headers, bitbucket, metadata):
    _seed(bitbucket, metadata)

    response = client.patch(
        f"{config.API_PREFIX}/{NAME}",
        json=_body(metadata, {"description": "new text"}),
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["mount_path"] == NAME
    assert yaml.safe_load(bitbucket.committed[VALUES_PATH])["mount"]["description"] == (
        "new text"
    )


def test_groups_on_a_multi_segment_name(client, auth_headers, bitbucket, metadata):
    """`/{app_name:path}/groups` must bind app_name without eating '/groups'."""
    _seed(bitbucket, metadata)

    response = client.post(
        f"{config.API_PREFIX}/{NAME}/groups",
        json=_body(metadata, {"group": "AD\\payments-ro", "capability": "read"}),
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["mount_path"] == NAME
    policies = {
        p["name"]: p for p in yaml.safe_load(bitbucket.committed[VALUES_PATH])["policies"]
    }
    assert "AD\\payments-ro" in policies[READ_POLICY]["entities"]


def test_kubernetes_auth_on_a_multi_segment_name(
    client, auth_headers, bitbucket, metadata
):
    _seed(bitbucket, metadata)

    response = client.post(
        f"{config.API_PREFIX}/{NAME}/kubernetes-auth",
        json=_body(metadata, {"service_accounts": ["sa"], "namespaces": ["ns"]}),
        headers=auth_headers,
    )

    assert response.status_code == 200
    role = yaml.safe_load(bitbucket.committed[VALUES_PATH])["kubernetes_auth"][0]
    # The role name is flattened; the policy it binds is the flattened one too.
    assert role["role"] == "payments-vault-secrets"
    assert role["policies"] == [READ_POLICY]


def test_a_name_ending_in_groups_still_resolves(client, auth_headers, bitbucket, metadata):
    """The converter backtracks to the anchored suffix, so 'x/groups' is a usable name."""
    metadata_copy = InfraMetadata(
        project="payments", network="net", region="kirya", space="net", environment="prod"
    )
    spec = VaultKVCreateSpec(app_name="team/groups", owner="a@b.c")
    _, values = build_values_data(
        VaultKVCreate(spec=spec, metadata=metadata_copy), "kingmagen", "prod"
    )
    bitbucket.existing_files["kv/prod/team/groups.yaml"] = render_values_yaml(values)

    response = client.post(
        f"{config.API_PREFIX}/team/groups/groups",
        json=_body(metadata, {"group": "AD\\x", "capability": "read"}),
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["mount_path"] == "team/groups"


def test_traversal_in_a_name_is_rejected(client, auth_headers, bitbucket, metadata):
    """The name reaches a file path, so '..' must never get through."""
    response = client.post(
        f"{config.API_PREFIX}/",
        json=_body(metadata, {"app_name": "../../etc/passwd", "owner": "a@b.c"}),
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert bitbucket.calls == []
