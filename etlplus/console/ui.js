// etl+ console helpers and components. Data is always set as text (textContent or h()),
// never as markup. Every request goes through api().

// ------------------------------------------------------------------ elements
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  addKids(el, kids);
  return el;
}

// the SVG namespace, read from the HTML parser (no URL written in the console)
let SVGNS = null;
export function s(tag, attrs, ...kids) {
  if (!SVGNS) SVGNS = new DOMParser().parseFromString("<svg></svg>", "text/html").querySelector("svg").namespaceURI;
  const el = document.createElementNS(SVGNS, tag);
  setAttrs(el, attrs);
  addKids(el, kids);
  return el;
}

function setAttrs(el, attrs) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
}
function addKids(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
}

// replaceChildren turns null into the text "null": drop empties, flatten nested lists
export function put(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false && k !== ""));
}
// Phosphor, except where the design has its own line icon for the same symbol: then that icon,
// sized 1em inside the same <i>, so every rule that sizes an icon still applies
export function icon(name, cls = "") {
  const same = SAME[name];
  if (same) return h("i", { class: `lic ${cls}`.trim(), "aria-hidden": "true" }, line(same, "1em"));
  return h("i", { class: `ph ph-${name} ${cls}`.trim(), "aria-hidden": "true" });
}

// The design's own line icons (24 grid, stroke 1.6), copied from the owner's mockup. They stand in
// for Phosphor wherever the same symbol appears: the nav pages, search, plus, chevron, sun and moon.
const LINES = {
  overview: [["rect", { x: 4, y: 4, width: 7, height: 7, rx: 1.5 }], ["rect", { x: 13, y: 4, width: 7, height: 7, rx: 1.5 }], ["rect", { x: 4, y: 13, width: 7, height: 7, rx: 1.5 }], ["rect", { x: 13, y: 13, width: 7, height: 7, rx: 1.5 }]],
  sources: [["path", { d: "M9 3v5" }], ["path", { d: "M15 3v5" }], ["path", { d: "M6 8h12v4a6 6 0 0 1-12 0z" }], ["path", { d: "M12 18v3" }]],
  destinations: [["ellipse", { cx: 12, cy: 5.5, rx: 8, ry: 3 }], ["path", { d: "M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13" }], ["path", { d: "M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" }]],
  jobs: [["circle", { cx: 6, cy: 18, r: 2 }], ["circle", { cx: 18, cy: 6, r: 2 }], ["path", { d: "M8 18h5a3 3 0 0 0 0-6h-2a3 3 0 0 1 0-6h5" }]],
  runs: [["path", { d: "M3 12a9 9 0 1 0 3-6.7" }], ["path", { d: "M3 4v4h4" }], ["path", { d: "M12 8v4l3 2" }]],
  stage: [["path", { d: "m12 3 9 5-9 5-9-5z" }], ["path", { d: "m3 13 9 5 9-5" }]],
  layouts: [["rect", { x: 4, y: 4, width: 16, height: 6, rx: 1.5 }], ["rect", { x: 4, y: 14, width: 16, height: 6, rx: 1.5 }]],
  settings: [["circle", { cx: 12, cy: 12, r: 3 }], ["path", { d: "M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1" }]],
  search: [["circle", { cx: 11, cy: 11, r: 7 }], ["path", { d: "m20 20-3.5-3.5" }]],
  plus: [["path", { d: "M12 5v14M5 12h14" }]],
  chevron: [["path", { d: "m6 9 6 6 6-6" }]],
  sun: [["circle", { cx: 12, cy: 12, r: 4 }], ["path", { d: "M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" }]],
  moon: [["path", { d: "M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z" }]],
  check: [["path", { d: "m5 12.5 4.5 4.5L19 7.5" }]],
};
export function line(name, size = 18, cls = "") {
  return s("svg", { class: `ic${cls ? ` ${cls}` : ""}`, width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "1.6", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false" },
  (LINES[name] || []).map(([tag, a]) => s(tag, a)));
}
export const hasLine = (name) => !!LINES[name];
// Phosphor names drawn with the design's line icons instead
const SAME = {
  "squares-four": "overview", plugs: "sources", plug: "sources", database: "destinations", "flow-arrow": "jobs",
  "clock-counter-clockwise": "runs", stack: "stage", rows: "layouts", "gear-six": "settings",
  "magnifying-glass": "search", plus: "plus", "caret-down": "chevron", sun: "sun", moon: "moon",
};
export const enc = encodeURIComponent;
export const clone = (o) => JSON.parse(JSON.stringify(o ?? null));

// ------------------------------------------------------------------ per-viewer memory
export const store = {
  get(key, fallback = null) {
    try { const v = localStorage.getItem(`etlplus:${key}`); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(`etlplus:${key}`, JSON.stringify(value)); } catch (e) { /* private mode */ }
  },
};

// ------------------------------------------------------------------ api and sign-in
const TOKEN_KEY = "etlplus-token";
function getToken() { try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; } }
function setToken(t) { try { sessionStorage.setItem(TOKEN_KEY, t); } catch (e) { /* private mode */ } }

