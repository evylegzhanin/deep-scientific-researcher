"""Run revised checks sequentially on the rented GPU host; preserve original results."""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import subprocess

READY = '''
import httpx, ssl, time
context=ssl.create_default_context(cafile="/results/certs/server.crt")
with httpx.Client(verify=context, trust_env=False, timeout=5) as client:
 for attempt in range(12):
  try:
   response=client.get("https://localhost:443/health/ready")
   if response.status_code==200:
    print("API ready through TLS proxy",flush=True); break
  except httpx.HTTPError:
   pass
  time.sleep(5)
 else:
  raise SystemExit("API not ready after 60 seconds")
'''

ROOT = Path("/home/ubuntu/deep-scientific-researcher")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="revision-1")
    parser.add_argument("--image", default="research-backend:revision1")
    args = parser.parse_args()
    if args.revision not in {"revision-1", "revision-2"}:
        parser.error("Unsupported revision output directory")
    results = Path("/home/ubuntu/benchmark-results") / args.revision
    results.mkdir(parents=True, exist_ok=True)
    stages = [
        ("quality", ["scripts.benchmark_quality", "--base-url", "https://localhost:443", "--results", "/results", "--output", "/results/revision-1/quality-checks.json", "--jaeger-url", "http://jaeger:16686"], "quality-checks.json"),
        ("security", ["scripts.benchmark_security", "--base-url", "https://localhost:443", "--results", "/results", "--output", "/results/revision-1/security-checks.json"], "security-checks.json"),
        ("load", ["scripts.run_pipeline_load", "--base-url", "https://localhost:443", "--results", "/results", "--output-dir", "/results/revision-1", "--levels", "2:1", "4:2"], "research-load-summary.json"),
        ("inference", ["scripts.benchmark_inference", "--base-url", "http://model:8000", "--count", "16", "--concurrency", "1", "4", "16", "--context-repeats", "64", "--max-tokens", "128", "--output", "/results/revision-1/inference-context.json"], "inference-context.json"),
    ]
    stages = [(name, [arg.replace("/revision-1", "/" + args.revision) for arg in command], artifact)
              for name, command, artifact in stages]
    status = {"started_at": datetime.now(timezone.utc).isoformat(), "image":args.image, "stages": []}
    def save():
        (results / "suite-status.json").write_text(json.dumps(status, indent=2))
    for name, command_args, artifact in stages:
        item = {"stage":name, "state":"running", "started_at":datetime.now(timezone.utc).isoformat()}
        status["stages"].append(item); save()
        prefix = ["docker", "run", "--rm", "--network", "container:research-benchmark-frontend-1",
                   "--env-file", str(ROOT / ".env.benchmark"), "-v", f"{ROOT}:/work:ro",
                   "-v", "/home/ubuntu/benchmark-results:/results", "-w", "/work",
                   args.image, "python"]
        command = [*prefix, "-m", *command_args]
        print("Starting " + name, flush=True)
        with (results / f"{name}.log").open("w") as log:
            ready = subprocess.call([*prefix, "-c", READY], stdout=log, stderr=subprocess.STDOUT)
            code = subprocess.call(command, stdout=log, stderr=subprocess.STDOUT) if ready == 0 else ready
        exists = (results / artifact).exists()
        item.update(state="complete" if code == 0 and exists else "checks_failed" if code != 0 and exists else "error",
                    exit_code=code, finished_at=datetime.now(timezone.utc).isoformat())
        save(); print(json.dumps(item), flush=True)
        if item["state"] == "error":
            raise SystemExit(code or 1)
    status["finished_at"] = datetime.now(timezone.utc).isoformat(); save()


if __name__ == "__main__":
    main()
