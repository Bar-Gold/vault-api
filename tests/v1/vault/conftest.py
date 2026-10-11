import pytest
from fastapi.testclient import TestClient

from app.helpers import (
    build_access_document,
    build_k8s_access_entry,
    build_oidc_access_entry,
    render_values_yaml,
)
from app.v1.vault.schemas import (
    DeleteRequest,
    K8sAccessRevoke,
    OidcAccessEntryRequest,
    OidcAccessRevoke,
    VaultKVCreate,
)
from tests.fakes import FakeBitbucket

TEAM = "cloudops"
KV = "my-passwords"
STORE_DIR = f"KVs/{TEAM}/{KV}"
OIDC_PATH = f"{STORE_DIR}/oidc-access.yaml"
K8S_PATH = f"{STORE_DIR}/k8s-access.yaml"
BRANCH = f"vault-kv/{TEAM}-{KV}-abc123"

DN = "CN=cloudops,OU=Groups,DC=corp,DC=example,DC=com"
OTHER_DN = "CN=platform,OU=Groups,DC=corp,DC=example,DC=com"

# Required on every route but the reads, and required *not* to reach a file.
# One dict, so a test asserting on the pull-request description can name the values.
METADATA = {
    "project": "cloudops",
    "network": "internal",
    "region": "il-central",
    "space": "shared",
    "environment": "production",
    "island": "island-a",
}


def oidc_entry(path="*", dn=DN, capabilities=("read", "write")):
    return build_oidc_access_entry(path, dn, list(capabilities))


def k8s_entry(
    path="*",
    name="cloudops-external-secrets",
    namespace="cloudops",
    cluster="dev",
    capabilities=("read",),
):
    return build_k8s_access_entry(path, name, namespace, cluster, list(capabilities))


def oidc_revoke(path="*", dn=DN):
    """A revoke body as an operation receives it: the grant's identity plus metadata."""
    return OidcAccessRevoke(path=path, auth_type="oidc", dn=dn, metadata=METADATA)


def k8s_revoke(
    path="*", name="cloudops-external-secrets", namespace="cloudops", cluster="dev"
):
    return K8sAccessRevoke(
        path=path,
        auth_type="k8s",
        name=name,
        namespace=namespace,
        cluster=cluster,
        metadata=METADATA,
    )


def delete_request():
    """The body of a store or Tashtiot delete: the metadata block and nothing else."""
    return DeleteRequest(metadata=METADATA)


def seed_store(bitbucket, team=TEAM, kv_name=KV, oidc=(), k8s=()):
    """Put a store on the fake's base branch, writing only the files it has grants for.

    Mirrors what a create commits: a store with no Kubernetes grants has **no**
    `k8s-access.yaml`, so a test that seeds one is testing a shape a create never produces.
    """
    directory = f"KVs/{team}/{kv_name}"
    if oidc:
        bitbucket.existing_files[f"{directory}/oidc-access.yaml"] = render_values_yaml(
            build_access_document(list(oidc))
        )
    if k8s:
        bitbucket.existing_files[f"{directory}/k8s-access.yaml"] = render_values_yaml(
            build_access_document(list(k8s))
        )
    return directory


@pytest.fixture
def payload():
    """A create: one OIDC grant and one Kubernetes grant, so both files get written."""
    return VaultKVCreate(
        team=TEAM,
        kv_name=KV,
        access=[
            {
                "path": "*",
                "auth_type": "oidc",
                "dn": DN,
                "capabilities": ["read", "write"],
            },
            {
                "path": "*",
                "auth_type": "k8s",
                "name": "cloudops-external-secrets",
                "namespace": "cloudops",
                "cluster": "dev",
                "capabilities": ["read"],
            },
        ],
        metadata=METADATA,
    )


@pytest.fixture
def oidc_only_payload():
    """A create with no Kubernetes grants — it must write no k8s-access.yaml at all."""
    return VaultKVCreate(
        team=TEAM,
        kv_name=KV,
        access=[
            {"path": "*", "auth_type": "oidc", "dn": DN, "capabilities": ["read"]}
        ],
        metadata=METADATA,
    )


@pytest.fixture
def k8s_only_payload():
    """The mirror: a create with no OIDC grants, which must write no oidc-access.yaml.

    Neither kind is individually required — the format's own wording is "k8s-access.yaml
    ... if applicable" — so both single-kind directions have to hold, not just the common
    one.
    """
    return VaultKVCreate(
        team=TEAM,
        kv_name=KV,
        access=[
            {
                "path": "*",
                "auth_type": "k8s",
                "name": "cloudops-external-secrets",
                "namespace": "cloudops",
                "cluster": "dev",
                "capabilities": ["read"],
            }
        ],
        metadata=METADATA,
    )


@pytest.fixture
def grant_payload():
    """One grant to append to an existing store.

    The **request** entry, not the bare one: the body of `POST /{kv_name}/access` is
    `AccessEntryRequest`, and the operation reads `payload.metadata` for the pull-request
    description — a plain `OidcAccessEntry` would raise `AttributeError` there.
    """
    return OidcAccessEntryRequest(
        path="*",
        auth_type="oidc",
        dn=OTHER_DN,
        capabilities=["read"],
        metadata=METADATA,
    )


@pytest.fixture
def bitbucket():
    """The only upstream — repo *and* CI gates, both green by default."""
    return FakeBitbucket()


@pytest.fixture
def client(monkeypatch, bitbucket):
    """TestClient over the real app, with the connector swapped for the fake.

    `create_app()` is the only place the connector is constructed, so patching the class
    it imports is enough to take the network out of the picture.
    """
    monkeypatch.setattr("app.main.BitbucketClient", lambda *args, **kwargs: bitbucket)

    from app.main import create_app

    return TestClient(create_app())
