// Shared state for the console pages: the project choice, cached lists and run actions.
import { api, enc, toast, put, h, line, setupSteps } from "./ui.js";

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

// The scope button in the top bar (the active project kind: My data or Demo data) and the side
// foot's project name. Both open the project menu; a server with one project shows its kind only.
export const KIND_WORD = { demo: "Demo data", real: "My data" };
export function projectInfo() {
  const st = app.start || {};
  const o = app.overview || {};
  const proj = o.project && typeof o.project === "object" ? o.project : null;
  const kind = st.active || (proj ? proj.kind || proj.id : null);
  // the project's own name first (from /start, so it is right before /overview answers), then its kind
  const listed = (st.projects || []).find((p) => p.id === st.active) || null;
  const name = (listed && listed.name) || (proj ? proj.name || proj.id : o.name || o.project) || null;
  return { kind, name, fixed: st.mode === "fixed" };
}
export function drawProjectBadge() {
  const { kind, name, fixed } = projectInfo();
  const scope = document.getElementById("scope");
  const word = KIND_WORD[kind] || name || "Project";
  if (scope) {
    put(scope, line("destinations", 16), h("span", { class: "sw" }, word), fixed ? null : line("chevron", 14));
    scope.classList.toggle("demo", kind === "demo");
    scope.classList.toggle("fixed", fixed);
    scope.setAttribute("aria-label", fixed ? `Data: ${word}` : `Data: ${word}. Switch`);
  }
  const nameEl = document.getElementById("project-name");
  if (nameEl) {
    nameEl.textContent = name || (kind === "demo" ? "Demo project" : kind === "real" ? "My project" : "Project");
    nameEl.setAttribute("aria-label", `Project ${nameEl.textContent}. ${fixed ? "Open settings" : "Switch project"}`);
    if (fixed) nameEl.removeAttribute("aria-haspopup"); else nameEl.setAttribute("aria-haspopup", "menu");
  }
}

// switch between demo and my data; both projects are kept. POST /api/start { choice } makes the
// chosen one active (and creates it the first time).
export async function switchProject(id) {
  const st = await api("/start", { method: "POST", body: { choice: id, run_jobs: true } });
  app.start = st;
  app.connections = null;
  app.overview = null;
  window.dispatchEvent(new CustomEvent("etl:project"));
  drawProjectBadge();
  toast(`Now on ${KIND_WORD[id] || id}`);
  refreshSide();
  return st;
}

// what the project has, for the Getting set up strip: GET /api/setup, else the overview's copy
export async function loadSetup() {
  try { return await api("/setup"); } catch (e) {
    const o = app.overview || await refreshSide();
    return (o && o.setup) || null;
  }
}
export async function stepsStrip() {
  const s = await loadSetup();
  return s ? setupSteps(s) : null;
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
