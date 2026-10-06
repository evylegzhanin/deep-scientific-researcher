import { useEffect, useRef, useState } from "react";
import { ReportContent, ReportOutline } from "./report-content";
import { api, Capabilities, dateTime, params, Research, ResearchEvent, ResearchItem, Source, statusLabels, User } from "./api";
import { Dialog, go, message } from "./ui";
import { useResearchEvents } from "./useResearchEvents";

export function ResearchList({ user, onError }: { user: User; onError: (error: string) => void }) {
  const [rows, setRows] = useState<ResearchItem[]>([]); const [search, setSearch] = useState(""); const [status, setStatus] = useState("");
  const [page, setPage] = useState(0); const [loading, setLoading] = useState(true); const [createOpen, setCreateOpen] = useState(false);
  const request = useRef(0); const limit = 20;
  useEffect(() => { const token = ++request.current; setLoading(true); api<ResearchItem[]>(`/researches${params({ q: search, status_filter: status, offset: page * limit, limit })}`).then((result) => { if (token === request.current) setRows(result); }).catch((cause) => { if (token === request.current) onError(message(cause)); }).finally(() => { if (token === request.current) setLoading(false); }); }, [search, status, page]);
  return <main className="page"><header className="page-heading"><div><h1>Исследования</h1><p className="muted">Ваши вопросы, ход работы и отчёты</p></div>{user.role !== "reader" && <button onClick={() => setCreateOpen(true)}>Новое исследование</button>}</header>
    <div className="toolbar"><label>Поиск<input type="search" placeholder="Название исследования" value={search} onChange={(event) => { setSearch(event.target.value); setPage(0); }} /></label><label>Статус<select value={status} onChange={(event) => { setStatus(event.target.value); setPage(0); }}><option value="">Все</option>{Object.entries(statusLabels).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label></div>
    {loading ? <p className="muted" role="status">Загружаем исследования…</p> : rows.length ? <div className="research-grid">{rows.map((row) => <a className="research-card" href={`#/research/${row.id}/overview`} key={row.id}><strong>{row.title}</strong><span className={`badge ${row.status}`}>{statusLabels[row.status] || row.status}</span><small>{row.classification === "restricted" ? "Закрытые данные" : "Публичные данные"} · {dateTime(row.updated_at)}</small></a>)}</div> : <div className="empty"><h2>{search || status ? "Ничего не найдено" : "Исследований пока нет"}</h2><p>Задайте вопрос, чтобы начать поиск и подготовить отчёт.</p></div>}
    <div className="pagination"><button className="secondary" disabled={page === 0 || loading} onClick={() => setPage(page - 1)}>Назад</button><span>Страница {page + 1}</span><button className="secondary" disabled={rows.length < limit || loading} onClick={() => setPage(page + 1)}>Далее</button></div>
    {createOpen && <CreateResearch onClose={() => setCreateOpen(false)} onError={onError} />}
  </main>;
}

function CreateResearch({ onClose, onError }: { onClose: () => void; onError: (value: string) => void }) {
  const [title, setTitle] = useState(""); const [question, setQuestion] = useState(""); const [classification, setClassification] = useState("public");
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null); const [busy, setBusy] = useState(false);
  useEffect(() => { api<Capabilities>("/capabilities").then(setCapabilities).catch((cause) => onError(message(cause))); }, []);
  return <Dialog title="Новое исследование" onClose={onClose}><form className="form" onSubmit={async (event) => { event.preventDefault(); setBusy(true); try { const result = await api<{ id: string }>("/researches", { method: "POST", body: JSON.stringify({ title, question, classification }) }); onClose(); go(`research/${result.id}/overview`); } catch (cause) { onError(message(cause)); } finally { setBusy(false); } }}>
    <label>Название<input value={title} onChange={(event) => setTitle(event.target.value)} minLength={3} maxLength={200} required placeholder="Например, безопасность LLM" /></label>
    <label>Что нужно выяснить?<textarea value={question} onChange={(event) => setQuestion(event.target.value)} minLength={10} maxLength={4000} required placeholder="Сформулируйте исследовательский вопрос" /></label>
    <label>Режим данных<select value={classification} onChange={(event) => setClassification(event.target.value)}><option value="public">Публичные данные</option><option value="restricted" disabled={!capabilities?.restricted_research}>Закрытые данные</option></select></label>
    <p className="hint">{classification === "restricted" ? "Используются доступные вам документы и локальная модель. Интернет-поиск отключён." : capabilities?.internet_search ? "Приложение ищет открытые публикации. Если модель внешняя, вопрос отправляется ей без закрытых документов." : "Интернет-поиск отключён. Используются доступные источники."}</p>
    {capabilities && !capabilities.restricted_research && <p className="hint">Закрытые исследования станут доступны после настройки локальной модели.</p>}
    <div className="form-actions"><button type="button" className="secondary" onClick={onClose}>Отмена</button><button disabled={busy || !capabilities}>{busy ? "Создаём…" : "Начать исследование"}</button></div>
  </form></Dialog>;
}

