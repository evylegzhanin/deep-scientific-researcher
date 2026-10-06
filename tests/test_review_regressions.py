"""Regression checks for project-review R1–R7; SQL tests use INTEGRATION_TESTS."""
import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from backend.api import get_document_file, followup, cancel, ResearchFollowup
from backend.audit import install_access_audit
from backend.auth import require_role
from backend.config import Settings
from backend.db import Database
from backend.jobs import claim_job, enqueue_run, heartbeat, require_claim, fail_job, LeaseLost
from backend.research import ResearchEngine
from backend.retrieval import Retriever
from backend.vision import describe_document_images


class StubDB:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def connection(self):
        yield self.conn


@pytest.mark.asyncio
async def test_html_original_is_download_only(tmp_path, monkeypatch):
    document_id, version_id = uuid4(), uuid4()
    path = tmp_path / 'original.html'
    path.write_text('<script>fetch("/api/v1/auth/me")</script>')
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value={
        'version_id': version_id, 'blob_path': str(path), 'media_type': 'text/html',
    }), fetch=AsyncMock(return_value=[]))
    monkeypatch.setattr('backend.document_reader.allowed_document_ids', AsyncMock(return_value=[document_id]))
    monkeypatch.setattr('backend.document_reader.allowed_chunk_ids', AsyncMock(return_value=[]))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=StubDB(conn), settings=SimpleNamespace(file_root=tmp_path))))
    response = await get_document_file(document_id, request, {})
    assert response.headers['content-disposition'].startswith('attachment;')
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert 'sandbox' in response.headers['content-security-policy']


@pytest.mark.asyncio
async def test_external_pixels_disabled_by_default():
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value={'classification': 'public'}), fetch=AsyncMock())
    model = SimpleNamespace(describe_page=AsyncMock())
    assert await describe_document_images(StubDB(conn), Settings(_env_file=None), model, uuid4(), 'public') == 0
    conn.fetch.assert_not_called()
    model.describe_page.assert_not_called()


def test_access_denials_audited_without_secrets():
    conn = SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(return_value={'id': uuid4(), 'role': 'reader', 'email': 'reader', 'csrf_hash': ''}))
    app = FastAPI()
    app.state.db = StubDB(conn)
    install_access_audit(app)

    @app.get('/private/{document_id}')
    async def private(document_id: str, user=Depends(require_role('admin'))):
        return {}

    client = TestClient(app)
    target = uuid4()
    response = client.get(f'/private/{target}?token=SECRET')
    assert response.status_code == 401
    args = conn.execute.call_args.args
    assert args[1] is None and args[2] == 'GET /private/{document_id}'
    assert 'SECRET' not in str(args)
    assert str(args[-1]) == response.headers['x-request-id']
    client.cookies.set('research_session', 'SESSION_SECRET')
    assert client.get(f'/private/{target}').status_code == 403
    assert conn.execute.call_args.args[1] == conn.fetchrow.return_value['id']
    assert 'SESSION_SECRET' not in str(conn.execute.call_args)


@pytest.mark.asyncio
async def test_new_relevant_evidence_replaces_full_old_set(monkeypatch):
    owner = uuid4()
    old = [{'chunk_id': str(uuid4()), 'title': 'unrelated', 'text': 'legacy topic'} for _ in range(18)]
    fresh = uuid4()
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value={'id': owner, 'role': 'researcher'}))
    engine = object.__new__(ResearchEngine)
    engine.db = StubDB(conn)
    engine.settings = Settings(_env_file=None, model_base_url='http://localhost:8001/v1')
    engine.event = AsyncMock()
    retriever = object.__new__(Retriever)
    retriever.settings = engine.settings
    retriever.search = AsyncMock(return_value=[{'id': fresh, 'document_id': uuid4(), 'title': 'new requirement',
        'body': 'new requirement', 'page_number': 1, 'source_url': None, 'sha256': 'a'*64, 'classification': 'public'}])
    engine.retriever = retriever
    monkeypatch.setattr('backend.research.allowed_chunk_ids', AsyncMock(return_value=[fresh] + [i['chunk_id'] for i in old]))
    result = await engine.retrieve({'research_id': str(uuid4()), 'owner_id': str(owner), 'question': 'new requirement', 'evidence': old})
    assert len(result['evidence']) == 18
    assert result['evidence'][0]['chunk_id'] == str(fresh)
    assert [i['marker'] for i in result['evidence']] == [f'E{i}' for i in range(1, 19)]


