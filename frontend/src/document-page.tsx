import { useEffect, useState } from "react";
import { api } from "./api";
import { message } from "./ui";

type Preview = { id: string; title: string; classification: string; source_url: string | null; media_type: string; sha256: string; pages: { number: number; text: string }[] };

export function DocumentReader({ id }: { id: string }) {
  const [doc, setDoc] = useState<Preview | null>(null);
  const [error, setError] = useState(""); const [fileError, setFileError] = useState("");
  const [pdf, setPdf] = useState(""); const [mode, setMode] = useState("original");
  const [page, setPage] = useState(Math.max(1, Number(new URLSearchParams(location.hash.split("?")[1]).get("page")) || 1));
  const [query, setQuery] = useState("");
  useEffect(() => {
    const controller = new AbortController(); let objectUrl = "";
    api<Preview>(`/documents/${id}/preview`, { signal: controller.signal }).then(async (result) => {
      if (controller.signal.aborted) return;
      setDoc(result); setPage((current) => Math.min(current, result.pages.length || 1));
      if (result.media_type !== "application/pdf") { setMode("text"); return; }
      try {
        const response = await fetch(`/api/v1/documents/${id}/file`, { credentials: "include", signal: controller.signal });
        if (!response.ok) throw new Error("Не удалось открыть PDF. Попробуйте обновить страницу.");
        objectUrl = URL.createObjectURL(new Blob([await response.arrayBuffer()], { type: "application/pdf" }));
        if (controller.signal.aborted) { URL.revokeObjectURL(objectUrl); return; }
        setPdf(objectUrl);
      } catch (cause) { if (!controller.signal.aborted) { setFileError(message(cause)); setMode("text"); } }
    }).catch((cause) => { if (!controller.signal.aborted) setError(message(cause)); });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [id]);
  const matches = doc?.pages.filter((item) => item.text.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) || [];
  const current = doc?.pages.find((item) => item.number === page);
  function highlighted(text: string) {
    const needle = query.trim(); if (!needle) return text;
    const parts = []; let start = 0; let found = text.toLocaleLowerCase().indexOf(needle.toLocaleLowerCase());
    while (found !== -1) { parts.push(text.slice(start, found), <mark key={found}>{text.slice(found, found + needle.length)}</mark>); start = found + needle.length; found = text.toLocaleLowerCase().indexOf(needle.toLocaleLowerCase(), start); }
    parts.push(text.slice(start)); return parts;
  }
  return <main className="page document-reader"><a href="#/library">← Библиотека</a>
    {error ? <p className="error" role="alert">{error}</p> : !doc ? <p role="status">Открываем документ…</p> : <>
      <header className="page-heading"><div><h1>{doc.title}</h1><p className="muted">{doc.classification === "public" ? "Открытый документ" : "Закрытый документ"} · {doc.media_type === "application/pdf" ? "PDF" : "Текст"} · Страниц: {doc.pages.length}</p></div><a className="button-link secondary" href={`/api/v1/documents/${id}/file`}>Скачать оригинал</a></header>
      {doc.source_url && <p><a href={doc.source_url} target="_blank" rel="noopener noreferrer">Документация производителя ↗</a></p>}
      <div className="reader-controls"><div className="header-actions">{doc.media_type === "application/pdf" && <><button className={mode === "original" ? "" : "secondary"} onClick={() => setMode("original")} aria-pressed={mode === "original"}>Оригинал PDF</button><button className={mode === "text" ? "" : "secondary"} onClick={() => setMode("text")} aria-pressed={mode === "text"}>Извлечённый текст</button></>}</div>
        <div className="reader-pagination"><button className="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>←</button><label>Страница<select value={page} onChange={(event) => setPage(Number(event.target.value))}>{doc.pages.map((item) => <option key={item.number} value={item.number}>{item.number}</option>)}</select></label><button className="secondary" disabled={page >= doc.pages.length} onClick={() => setPage(page + 1)}>→</button></div>
        <label>Поиск по тексту<input type="search" value={query} placeholder="Термин или параметр" onChange={(event) => { setQuery(event.target.value); if (event.target.value) setMode("text"); }} /></label>
      </div>
      {query.trim() && <div className="reader-results" aria-live="polite">{matches.length ? <>Найдено на страницах: {matches.map((item) => <button className="secondary" key={item.number} onClick={() => { setPage(item.number); setMode("text"); }}>{item.number}</button>)}</> : "Совпадений в извлечённом тексте нет."}</div>}
      {fileError && <p className="error" role="alert">{fileError}</p>}
      {mode === "original" ? pdf ? <><iframe key={page} className="reader-pdf" src={`${pdf}#page=${page}`} title={`Документ: ${doc.title}, страница ${page}`} /><p className="hint">Если браузер не показывает PDF, откройте «Извлечённый текст».</p></> : <p role="status">Загружаем PDF…</p> : <section className="reader-text" aria-label={`Текст страницы ${page}`}><p className="hint">Извлечённый текст может не сохранять структуру таблиц и схем. Проверяйте их по оригиналу PDF.</p><pre>{current?.text ? highlighted(current.text) : "На этой странице нет извлечённого текста. Откройте оригинал PDF."}</pre></section>}
      <details className="reader-provenance"><summary>Контрольная сумма источника</summary><code>{doc.sha256}</code></details>
    </>}
  </main>;
}
