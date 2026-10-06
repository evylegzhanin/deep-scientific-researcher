"""Check stopped model startup on the existing internal benchmark network."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time


def inspect(container):
    return json.loads(subprocess.check_output(["docker", "inspect", container], text=True))[0]


PROBE = '''
import json, os, urllib.request
request = urllib.request.Request("http://model:8000/v1/models", headers={"Authorization": "Bearer " + os.environ["MODEL_API_KEY"]})
with urllib.request.urlopen(request, timeout=10) as response:
 print(json.dumps({"status":response.status}))
'''

GENERATE = '''
import json, os, urllib.request
body = {"model":"research-text", "messages":[{"role":"user","content":"Напиши одно предложение: система работает локально."}], "max_tokens":32, "temperature":0}
request = urllib.request.Request("http://model:8000/v1/chat/completions", data=json.dumps(body).encode(), headers={"Authorization":"Bearer " + os.environ["MODEL_API_KEY"], "Content-Type":"application/json"})
with urllib.request.urlopen(request, timeout=120) as response:
 result = json.load(response)
 content = result["choices"][0]["message"].get("content") or ""
 print(json.dumps({"status":response.status,"nonempty":bool(content.strip()),"usage":result.get("usage"),"finish_reason":result["choices"][0].get("finish_reason")}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    model = "research-benchmark-model-1"
    api = "research-benchmark-api-1"
    before = inspect(model)
    network = json.loads(subprocess.check_output(["docker", "network", "inspect", "research-benchmark_default"], text=True))[0]
    environment = dict(value.split("=", 1) for value in before["Config"]["Env"] if "=" in value)
    isolated = network["Internal"] and not network["EnableIPv6"] and list(before["NetworkSettings"]["Networks"]) == [network["Name"]]
    offline = all(environment.get(name) == "1" for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"))
    if before["State"]["Running"] or not isolated or not offline:
        raise SystemExit("Expected stopped model with offline flags on its sole internal network")
    result = {"scope":"process startup after host reboot; staged weights and existing compiler cache; host remains connected",
              "started_at":datetime.now(timezone.utc).isoformat(), "model_was_stopped":True,
              "network_internal":network["Internal"], "ipv6_enabled":network["EnableIPv6"],
              "offline_environment":offline, "model_image_id":before["Image"], "passed":False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    save()
    started = time.monotonic()
    for service in ("postgres", "qdrant", "jaeger", "frontend", "model"):
        subprocess.run(["docker", "start", "research-benchmark-" + service + "-1"], check=True)
    deadline = started + args.timeout
    while time.monotonic() < deadline:
        response = subprocess.run(["docker", "exec", api, "python", "-c", PROBE], capture_output=True, text=True)
        if response.returncode == 0:
            result["models_endpoint"] = json.loads(response.stdout)
            result["readiness_observed_after_s"] = round(time.monotonic() - started, 3)
            break
        time.sleep(5)
    else:
        result["error"] = "Model readiness not observed within timeout"
        save()
        raise SystemExit(result["error"])
    generation = subprocess.run(["docker", "exec", api, "python", "-c", GENERATE], capture_output=True, text=True)
    result["generation"] = json.loads(generation.stdout) if generation.returncode == 0 else {"nonempty":False,"error":"generation command failed"}
    after = inspect(model)
    result["model_started_at"] = after["State"]["StartedAt"]
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result["passed"] = after["State"]["Running"] and result["generation"]["nonempty"] and list(after["NetworkSettings"]["Networks"]) == [network["Name"]]
    save()
    print(json.dumps(result), flush=True)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