let askingToken = null;
function askToken() {
  if (askingToken) return askingToken;
  askingToken = new Promise((resolve) => {
    const input = h("input", { class: "input mono", type: "password", autocomplete: "new-password", "aria-label": "Token" });
    const done = () => { setToken(input.value.trim()); bg.remove(); askingToken = null; resolve(); };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") done(); });
    const bg = h("div", { class: "dialog-bg" }, h("div", { class: "dialog", role: "dialog", "aria-modal": "true", "aria-label": "Sign in" },
      h("div", { class: "row" }, icon("lock-key"), h("h2", {}, "Sign in")),
      h("label", { class: "f" }, "Console token", input, h("span", { class: "help" }, "ETLPLUS_TOKEN on the server. Kept for this tab.")),
      h("div", { class: "row" }, h("span", { class: "grow" }), h("button", { class: "btn primary", onclick: done }, "Sign in"))));
    document.body.append(bg);
    input.focus();
  });
  return askingToken;
}

export class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data; }
}

// opts.raw: return {data, headers} instead of the data
export async function api(path, opts = {}, retried = false) {
  const headers = { "Content-Type": "application/json" };
  const t = getToken();
  if (t) headers.Authorization = `Bearer ${t}`;
  const res = await fetch(`/api${path}`, {
    method: opts.method || "GET",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });
  if (res.status === 401 && !retried) {
    await askToken();
    return api(path, opts, true);
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    if (res.status === 409 && data && data.code === "choose") window.dispatchEvent(new CustomEvent("etl:choose"));
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg || String(d)).join(" ");
    else if (msg && typeof msg === "object") msg = msg.message || JSON.stringify(msg);
    if (res.status === 404 && !msg) msg = "This route is not on the server yet.";
    throw new ApiError(msg || `${res.status} ${res.statusText}`, res.status, data);
  }
  return opts.raw ? { data, headers: res.headers } : data;
}

// One page of a list route. Server side through offset/limit and X-Total-Count; when the
// server sends the whole list (no header and more rows than asked), it is sliced here.
export async function apiPage(path, st) {
  const sep = path.includes("?") ? "&" : "?";
  const { data, headers } = await api(`${path}${sep}offset=${st.offset}&limit=${st.limit}`, { raw: true });
  const items = Array.isArray(data) ? data : [];
  const hdr = headers.get("X-Total-Count");
  if (hdr !== null && hdr !== "") return { items: items.slice(0, st.limit), total: Number(hdr) };
  if (items.length > st.limit) return { items: items.slice(st.offset, st.offset + st.limit), total: items.length, all: items };
  return { items, total: st.offset + items.length };
}

