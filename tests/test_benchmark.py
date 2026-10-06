from __future__ import annotations

import httpx
import pytest

from scripts.benchmark_research import benchmark, percentile, summarize_samples, event_timings


def test_percentile_uses_nearest_rank() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4000
    assert percentile([], 0.50) is None


@pytest.mark.asyncio
async def test_full_research_benchmark_requires_report() -> None:
    created = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal created
        if request.method == "POST":
            created += 1
            assert request.headers["X-CSRF-Token"] == "test-csrf"
            return httpx.Response(200, json={"id": f"research-{created}"})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, text='event: phase\ndata: {"name":"Планирование"}\n\n')
        if request.url.path.endswith("/event-history"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"status": "completed", "report": {"version": 1, "body": "Отчёт"}})

    async with httpx.AsyncClient(base_url="http://test.local", transport=httpx.MockTransport(respond)) as client:
        result = await benchmark(client, question="Порог давления датчика", count=2, concurrency=2, timeout_s=10, csrf="test-csrf")
    assert result["completed_with_report"] == 2
    assert result["statuses"] == {"completed": 2}
    assert result["research_p95_ms"] is not None


def test_timeout_does_not_become_successful_completion_percentile():
    result = summarize_samples([
        {"status": "completed", "report_available": True, "elapsed_s": 800, "first_event_s": 2},
        {"status": "timeout", "elapsed_s": 900, "first_event_s": 3},
    ], 900, 2, 2)
    assert result["research_p95_ms"] == 800000
    assert result["overall_completion_p95_ms"] is None
    assert result["timeout_rate"] == .5
    assert result["first_event_observed_count"] == 2


def test_queue_timings_use_latest_run_server_timestamps():
    events = [
        {"id": 1, "kind": "queued", "run_id": "old", "created_at": "2026-10-04T00:00:00+00:00"},
        {"id": 2, "kind": "queued", "run_id": "new", "created_at": "2026-10-04T01:00:00+00:00"},
        {"id": 3, "kind": "completed", "run_id": "old", "created_at": "2026-10-04T01:00:01+00:00"},
        {"id": 4, "kind": "started", "run_id": "new", "created_at": "2026-10-04T01:00:05+00:00"},
        {"id": 5, "kind": "completed", "run_id": "new", "created_at": "2026-10-04T01:00:20+00:00"},
    ]
    assert event_timings(events) == {"queue_s": 5, "execution_s": 15}


@pytest.mark.asyncio
async def test_timeout_cancels_only_the_created_job(monkeypatch):
    import asyncio
    import scripts.benchmark_research as module
    clock = [100.0]
    requests = []
    original_sleep = asyncio.sleep

    async def advance(_):
        clock[0] += 1
        await original_sleep(0)

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/cancel"):
            assert request.headers["X-CSRF-Token"] == "test-csrf"
            return httpx.Response(200, json={"status":"cancelled"})
        if request.method == "POST":
            return httpx.Response(200, json={"id":"owned-benchmark-job"})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, text="event: queued\ndata: {}\n\n")
        return httpx.Response(200, json={"status":"running"})

    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(module.asyncio, "sleep", advance)
    async with httpx.AsyncClient(base_url="http://test.local", transport=httpx.MockTransport(respond)) as client:
        result = await benchmark(client, question="pressure", count=1, concurrency=1,
                                 timeout_s=.1, csrf="test-csrf", cancel_timeouts=True)
    assert result["timeout_rate"] == 1
    assert result["overall_completion_p95_ms"] is None
    assert result["samples"][0]["cancellation_http_status"] == 200
    assert [r.url.path for r in requests if r.url.path.endswith("/cancel")] == ["/api/v1/researches/owned-benchmark-job/cancel"]
