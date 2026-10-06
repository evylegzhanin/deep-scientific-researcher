"""One-user quality pass: citations, OCR/table retrieval, refusal, SSE and traces."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import ssl
import threading
import time

import httpx

CLARIFICATION = "\nУточнение: документы уже загружены в стенд, ищи только по ним и не запрашивай внешние источники."


def login(client: httpx.Client, email: str, password: str) -> None:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    client.headers["X-CSRF-Token"] = client.cookies["research_csrf"]


def run_research(client: httpx.Client, stream: httpx.Client, title: str, question: str, timeout_s: float) -> dict:
    started = time.perf_counter()
    created = client.post(
        "/api/v1/researches",
        json={"title": title, "question": question, "classification": "public"},
    )
    created.raise_for_status()
    research_id = created.json()["id"]
    first_event: list[float] = []

    def watch() -> None:
        try:
            with stream.stream("GET", f"/api/v1/researches/{research_id}/events") as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        first_event.append(time.perf_counter() - started)
                        return
        except httpx.HTTPError:
            return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    deadline = started + timeout_s
    data = {"status": "timeout"}
    while time.perf_counter() < deadline:
        response = client.get(f"/api/v1/researches/{research_id}")
        response.raise_for_status()
        data = response.json()
        if data["status"] in {"completed", "failed", "cancelled", "awaiting_clarification"}:
            break
        time.sleep(5)
    watcher.join(timeout=2)
    history = client.get(f"/api/v1/researches/{research_id}/event-history", params={"limit": 200})
    history.raise_for_status()
    sources = client.get(f"/api/v1/researches/{research_id}/sources")
    sources.raise_for_status()
    report = (data.get("report") or {}).get("body") or ""
    return {
        "research_id": research_id,
        "status": data["status"],
        "elapsed_s": round(time.perf_counter() - started, 3),
        "first_event_s": round(first_event[0], 3) if first_event else None,
        "event_kinds": [item["kind"] for item in history.json()],
        "sources": sources.json(),
        "report": report,
    }


def span_summary(trace: dict) -> list[dict]:
    summary = []
    for span in trace.get("spans", []):
        name = span.get("operationName", "")
        if name.startswith("research."):
            summary.append({"name": name, "duration_ms": round(span.get("duration", 0) / 1000, 1)})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://localhost:8443")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jaeger-url", default="http://127.0.0.1:16686")
    args = parser.parse_args()
    prepared = json.loads((args.results / "corpus-preparation.json").read_text())
    accounts = json.loads((args.results / "benchmark-users.json").read_text())
    context = ssl.create_default_context(cafile=str(args.results / "certs/server.crt"))
    known_ids = {item["id"] for item in prepared["documents"]}
    checks = []

    def record(name: str, ok: bool, **detail) -> None:
        checks.append({"name": name, "ok": ok, **detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}", flush=True)

    timeout = httpx.Timeout(30, read=args.timeout)
    with httpx.Client(base_url=args.base_url, verify=context, trust_env=False, timeout=timeout) as client, \
            httpx.Client(base_url=args.base_url, verify=context, trust_env=False, timeout=timeout) as stream:
        login(client, **accounts.get("quality", accounts["a"]))
        stream.cookies.update(client.cookies)
        stream.headers.update(client.headers)
        main_run = run_research(client, stream, "Качество корпуса", prepared["question"] + CLARIFICATION, args.timeout)
        report = main_run["report"]
        answer_body = report.split("\n## Источники", 1)[0]
        source_ids = {item["document_id"] for item in main_run["sources"]}
        record("main_completed", main_run["status"] == "completed" and bool(report), status=main_run["status"])
        record("main_has_limits_and_calibration",
               all(token in report for token in ("40", "бар", "12")) and ("80" in report or "минус 20" in report))
        record("main_has_citations", bool(re.search(r"\[E\d+\]", report)) and bool(main_run["sources"]))
        used_markers = {m for group in re.findall(r"\[(E\d+(?:\s*,\s*E\d+)*)\]", answer_body)
                        for m in re.findall(r"E\d+", group)}
        used_sources = [s for s in main_run["sources"] if s["marker"] in used_markers]
        record("used_citations_point_at_corpus", bool(used_markers)
               and used_markers <= {s["marker"] for s in used_sources}
               and all(s["document_id"] in known_ids for s in used_sources))
        authorized = client.get("/api/v1/documents", params={"limit": 200})
        authorized.raise_for_status()
        record("retrieved_evidence_is_authorized", source_ids <= {d["id"] for d in authorized.json()})
        record("irrelevant_canary_not_retrieved", not any("CANARY-" in s.get("excerpt", "") for s in main_run["sources"]))
        record("report_has_no_insufficient_evidence_warning", "Проверка выявила недостаток доказательств" not in report)
        record("sse_events", main_run["first_event_s"] is not None and "phase" in main_run["event_kinds"],
               first_event_s=main_run["first_event_s"], kinds=main_run["event_kinds"])
        markdown = client.get(f"/api/v1/researches/{main_run['research_id']}/report.md")
        pdf = client.get(f"/api/v1/researches/{main_run['research_id']}/report.pdf")
        record("report_exports", markdown.status_code == 200 and "Источники" in markdown.text
               and pdf.status_code == 200 and pdf.content.startswith(b"%PDF"))

        scan = run_research(client, stream,
            "Скан и таблица",
            "Какие пары давление и ток приведены в синтетической таблице ДД-42 и какая надпись о предельном давлении есть на скане ДД-42?" + CLARIFICATION,
            args.timeout,
        )
        evidence = scan["report"] + "\n" + "\n".join(item.get("excerpt") or "" for item in scan["sources"])
        record("scan_and_table_retrieved", scan["status"] == "completed" and "42" in evidence and "мА" in evidence,
               status=scan["status"])

        refusal = run_research(client, stream,
            "Нет данных о ресурсе",
            "Сколько часов составляет подтверждённый ресурс безотказной работы датчика ДД-42 по загруженным документам?" + CLARIFICATION,
            args.timeout,
        )
        refusal_markers = ("не установлен", "не проводил", "недостат", "нет данных", "не найден", "не подтверж")
        record("refusal_when_data_absent", refusal["status"] == "completed"
               and any(marker in refusal["report"].lower() for marker in refusal_markers)
               and not re.search(r"\b\d{3,}\s*час", refusal["report"].lower()),
               status=refusal["status"])

    output = args.output or args.results / "quality-checks.json"
    result = {"checks": checks, "main": {k: v for k, v in main_run.items() if k != "report"},
              "scan": {k: v for k, v in scan.items() if k != "report"},
              "refusal": {k: v for k, v in refusal.items() if k != "report"},
              "reports": {"main": main_run["report"], "scan": scan["report"], "refusal": refusal["report"]},
              "passed": sum(item["ok"] for item in checks), "failed": sum(not item["ok"] for item in checks),
              "trace_collection": "pending"}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    traces = httpx.get(args.jaeger_url.rstrip("/") + "/api/traces", params={"service": "research-worker", "limit": 30}, timeout=30, trust_env=False)
    traces.raise_for_status()
    matched = [trace for trace in traces.json().get("data", []) if main_run["research_id"] in json.dumps(trace)]
    (output.parent / "jaeger-main-research.json").write_text(json.dumps(matched, ensure_ascii=False))
    phases = [item for trace in matched for item in span_summary(trace)]
    result = {"checks": checks, "main": {k: v for k, v in main_run.items() if k != "report"},
              "scan": {k: v for k, v in scan.items() if k != "report"},
              "refusal": {k: v for k, v in refusal.items() if k != "report"},
              "reports": {"main": main_run["report"], "scan": scan["report"], "refusal": refusal["report"]},
              "trace_collection": "completed", "jaeger_phase_ms": phases, "passed": sum(item["ok"] for item in checks),
              "failed": sum(not item["ok"] for item in checks)}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"passed": result["passed"], "failed": result["failed"], "phases": phases}, ensure_ascii=False))
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