export function toast(text, bad = false) {
  const t = h("div", { class: `toast${bad ? " bad" : ""}`, role: "status" }, text);
  document.body.append(t);
  setTimeout(() => t.remove(), bad ? 5200 : 2600);
}

// ------------------------------------------------------------------ formatting
export const fmt = {
  int: (n) => (n === null || n === undefined || n === "" ? "-" : Number(n).toLocaleString("en-GB")),
  bytes: (n) => {
    if (n === null || n === undefined) return "-";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0; let v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(i && v < 10 ? 1 : 0)} ${u[i]}`;
  },
  time: (x) => (x ? new Date(x).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "-"),
  timeIn: (x, tz) => {
    if (!x) return "-";
    try {
      return `${new Date(x).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: tz || "UTC" })} ${tz || "UTC"}`;
    } catch (e) { return fmt.time(x); }
  },
  ago: (x) => {
    if (!x) return "-";
    const d = (Date.now() - new Date(x).getTime()) / 1000;
    if (Number.isNaN(d)) return String(x);
    if (d < 0) {
      const a = -d;
      if (a < 3600) return `in ${Math.max(1, Math.round(a / 60))} min`;
      if (a < 86400) return `in ${Math.round(a / 3600)} h`;
      return fmt.time(x);
    }
    if (d < 60) return "just now";
    if (d < 3600) return `${Math.round(d / 60)} min ago`;
    if (d < 86400) return `${Math.round(d / 3600)} h ago`;
    return fmt.time(x);
  },
  dur: (ms) => {
    if (ms === null || ms === undefined || Number.isNaN(ms)) return "-";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    const sec = ms / 1000;
    if (sec < 60) return `${sec.toFixed(1)} s`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m} min ${Math.round(sec % 60)} s`;
    return `${Math.floor(m / 60)} h ${m % 60} min`;
  },
};

export function between(a, b) {
  if (!a) return null;
  const end = b ? new Date(b).getTime() : Date.now();
  return end - new Date(a).getTime();
}

// ------------------------------------------------------------------ page pieces
// the page header: the title, one short line under it, the page's actions on the right
export function head(title, sub, ...actions) {
  return h("div", { class: "head" },
    h("div", { class: "t" }, h("h1", {}, title), sub ? h("p", { class: "muted" }, sub) : null),
    actions.filter(Boolean).length ? h("div", { class: "row" }, ...actions) : null);
}

export function skeleton(rows = 4) {
  return h("div", { class: "stack", "aria-busy": "true" }, h("div", { class: "sk", style: "height:28px;width:240px" }),
    ...Array.from({ length: rows }, () => h("div", { class: "sk", style: "height:64px" })));
}

export function errorBox(err, retry) {
  return h("div", { class: "banner bad", role: "alert" }, icon("warning-octagon"),
    h("div", { class: "grow" }, h("strong", {}, "Could not load. "), err.message),
    retry ? h("button", { class: "btn sm", onclick: retry }, "Try again") : null);
}

export function inlineError(err) {
  return h("div", { class: "banner bad", role: "alert" }, icon("warning-octagon"), h("div", { class: "grow small" }, err.message || String(err)));
}

// The empty state for a list page: a bordered card, the icon tile, "No X yet", one sentence and
// the primary action. o.art is a line icon name (LINES) or else a Phosphor name.
export function emptyState(o) {
  const art = hasLine(o.art) ? line(o.art, 24) : icon(o.art || "circle-dashed");
  return h("section", { class: "void" }, h("div", { class: "tile" }, art),
    h("div", { class: "vb" }, h("h2", {}, o.title), o.text ? h("p", {}, o.text) : null,
      (o.actions || []).filter(Boolean).length ? h("div", { class: "row" }, ...o.actions) : null));
}

