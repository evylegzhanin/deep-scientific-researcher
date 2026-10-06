import { useEffect, useState } from "react";
import { api, ResearchEvent } from "./api";

export function useResearchEvents(researchId: string | null, active: boolean, onChange: () => void) {
  const [events, setEvents] = useState<ResearchEvent[]>([]);
  const [connection, setConnection] = useState<"connected" | "reconnecting">("connected");
  useEffect(() => {
    setEvents([]);
    setConnection("connected");
    if (!researchId) return;
    let cancelled = false;
    let source: EventSource | null = null;
    const add = (event: ResearchEvent) => setEvents((current) => current.some((item) => item.id === event.id) ? current : [...current, event].sort((a, b) => a.id - b.id));
    const openStream = (after: number) => {
      if (cancelled || !active) return;
      source = new EventSource(`/api/v1/researches/${researchId}/events?after=${after}`, { withCredentials: true });
      source.onopen = () => setConnection("connected");
      source.onerror = () => setConnection("reconnecting");
      ["phase", "sources", "evidence", "clarification", "completed", "failed", "retry"].forEach((kind) => {
        source?.addEventListener(kind, (message) => {
          const event = message as MessageEvent;
          let payload = JSON.parse(event.data);
          if (typeof payload === "string") payload = JSON.parse(payload);
          const id = Number(event.lastEventId);
          if (Number.isFinite(id)) add({ id, kind, payload, created_at: new Date().toISOString() });
          if (["clarification", "completed", "failed", "retry"].includes(kind)) onChange();
        });
      });
    };
    api<ResearchEvent[]>(`/researches/${researchId}/event-history?limit=200`).then((history) => {
      if (cancelled) return;
      setEvents(history);
      openStream(history.at(-1)?.id ?? 0);
    }).catch(() => { if (!cancelled) setConnection("reconnecting"); });
    return () => { cancelled = true; source?.close(); };
  }, [researchId, active]);
  return { events, connection };
}
