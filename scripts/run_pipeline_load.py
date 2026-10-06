"""Run the planned research-load steps and a stability rerun at the best passing level."""
from __future__ import annotations

import json
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys


def run_level(name: str, count: int, concurrency: int, question: str, results: Path, args) -> dict:
    print(f"start {name} count={count} concurrency={concurrency}", flush=True)
    output = args.output_dir / f"research-load-{name}.json"
    started_at = datetime.now(timezone.utc).isoformat()
    command = [
        sys.executable, "-m", "scripts.benchmark_research",
        "--base-url", args.base_url,
        "--ca-cert", str(results / "certs/server.crt"),
        "--question", question,
        "--count", str(count),
        "--concurrency", str(concurrency),
        "--timeout", str(args.timeout), "--cancel-timeouts",
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        output.with_suffix(".error.log").write_text(completed.stderr)
        raise SystemExit(f"{name} failed: {completed.stderr[-2000:]}")
    payload = json.loads(completed.stdout)
    payload.update(started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat())
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    summary = {key: value for key, value in payload.items() if key != "samples"}
    print(json.dumps({"level": name, **summary}, ensure_ascii=False), flush=True)
    return payload


def passes(payload: dict, baseline_p95: float | None) -> bool:
    count = payload["count"]
    errors = count - payload["completed_with_report"]
    if errors / count >= 0.05:
        return False
    p95 = payload["overall_completion_p95_ms"]
    if p95 is None:
        return False
    if baseline_p95 and p95 is not None and p95 > 2 * baseline_p95:
        return False
    return payload["completed_with_report"] > 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://localhost:8443")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--levels", nargs="+", default=["5:1", "4:2", "8:4", "16:8"], help="count:concurrency; first is baseline")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--stability-count", type=int, default=0)
    parser.add_argument("--account", choices=["a", "quality"], help="Explicit account for reproducing mixed or fixed corpus measurements")
    args = parser.parse_args()
    levels = [tuple(int(p) for p in level.split(":")) for level in args.levels]
    if any(len(level) != 2 or min(level) < 1 for level in levels) or args.timeout <= 0 or args.stability_count < 0:
        parser.error("Invalid load levels or time limits")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = args.results
    accounts = json.loads((results / "benchmark-users.json").read_text())
    question = json.loads((results / "corpus-preparation.json").read_text())["question"]
    account_name = args.account or ("quality" if "quality" in accounts else "a")
    if account_name not in accounts:
        parser.error("Selected benchmark account has not been provisioned")
    account = accounts[account_name]
    os.environ["BENCH_EMAIL"] = account["email"]
    os.environ["BENCH_PASSWORD"] = account["password"]
    finished = []
    baseline_p95 = None
    for index, (count, concurrency) in enumerate(levels):
        name = "baseline" if index == 0 else f"c{concurrency}"
        payload = run_level(name, count, concurrency, question, results, args)
        if name == "baseline":
            baseline_p95 = payload["overall_completion_p95_ms"]
        finished.append((name, count, concurrency, payload, passes(payload, baseline_p95)))
        if not finished[-1][4]:
            break
    stable = [item for item in finished if item[4]]
    chosen = stable[-1] if stable else None
    stability = None
    if chosen and args.stability_count:
        stability = run_level(f"stability-c{chosen[2]}", args.stability_count, chosen[2], question, results, args)
    report = {
        "account": account_name,
        "baseline_p95_ms": baseline_p95,
        "levels": [{"name": name, "pass": ok, "concurrency": concurrency,
                    "completed_with_report": payload["completed_with_report"],
                    "count": payload["count"], "successful_rps": payload["successful_rps"],
                    "researches_per_min": payload["researches_per_min"],
                    "research_p50_ms": payload["research_p50_ms"],
                    "research_p95_ms": payload["research_p95_ms"],
                    "research_p99_ms": payload["research_p99_ms"]}
                   for name, _count, concurrency, payload, ok in finished],
        "candidate_concurrency": chosen[2] if chosen else None,
        "stability_concurrency": chosen[2] if stability else None,
        "stability_pass": passes(stability, baseline_p95) if stability else None,
        "capacity_certified": False,
        "qualification": "Observed candidate levels only; a small rerun cannot certify stability.",
    }
    (args.output_dir / "research-load-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