// "Getting set up": add a source, add a destination, create a job. A step is done when the project
// already has one; the first one not done is the current step. Gone once all three exist.
const STEPS = [
  { id: "connection", label: "Add a source", line: "Where the data comes from", href: "#/sources/add" },
  { id: "destination", label: "Add a destination", line: "Where the data goes", href: "#/destinations/new" },
  { id: "job", label: "Create a job", line: "Moves data from one to the other", href: "#/jobs/new" },
];
export function setupSteps(setup) {
  const done = Object.fromEntries(((setup && setup.steps) || []).map((x) => [x.id, !!x.done]));
  if (STEPS.every((x) => done[x.id])) return null;
  const now = (STEPS.find((x) => !done[x.id]) || {}).id;
  return h("ol", { class: "steps", "aria-label": "Getting set up" }, STEPS.map((x, i) => {
    const cls = done[x.id] ? "done" : x.id === now ? "now" : "";
    // ?setup=1: the job builder prefills the source and destination made in steps 1 and 2
    return h("li", { class: `step ${cls}`.trim() }, h("a", { href: `${x.href}?setup=1`, "aria-current": x.id === now ? "step" : null },
      h("span", { class: "n" }, done[x.id] ? line("check", 14) : String(i + 1)),
      h("span", { class: "sx" }, h("span", { class: "sl" }, x.label, done[x.id] ? h("span", { class: "sr" }, " (done)") : null), h("span", { class: "cs" }, x.line))));
  }));
}

export function empty(ico, text, ...actions) {
  return h("div", { class: "empty" }, icon(ico), h("p", {}, text), actions.filter(Boolean).length ? h("div", { class: "row" }, ...actions) : null);
}

export function panel(title, right, ...body) {
  return h("div", { class: "panel" }, h("div", { class: "pnh" }, typeof title === "string" ? h("h2", {}, title) : title, right || null), ...body);
}

// a metric card: the label with its icon, the figure, an optional short line; href makes it a link
export function stat(v, k, o = {}) {
  return h(o.href ? "a" : "div", { class: "stat", href: o.href || null },
    h("div", { class: "k" }, o.art ? line(o.art, 14) : o.icon ? icon(o.icon) : null, k),
    h("div", { class: `v${o.tone ? ` ${o.tone}` : ""}` }, v),
    o.sub ? h("div", { class: "s" }, o.sub) : null);
}

// a count pill for a tab or a card header
export const pill = (n) => h("span", { class: "pill" }, typeof n === "number" ? fmt.int(n) : String(n));

export function kv(k, v) { return h("div", {}, h("span", { class: "k" }, k), h("span", { class: "v", title: String(v) }, v)); }

export function statusBadge(status) {
  if (!status) return h("span", { class: "badge" }, "Never run");
  if (status === "running") return h("span", { class: "badge info" }, h("span", { class: "spin", "aria-hidden": "true" }), "Running");
  if (status === "done") return h("span", { class: "badge ok" }, icon("check-circle"), "Done");
  if (status === "failed") return h("span", { class: "badge bad" }, icon("x-circle"), "Failed");
  if (status === "cancelled" || status === "interrupted") return h("span", { class: "badge warn" }, icon("stop"), "Cancelled");
  if (status === "skipped") return h("span", { class: "badge" }, icon("minus"), "Skipped");
  if (status === "pending") return h("span", { class: "badge" }, "Waiting");
  return h("span", { class: "badge" }, status);
}

// a status as an icon and a word, never colour alone
const CELL_ICON = { done: "check-circle", failed: "x-circle", running: "circle-notch", cancelled: "stop", interrupted: "stop" };
const CELL = { done: ["ok", "Done"], failed: ["bad", "Failed"], running: ["run", "Running"], cancelled: ["idle", "Cancelled"], interrupted: ["idle", "Cancelled"] };
export function statusCell(status) {
  const [cls, word] = CELL[status] || ["idle", "Never run"];
  return h("span", { class: `stt-cell ${cls}`, title: word }, icon(CELL_ICON[status] || "circle-dashed"), h("span", { class: "sr" }, word));
}

