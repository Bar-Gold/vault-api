"""Transport-failure mapping — the gap that turned an unreachable Bitbucket into a 500.

`httpx.RequestError` is not an HTTP response, so without this mapping it sails past the
routes' `except ExternalServiceError` and the caller learns nothing about which system broke.
"""
import httpx
import pytest
import respx
from tashtiot_apis_library.connectors import ExternalServiceError

from app.clients import http
from app.clients.bitbucket import BitbucketClient
from app.clients.woodpecker import WoodpeckerClient

BASE = "https://service.test"


@pytest.fixture
def client():
    return httpx.AsyncClient(base_url=BASE)


@pytest.mark.parametrize(
    "transport_error,expected_status",
    [
        (httpx.ConnectError("getaddrinfo failed"), 502),
        (httpx.ConnectTimeout("connect timed out"), 504),
        (httpx.ReadTimeout("read timed out"), 504),
        (httpx.RemoteProtocolError("peer closed connection"), 502),
    ],
)
@respx.mock
async def test_transport_failures_map_to_external_service_error(
    client, transport_error, expected_status
):
    respx.get(f"{BASE}/thing").mock(side_effect=transport_error)

    with pytest.raises(ExternalServiceError) as exc_info:
        await http.request(client, "GET", "/thing", service_name="demo")

    assert exc_info.value.status_code == expected_status
    assert exc_info.value.service_name == "demo"
    assert "demo" in exc_info.value.detail


@respx.mock
async def test_transport_detail_names_the_url_and_error_kind(client):
    respx.get(f"{BASE}/thing").mock(side_effect=httpx.ConnectError("getaddrinfo failed"))

    with pytest.raises(ExternalServiceError) as exc_info:
        await http.request(client, "GET", "/thing", service_name="demo")

    assert "/thing" in exc_info.value.detail
    assert "ConnectError" in exc_info.value.detail


@respx.mock
async def test_successful_response_passes_through(client):
    respx.get(f"{BASE}/thing").mock(return_value=httpx.Response(200, json={"ok": True}))

    response = await http.request(client, "GET", "/thing", service_name="demo")

    assert response.json() == {"ok": True}


@respx.mock
async def test_default_detail_extraction_for_json_and_text(client):
    respx.get(f"{BASE}/json").mock(return_value=httpx.Response(400, json={"why": "nope"}))
    respx.get(f"{BASE}/text").mock(return_value=httpx.Response(400, text="plain failure"))

    with pytest.raises(ExternalServiceError) as json_error:
        await http.request(client, "GET", "/json", service_name="demo")
    with pytest.raises(ExternalServiceError) as text_error:
        await http.request(client, "GET", "/text", service_name="demo")

    assert "nope" in json_error.value.detail
    assert "plain failure" in text_error.value.detail


# --------------------------------------------------------------------------- #
# both real clients inherit the mapping
# --------------------------------------------------------------------------- #
@respx.mock
async def test_bitbucket_unreachable_is_a_bad_gateway_not_a_crash():
    respx.get(
        "https://bitbucket.test/rest/api/1.0/projects/INFRA/repos/vault-values/raw/kv/prod/a.yaml"
    ).mock(side_effect=httpx.ConnectError("getaddrinfo failed"))
    bitbucket = BitbucketClient(
        httpx.AsyncClient(base_url="https://bitbucket.test"),
        project_key="INFRA",
        repo_slug="vault-values",
    )

    with pytest.raises(ExternalServiceError) as exc_info:
        await bitbucket.get_file_content("kv/prod/a.yaml")

    assert exc_info.value.status_code == 502
    assert exc_info.value.service_name == "bitbucket"


@respx.mock
async def test_woodpecker_unreachable_is_a_bad_gateway_not_a_crash():
    respx.get("https://woodpecker.test/api/repos/42/pipelines").mock(
        side_effect=httpx.ConnectError("getaddrinfo failed")
    )
    woodpecker = WoodpeckerClient(
        httpx.AsyncClient(base_url="https://woodpecker.test"), repo_id="42", poll_interval=0
    )

    with pytest.raises(ExternalServiceError) as exc_info:
        await woodpecker.list_pipelines()

    assert exc_info.value.status_code == 502
    assert exc_info.value.service_name == "woodpecker"
