export type User = { id: string; email: string; role: "admin" | "researcher" | "reader" };
export type ResearchItem = { id: string; title: string; status: string; classification: string; updated_at: string };
export type Research = ResearchItem & {
  question: string; error: string | null; report_unavailable_reason: string | null;
  messages: { author: string; body: string; created_at: string }[];
  report: { version: number; body: string; review_status: string | null; dropped_claims: number | null } | null;
};
export type ResearchEvent = { id: number; kind: string; payload: Record<string, unknown>; created_at: string };
export type Source = { marker: string; document_id: string; title: string; page: number | null; excerpt: string; source_url: string | null; sha256: string };
export type DocumentItem = { id: string; owner_id: string | null; domain_id: string | null; domain_title: string | null; domain_code: string | null; title: string; classification: string; source_url: string | null; created_at: string; can_analyze: boolean; can_export: boolean };
export type Domain = { id: string; code: string; title: string; classification: string; role: string | null };
export type Member = { id: string; email: string; role: string };
export type Capabilities = { app_mode: string; local_model: boolean; internet_search: boolean; restricted_research: boolean; max_document_bytes: number };

export function csrf(): string {
  return document.cookie.split("; ").find((item) => item.startsWith("research_csrf="))?.split("=")[1] ?? "";
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData) && init.body) headers.set("Content-Type", "application/json");
  if (init.method && init.method !== "GET") headers.set("X-CSRF-Token", csrf());
  const response = await fetch(`/api/v1${path}`, { ...init, headers, credentials: "include" });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.detail === "string" ? payload.detail : `Ошибка ${response.status}`);
  }
  return response.json();
}

export function params(values: Record<string, string | number | undefined | null>): string {
  const query = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => { if (value !== undefined && value !== null && value !== "") query.set(key, String(value)); });
  return query.size ? `?${query}` : "";
}

export const statusLabels: Record<string, string> = {
  queued: "В очереди", running: "В работе", awaiting_clarification: "Ждёт ответа",
  completed: "Завершено", failed: "Ошибка", cancelled: "Отменено",
};

export const systemRoles: Record<string, string> = { admin: "Администратор", researcher: "Исследователь", reader: "Читатель" };
export const domainRoles: Record<string, string> = { manager: "Руководитель пространства", researcher: "Исследователь", reader: "Читатель" };
export const dateTime = (value: string) => new Date(value).toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" });

export async function downloadPackage(documentId: string): Promise<void> {
  const response = await fetch("/api/v1/packages/export", {
    method: "POST", credentials: "include",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
    body: JSON.stringify({ document_ids: [documentId] }),
  });
  if (!response.ok) throw new Error("Не удалось экспортировать пакет");
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a"); link.href = url; link.download = "research-sources.zip"; link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
