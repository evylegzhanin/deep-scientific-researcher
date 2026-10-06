"""Download selected vendor manuals and ingest them into the configured service.

Run explicitly; startup and airgap deployments never fetch these sources.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path

import httpx
from pypdf import PdfReader

from backend.config import Settings
from backend.db import Database
from backend.ingestion import ingest_document
ROOT = Path(__file__).resolve().parents[1]

CORPUS = ROOT / "demo/documents"


async def main(download_only: bool = False, env_file: str = ".env") -> None:
    sources = json.loads((CORPUS / "sources.json").read_text())
    settings = Settings(_env_file=env_file)
    records = []
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for source in sources:
            path = CORPUS / source["filename"]
            if not path.exists():
                response = await client.get(source["url"])
                response.raise_for_status()
                if not response.content.startswith(b"%PDF"):
                    raise ValueError(f"Vendor returned a non-PDF response for {source['filename']}")
                if len(response.content) > settings.max_document_bytes:
                    raise ValueError("Manual exceeds upload limit")
                PdfReader(io.BytesIO(response.content))
                path.write_bytes(response.content)
            data = path.read_bytes()
            reader = PdfReader(io.BytesIO(data))
            records.append({**source, "sha256": hashlib.sha256(data).hexdigest(),
                            "pages": len(reader.pages), "bytes": len(data)})
            print(f"Ready: {source['filename']} ({len(reader.pages)} pages)")
    (CORPUS / "manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    if download_only:
        return
    db = Database(settings)
    await db.start()
    try:
        for source in sources:
            document_id = await ingest_document(
                db, settings, None, source["title"], (CORPUS / source["filename"]).read_bytes(),
                "application/pdf", "documentation", source_url=source["url"], classification="public",
            )
            print(f"Indexed: {source['title']} → #/document/{document_id}")
    finally:
        await db.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--env-file", default=".env", help="Service configuration (never committed)")
    args = parser.parse_args()
    asyncio.run(main(args.download_only, args.env_file))
