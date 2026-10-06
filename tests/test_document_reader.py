from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from reportlab.pdfgen.canvas import Canvas

import backend.document_reader as reader


def test_txt_preview_preserves_unicode_and_order(tmp_path):
    path = tmp_path / 'text'
    body = 'ПД150: настройка\n' + 'Диапазон и пределы\n' * 300
    path.write_text(body)
    assert reader.preview_pages(path, 'text/plain', []) == [{'number': 1, 'text': body}]


def test_pdf_preview_keeps_page_numbers(tmp_path):
    path = tmp_path / 'pdf'
    pdf = Canvas(str(path))
    for text in ['First page', 'Second page']:
        pdf.drawString(50, 700, text)
        pdf.showPage()
    pdf.save()
    pages = reader.preview_pages(path, 'application/pdf', [])
    assert [p['number'] for p in pages] == [1, 2]
    assert 'First page' in pages[0]['text'] and 'Second page' in pages[1]['text']


def test_html_preview_uses_extracted_text_only(tmp_path):
    path = tmp_path / 'html'
    path.write_text('<script>alert(1)</script><p>Manual</p>')
    pages = reader.preview_pages(path, 'text/html', [{'kind': 'text', 'body': 'Manual'}])
    assert pages == [{'number': 1, 'text': 'Manual'}]


@pytest.mark.asyncio
async def test_reader_denies_domain_and_partial_chunk_access(tmp_path, monkeypatch):
    document_id, chunk_id = uuid4(), uuid4()
    path = tmp_path / 'blob'
    path.write_text('Restricted')
    row = {'version_id': uuid4(), 'blob_path': str(path)}
    class Connection:
        async def fetchrow(self, *args): return row
        async def fetch(self, *args): return [{'id': chunk_id}]
    class Database:
        @asynccontextmanager
        async def connection(self): yield Connection()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=Database(), settings=SimpleNamespace(file_root=tmp_path))))
    async def documents(*args): return []
    async def chunks(*args): return []
    monkeypatch.setattr(reader, 'allowed_document_ids', documents)
    monkeypatch.setattr(reader, 'allowed_chunk_ids', chunks)
    with pytest.raises(HTTPException) as denied:
        await reader.readable_document(document_id, request, {'id': uuid4()})
    assert denied.value.status_code == 404
    async def documents(*args): return [document_id]
    monkeypatch.setattr(reader, 'allowed_document_ids', documents)
    with pytest.raises(HTTPException) as partial:
        await reader.readable_document(document_id, request, {'id': uuid4()})
    assert partial.value.status_code == 403
    async def chunks(*args): return [chunk_id]
    monkeypatch.setattr(reader, 'allowed_chunk_ids', chunks)
    assert (await reader.readable_document(document_id, request, {'id': uuid4()}))[2] == path
    outside = tmp_path / 'nested'
    outside.mkdir()
    other = outside / 'blob'
    other.write_text('Outside root')
    row['blob_path'] = str(other)
    with pytest.raises(HTTPException) as missing:
        await reader.readable_document(document_id, request, {'id': uuid4()})
    assert missing.value.status_code == 404
