import pytest
from fastapi.testclient import TestClient
from tashtiot_apis_library.fastapi_template.config_api import InfraMetadata

from app.v1.vault.schemas import VaultKVCreate, VaultKVCreateSpec
from tests.fakes import FakeBitbucket, FakeWoodpecker, make_pipeline


@pytest.fixture
def metadata():
    return InfraMetadata(
        project="payments", network="net", region="kirya", space="net", environment="prod"
    )


@pytest.fixture
def create_spec():
    return VaultKVCreateSpec(
        app_name="myapp",
        owner="team-dl@example.com",
        readers=["group/readers"],
        writers=["group/writers"],
    )


@pytest.fixture
def payload(metadata, create_spec):
    return VaultKVCreate(spec=create_spec, metadata=metadata)


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