function eventText(event: ResearchEvent) {
  const payload = typeof event.payload === "string" ? JSON.parse(event.payload) : event.payload;
  if (event.kind === "phase") return String(payload.name || "Этап исследования") + (payload.round ? ` · проход ${payload.round}` : "");
  if (event.kind === "sources") return `Найдено источников: ${payload.found ?? 0}`;
  if (event.kind === "evidence") return `Найдено подтверждающих фрагментов: ${payload.count ?? 0}`;
  if (event.kind === "clarification") return "Ожидается ответ на уточнение";
  if (event.kind === "completed") return "Исследование завершено";
  if (event.kind === "failed") return "Ошибка исследования";
  if (event.kind === "retry") return "Повторная попытка запланирована";
  return "Событие исследования";
}

export function ResearchDetail({ id, tab, user, onError }: { id: string; tab: string; user: User; onError: (value: string) => void }) {
  const [research, setResearch] = useState<Research | null>(null); const [sources, setSources] = useState<Source[]>([]);
  const [busy, setBusy] = useState(false); const [cancelOpen, setCancelOpen] = useState(false); const [followupOpen, setFollowupOpen] = useState(false);
  const [followup, setFollowup] = useState(""); const [loading, setLoading] = useState(true);
  const load = async () => { const [detail, sourceRows] = await Promise.all([api<Research>(`/researches/${id}`), api<Source[]>(`/researches/${id}/sources`)]); setResearch(detail); setSources(sourceRows); };
  useEffect(() => { setLoading(true); load().catch((cause) => onError(message(cause))).finally(() => setLoading(false)); }, [id]);
  useEffect(() => { if (!research || !["queued", "running"].includes(research.status)) return; const timer = window.setInterval(() => load().catch((cause) => onError(message(cause))), 5000); return () => window.clearInterval(timer); }, [id, research?.status]);
  const { events, connection } = useResearchEvents(id, !research || ["queued", "running"].includes(research.status), () => load().catch((cause) => onError(message(cause))));
  useEffect(() => {
    if (tab !== "sources") return;
    const marker = new URLSearchParams(location.hash.split("?")[1] || "").get("marker");
    if (marker) window.setTimeout(() => document.getElementById(marker)?.scrollIntoView({ block: "center" }), 50);
  }, [tab, sources.length]);
  if (loading) return <main className="page"><p role="status">Загружаем исследование…</p></main>;
  if (!research) return <main className="page"><div className="empty"><h1>Исследование недоступно</h1><a href="#/researches">Вернуться к списку</a></div></main>;
  const working = ["queued", "running"].includes(research.status);
  const canContinue = user.role !== "reader" && ["completed", "failed", "cancelled", "awaiting_clarification"].includes(research.status);
  const uniqueSources = [...new Set(sources.map((source) => source.document_id))];
  return <main className="page detail-page"><a href="#/researches" className="back-link">← Исследования</a><header className="page-heading"><div><h1>{research.title}</h1><div className="meta-row"><span className={`badge ${research.status}`}>{statusLabels[research.status] || research.status}</span><span>{research.classification === "restricted" ? "Закрытые данные" : "Публичные данные"}</span>{research.report && <span>Отчёт · версия {research.report.version}</span>}</div></div><div className="header-actions">{working && <button className="secondary" onClick={() => setCancelOpen(true)}>Отменить</button>}{canContinue && <button onClick={() => setFollowupOpen(true)}>{research.status === "awaiting_clarification" ? "Ответить" : "Продолжить исследование"}</button>}</div></header>
    {research.report?.review_status === "insufficient" && <div className="notice" role="status"><strong>Отчёт с ограничениями</strong><span>Полноценный ответ не прошёл все проверки. Доступны только подтверждённые результаты.</span></div>}
    {working && <div className="notice" role="status"><strong>Текущий этап:</strong><span>{events.length ? eventText(events.at(-1)!) : "Ожидание первого события"}</span></div>}
    {research.report && (research.report.dropped_claims ?? 0) > 0 && <p className="muted">Проверка исключила неподтверждённых формулировок: {research.report.dropped_claims}.</p>}
    {research.report_unavailable_reason === "access_changed" && <div className="notice" role="alert">Отчёт недоступен: изменился доступ к его источникам.</div>}
    {research.error && <div className="error" role="alert">{research.error}</div>}
    <nav className="tabs" aria-label="Разделы исследования"><a href={`#/research/${id}/overview`} aria-current={tab === "overview" ? "page" : undefined}>Обзор</a><a href={`#/research/${id}/report`} aria-current={tab === "report" ? "page" : undefined}>Отчёт</a><a href={`#/research/${id}/sources`} aria-current={tab === "sources" ? "page" : undefined}>Источники ({uniqueSources.length})</a></nav>
    {tab === "overview" && <div className="detail-columns"><div><section className="panel"><h2>Задача</h2><p>{research.question}</p></section>
      {research.status === "awaiting_clarification" && <section className="panel highlight"><h2>Нужен ваш ответ</h2><p>{research.messages.filter((item) => item.author === "assistant").at(-1)?.body}</p>{canContinue && <button onClick={() => setFollowupOpen(true)}>Ответить на уточнение</button>}</section>}
      <section className="panel"><h2>Диалог</h2>{research.messages.length ? <div className="conversation">{research.messages.map((item, index) => <div className="message" key={index}><strong>{item.author === "user" ? "Вы" : "Исследователь"}</strong><small>{dateTime(item.created_at)}</small><p>{item.body}</p></div>)}</div> : <p className="muted">Сообщений пока нет.</p>}</section>
      {research.report && <a className="callout-link" href={`#/research/${id}/report`}>Читать отчёт →</a>}</div>
      <aside className="panel timeline"><h2>Ход работы</h2>{working && connection === "reconnecting" && <p className="notice">Соединение прервано. Восстанавливаем обновления…</p>}{events.length ? <><p className="muted">Последнее событие: {dateTime(events.at(-1)!.created_at)}</p><ol>{events.map((event) => <li key={event.id}><strong>{eventText(event)}</strong><small>{dateTime(event.created_at)}</small></li>)}</ol></> : <p className="muted">{working ? "Ожидаем первый этап…" : "История этапов недоступна."}</p>}</aside></div>}
    {tab === "report" && (research.report ? <div className="report-layout"><div><div className="download-bar"><a className="secondary button-link" href={`/api/v1/researches/${id}/report.md`}>Скачать Markdown</a><a className="secondary button-link" href={`/api/v1/researches/${id}/report.pdf`}>Скачать PDF</a></div><article className="panel report"><ReportContent body={research.report.body} researchId={id} /></article></div><ReportOutline body={research.report.body} /></div> : <div className="empty"><h2>Отчёта пока нет</h2><p>{working ? "Исследование выполняется." : "Доступной версии отчёта нет."}</p></div>)}
    {tab === "sources" && <section className="panel"><h2>Использованные источники</h2><p className="muted">Это фрагменты, на которые опирался отчёт. Все документы доступны в библиотеке.</p>{sources.length ? <div className="sources-list">{uniqueSources.map((documentId) => { const entries = sources.filter((source) => source.document_id === documentId); return <div className="source-group" key={documentId}><h3>{entries[0].title}</h3>{entries.map((source) => <details key={source.marker} id={source.marker} open={new URLSearchParams(location.hash.split("?")[1] || "").get("marker") === source.marker}><summary>[{source.marker}] {source.page ? `Страница ${source.page}` : "Фрагмент"}</summary><blockquote>{source.excerpt}</blockquote><p><a href={`#/document/${source.document_id}?page=${source.page || 1}`}>Открыть документ</a>{source.source_url && <> · <a href={source.source_url} target="_blank" rel="noopener noreferrer">Исходная публикация</a></>}</p><small>SHA-256: {source.sha256}</small></details>)}</div>; })}</div> : <p className="muted">Подтверждающих фрагментов нет или доступ к ним изменился.</p>}</section>}
    {cancelOpen && <Dialog title="Отменить исследование?" onClose={() => setCancelOpen(false)}><p>Исследование «{research.title}» будет остановлено.</p><div className="form-actions"><button className="secondary" onClick={() => setCancelOpen(false)}>Оставить</button><button className="danger" disabled={busy} onClick={async () => { setBusy(true); try { await api(`/researches/${id}/cancel`, { method: "POST" }); await load(); setCancelOpen(false); } catch (cause) { onError(message(cause)); } finally { setBusy(false); } }}>Отменить исследование</button></div></Dialog>}
    {followupOpen && <Dialog title={research.status === "awaiting_clarification" ? "Ответить на уточнение" : "Продолжить исследование"} onClose={() => setFollowupOpen(false)}><form className="form" onSubmit={async (event) => { event.preventDefault(); setBusy(true); try { await api(`/researches/${id}/messages`, { method: "POST", body: JSON.stringify({ message: followup }) }); setFollowup(""); setFollowupOpen(false); await load(); go(`research/${id}/overview`); } catch (cause) { onError(message(cause)); } finally { setBusy(false); } }}><p className="hint">{research.status === "awaiting_clarification" ? research.messages.filter((item) => item.author === "assistant").at(-1)?.body : "Добавьте вопрос или уточнение. Начнётся новый проход исследования."}</p>{research.report && <p className="hint">Текущая версия отчёта: {research.report.version}.</p>}<label>Ваше сообщение<textarea value={followup} minLength={3} required onChange={(event) => setFollowup(event.target.value)} /></label><div className="form-actions"><button type="button" className="secondary" onClick={() => setFollowupOpen(false)}>Отмена</button><button disabled={busy}>{busy ? "Отправляем…" : "Отправить и продолжить"}</button></div></form></Dialog>}
  </main>;
}

