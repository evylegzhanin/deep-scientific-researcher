from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.guardrails import filter_cited_blocks, report_blocks
from backend.report_review import review_report
from backend.research import ResearchEngine


EVIDENCE = [{"marker": "E1", "title": "Испытания", "text": "Порог давления 42 бар. Проверка обязательна."}]


def verdict(blocks, rejected=(), coherent=True):
    return json.dumps({
        "status": "need_more" if rejected else "ok" if coherent else "needs_revision",
        "coherent": coherent, "feedback": "" if coherent else "Нужен связный ответ по темам.",
        "blocks": [{"id": block["id"], "supported": block["id"] not in rejected,
                    "feedback": "Нет подтверждения." if block["id"] in rejected else ""}
                   for block in blocks],
    })


class Reviewer:
    def __init__(self, reject_text="", coherent=True, rewrite=None):
        self.calls = []
        self.reject_text = reject_text
        self.coherent = coherent
        self.rewrite = rewrite

    async def generate(self, system, user):
        payload = json.loads(user)
        self.calls.append(payload)
        if "blocks" not in payload:
            return self.rewrite
        rejected = [block["id"] for block in payload["blocks"]
                    if self.reject_text and self.reject_text in block["text"]]
        return verdict(payload["blocks"], rejected, self.coherent)


def engine(model):
    instance = ResearchEngine.__new__(ResearchEngine)
    instance.model = model
    instance.event = AsyncMock()
    instance.settings = SimpleNamespace(max_research_steps=3)
    return instance


def state(draft, **kwargs):
    return {"research_id": "test", "question": "Какой порог давления?", "draft": draft,
            "evidence": EVIDENCE, "iteration": 1, **kwargs}


def test_paragraph_citations_cover_soft_breaks_and_multiple_sources():
    draft = "## Краткий ответ\n\nПорог давления 42 бар.\nПроверка обязательна [E1]."
    checked, issues = filter_cited_blocks(draft, EVIDENCE)
    assert checked == draft
    assert not issues
    multiple = "Порог 42 бар, а температура 80 градусов [E1] [E2]."
    assert filter_cited_blocks(multiple, EVIDENCE + [{"marker": "E2", "text": "Температура 80 градусов."}]) == (multiple, [])


def test_unknown_marker_drops_entire_paragraph_and_empty_section():
    draft = "## Пустой раздел\n\nНовый факт [E99].\n\n## Давление\n\nПорог 42 бар [E1]."
    checked, issues = filter_cited_blocks(draft, EVIDENCE)
    assert "Пустой" not in checked
    assert "Новый" not in checked
    assert "## Давление" in checked
    assert len(issues) == 1


def test_missing_math_citation_does_not_consume_next_paragraph():
    checked, issues = filter_cited_blocks("$$x=5$$\n\nПорог 42 бар [E1].", EVIDENCE)
    assert checked == "Порог 42 бар [E1]."
    assert len(issues) == 1


@pytest.mark.asyncio
async def test_semantic_review_rejects_uncited_sentence_inside_cited_paragraph_and_heading():
    draft = "## Соответствует стандартам\n\nПорог 42 бар. Соответствует стандартам [E1].\n\n## Проверка\n\nПроверка обязательна [E1]."
    checked, issues = filter_cited_blocks(draft, EVIDENCE)
    # Mechanical checks cannot decide entailment. The semantic layer must.
    assert not issues
    result = await review_report(Reviewer("стандартам"), "Вопрос", checked, EVIDENCE)
    assert not result.ok
    assert "стандартам" not in result.draft
    assert "Проверка обязательна" in result.draft
    assert result.dropped == 2
    assert any("## Соответствует стандартам" in issue for issue in result.issues)
    assert any("Порог 42 бар. Соответствует стандартам [E1]." in issue for issue in result.issues)


@pytest.mark.asyncio
async def test_semantic_review_receives_table_headers_and_rejects_false_conclusion():
    draft = "| Гарантированная безопасность | Значение |\n| --- | --- |\n| Порог | 42 бар [E1] |"
    checked, _ = filter_cited_blocks(draft, EVIDENCE)
    result = await review_report(Reviewer("Гарантированная"), "Вопрос", checked, EVIDENCE)
    assert result.draft == ""
    assert not result.ok


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["OK", "[]", "null", '{"status":"ok"}',
    '{"status":"ok","coherent":true,"feedback":"","blocks":[]}',
    '{"status":"ok","coherent":true,"feedback":"","blocks":[{"id":"B1","supported":false,"feedback":""}]}',
])
async def test_malformed_or_incomplete_verdict_never_publishes(output):
    model = SimpleNamespace(generate=AsyncMock(return_value=output))
    result = await review_report(model, "Вопрос", "Порог 42 бар [E1].", EVIDENCE)
    assert not result.ok
    assert result.draft == ""


@pytest.mark.asyncio
async def test_long_report_and_evidence_are_reviewed_without_truncation():
    paragraphs = [f"Раздел {index}: " + "текст " * 600 + "[E1]." for index in range(8)]
    evidence = [{"marker": "E1", "text": "доказательство " * 2000 + " КОНЕЦ ИСТОЧНИКА"}]
    model = Reviewer()
    draft = "\n\n".join(paragraphs) + "\n\nКОНЕЦ ОТЧЁТА [E1]."
    result = await review_report(model, "Вопрос", draft, evidence)
    assert result.ok
    assert len(model.calls) > 1
    seen = [block["text"] for call in model.calls for block in call["blocks"]]
    assert seen == [block["text"] for block in report_blocks(draft)]
    assert all(call["evidence"][0]["text"].endswith("КОНЕЦ ИСТОЧНИКА") for call in model.calls)
    assert "КОНЕЦ ОТЧЁТА" in result.draft


