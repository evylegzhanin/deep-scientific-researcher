"""Run identity, lease ownership and fenced queue transitions.

All multi-row transitions lock research before job. A token is invalidated by
cancel/follow-up/reclaim; stale workers may never publish results or events.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4


class LeaseLost(Exception):
    pass


@dataclass(frozen=True)
class Claim:
    research_id: UUID
    run_id: UUID
    token: UUID


async def enqueue_run(conn, research_id: UUID, question: str) -> UUID:
    run_id = await conn.fetchval(
        "INSERT INTO research_runs(research_id,question) VALUES($1,$2) RETURNING id",
        research_id, question,
    )
    await conn.execute(
        """INSERT INTO research_jobs(research_id,run_id) VALUES($1,$2)
           ON CONFLICT(research_id) DO UPDATE SET run_id=$2,status='queued',attempts=0,
           available_at=now(),locked_at=NULL,lease_token=NULL,last_error=NULL""",
        research_id, run_id,
    )
    await conn.execute("UPDATE researches SET error=NULL WHERE id=$1", research_id)
    await conn.execute(
        "INSERT INTO research_events(research_id,run_id,kind,payload) "
        "VALUES($1,$2,'queued',jsonb_build_object('status','queued'))", research_id, run_id,
    )
    return run_id


async def claim_job(db, research_id: UUID | None = None) -> Claim | None:
    async with db.connection() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT r.id FROM researches r WHERE ($1::uuid IS NULL OR r.id=$1)
                   AND r.status IN ('queued','running') AND EXISTS(
                     SELECT 1 FROM research_jobs j WHERE j.research_id=r.id
                     AND j.available_at<=now() AND (j.status='queued' OR
                     (j.status='running' AND j.locked_at<now()-interval '2 minutes')))
                   ORDER BY r.updated_at FOR UPDATE OF r SKIP LOCKED LIMIT 1""", research_id,
            )
            if not row:
                return None
            token = uuid4()
            run_id = await conn.fetchval(
                """UPDATE research_jobs SET status='running',locked_at=now(),
                   attempts=attempts+1,lease_token=$2 WHERE research_id=$1 RETURNING run_id""",
                row['id'], token,
            )
            await conn.execute("UPDATE research_runs SET status='running' WHERE id=$1", run_id)
            await conn.execute("UPDATE researches SET status='running',error=NULL,updated_at=now() WHERE id=$1", row['id'])
            await conn.execute(
                "INSERT INTO research_events(research_id,run_id,kind,payload) "
                "VALUES($1,$2,'started',jsonb_build_object('status','running'))", row['id'], run_id,
            )
            return Claim(row['id'], run_id, token)


async def require_claim(conn, claim: Claim):
    """Must be called inside the transaction that writes protected state."""
    research = await conn.fetchrow("SELECT * FROM researches WHERE id=$1 FOR UPDATE", claim.research_id)
    owned = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM research_jobs WHERE research_id=$1 AND run_id=$2
           AND lease_token=$3 AND status='running' AND locked_at>now()-interval '2 minutes')""",
        claim.research_id, claim.run_id, claim.token,
    )
    if not research or not owned or research['status'] != 'running':
        raise LeaseLost()
    return research


async def heartbeat(db, claim: Claim) -> bool:
    async with db.connection() as conn:
        result = await conn.execute(
            """UPDATE research_jobs SET locked_at=now() WHERE research_id=$1 AND run_id=$2
               AND lease_token=$3 AND status='running' AND locked_at>now()-interval '2 minutes'""",
            claim.research_id, claim.run_id, claim.token,
        )
    return result == 'UPDATE 1'


async def finish_job(conn, claim: Claim, status: str):
    await conn.execute("UPDATE research_runs SET status=$2 WHERE id=$1", claim.run_id, status)
    await conn.execute(
        """UPDATE research_jobs SET status=$4,locked_at=NULL,lease_token=NULL
           WHERE research_id=$1 AND run_id=$2 AND lease_token=$3""",
        claim.research_id, claim.run_id, claim.token, status,
    )


async def fail_job(db, claim: Claim, error: str):
    async with db.connection() as conn:
        async with conn.transaction():
            try:
                await require_claim(conn, claim)
            except LeaseLost:
                return
            attempts = await conn.fetchval("SELECT attempts FROM research_jobs WHERE research_id=$1", claim.research_id)
            status = 'queued' if attempts < 3 else 'failed'
            await finish_job(conn, claim, status)
            await conn.execute(
                "UPDATE research_jobs SET available_at=now()+($2::int * interval '1 minute'),last_error=$3 WHERE research_id=$1",
                claim.research_id, attempts, error,
            )
            await conn.execute("UPDATE researches SET status=$2,error=$3,updated_at=now() WHERE id=$1", claim.research_id, status, error)
            await conn.execute(
                "INSERT INTO research_events(research_id,run_id,kind,payload) VALUES($1,$2,$3,jsonb_build_object('error',$4::text))",
                claim.research_id, claim.run_id, 'retry' if status == 'queued' else 'failed', error,
            )
