import pytest
from fastapi.testclient import TestClient

from app.v1.vault.schemas import K8sServiceAccountCreate, VaultKVCreate
from tests.fakes import FakeBitbucket, FakeWoodpecker, make_pipeline


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
    return FakeBitbucket()


@pytest.fixture
def woodpecker():
    """Both gates green: validation #2 then deploy #3."""
    return FakeWoodpecker(
        results=[
            make_pipeline(number=2, status="success", event="pull_request"),
            make_pipeline(number=3, status="success", event="push", commit="merge-sha-1"),
        ]
    )


@pytest.fixture
def client(monkeypatch, bitbucket, woodpecker):
    """TestClient over the real app, with both connectors swapped for the fakes.

    `create_app()` is the only place the connectors are constructed, so patching the
    classes it imports is enough to take the network out of the picture.
    """
    monkeypatch.setattr("app.main.BitbucketClient", lambda *args, **kwargs: bitbucket)
    monkeypatch.setattr("app.main.WoodpeckerClient", lambda *args, **kwargs: woodpecker)

    from app.main import create_app

    return TestClient(create_app())
