"""Замер полного исследования через API на выделенном стенде."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import ssl
import time
from uuid import uuid4
from datetime import datetime

import httpx


TERMINAL = {"completed", "failed", "cancelled", "awaiting_clarification"}


def event_timings(events: list[dict]) -> dict:
    """Use one run's persisted server timestamps; never mix client and server clocks."""
    queued = next((e for e in reversed(events) if e["kind"] == "queued"), None)
    if not queued:
        return {}
    run_id = queued.get("run_id")
    current = [e for e in events if e.get("run_id") == run_id and e["id"] >= queued["id"]]
    started = next((e for e in current if e["kind"] == "started"), None)
    finished = next((e for e in current if e["kind"] == "completed"), None)
    def seconds(a, b):
        return round((datetime.fromisoformat(b["created_at"]) - datetime.fromisoformat(a["created_at"])).total_seconds(), 3)
    return {"queue_s": seconds(queued, started) if started else None,
            "execution_s": seconds(started, finished) if started and finished else None}


def summarize_samples(samples: list[dict], wall: float, count: int, concurrency: int) -> dict:
    completed = [s for s in samples if s["status"] == "completed" and s.get("report_available")]
    latencies = [s["elapsed_s"] for s in completed]
    first_events = [s["first_event_s"] for s in samples if s.get("first_event_s") is not None]
    timeouts = sum(s["status"] == "timeout" for s in samples)
    return {
        "count": count, "concurrency": concurrency, "completed_with_report": len(completed),
        "statuses": {status: sum(s["status"] == status for s in samples) for status in sorted({s["status"] for s in samples})},
        "wall_time_s": round(wall, 3), "researches_per_min": round(len(completed)*60/wall, 2),
        "successful_rps": round(len(completed)/wall, 6),
        "timeout_rate": timeouts/count, "noncompletion_rate": (count-len(completed))/count,
        "latency_population": "completed_with_report_only",
        "research_p50_ms": percentile(latencies, .50),
        "research_p95_ms": percentile(latencies, .95), "research_p99_ms": percentile(latencies, .99),
        "overall_completion_p95_ms": percentile(latencies, .95) if len(completed) == count else None,
        "overall_completion_p95_note": "not observed for all requests" if len(completed) != count else None,
        "first_event_observed_count": len(first_events),
        "first_event_p50_ms": percentile(first_events, .50), "first_event_p95_ms": percentile(first_events, .95),
        "queue_p95_ms": percentile([s["queue_s"] for s in completed if s.get("queue_s") is not None], .95),
        "execution_p95_ms": percentile([s["execution_s"] for s in completed if s.get("execution_s") is not None], .95),
        "samples": samples,
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] * 1000, 2)


async def first_event(client: httpx.AsyncClient, research_id: str, started: float) -> float | None:
    try:
        async with client.stream("GET", f"/api/v1/researches/{research_id}/events") as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    return time.perf_counter() - started
    except (httpx.HTTPError, asyncio.CancelledError):
        return None
    return None


async def benchmark(
    client: httpx.AsyncClient, *, question: str, count: int,
    concurrency: int, timeout_s: float, csrf: str, cancel_timeouts: bool = False,
) -> dict:
    semaphore = asyncio.Semaphore(concurrency)
    batch_start = time.perf_counter()

    async def one(index: int) -> dict:
        async with semaphore:
            started = time.perf_counter()
            research_id = None
            event_task = None
            try:
                response = await client.post(
                    "/api/v1/researches",
                    json={"title": f"Нагрузочная проверка {index + 1} {uuid4().hex[:8]}", "question": question, "classification": "public"},
                    headers={"X-CSRF-Token": csrf},
                )
                response.raise_for_status()
                research_id = response.json()["id"]
                event_task = asyncio.create_task(first_event(client, research_id, started))
                deadline = started + timeout_s
                while time.perf_counter() < deadline:
                    detail = await client.get(f"/api/v1/researches/{research_id}")
                    detail.raise_for_status()
                    data = detail.json()
                    if data["status"] in TERMINAL:
                        elapsed = time.perf_counter() - started
                        try:
                            first = await asyncio.wait_for(event_task, timeout=1)
                        except asyncio.TimeoutError:
                            first = None
                        history = await client.get(f"/api/v1/researches/{research_id}/event-history", params={"limit": 200})
                        history.raise_for_status()
                        return {
                            "research_id": research_id,
                            "status": data["status"],
                            "report_available": bool(data.get("report")),
                            "elapsed_s": round(elapsed, 3),
                            "first_event_s": round(first, 3) if first is not None else None,
                            **event_timings(history.json()),
                        }
                    await asyncio.sleep(2)
                first = event_task.result() if event_task and event_task.done() and not event_task.cancelled() else None
                elapsed = time.perf_counter() - started
                cancellation_status = None
                if cancel_timeouts:
                    cancelled = await client.post(f"/api/v1/researches/{research_id}/cancel", headers={"X-CSRF-Token": csrf})
                    cancellation_status = cancelled.status_code
                return {"research_id": research_id, "status": "timeout",
                        "first_event_s": round(first, 3) if first is not None else None,
                        "elapsed_s": round(elapsed, 3), "cancellation_http_status": cancellation_status}
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                return {"research_id": research_id, "status": "error", "error": type(exc).__name__}
            finally:
                if event_task and not event_task.done():
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)

    samples = await asyncio.gather(*(one(index) for index in range(count)))
    wall = time.perf_counter() - batch_start
    return summarize_samples(samples, wall, count, concurrency)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Адрес прикладного сервера без /api/v1")
    parser.add_argument("--question", required=True, help="Точный вопрос, для которого в стенде подготовлен корпус")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--ca-cert", help="Trusted benchmark TLS certificate or CA bundle")
    parser.add_argument("--cancel-timeouts", action="store_true", help="Cancel only this benchmark's timed-out jobs")
    args = parser.parse_args()
    if args.count < 1 or args.concurrency < 1 or args.timeout <= 0:
        parser.error("count, concurrency и timeout должны быть положительными")
    email, password = os.getenv("BENCH_EMAIL"), os.getenv("BENCH_PASSWORD")
    if not email or not password:
        parser.error("Задайте BENCH_EMAIL и BENCH_PASSWORD для отдельной тестовой учётной записи")
    timeout = httpx.Timeout(15, read=max(120, args.timeout))
    verify = ssl.create_default_context(cafile=args.ca_cert) if args.ca_cert else True
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=timeout, trust_env=False, verify=verify) as client:
        response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
        response.raise_for_status()
        csrf = client.cookies.get("research_csrf")
        if not csrf:
            raise RuntimeError("API не выдал CSRF cookie")
        result = await benchmark(client, question=args.question, count=args.count, concurrency=args.concurrency, timeout_s=args.timeout, csrf=csrf, cancel_timeouts=args.cancel_timeouts)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