@pytest.mark.asyncio
async def test_repair_is_rechecked_and_happens_only_once_across_rounds():
    model = Reviewer(rewrite="Порог 99 бар [E1].")
    instance = engine(model)
    result = await instance.review(state("Порог 90 бар [E1]."))
    assert result["editorial_revision_done"]
    assert result["review"] == "insufficient"
    assert result["draft"] == ""
    assert instance.route({**state(""), **result}) == "again"
    await instance.review(state("Порог 90 бар [E1].", editorial_revision_done=True))
    assert sum("blocks" not in call for call in model.calls) == 1


@pytest.mark.asyncio
async def test_editorial_revision_can_recover_coherent_report():
    model = Reviewer(rewrite="## Краткий ответ\n\nПорог 42 бар. Проверка обязательна [E1].")
    instance = engine(model)
    result = await instance.review(state("Порог 99 бар [E1]."))
    assert result["review"] == "ok"
    assert "## Краткий ответ" in result["draft"]
    assert "99" not in result["draft"]
    assert len(model.calls) == 2  # rewrite, then semantic check of the new version


@pytest.mark.asyncio
async def test_persistent_style_failure_publishes_only_verified_fallback():
    model = Reviewer(coherent=False)
    instance = engine(model)
    result = await instance.review(state("Порог 42 бар [E1].", editorial_revision_done=True))
    assert result["review"] == "insufficient"
    assert result["draft"].startswith("## Подтверждённые результаты")
    assert "42 бар [E1]" in result["draft"]
    assert result["needs_more_evidence"] is False
    assert instance.route({**state(""), **result}) == "done"
    assert instance.route({**state(""), **result, "iteration": 3}) == "done"


@pytest.mark.asyncio
async def test_removed_claim_does_not_invalidate_fully_reviewed_remaining_answer():
    model = Reviewer()
    result = await engine(model).review(state(
        "Порог 42 бар [E1].\n\nПорог гарантированно составляет 99 бар.",
        editorial_revision_done=True,
    ))
    assert result["review"] == "ok"
    assert result["needs_more_evidence"] is False
    assert result["dropped_claims"] == 1
    assert "42 бар" in result["draft"] and "99" not in result["draft"]
    assert model.calls[0]["blocks"] == report_blocks("Порог 42 бар [E1].")


@pytest.mark.asyncio
async def test_removed_claim_cannot_override_semantic_request_for_more_evidence():
    model = SimpleNamespace(generate=AsyncMock(return_value=json.dumps({
        "status":"need_more", "coherent":True,
        "feedback":"Не подтверждён ответ на вторую часть вопроса.",
        "blocks":[{"id":"B1", "supported":True, "feedback":""}],
    })))
    result = await engine(model).review(state(
        "Порог 42 бар [E1].\n\nНеподтверждённый вывод 99 бар.",
        editorial_revision_done=True,
    ))
    assert result["review"] == "insufficient"
    assert result["needs_more_evidence"] is True
    assert "99" not in result["draft"]


@pytest.mark.asyncio
async def test_repaired_semantic_hallucination_is_not_published():
    model = Reviewer(reject_text="стандартам", rewrite="Соответствует стандартам [E1].")
    result = await engine(model).review(state("Соответствует стандартам [E1]."))
    assert result["review"] == "insufficient"
    assert result["draft"] == ""
    assert result["editorial_revision_done"]
    assert sum("blocks" in call for call in model.calls) == 2


@pytest.mark.asyncio
async def test_bad_evaluator_after_rewrite_cannot_leak_draft():
    model = SimpleNamespace(generate=AsyncMock(side_effect=[
        "malformed", "Порог 42 бар [E1].", "malformed again",
    ]))
    result = await engine(model).review(state("Порог 42 бар [E1]."))
    assert result["review"] == "insufficient"
    assert result["draft"] == ""
    assert model.generate.await_count == 3


@pytest.mark.asyncio
async def test_no_evidence_does_not_call_model():
    model = Reviewer()
    assert (await engine(model).review(state("Нет материалов", evidence=[])))["review"] == "insufficient"
    assert not model.calls


@pytest.mark.asyncio
async def test_bibliography_groups_versions_and_keeps_used_markers(monkeypatch):
    document = str(uuid4())
    evidence = [dict(EVIDENCE[0], document_id=document, sha256=sha, marker=marker,
                     chunk_id=str(uuid4()), page=page, url="https://example.org/paper")
                for marker, sha, page in [("E1", "aaa", 1), ("E2", "aaa", 2), ("E3", "bbb", 3), ("E4", "aaa", 4)]]

    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(fetchrow=AsyncMock(return_value={"id": uuid4()}))

    instance = engine(Reviewer())
    instance.db = SimpleNamespace(connection=connection)
    monkeypatch.setattr("backend.research.allowed_chunk_ids", AsyncMock(return_value=[row["chunk_id"] for row in evidence]))
    result = await instance.finalize(state("Порог 42 бар [E1] [E2] [E3].", evidence=evidence,
                                           owner_id=str(uuid4()), review="ok"))
    bibliography, appendix = result["report"].split("## Источники")[1].split("## Техническое приложение")
    assert bibliography.count("[Испытания]") == 2
    assert "[E1]" in bibliography and "[E2]" in bibliography
    assert "[E4]" not in bibliography
    assert "SHA-256" not in bibliography
    assert "aaa" in appendix and "bbb" in appendix
