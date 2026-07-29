"""Woodpecker client: pipeline discovery, polling to completion and timeout behaviour."""

import httpx
import pytest
import respx
from tashtiot_apis_library.connectors import ExternalServiceError

from app.clients.woodpecker import (
    Pipeline,
    PipelineTimeoutError,
    WoodpeckerClient,
)

BASE = "https://woodpecker.test"
PIPELINES = f"{BASE}/api/repos/42/pipelines"


@pytest.fixture
def woodpecker():
    client = httpx.AsyncClient(base_url=BASE)
    # poll_interval=0 keeps the suite fast; the timeouts are set per-test.
    return WoodpeckerClient(client, repo_id="42", poll_interval=0)


def _pipeline(number=1, status="success", event="pull_request", **kwargs):
    data = {"number": number, "status": status, "event": event}
    data.update(kwargs)
    return data


# --------------------------------------------------------------------------- #
# status semantics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["pending", "running", "blocked", "waiting_on_deps"])
def test_pending_statuses_are_not_terminal(status):
    assert not Pipeline(number=1, status=status).is_terminal


@pytest.mark.parametrize("status", ["success", "failure", "error", "killed", "declined", "skipped"])
def test_finished_statuses_are_terminal(status):
    assert Pipeline(number=1, status=status).is_terminal


def test_only_success_counts_as_succeeded():
    assert Pipeline(number=1, status="success").succeeded
    assert not Pipeline(number=1, status="failure").succeeded
    assert not Pipeline(number=1, status="skipped").succeeded


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
@respx.mock
async def test_list_pipelines_parses_and_paginates(woodpecker):
    route = respx.get(PIPELINES).mock(
        return_value=httpx.Response(
            200,
            json=[
                _pipeline(number=9, status="running", branch="master", commit="sha9"),
                _pipeline(number=8, status="success"),
            ],
        )
    )

    pipelines = await woodpecker.list_pipelines()

    assert [p.number for p in pipelines] == [9, 8]
    assert pipelines[0].commit == "sha9"
    assert route.calls.last.request.url.params["perPage"] == "50"


@respx.mock
async def test_get_pipeline_by_number(woodpecker):
    respx.get(f"{PIPELINES}/9").mock(
        return_value=httpx.Response(200, json=_pipeline(number=9, status="success"))
    )

    assert (await woodpecker.get_pipeline(9)).status == "success"


@respx.mock
async def test_null_pipeline_list_is_handled(woodpecker):
    respx.get(PIPELINES).mock(return_value=httpx.Response(200, text="null"))
    assert await woodpecker.list_pipelines() == []


@respx.mock
async def test_non_json_body_does_not_break_the_poll_loop(woodpecker):
    """A 2xx with an empty body must degrade to 'nothing yet', not a JSONDecodeError."""
    respx.get(PIPELINES).mock(return_value=httpx.Response(200, text=""))
    assert await woodpecker.list_pipelines() == []


@respx.mock
async def test_http_error_maps_to_external_service_error(woodpecker):
    respx.get(PIPELINES).mock(return_value=httpx.Response(401, text="unauthorized"))

    with pytest.raises(ExternalServiceError) as exc_info:
        await woodpecker.list_pipelines()

    assert exc_info.value.status_code == 401
    assert exc_info.value.service_name == "woodpecker"


# --------------------------------------------------------------------------- #
# find_pipeline
# --------------------------------------------------------------------------- #
@respx.mock
async def test_find_pipeline_waits_for_the_webhook_to_land(woodpecker):
    # The pipeline does not exist on the first scan — the forge webhook is asynchronous.
    respx.get(PIPELINES).mock(
        side_effect=[
            httpx.Response(200, json=[_pipeline(number=1, status="success")]),
            httpx.Response(200, json=[_pipeline(number=2, status="pending"), _pipeline(number=1)]),
        ]
    )

    found = await woodpecker.find_pipeline(lambda p: p.number == 2, start_timeout=10)

    assert found.number == 2


@respx.mock
async def test_find_pipeline_times_out_when_nothing_matches(woodpecker):
    respx.get(PIPELINES).mock(
        return_value=httpx.Response(200, json=[_pipeline(number=1, status="success")])
    )

    with pytest.raises(PipelineTimeoutError) as exc_info:
        await woodpecker.find_pipeline(lambda p: p.number == 99, start_timeout=0)

    assert "No matching Woodpecker pipeline" in exc_info.value.message


# --------------------------------------------------------------------------- #
# wait_for_completion
# --------------------------------------------------------------------------- #
@respx.mock
async def test_wait_for_completion_polls_until_terminal(woodpecker):
    respx.get(f"{PIPELINES}/5").mock(
        side_effect=[
            httpx.Response(200, json=_pipeline(number=5, status="pending")),
            httpx.Response(200, json=_pipeline(number=5, status="running")),
            httpx.Response(200, json=_pipeline(number=5, status="failure")),
        ]
    )

    finished = await woodpecker.wait_for_completion(5, completion_timeout=10)

    assert finished.status == "failure"
    assert not finished.succeeded


@respx.mock
async def test_wait_for_completion_times_out_and_reports_last_state(woodpecker):
    respx.get(f"{PIPELINES}/5").mock(
        return_value=httpx.Response(200, json=_pipeline(number=5, status="running"))
    )

    with pytest.raises(PipelineTimeoutError) as exc_info:
        await woodpecker.wait_for_completion(5, completion_timeout=0)

    assert exc_info.value.pipeline.status == "running"
    assert "still running" in exc_info.value.message


@respx.mock
async def test_blocked_pipeline_never_settles(woodpecker):
    """`blocked` means awaiting human approval — it must not be read as finished."""
    respx.get(f"{PIPELINES}/5").mock(
        return_value=httpx.Response(200, json=_pipeline(number=5, status="blocked"))
    )

    with pytest.raises(PipelineTimeoutError):
        await woodpecker.wait_for_completion(5, completion_timeout=0)


# --------------------------------------------------------------------------- #
# await_pipeline
# --------------------------------------------------------------------------- #
@respx.mock
async def test_await_pipeline_returns_immediately_when_already_terminal(woodpecker):
    respx.get(PIPELINES).mock(
        return_value=httpx.Response(200, json=[_pipeline(number=7, status="success")])
    )
    detail = respx.get(f"{PIPELINES}/7").mock(return_value=httpx.Response(200, json={}))

    result = await woodpecker.await_pipeline(lambda p: p.number == 7, start_timeout=1)

    assert result.status == "success"
    assert not detail.called  # no need to re-fetch a finished pipeline


@respx.mock
async def test_await_pipeline_finds_then_polls(woodpecker):
    respx.get(PIPELINES).mock(
        return_value=httpx.Response(200, json=[_pipeline(number=7, status="running")])
    )
    respx.get(f"{PIPELINES}/7").mock(
        side_effect=[
            httpx.Response(200, json=_pipeline(number=7, status="running")),
            httpx.Response(200, json=_pipeline(number=7, status="success")),
        ]
    )

    result = await woodpecker.await_pipeline(
        lambda p: p.number == 7, start_timeout=1, completion_timeout=10
    )

    assert result.succeeded
