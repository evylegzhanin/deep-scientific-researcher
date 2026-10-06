from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pypdf import PdfReader

from backend import api
from backend.report_pdf import MAX_REPORT_CHARS, render_report_pdf


def test_pdf_keeps_russian_text_sources_and_math() -> None:
    data = render_report_pdf(
        "Расчёт давления",
        "# Результат\nПо формуле $E=mc^2$ получено значение [E1].\n\n"
        "$$\n\\frac{a^2+b^2}{c^2}=1\n$$\n\n"
        "[Источник](https://example.org/paper).",
    )
    assert data.startswith(b"%PDF-")
    reader = PdfReader(BytesIO(data))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "Расчёт давления" in text
    assert "Результат" in text
    assert "[E1]" in text
    assert "Источник" in text
    assert any("/XObject" in page["/Resources"] for page in reader.pages)


def test_pdf_ignores_html_remote_images_and_unsupported_tex() -> None:
    data = render_report_pdf(
        "Безопасный отчёт",
        '<img src="https://private.example/collect">\n\n'
        '![картинка](https://private.example/image.png)\n\n'
        '$$\\input{private-file}$$\n\nТекст после формулы.',
    )
    page = PdfReader(BytesIO(data)).pages[0]
    text = page.extract_text()
    assert "картинка" in text
    assert "Текст после формулы" in text
    assert "\\input" in text
    assert "/XObject" not in page["/Resources"]


def test_pdf_size_limit() -> None:
    with pytest.raises(ValueError, match="слишком велик"):
        render_report_pdf("Тема", "a" * (MAX_REPORT_CHARS + 1))


def test_pdf_table_wraps_and_repeats_header_across_pages() -> None:
    markdown = "## Сравнение\n\n| Критерий | Результат |\n| --- | --- |\n"
    markdown += "\n".join(
        f"| Испытание {index} | Подробное описание условий испытания и ограничений метода. "
        f"Значение $E=mc^2$ проверяется по источнику [E1]. |" for index in range(45)
    )
    reader = PdfReader(BytesIO(render_report_pdf("Сравнительный анализ", markdown)))
    assert len(reader.pages) >= 2
    for page in reader.pages:
        text = page.extract_text()
        assert "Критерий" in text and "Результат" in text
    assert "Испытание 44" in reader.pages[-1].extract_text()


def test_pdf_splits_a_single_long_table_row() -> None:
    markdown = "| Критерий | Результат |\n| --- | --- |\n| Проверка | "
    markdown += "Длинное описание ограничений метода. " * 450 + "Конец строки [E1]. |"
    reader = PdfReader(BytesIO(render_report_pdf("Длинная строка", markdown)))
    assert len(reader.pages) > 1
    assert "Конец строки" in reader.pages[-1].extract_text()


@pytest.mark.asyncio
async def test_pdf_endpoint_rechecks_report_access(monkeypatch) -> None:
    research_id = uuid4()
    request = SimpleNamespace()
    user = {"id": uuid4(), "role": "reader"}

    async def permitted(*_args):
        return {"title": "Проверка", "report": {"version": 1, "body": "Текст [E1]."}}

    monkeypatch.setattr(api, "get_research", permitted)
    response = await api.download_report_pdf(research_id, request, user)
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF-")
    assert response.headers["cache-control"] == "no-store"

    async def revoked(*_args):
        return {"title": "Проверка", "report": None}

    monkeypatch.setattr(api, "get_research", revoked)
    with pytest.raises(HTTPException) as denied:
        await api.download_report_pdf(research_id, request, user)
    assert denied.value.status_code == 404

    calls = 0

    async def revoked_during_export(*_args):
        nonlocal calls
        calls += 1
        return await (permitted(*_args) if calls == 1 else revoked(*_args))

    monkeypatch.setattr(api, "get_research", revoked_during_export)
    with pytest.raises(HTTPException) as denied_during_export:
        await api.download_report_pdf(research_id, request, user)
    assert denied_during_export.value.status_code == 404