export function dataGrid(p) {
  if (!p || !p.columns) return null;
  const cols = p.columns.map((c) => (typeof c === "string" ? c : c.name));
  if (!p.rows || !p.rows.length) {
    return h("div", { class: "stack tight" }, h("div", { class: "chips" }, cols.map((c) => h("span", { class: "chip static mono" }, c))),
      h("p", { class: "faint small" }, "No rows."));
  }
  return h("div", { class: "grid-data" }, h("table", {},
    h("thead", {}, h("tr", {}, cols.map((c) => h("th", {}, c)))),
    h("tbody", {}, p.rows.map((r) => h("tr", {}, (Array.isArray(r) ? r : cols.map((c) => r[c])).map((v) =>
      v === null || v === undefined ? h("td", { class: "null" }, "null") : h("td", { title: String(typeof v === "object" ? JSON.stringify(v) : v) }, typeof v === "object" ? JSON.stringify(v) : String(v))))))));
}

// rows of a preview, paginated on the client
export function pagedGrid(p, key, size) {
  const box = h("div", { class: "stack tight" });
  if (!p || !p.columns) return box;
  const rows = p.rows || [];
  const st = pageState(key, size);
  const draw = () => put(box, dataGrid({ columns: p.columns, rows: rows.slice(st.offset, st.offset + st.limit) }),
    rows.length > 10 ? pager(st, rows.length, draw) : null);
  draw();
  return box;
}

// ------------------------------------------------------------------ collapsible section
// closed by default; the open state is remembered per viewer
export function sect(id, title, opts, ...body) {
  const o = opts || {};
  const key = `sect:${id}`;
  const open = store.get(key, !!o.open);
  const el = h("details", { class: `sect${o.flat ? " flat" : ""}`, open: open ? true : null, id: o.domId || null },
    h("summary", {}, icon("caret-right", "caret"), o.icon ? icon(o.icon) : null, h("span", {}, title), o.right ? h("span", { class: "sum-r" }, o.right) : null),
    h("div", { class: "sb" }, ...body));
  el.addEventListener("toggle", () => { store.set(key, el.open); if (el.open && o.onOpen) o.onOpen(el); });
  if (open && o.onOpen) queueMicrotask(() => o.onOpen(el));
  return el;
}

// ------------------------------------------------------------------ pager
export const PAGE_SIZES = [10, 25, 50, 100];
// card grids page by rows of four: CARD_SIZES
export const CARD_SIZES = [12, 24, 48];
export function pageState(key, size = 25, sizes = null) {
  const choices = sizes || PAGE_SIZES;
  let limit = store.get(`page:${key}`, size);
  if (!choices.includes(limit)) limit = size;
  return { key, offset: 0, limit, sizes: choices };
}
// "1-25 of 312", page size choice, previous and next
export function pageText(offset, count, total) {
  if (!total) return "0 of 0";
  return `${fmt.int(offset + 1)}-${fmt.int(Math.min(offset + count, total))} of ${fmt.int(total)}`;
}
export function pager(state, total, onChange) {
  const shown = Math.min(state.limit, Math.max(0, total - state.offset));
  return h("div", { class: "pager", role: "navigation", "aria-label": "Pages" },
    h("span", { class: "count" }, pageText(state.offset, shown, total)),
    h("span", { class: "grow" }),
    h("label", { class: "row nowrap", style: "gap:6px" }, h("span", {}, "Per page"),
      h("select", { class: "input", "aria-label": "Rows per page", onchange: (e) => {
        state.limit = Number(e.target.value); state.offset = 0; store.set(`page:${state.key}`, state.limit); onChange(state);
      } }, (state.sizes || PAGE_SIZES).map((n) => h("option", { value: String(n), selected: n === state.limit }, String(n))))),
    h("button", { class: "btn sm icon", "aria-label": "Previous page", disabled: state.offset <= 0,
      onclick: () => { state.offset = Math.max(0, state.offset - state.limit); onChange(state); } }, icon("caret-left")),
    h("button", { class: "btn sm icon", "aria-label": "Next page", disabled: state.offset + state.limit >= total,
      onclick: () => { state.offset += state.limit; onChange(state); } }, icon("caret-right")));
}

