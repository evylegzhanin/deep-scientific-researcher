"""Compare local model snapshots with Hugging Face download metadata."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(root: Path) -> dict:
    report = {"models": {}}
    for name in ("generation", "embedding", "reranker"):
        folder = root / name
        trees = sorted((folder / ".cache/huggingface/trees").glob("*.json"))
        if not trees:
            report["models"][name] = {"error": "no snapshot tree"}
            print(f"{name} NO TREE", flush=True)
            continue
        tree = json.loads(trees[-1].read_text())
        checked = []
        mismatches = []
        for fname, meta in tree["files"].items():
            path = folder / fname
            if not path.is_file():
                mismatches.append({"file": fname, "error": "missing"})
                print(f"{name} {fname} MISSING", flush=True)
                continue
            size = path.stat().st_size
            expected_size = meta.get("lfs_size", meta.get("size"))
            record = {
                "file": fname,
                "size": size,
                "expected_size": expected_size,
                "size_ok": size == expected_size,
            }
            expected_hash = meta.get("lfs_sha256")
            if expected_hash:
                record["sha256"] = sha256(path)
                record["sha256_ok"] = record["sha256"] == expected_hash
                print(
                    f"{name} {fname} size_ok={record['size_ok']} sha256_ok={record['sha256_ok']}",
                    flush=True,
                )
            else:
                record["sha256_ok"] = None
                print(f"{name} {fname} size_ok={record['size_ok']} (no lfs hash)", flush=True)
            (mismatches if not record["size_ok"] or record["sha256_ok"] is False else checked).append(record)
        report["models"][name] = {
            "revision": trees[-1].stem,
            "files_ok": len(checked),
            "mismatches": mismatches,
        }
    return report


def main() -> None:
    root = Path.home() / "research-models"
    report = validate(root)
    dest = Path.home() / "benchmark-results" / "model-manifest.json"
    dest.write_text(json.dumps(report, indent=2) + "\n")
    bad = sum(len(item.get("mismatches", [])) for item in report["models"].values())
    print(f"MANIFEST {dest}", flush=True)
    print(f"MISMATCHES {bad}", flush=True)
    if bad:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
