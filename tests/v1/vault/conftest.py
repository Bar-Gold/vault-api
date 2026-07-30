import pytest
from fastapi.testclient import TestClient

from app.v1.vault.schemas import VaultKVCreate
from tests.fakes import FakeBitbucket, FakeWoodpecker, make_pipeline


@pytest.fixture
def payload():
    """The whole create request: a name, and what the KV is for."""
    return VaultKVCreate(kv_name="myapp", kv_description="payments secrets")


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
