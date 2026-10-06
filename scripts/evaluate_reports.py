"""Opt-in live evaluation using only the bundled synthetic public scenarios.

Run: poetry run python -m scripts.evaluate_reports --live --output /tmp/report-eval
No user documents, database access, ingestion or source collection are involved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from backend.config import Settings
from backend.models import ModelGateway
from backend.report_review import REPORT_INSTRUCTIONS
from backend.research import ResearchEngine


async def evaluate(output: Path, only: str | None = None) -> None:
    model = ModelGateway(Settings())
    engine = ResearchEngine.__new__(ResearchEngine)
    engine.model = model

    async def event(*args):
        pass

    engine.event = event
    scenarios = json.loads((Path(__file__).resolve().parents[1] /
                            "tests/fixtures/reports/scenarios.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for scenario in scenarios:
        if only and scenario["name"] != only:
            continue
        calls = []

        class RecordingModel:
            async def generate(self, system, user):
                response = await model.generate(system, user)
                calls.append({"system": system, "user": user, "response": response})
                (output / f"{scenario['name']}.trace.json").write_text(
                    json.dumps(calls, ensure_ascii=False, indent=2))
                return response

        engine.model = RecordingModel()
        print(f"{scenario['name']}: generating", flush=True)
        draft = await engine.model.generate(REPORT_INSTRUCTIONS, json.dumps(scenario, ensure_ascii=False))
        (output / f"{scenario['name']}.draft.md").write_text(draft)
        print(f"{scenario['name']}: reviewing", flush=True)
        result = await engine.review({**scenario, "research_id": "synthetic-report-evaluation",
                                      "draft": draft, "iteration": 1})
        body = result["draft"] or "Подтверждённых утверждений для ответа не найдено."
        if result["review"] != "ok":
            body += "\n\nНедостаточно доказательств; требуется дополнительная проверка."
        (output / f"{scenario['name']}.md").write_text(body)
        record = {"scenario": scenario["name"], "review": result["review"],
                  "revised": result.get("editorial_revision_done", False),
                  "dropped": result.get("dropped_claims", 0), "characters": len(body)}
        results.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
    (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True,
                        help="Allow calls to the configured model using synthetic public text")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", choices=["narrow", "comparison", "insufficient"])
    args = parser.parse_args()
    asyncio.run(evaluate(args.output, args.scenario))
