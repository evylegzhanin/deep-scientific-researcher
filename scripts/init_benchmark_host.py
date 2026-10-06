"""Create local benchmark secrets and localhost TLS certificate; never print secrets."""
import os
from pathlib import Path
import secrets
import subprocess

root = Path(__file__).resolve().parents[1]
storage = Path.home() / "benchmark-results"
certs = storage / "certs"
certs.mkdir(parents=True, exist_ok=True)
certs.chmod(0o700)
env_path = root / ".env.benchmark"
if not env_path.exists():
    values = {
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "MODEL_API_KEY": secrets.token_hex(24),
        "BOOTSTRAP_EMAIL": "admin@example.local",
        "BOOTSTRAP_PASSWORD": secrets.token_urlsafe(24),
        "MODEL_ARTIFACTS_DIR": str(Path.home() / "research-models"),
        "BENCHMARK_CERT_DIR": str(certs),
        "BENCHMARK_NETWORK_INTERNAL": "false",
    }
    with os.fdopen(os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write("".join(f"{key}={value}\n" for key, value in values.items()))
    print("Created owner-readable .env.benchmark (values withheld)")
else:
    print("Preserved existing .env.benchmark")
if not (certs / "server.key").exists() and not (certs / "server.crt").exists():
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
        "-keyout", str(certs / "server.key"), "-out", str(certs / "server.crt"),
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (certs / "server.key").chmod(0o600)
    print("Created localhost benchmark certificate")
elif not all((certs / f).exists() for f in ("server.key", "server.crt")):
    raise RuntimeError("Incomplete existing certificate pair; inspect before replacing")