// ------------------------------------------------------------------ drawer and dialogs
// every open drawer and dialog; a page change (hashchange) closes them all
const OPEN = new Set();
export function closeOverlays() { [...OPEN].forEach((f) => f()); }

export function drawer({ title, wide = false, label }) {
  const body = h("div", { class: "db" });
  const foot = h("div", { class: "df" });
  const titleEl = h("h2", {}, title);
  const bg = h("div", { class: "drawer-bg", onclick: () => close() });
  const el = h("aside", { class: `drawer${wide ? " wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-label": label || title },
    h("div", { class: "dh" }, titleEl, h("button", { class: "btn ghost sm icon", onclick: () => close(), "aria-label": "Close" }, icon("x"))),
    body, foot);
  const before = document.activeElement;
  const onKey = (e) => { if (e.key === "Escape" && document.body.lastElementChild === el) close(); };
  const handlers = [];
  function close() {
    if (!OPEN.delete(close)) return;
    bg.remove(); el.remove(); document.removeEventListener("keydown", onKey);
    handlers.forEach((f) => f());
    if (before && before.focus) before.focus();
  }
  document.addEventListener("keydown", onKey);
  OPEN.add(close);
  document.body.append(bg, el);
  return { el, body, foot, titleEl, close, onClose: (f) => handlers.push(f), focus: () => { const f = el.querySelector("input, select, textarea, button.primary"); if (f) f.focus(); } };
}

export function dialog(title, wide = false) {
  const box = h("div", { class: `dialog${wide ? " wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-label": title });
  const bg = h("div", { class: "dialog-bg", onclick: (e) => { if (e.target === bg) close(); } }, box);
  const before = document.activeElement;
  const onKey = (e) => { if (e.key === "Escape") close(); };
  function close() {
    if (!OPEN.delete(close)) return;
    bg.remove(); document.removeEventListener("keydown", onKey); if (before && before.focus) before.focus();
  }
  document.addEventListener("keydown", onKey);
  OPEN.add(close);
  document.body.append(bg);
  return { box, close };
}

// a confirm with the person's own words; resolves true or false
export function confirmBox(title, text, okLabel = "Confirm", danger = false) {
  return new Promise((resolve) => {
    const d = dialog(title);
    const done = (v) => { d.close(); resolve(v); };
    const ok = h("button", { class: `btn ${danger ? "danger" : "primary"}`, onclick: () => done(true) }, okLabel);
    put(d.box, h("h2", {}, title), text ? h("p", { class: "muted" }, text) : null,
      h("div", { class: "row" }, h("span", { class: "grow" }), h("button", { class: "btn ghost", onclick: () => done(false) }, "Cancel"), ok));
    ok.focus();
  });
}

export function copyBtn(text, label = "Copy") {
  return h("button", { class: "btn ghost sm icon", "aria-label": `${label}: ${text}`, title: label, onclick: async (e) => {
    e.stopPropagation();
    try { await navigator.clipboard.writeText(text); toast("Copied"); } catch (err) { toast("Copy is not allowed here", true); }
  } }, icon("copy"));
}

// ------------------------------------------------------------------ forms
export const lab = (label, control, help) => h("label", { class: "f" }, label, control, help ? h("span", { class: "help" }, help) : null);
export function inp(obj, key, attrs = {}) {
  const { mono, redraw, ...rest } = attrs;
  return h("input", { class: `input${mono ? " mono" : ""}`, value: obj[key] ?? "", ...rest,
    oninput: (e) => {
      obj[key] = rest.type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value;
      if (redraw) redraw();
    } });
}
export function seg(label, options, value, set) {
  const box = h("div", { class: "seg", role: "radiogroup", "aria-label": label });
  options.forEach(([v, l]) => box.append(h("button", { type: "button", role: "radio", "aria-checked": String(value === v), class: value === v ? "on" : "", onclick: (e) => {
    for (const b of box.children) { const on = b === e.currentTarget; b.classList.toggle("on", on); b.setAttribute("aria-checked", String(on)); }
    set(v);
  } }, l)));
  return h("div", { class: "f" }, h("span", {}, label), box);
}

// The body for a PUT with write-only secrets: an empty secret is left out, so the saved one stays.
export function withSecrets(body, secrets) {
  const out = { ...body };
  for (const [k, v] of Object.entries(secrets || {})) {
    delete out[k];
    if (typeof v === "string" && v !== "") out[k] = v;
  }
  for (const k of Object.keys(out)) if (k.endsWith("_set")) delete out[k];
  return out;
}

export function cleanObj(o) {
  const out = {};
  for (const [k, v] of Object.entries(o || {})) {
    if (v === "" || v === null || v === undefined) continue;
    if (Array.isArray(v) && !v.length) continue;
    out[k] = v;
  }
  return out;
}

export function parseOptions(text) {
  const out = {};
  for (const line of String(text || "").split(/\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const i = t.indexOf("=");
    if (i < 1) throw new Error(`Options: "${t}" needs key=value.`);
    const k = t.slice(0, i).trim();
    let v = t.slice(i + 1).trim();
    if (/^-?\d+(\.\d+)?$/.test(v)) v = Number(v);
    else if (v === "true" || v === "false") v = v === "true";
    out[k] = v;
  }
  return out;
}
export const optionsText = (o) => Object.entries(o || {}).map(([k, v]) => `${k}=${v}`).join("\n");

// ------------------------------------------------------------------ icons for kinds
const FAMILY_ICON = {
  postgres: "database", sqlserver: "database", mysql: "database", oracle: "database", db2: "database", sqlite: "database",
  duckdb: "database", clickhouse: "database", snowflake: "database", hana: "database", teradata: "database", firebird: "database",
  odbc: "plug", mongodb: "brackets-curly", folder: "folder-open", remote: "cloud",
};
const KIND_ICON = { share: "share-network", sftp: "hard-drives", ftp: "hard-drives", s3: "cloud", azure_blob: "cloud", http: "globe", folder: "folder-open" };
export function kindIcon(k) {
  if (!k) return "database";
  if (KIND_ICON[k.kind || k]) return KIND_ICON[k.kind || k];
  if (k.family && FAMILY_ICON[k.family]) return FAMILY_ICON[k.family];
  if (k.category === "remote") return "cloud";
  if (k.category === "folder") return "folder-open";
  if (k.category === "document") return "brackets-curly";
  return FAMILY_ICON[k.kind || k] || "database";
}

export const STATUS_LABEL = { real: ["Tested", "ok"], simulated: ["Simulated", "info"], untested: ["Untested", "warn"] };
export function kindStatus(status) {
  const [l, c] = STATUS_LABEL[status] || [status || "Unknown", ""];
  return h("span", { class: `badge ${c}`, title: { real: "Tested against a real server", simulated: "Tested through a recorded fake driver", untested: "SQL is generated; never run against a server" }[status] || "" }, l);
}

// ------------------------------------------------------------------ cron reading
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const MONTHS = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
export function describeCron(expr) {
  const x = String(expr || "").trim();
  if (!x) return { ok: true, text: "Runs only when started by hand." };
  const f = x.split(/\s+/);
  const part = /^(\*|\d+(-\d+)?)(\/\d+)?(,(\d+(-\d+)?)(\/\d+)?)*$/;
  if (f.length !== 5 || !f.every((p) => part.test(p) || /^[a-z,-]+$/i.test(p))) {
    return { ok: false, text: "Not a valid schedule. Five fields: minute hour day month weekday." };
  }
  const [mi, hr, dom, mon, dow] = f;
  const num = (p) => /^\d+$/.test(p);
  const pad = (n) => String(n).padStart(2, "0");
  if (x === "* * * * *") return { ok: true, text: "Every minute." };
  let m;
  if ((m = mi.match(/^\*\/(\d+)$/)) && hr === "*" && dom === "*" && mon === "*" && dow === "*") return { ok: true, text: `Every ${m[1]} minutes.` };
  if (num(mi) && hr === "*" && dom === "*" && mon === "*" && dow === "*") return { ok: true, text: `Every hour at ${pad(mi)} past.` };
  if (num(mi) && (m = hr.match(/^\*\/(\d+)$/)) && dom === "*" && mon === "*" && dow === "*") return { ok: true, text: `Every ${m[1]} hours at ${pad(mi)} past.` };
  if (!num(mi) || !/^\d+(,\d+)*$/.test(hr)) return { ok: true, text: `Custom schedule: ${x}.` };
  const at = hr.split(",").map((p) => `${pad(p)}:${pad(mi)}`).join(" and ");
  let days;
  if (dom === "*" && dow === "*") days = "Every day";
  else if (dom === "*" && dow === "1-5") days = "Weekdays";
  else if (dom === "*" && (dow === "0,6" || dow === "6,0")) days = "Weekends";
  else if (dom === "*" && /^\d(,\d)*$/.test(dow)) days = `Every ${dow.split(",").map((d) => DAYS[+d] || d).join(", ")}`;
  else if (num(dom) && dow === "*") days = `On day ${dom} of the month`;
  else return { ok: true, text: `Custom schedule: ${x}.` };
  let monthTxt = "";
  if (mon !== "*") {
    if (!/^\d+(,\d+)*$/.test(mon)) return { ok: true, text: `Custom schedule: ${x}.` };
    monthTxt = ` in ${mon.split(",").map((p) => MONTHS[+p] || p).join(", ")}`;
  }
  return { ok: true, text: `${days}${monthTxt} at ${at}.` };
}

// ------------------------------------------------------------------ runs
export function rowsOf(run, step) {
  if (!run) return null;
  const st = run.steps && run.steps[step];
  if (st && st.rows !== undefined && st.rows !== null) return st.rows;
  const r = run.rows || {};
  const alias = { extract: ["extract", "extracted"], transform: ["transform", "transformed"], map: ["map", "mapped"], load: ["load", "loaded", "rows_loaded"] }[step] || [step];
  for (const k of alias) if (r[k] !== undefined && r[k] !== null) return r[k];
  return null;
}
export function stepMs(st) {
  if (!st || typeof st !== "object") return null;
  if (st.ms !== undefined && st.ms !== null) return st.ms;
  if (st.seconds !== undefined && st.seconds !== null) return st.seconds * 1000;
  if (st.duration_ms !== undefined) return st.duration_ms;
  return st.started_at ? between(st.started_at, st.finished_at) : null;
}
export function runMs(r) {
  if (!r) return null;
  const steps = Object.values(r.steps || {}).map(stepMs).filter((v) => v !== null && v !== undefined);
  if (r.status !== "running" && steps.length) return steps.reduce((a, b) => a + b, 0);
  return between(r.started_at, r.finished_at);
}

// ------------------------------------------------------------------ timers (stopped on every route change)
const timers = [];
export function every(ms, fn) { const t = setInterval(fn, ms); timers.push(t); return t; }
export function stopTimers() { timers.splice(0).forEach(clearInterval); }

// a shared hash query reader: #/jobs/new?connection=x
export function hashQuery() {
  const i = location.hash.indexOf("?");
  return new URLSearchParams(i >= 0 ? location.hash.slice(i + 1) : "");
}

export function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
