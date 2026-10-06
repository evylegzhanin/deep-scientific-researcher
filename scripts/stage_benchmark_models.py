"""Connected preparation only: pin and download local retrieval model snapshots."""
import argparse
import json
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    manifest_path = args.root / "retrieval-manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for folder, repo in [("embedding", "Qwen/Qwen3-Embedding-0.6B"),
                         ("reranker", "Qwen/Qwen3-Reranker-0.6B")]:
        revision = manifest.get(folder, {}).get("revision") or HfApi().model_info(repo).sha
        manifest[folder] = {"repository": repo, "revision": revision, "complete": False}
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Downloading {repo} at {revision}", flush=True)
        snapshot_download(repo, revision=revision, local_dir=args.root / folder,
                          max_workers=2, ignore_patterns=["*.onnx", "*.bin", "*.h5", "*.msgpack"])
        manifest[folder]["complete"] = True
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
