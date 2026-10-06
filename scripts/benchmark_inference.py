"""Short reproducible streaming inference baseline; not a saturation/SLO certification."""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import secrets
import time

import httpx


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values)*p)-1)] if values else None


async def run(args):
    headers = {"Authorization": "Bearer " + os.environ["MODEL_API_KEY"]}
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=180, trust_env=False) as client:
        async def one(index):
            started = time.perf_counter()
            first = None
            usage = {}
            chunks = []
            payload = {
                "model": "research-text", "temperature": 0, "max_tokens": args.max_tokens,
                "stream": True, "stream_options": {"include_usage": True},
                "cache_salt": secrets.token_hex(16),
                "messages": [{"role": "user", "content": f"Тест {index}. На русском языке подробно объясни, как проверять цитаты в техническом отчёте и отмечать отсутствие доказательств. Не используй внешние источники."}],
            }
            if args.context_repeats:
                payload["messages"][0]["content"] += "\nКонтекст:\n" + (
                    "В техническом отчёте нужно проверять происхождение каждого утверждения, единицы измерения, область применимости испытаний и версию исходного документа.\n"
                    * args.context_repeats)
            try:
                async with client.stream("POST", "/v1/chat/completions", json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        event = json.loads(line[6:])
                        usage = event.get("usage") or usage
                        for choice in event.get("choices", []):
                            content = choice.get("delta", {}).get("content")
                            if content:
                                if first is None:
                                    first = time.perf_counter()-started
                                chunks.append(content)
                elapsed = time.perf_counter()-started
                tokens = usage.get("completion_tokens", 0)
                return {"ok": first is not None and tokens > 0, "elapsed_s": elapsed,
                        "ttft_s": first, "usage": usage,
                        "decode_tokens_per_s": (tokens-1)/(elapsed-first) if first is not None and tokens > 1 and elapsed > first else None,
                        "output": "".join(chunks)}
            except Exception as exc:
                return {"ok": False, "elapsed_s": time.perf_counter()-started, "error": type(exc).__name__}

        warmup = await one(-1)
        if not warmup["ok"]:
            raise RuntimeError(f"Warmup failed: {warmup}")
        result = {"kind": "short_inference_baseline", "max_tokens": args.max_tokens,
                  "context_repeats": args.context_repeats,
                  "cache_policy": "unique per-request salt", "warmup": warmup, "runs": []}
        for concurrency in args.concurrency:
            sem = asyncio.Semaphore(concurrency)
            async def limited(i):
                async with sem:
                    return await one(i)
            started = time.perf_counter()
            samples = await asyncio.gather(*(limited(i) for i in range(args.count)))
            wall = time.perf_counter()-started
            good = [s for s in samples if s["ok"]]
            run = {"concurrency": concurrency, "count": args.count, "success": len(good),
                   "errors": len(samples)-len(good), "wall_s": wall, "successful_rps": len(good)/wall,
                   "aggregate_output_tokens_per_s": sum(s["usage"]["completion_tokens"] for s in good)/wall,
                   "samples": samples}
            for label, key in [("latency", "elapsed_s"), ("ttft", "ttft_s")]:
                for p in [50, 95, 99]:
                    run[f"{label}_p{p}_s"] = percentile([s[key] for s in good], p/100)
            result["runs"].append(run)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps({k:v for k,v in run.items() if k != "samples"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--context-repeats", type=int, default=0, help="Synthetic context repetitions; actual token counts are returned in usage")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.count < 1 or args.max_tokens < 1 or args.context_repeats < 0 or any(c < 1 for c in args.concurrency):
        parser.error("Count, concurrency and max-tokens must be positive")
    asyncio.run(run(args))