@pytest.mark.asyncio
async def test_local_round_uses_next_query_and_stops_without_progress(monkeypatch):
    engine = object.__new__(ResearchEngine)
    engine.db = StubDB(SimpleNamespace(fetchval=AsyncMock(return_value='restricted')))
    engine.settings = Settings(_env_file=None)
    engine.event = AsyncMock()
    state = {'research_id': str(uuid4()), 'question': 'compare sensors', 'search_queries': ['accuracy', 'range']}
    first = await engine.collect(state)
    second = await engine.collect({**state, **first, 'evidence_gaps': ['temperature']})
    assert first['retrieval_query'] == 'accuracy'
    assert second['retrieval_query'] == 'range temperature'
    assert engine.route({**second, 'review': 'insufficient', 'needs_more_evidence': True, 'new_evidence_count': 0}) == 'done'

    # Distinct local queries actually expand evidence, not just the round counter.
    owner, one, two = uuid4(), uuid4(), uuid4()
    engine.settings = Settings(_env_file=None, model_base_url='http://localhost:8001/v1')
    engine.db.conn.fetchrow = AsyncMock(return_value={'id': owner, 'role': 'researcher'})
    monkeypatch.setattr('backend.research.allowed_chunk_ids', AsyncMock(return_value=[one, two]))
    retriever = object.__new__(Retriever)
    retriever.settings = engine.settings

    async def search(query, *args, **kwargs):
        return [{'id': one if query == 'accuracy' else two, 'document_id': uuid4(),
                 'title': query, 'body': query, 'page_number': 1, 'source_url': None,
                 'sha256': 'a'*64, 'classification': 'public'}]

    retriever.search = search
    engine.retriever = retriever
    state['owner_id'] = str(owner)
    result = await engine.retrieve({**state, **first})
    next_result = await engine.retrieve({**state, **result, **second})
    assert {e['chunk_id'] for e in next_result['evidence']} == {str(one), str(two)}
    repeated = await engine.retrieve({**state, **next_result, **second})
    assert repeated['new_evidence_count'] == 0


@pytest.fixture
async def sql_db():
    if not os.getenv('INTEGRATION_TESTS'):
        pytest.skip('Requires PostgreSQL')
    settings = Settings(_env_file=None, database_url=os.getenv('TEST_DATABASE_URL', 'postgresql://research:research-dev-only@localhost:5434/research'), model_base_url='http://localhost:8001/v1')
    db = Database(settings)
    await db.start()
    async with db.connection() as conn:
        owner = await conn.fetchval("INSERT INTO users(email,password_hash,role) VALUES($1,'test','researcher') RETURNING id", f'{uuid4()}@test.local')
        research = await conn.fetchval("INSERT INTO researches(owner_id,title,question) VALUES($1,'test','original question') RETURNING id", owner)
        async with conn.transaction():
            await enqueue_run(conn, research, 'original question')
    yield db, settings, owner, research
    async with db.connection() as conn:
        await conn.execute('DELETE FROM researches WHERE id=$1', research)
        await conn.execute('DELETE FROM users WHERE id=$1', owner)
    await db.stop()


@pytest.mark.asyncio
async def test_claim_heartbeat_and_stale_worker_fencing(sql_db):
    db, _, _, research = sql_db
    claims = await asyncio.gather(claim_job(db, research), claim_job(db, research))
    claim = next(c for c in claims if c)
    assert sum(c is not None for c in claims) == 1
    assert await heartbeat(db, claim)
    assert await claim_job(db, research) is None
    async with db.connection() as conn:
        await conn.execute("UPDATE research_jobs SET locked_at=now()-interval '3 minutes' WHERE research_id=$1", research)
    successor = await claim_job(db, research)
    assert successor.run_id == claim.run_id and successor.token != claim.token
    assert not await heartbeat(db, claim)
    async with db.connection() as conn:
        async with conn.transaction():
            with pytest.raises(LeaseLost):
                await require_claim(conn, claim)
    await fail_job(db, claim, 'stale failure')
    async with db.connection() as conn:
        assert await conn.fetchval('SELECT lease_token FROM research_jobs WHERE research_id=$1', research) == successor.token


@pytest.mark.asyncio
async def test_cancel_then_followup_fences_old_run(sql_db):
    db, settings, owner, research = sql_db
    user = {'id': owner, 'role': 'researcher'}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, settings=settings)))
    claim = await claim_job(db, research)
    await cancel(research, request, user)
    followups = await asyncio.gather(
        followup(research, ResearchFollowup(message='NEW REQUIREMENT'), request, user),
        followup(research, ResearchFollowup(message='NEW REQUIREMENT'), request, user), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in followups) == 1
    assert sum(getattr(result, 'status_code', None) == 409 for result in followups) == 1
    async with db.connection() as conn:
        assert await conn.fetchval('SELECT status FROM research_runs WHERE id=$1', claim.run_id) == 'cancelled'
        async with conn.transaction():
            with pytest.raises(LeaseLost):
                await require_claim(conn, claim)
        job = await conn.fetchrow('SELECT * FROM research_jobs WHERE research_id=$1', research)
        assert job['status'] == 'queued' and job['run_id'] != claim.run_id
    results = await asyncio.gather(
        followup(research, ResearchFollowup(message='another'), request, user),
        followup(research, ResearchFollowup(message='another'), request, user), return_exceptions=True)
    assert all(getattr(r, 'status_code', None) == 409 for r in results)


