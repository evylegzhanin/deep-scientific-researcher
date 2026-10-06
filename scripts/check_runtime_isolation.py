"""Verify runtime container isolation without claiming physical host air-gapping."""
import argparse
import json
from pathlib import Path
import subprocess

PROBE = '''
import json, socket
results = []
for name, address, family in [
 ("external_dns", "huggingface.co", socket.AF_INET),
 ("external_ipv4", "1.1.1.1", socket.AF_INET),
 ("external_ipv6", "2606:4700:4700::1111", socket.AF_INET6),
]:
 try:
  host = socket.getaddrinfo(address, 443, family, socket.SOCK_STREAM)[0][4]
  with socket.socket(family, socket.SOCK_STREAM) as sock:
   sock.settimeout(5); sock.connect(host)
  results.append({"name":name, "reachable":True})
 except OSError as exc:
  results.append({"name":name, "reachable":False, "error":type(exc).__name__, "errno":exc.errno})
print(json.dumps(results))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    network = json.loads(subprocess.check_output(["docker", "network", "inspect", "research-benchmark_default"], text=True))[0]
    checks = []
    for service in ("api", "worker", "model"):
        container = "research-benchmark-" + service + "-1"
        state = json.loads(subprocess.check_output(["docker", "inspect", container], text=True))[0]
        python = "python3" if service == "model" else "python"
        probes = json.loads(subprocess.check_output(["docker", "exec", container, python, "-c", PROBE], text=True))
        networks = list(state["NetworkSettings"]["Networks"])
        checks.append({"service":service, "networks":networks, "probes":probes,
                       "ok":networks == [network["Name"]] and not any(p["reachable"] for p in probes)})
    result = {"scope":"runtime containers; host remains connected; startup verification is recorded separately",
              "network_internal":network["Internal"], "ipv6_enabled":network["EnableIPv6"],
              "checks":checks, "passed":network["Internal"] and all(c["ok"] for c in checks)}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
