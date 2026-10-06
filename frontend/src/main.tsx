import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import "katex/dist/katex.min.css";
import "./style.css";
import { api, systemRoles, User } from "./api";
import { go, message } from "./ui";
import { ResearchList, ResearchDetail } from "./research-page";
import { DocumentReader } from "./document-page";
import { Library } from "./library-page";
import { Access } from "./access-page";

type Route = { page: "researches" | "research" | "library" | "access" | "document"; id?: string; tab?: string };
function routeFromHash(): Route {
  const [page, id, tab] = location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
  if (page === "document" && id) return { page, id };
  if (page === "research" && id) return { page, id, tab: tab || "overview" };
  if (page === "library" || page === "access") return { page };
  return { page: "researches" };
}

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [email, setEmail] = useState(""); const [password, setPassword] = useState("");
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  return <main className="login"><h1>Исследователь</h1><p className="muted">Техническая литература с проверяемыми источниками</p>
    <form onSubmit={async (event) => { event.preventDefault(); setBusy(true); setError(""); try { onLogin(await api<User>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) })); setPassword(""); } catch (cause) { setError(message(cause)); } finally { setBusy(false); } }}>
      <label>Почта<input type="email" name="username" autoComplete="username" value={email} onChange={(event) => setEmail(event.target.value)} required /></label>
      <label>Пароль<input type="password" name="current-password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
      {error && <p className="error" role="alert">{error}</p>}<button disabled={busy}>{busy ? "Входим…" : "Войти"}</button>
    </form></main>;
}

function App() {
  const [user, setUser] = useState<User | null>(null); const [checking, setChecking] = useState(true);
  const [route, setRoute] = useState<Route>(routeFromHash); const [mobileNav, setMobileNav] = useState(false);
  const [alert, setAlert] = useState("");
  useEffect(() => { api<User>("/auth/me").then(setUser).catch(() => {}).finally(() => setChecking(false)); }, []);
  useEffect(() => { const change = () => { setRoute(routeFromHash()); setMobileNav(false); window.scrollTo(0, 0); }; window.addEventListener("hashchange", change); return () => window.removeEventListener("hashchange", change); }, []);
  if (checking) return <main className="loading-screen">Загрузка…</main>;
  if (!user) return <Login onLogin={setUser} />;
  return <div className="shell">
    <aside className={`sidebar ${mobileNav ? "open" : ""}`}>
      <div className="brand">Исследователь <button className="quiet mobile-only" onClick={() => setMobileNav(false)} aria-label="Закрыть меню">×</button></div>
      <nav aria-label="Главная навигация">
        <a href="#/researches" aria-current={["researches", "research"].includes(route.page) ? "page" : undefined}>Исследования</a>
        <a href="#/library" aria-current={["library", "document"].includes(route.page) ? "page" : undefined}>Библиотека</a>
        {user.role === "admin" && <a href="#/access" aria-current={route.page === "access" ? "page" : undefined}>Управление доступом</a>}
      </nav>
      <div className="profile"><strong>{user.email}</strong><small>{systemRoles[user.role]}</small><button className="quiet" onClick={async () => { try { await api("/auth/logout", { method: "POST" }); setUser(null); go("researches"); } catch (cause) { setAlert(message(cause)); } }}>Выйти</button></div>
    </aside>
    <div className="workspace"><div className="mobile-bar"><button className="quiet" onClick={() => setMobileNav(true)} aria-label="Открыть меню">☰</button><strong>Исследователь</strong></div>
      {alert && <p className="error global-error" role="alert">{alert}<button className="quiet" onClick={() => setAlert("")} aria-label="Закрыть">×</button></p>}
      {route.page === "researches" && <ResearchList user={user} onError={setAlert} />}
      {route.page === "research" && route.id && <ResearchDetail key={route.id} id={route.id} tab={route.tab || "overview"} user={user} onError={setAlert} />}
      {route.page === "document" && route.id && <DocumentReader key={route.id} id={route.id} />}
      {route.page === "library" && <Library user={user} onError={setAlert} />}
      {route.page === "access" && user.role === "admin" && <Access onError={setAlert} />}
    </div>
  </div>;
}

ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
