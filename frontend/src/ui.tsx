import React, { useEffect, useRef } from "react";

export function go(path: string) { location.hash = `#/${path}`; }
export function message(error: unknown) { return error instanceof Error ? error.message : "Неизвестная ошибка"; }

export function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const dialog = useRef<HTMLDivElement>(null);
  const previous = useRef<HTMLElement | null>(null);
  useEffect(() => {
    previous.current = document.activeElement as HTMLElement;
    dialog.current?.querySelector<HTMLElement>("input,textarea,select,button")?.focus();
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("keydown", escape); previous.current?.focus(); };
  }, []);
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="dialog" role="dialog" aria-modal="true" aria-label={title} ref={dialog} onKeyDown={(event) => {
      if (event.key !== "Tab") return;
      const focusable = [...dialog.current!.querySelectorAll<HTMLElement>("button,input,select,textarea,a[href]")].filter((item) => !item.hasAttribute("disabled"));
      if (!focusable.length) return;
      if (event.shiftKey && document.activeElement === focusable[0]) { event.preventDefault(); focusable.at(-1)?.focus(); }
      if (!event.shiftKey && document.activeElement === focusable.at(-1)) { event.preventDefault(); focusable[0].focus(); }
    }}><header className="dialog-heading"><h2>{title}</h2><button type="button" className="quiet" onClick={onClose} aria-label="Закрыть">×</button></header>{children}</div>
  </div>;
}