@pytest.mark.asyncio
async def test_retry_resumes_but_user_revision_gets_fresh_state(sql_db, monkeypatch):
    db, settings, owner, research = sql_db
    questions = []
    fail = True

    async def plan(self, state):
        questions.append(state['question'])
        assert state['evidence'] == []
        return {'clarification': '', 'iteration': 0}

    async def passthrough(self, state):
        return {}

    async def analyze(self, state):
        nonlocal fail
        if fail:
            fail = False
            raise RuntimeError('temporary')
        return {'draft': state['question'], 'review': 'ok'}

    async def finalize(self, state):
        return {'report': state['draft'], 'evidence': [], 'review_status': 'ok'}

    for name, fn in [('plan', plan), ('collect', passthrough), ('retrieve', passthrough), ('analyze', analyze), ('review', passthrough), ('finalize', finalize)]:
        monkeypatch.setattr(ResearchEngine, name, fn)
    engine = ResearchEngine(db, settings, None, checkpointer=InMemorySaver())
    claim = await claim_job(db, research)
    with pytest.raises(RuntimeError):
        await engine.run(research, claim)
    # Force the last SQL write to fail: report/job/event must roll back together.
    import asyncpg
    async with db.connection() as conn:
        await conn.execute(f"ALTER TABLE research_events ADD CONSTRAINT test_completion_failure CHECK (research_id != '{research}'::uuid OR kind != 'completed')")
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await engine.run(research, claim)
        async with db.connection() as conn:
            assert await conn.fetchval('SELECT count(*) FROM reports WHERE research_id=$1', research) == 0
            assert await conn.fetchval('SELECT status FROM researches WHERE id=$1', research) == 'running'
    finally:
        async with db.connection() as conn:
            await conn.execute('ALTER TABLE research_events DROP CONSTRAINT test_completion_failure')
    await engine.run(research, claim)
    assert questions == ['original question']
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, settings=settings)))
    await followup(research, ResearchFollowup(message='NEW REQUIREMENT'), request, {'id': owner, 'role': 'researcher'})
    next_claim = await claim_job(db, research)
    fail = True
    with pytest.raises(RuntimeError):
        await engine.run(research, next_claim)
    async with db.connection() as conn:
        await conn.execute('UPDATE research_jobs SET attempts=3 WHERE research_id=$1', research)
    await fail_job(db, next_claim, 'RuntimeError')
    await followup(research, ResearchFollowup(message='AFTER FAILURE'), request, {'id': owner, 'role': 'researcher'})
    final_claim = await claim_job(db, research)
    await engine.run(research, final_claim)
    assert 'AFTER FAILURE' in questions[-1]
    assert len(questions) == 3 and 'NEW REQUIREMENT' in questions[-1]
    async with db.connection() as conn:
        reports = await conn.fetch('SELECT body,run_id FROM reports WHERE research_id=$1 ORDER BY version', research)
        assert len(reports) == 2 and 'NEW REQUIREMENT' in reports[-1]['body']
        assert reports[0]['run_id'] != reports[1]['run_id']
        assert await conn.fetchval("SELECT count(*) FROM research_events WHERE research_id=$1 AND kind='completed'", research) == 2
    with pytest.raises(LeaseLost):
        await engine.run(research, claim)


@pytest.mark.asyncio
async def test_text_acl_blocks_sibling_image(sql_db, tmp_path):
    db, _, _, _ = sql_db
    settings = Settings(_env_file=None, file_root=tmp_path, allow_external_images=True)
    path = tmp_path / 'page.png'
    path.write_bytes(b'synthetic pixels')
    model = SimpleNamespace(describe_page=AsyncMock(return_value='public caption'))
    async with db.connection() as conn:
        doc = await conn.fetchval("INSERT INTO documents(title,source_kind,classification) VALUES('test','upload','public') RETURNING id")
        version = await conn.fetchval("INSERT INTO document_versions(document_id,sha256,blob_path,media_type) VALUES($1,$2,'test','application/pdf') RETURNING id", doc, uuid4().hex*2)
        text = await conn.fetchval("INSERT INTO chunks(version_id,page_number,kind,body) VALUES($1,1,'text','restricted words') RETURNING id", version)
        await conn.execute("INSERT INTO chunks(version_id,page_number,kind,body) VALUES($1,1,'image','Фрагмент страницы: page.png')", version)
        await conn.execute("INSERT INTO chunk_acl(chunk_id,principal_type,principal_id) VALUES($1,'user',$2)", text, uuid4())
    try:
        assert await describe_document_images(db, settings, model, doc, 'public') == 0
        model.describe_page.assert_not_called()
    finally:
        async with db.connection() as conn:
            await conn.execute('DELETE FROM documents WHERE id=$1', doc)
