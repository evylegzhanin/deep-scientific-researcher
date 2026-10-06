from __future__ import annotations

import asyncio
import os
import traceback

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from backend.auth import bootstrap_admin
from backend.jobs import claim_job, heartbeat, fail_job, LeaseLost
from backend.config import get_settings
from backend.db import Database
from backend.research import ResearchEngine
from backend.retrieval import Retriever
from backend.secrets import load_openbao_secrets
from backend.telemetry import configure_tracing, worker_metrics_server


async def execute_claim(db, engine, claim):
    async def renew():
        while True:
            await asyncio.sleep(20)
            if not await heartbeat(db, claim):
                raise LeaseLost()

    async def execute():
        # Session lock prevents overlapping checkpoint writes after lease expiry.
        # Different user runs have different keys and may proceed independently.
        async with db.connection() as conn:
            key = int.from_bytes(claim.run_id.bytes[:8], 'big', signed=True)
            await conn.execute("SELECT pg_advisory_lock($1)", key)
            try:
                await engine.run(claim.research_id, claim)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", key)

    work = asyncio.create_task(execute())
    pulse = asyncio.create_task(renew())
    try:
        done, _ = await asyncio.wait({work, pulse}, return_when=asyncio.FIRST_COMPLETED,
                                     timeout=engine.settings.research_timeout_seconds)
        if not done:
            raise TimeoutError('Research deadline exceeded')
        for task in done:
            task.result()
    except LeaseLost:
        pass
    except Exception as exc:
        traceback.print_exc()
        await fail_job(db, claim, type(exc).__name__)
    finally:
        work.cancel()
        pulse.cancel()
        await asyncio.gather(work, pulse, return_exceptions=True)


async def main() -> None:
    await load_openbao_secrets()
    get_settings.cache_clear()
    settings = get_settings()
    configure_tracing("research-worker")
    worker_metrics_server()
    db = Database(settings)
    await db.start()
    await bootstrap_admin(db, settings.bootstrap_email, settings.bootstrap_password)
    retriever = Retriever(db, settings)
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    try:
        async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
            await saver.setup()
            engine = ResearchEngine(db, settings, retriever, checkpointer=saver)
            await engine.model.check_available()
            while True:
                claim = await claim_job(db)
                if claim is None:
                    await asyncio.sleep(2)
                    continue
                await execute_claim(db, engine, claim)
    finally:
        await retriever.close()
        await db.stop()


if __name__ == "__main__":
    asyncio.run(main())
