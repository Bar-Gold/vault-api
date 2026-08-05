import pytest
from fastapi.testclient import TestClient

from app.v1.vault.schemas import K8sServiceAccountCreate, VaultKVCreate
from tests.fakes import FakeBitbucket


@pytest.fixture
def payload():
    """The whole create request: which file, the store's name, why, and who reaches it."""
    return VaultKVCreate(
        file="payments",
        kv_name="myapp",
        kv_description="payments secrets",
        roles={"read": ["app01.corp.example.com"]},
    )


@pytest.fixture
def account_payload():
    """A service account binding for the store the `payload` fixture creates."""
    return K8sServiceAccountCreate(
        service_account="vault",
        namespace="payments",
        cluster="dev",
    )


@pytest.fixture
def account_identity(account_payload):
    """The same binding as the `(serviceAccount, namespace, cluster)` a delete takes."""
    return (
        account_payload.service_account,
        account_payload.namespace,
        account_payload.cluster,
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
