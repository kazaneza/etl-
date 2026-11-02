// Shared state for the console pages: the project choice, cached lists and run actions.
import { api, enc, toast, put, h, icon } from "./ui.js";

export const app = { start: null, overview: null, connections: null, kinds: null, sideTimer: null };

// re-render the current page (keep: no skeleton, keep the scroll position)
export function reroute(keep = true) { window.dispatchEvent(new CustomEvent("etl:reroute", { detail: { keep } })); }

export async function loadStart() {
  try {
    app.start = await api("/start");
  } catch (e) {
    // a server without the start routes serves one project
    if (e.status === 404) app.start = { state: "ready", mode: "fixed", active: null, projects: [] };
    else throw e;
  }
  return app.start;
}

export async function loadConnections(force = false) {
  if (!app.connections || force) app.connections = await api("/connections");
  return app.connections;
}

// GET /api/kinds; when the route is missing, a minimal table for the v1 kinds
const FALLBACK_KINDS = [
  ["postgres", "PostgreSQL", "postgres", "database", 5432], ["sqlserver", "SQL Server", "sqlserver", "database", 1433],
  ["mysql", "MySQL", "mysql", "database", 3306], ["oracle", "Oracle", "oracle", "database", 1521],
  ["sqlite", "SQLite", "sqlite", "database", null], ["duckdb", "DuckDB", "duckdb", "database", null], ["folder", "Folder on this machine", "folder", "folder", null],
].map(([kind, label, family, category, port]) => ({
  kind, label, family, category, default_port: port, roles: ["source", "target"], status: "real", installed: true, fallback: true,
  fields: port ? [
    { name: "host", label: "Host", type: "text", required: true, endpoint: true },
    { name: "port", label: "Port", type: "int", default: port, endpoint: true },
    { name: "database", label: kind === "oracle" ? "Service name" : "Database", type: "text" },
    { name: "user", label: "User", type: "text", endpoint: true },
    { name: "password", label: "Password", type: "password", secret: true },
  ] : [{ name: "path", label: category === "folder" ? "Folder" : "File", type: "path", required: true, help: "Relative to the project folder." }],
}));

export async function loadKinds() {
  if (app.kinds) return app.kinds;
  try { app.kinds = await api("/kinds"); } catch (e) { app.kinds = FALLBACK_KINDS; }
  return app.kinds;
}
export const kindOf = (k) => (app.kinds || FALLBACK_KINDS).find((x) => x.kind === k) || null;
export const kindLabel = (k) => (kindOf(k) || { label: k }).label;
export const isFileKind = (k) => { const d = kindOf(k); return !!d && (d.category === "folder" || d.category === "remote"); };

export async function refreshSide() {
  try {
    const o = await api("/overview");
    app.overview = o;
    drawProjectBadge();
    const box = document.getElementById("running");
    const n = (o.counts && o.counts.running) || 0;
    if (n) {
      put(box, h("a", { class: "job", href: "#/runs" }, h("span", { class: "spin", "aria-hidden": "true" }), h("span", {}, `${n} running`)));
      if (!app.sideTimer) app.sideTimer = setInterval(refreshSide, 3000);
    } else {
      put(box);
      if (app.sideTimer) { clearInterval(app.sideTimer); app.sideTimer = null; }
    }
    return o;
  } catch (e) { return null; }
}

// the top bar tag (Demo data in gold, My data in teal) and the side foot's project line
export function drawProjectBadge() {
  const el = document.getElementById("project-badge");
  if (!el) return;
  const st = app.start || {};
  const o = app.overview || {};
  const proj = o.project && typeof o.project === "object" ? o.project : null;
  const kind = st.active || (proj ? proj.kind || proj.id : null);
  const name = proj ? proj.name || proj.label || proj.id : o.name || o.project;
  const nameEl = document.getElementById("project-name");
  const subEl = document.getElementById("project-sub");
  if (kind === "demo" || kind === "real") {
    const word = kind === "demo" ? "Demo data" : "My data";
    el.className = `badge proj ${kind === "demo" ? "warn" : "info"}`;
    put(el, h("span", { class: "proj-text" }, word));
    el.setAttribute("aria-label", `Project: ${word}. Open settings`);
    if (nameEl) nameEl.textContent = name || word;
    if (subEl) subEl.textContent = word;
  } else {
    el.className = "badge proj";
    put(el, icon("folder-simple"), h("span", { class: "proj-text" }, name || "Project"));
    el.setAttribute("aria-label", `Project ${name || ""}`);
    if (nameEl) nameEl.textContent = name || "Project";
    if (subEl) subEl.textContent = "One project";
  }
}

export async function startRun(name, steps, extra = {}) {
  try {
    const r = await api(`/jobs/${enc(name)}/run`, { method: "POST", body: steps ? { steps, ...extra } : { ...extra } });
    toast(`${name} started`);
    refreshSide();
    return r;
  } catch (e) { toast(e.message, true); return null; }
}

// a job's target, whichever form it has (v2 destination or v1 inline)
export function targetText(j) {
  const t = j.target || {};
  if (t.destination) return t.destination;
  return `${t.connection || "?"}: ${t.table || t.file || "?"}`;
}
export function sourceText(j) {
  const sx = j.source || {};
  const what = sx.table || (Array.isArray(sx.files) ? sx.files.join(", ") : sx.files) || (sx.query ? "query" : "?");
  return `${sx.connection || "?"}: ${what}`;
}
