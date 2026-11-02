// Destinations: the grid of cards, the editor drawer (existing table or a column design,
// DDL preview, create), the test dialog and the paginated preview.
import {
  h, put, icon, line, api, apiPage, enc, head, panel, empty, emptyState, toast, pager, pageState, sect, drawer, dialog, confirmBox,
  kindIcon, CARD_SIZES, clone, dataGrid, inlineError, lab, seg, hashQuery,
} from "./ui.js";
import { loadConnections, loadKinds, kindOf, kindLabel, isFileKind, stepsStrip } from "./state.js";
import { testResult, tableBrowser } from "./pages-sources.js";

export const GENERIC_TYPES = ["string", "text", "integer", "bigint", "decimal", "double", "boolean", "date", "time", "timestamp", "timestamptz", "binary", "json"];
export const FILE_FORMATS = [["parquet", "Parquet"], ["csv", "CSV"], ["jsonl", "JSON lines"]];

export function parseType(t) {
  const m = String(t || "string").trim().toLowerCase().match(/^([a-z]+)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?$/);
  if (!m) return { base: "string", n: null, p: null, s: null };
  if (m[1] === "decimal") return { base: "decimal", p: m[2] ? Number(m[2]) : 18, s: m[3] ? Number(m[3]) : 2 };
  if (m[1] === "string") return { base: "string", n: m[2] ? Number(m[2]) : 255 };
  return { base: GENERIC_TYPES.includes(m[1]) ? m[1] : "string", n: null };
}
export function typeText(x) {
  if (x.base === "string") return `string(${x.n || 255})`;
  if (x.base === "decimal") return `decimal(${x.p || 18},${x.s ?? 2})`;
  return x.base;
}

export async function pageDestinations(args) {
  const [id, sub] = args;
  await loadKinds();
  const top = head("Destinations", "Where your jobs write their results.", h("a", { class: "btn primary", href: "#/destinations/new" }, line("plus", 16), "New destination"));
  if (id && sub === "preview") return [top, h("a", { class: "btn ghost sm", href: "#/destinations", style: "align-self:flex-start" }, icon("arrow-left"), "All destinations"), previewPanel(id)];
  const grid = gridView();
  if (id === "new") queueMicrotask(() => openDestination({ onClose: () => { if (location.hash.startsWith("#/destinations/new")) location.hash = "#/destinations"; } }));
  else if (id) {
    queueMicrotask(async () => {
      try {
        const spec = await api(`/destinations/${enc(id)}`);
        openDestination({ id, spec, onClose: () => { if (location.hash.startsWith(`#/destinations/${enc(id)}`)) location.hash = "#/destinations"; } });
      } catch (e) { toast(e.message, true); }
    });
  }
  return [top, grid];
}

function gridView() {
  const box = h("div", { class: "stack" });
  const st = pageState("destinations", 12, CARD_SIZES);
  async function draw() {
    put(box, h("div", { class: "cards" }, [1, 2, 3].map(() => h("div", { class: "sk", style: "height:150px" }))));
    let page;
    try { page = await apiPage("/destinations", st); } catch (e) { put(box, inlineError(e)); return; }
    if (!page.total) {
      const steps = await stepsStrip();
      put(box, emptyState({ art: "destinations", title: "No destinations yet",
        text: "A destination is where a job delivers its data, such as a database table or a folder. Add one before you create your first job.",
        actions: [h("a", { class: "btn primary", href: "#/destinations/new?setup=1" }, line("plus", 16), "New destination")] }), steps);
      return;
    }
    put(box, h("div", { class: "cards" }, page.items.map(destCard)), page.total > 12 ? pager(st, page.total, draw) : null);
  }
  draw();
  return box;
}

function destCard(x) {
  const jobs = x.jobs || [];
  const isFile = !x.table && (x.path || x.file || x.format);
  return h("div", { class: "card" },
    h("div", { class: "ch" }, h("span", { class: "ico" }, icon(isFile ? "folder-open" : kindIcon(kindOf(x.kind) || { kind: x.kind }))),
      h("span", { class: "ttl mono", title: x.id }, x.id),
      isFile ? h("span", { class: "badge info" }, (x.format || "file").toUpperCase())
        : x.exists ? h("span", { class: "badge ok" }, icon("check"), "Exists") : h("span", { class: "badge warn" }, icon("circle-dashed"), "Not created")),
    h("div", { class: "cb" },
      h("div", {}, `${x.connection} · ${kindLabel(x.kind) || x.kind || ""}`),
      h("div", { class: "mono", title: x.table || x.path || "" }, x.table || [x.path, x.file].filter(Boolean).join("/") || "-")),
    jobs.length ? h("details", {}, h("summary", {}, `${jobs.length} ${jobs.length === 1 ? "job feeds" : "jobs feed"} it`),
      h("div", { class: "chips", style: "margin-top:6px" }, jobs.map((j) => h("a", { class: "chip mono", href: `#/jobs/${enc(j)}` }, j))))
      : h("div", { class: "small faint" }, "No job feeds it yet"),
    h("div", { class: "cf" },
      h("a", { class: "btn sm", href: `#/destinations/${enc(x.id)}/preview` }, icon("eye"), "Preview"),
      h("button", { class: "btn sm", onclick: () => testDialog(x.id) }, icon("plug-charging"), "Test"),
      h("span", { class: "grow" }),
      h("a", { class: "btn ghost sm icon", href: `#/destinations/${enc(x.id)}`, "aria-label": `Edit ${x.id}`, title: "Edit" }, icon("pencil-simple"))));
}

async function testDialog(id, spec) {
  const d = dialog(`Test ${id}`);
  put(d.box, h("h2", {}, `Test ${id}`), h("div", { class: "sk", style: "height:80px" }));
  const close = h("div", { class: "row" }, h("span", { class: "grow" }), h("button", { class: "btn", onclick: d.close }, "Close"));
  try {
    const r = await api(`/destinations/${enc(id)}/test`, { method: "POST", body: spec || undefined });
    put(d.box, h("h2", {}, `Test ${id}`), testResult({ ok: r.ok, checks: r.checks }), close);
  } catch (e) { put(d.box, h("h2", {}, `Test ${id}`), inlineError(e), close); }
  close.querySelector("button").focus();
}

function previewPanel(id) {
  const st = pageState(`dest-preview-${id}`, 25);
  const box = h("div", { class: "pb stack" });
  async function draw() {
    put(box, h("div", { class: "sk", style: "height:220px" }));
    try {
      const p = await api(`/destinations/${enc(id)}/preview?offset=${st.offset}&limit=${st.limit}`);
      const n = (p.rows || []).length;
      const total = p.total !== undefined && p.total !== null ? p.total : st.offset + n + (n === st.limit ? 1 : 0);
      put(box, n ? dataGrid(p) : empty("database", "No rows yet. Run a job that feeds it."), total ? pager(st, total, draw) : null);
    } catch (e) { put(box, inlineError(e)); }
  }
  draw();
  return panel(h("div", { class: "row" }, icon("eye"), h("h2", { class: "mono" }, id)), h("a", { class: "btn sm ghost", href: `#/destinations/${enc(id)}` }, icon("pencil-simple"), "Edit"), box);
}

// ------------------------------------------------------------------ editor drawer
// openDestination({id, spec, onSaved(id), onClose})
export async function openDestination({ id, spec, onSaved, onClose, connection } = {}) {
  await loadKinds();
  const conns = await loadConnections(true);
  const targets = conns.filter((c) => c.role === "target" || c.role === "both");
  const isNew = !id;
  const q = hashQuery();
  const d = { id: id || "", connection: connection || q.get("connection") || "", design: "create", columns: [], keys: [], description: "", ...(spec ? clone(spec) : {}) };
  delete d.exists; delete d.jobs; delete d.kind; delete d.columns_count;
  if (!d.connection && targets.length === 1) d.connection = targets[0].id;
  d.columns = (d.columns || []).map((c) => ({ ...c }));
  let saved = !isNew;
  let liveCols = null;
  const dr = drawer({ title: isNew ? "New destination" : id, wide: true, label: "Destination" });
  if (onClose) dr.onClose(onClose);
  const err = h("div", { class: "stack tight", role: "alert" });
  const result = h("div");
  const conn = () => conns.find((c) => c.id === d.connection);
  const fileDest = () => { const c = conn(); return !!c && isFileKind(c.kind); };
  const dirty = () => { saved = false; };

  const colPage = pageState("dest-columns", 25);
  function designTable() {
    const keys = new Set(d.keys || []);
    if (colPage.offset >= d.columns.length) colPage.offset = Math.max(0, Math.floor((d.columns.length - 1) / colPage.limit) * colPage.limit);
    const move = (i, k) => { const j = i + k; if (j < 0 || j >= d.columns.length) return; [d.columns[i], d.columns[j]] = [d.columns[j], d.columns[i]]; dirty(); draw(); };
    return h("div", { class: "stack tight" },
      d.columns.length ? h("div", { class: "scroll-x" }, h("table", { class: "design" },
        h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Type"), h("th", { class: "c" }, "Nullable"), h("th", { class: "c" }, "Key"),
          h("th", {}, "Default"), h("th", { class: "c" }, "Personal"), h("th", {}, h("span", { class: "sr" }, "Order")))),
        h("tbody", {}, d.columns.map((c, i) => [c, i]).slice(colPage.offset, colPage.offset + colPage.limit).map(([c, i]) => {
          const t = parseType(c.type);
          const setT = (patch) => { Object.assign(t, patch); c.type = typeText(t); dirty(); };
          return h("tr", {},
            h("td", { class: "full" }, h("input", { class: "input sm mono", value: c.name || "", "aria-label": `Column ${i + 1} name`, style: "min-width:130px",
              oninput: (e) => { const old = c.name; c.name = e.target.value; if (keys.has(old)) { d.keys = d.keys.map((k) => (k === old ? c.name : k)); } dirty(); } })),
            h("td", { class: "full" }, h("div", { class: "typ" },
              h("select", { class: "input sm", "aria-label": `Column ${c.name || i + 1} type`, onchange: (e) => { setT({ base: e.target.value }); draw(); } },
                GENERIC_TYPES.map((g) => h("option", { value: g, selected: g === t.base }, g))),
              t.base === "string" ? h("input", { class: "input sm mono", type: "number", min: 1, value: t.n, "aria-label": "Length", title: "Length", oninput: (e) => setT({ n: Number(e.target.value) || 1 }) }) : null,
              t.base === "decimal" ? [
                h("input", { class: "input sm mono", type: "number", min: 1, value: t.p, "aria-label": "Precision", title: "Precision", oninput: (e) => setT({ p: Number(e.target.value) || 1 }) }),
                h("input", { class: "input sm mono", type: "number", min: 0, value: t.s, "aria-label": "Scale", title: "Scale", oninput: (e) => setT({ s: Number(e.target.value) || 0 }) })] : null)),
            h("td", { class: "c", "data-l": "Nullable" }, h("input", { type: "checkbox", checked: c.nullable !== false, "aria-label": `${c.name} nullable`, onchange: (e) => { c.nullable = e.target.checked; dirty(); } })),
            h("td", { class: "c", "data-l": "Key" }, h("input", { type: "checkbox", checked: keys.has(c.name), "aria-label": `${c.name} key`, onchange: (e) => {
              const ks = new Set(d.keys || []); if (e.target.checked) { ks.add(c.name); c.nullable = false; } else ks.delete(c.name);
              d.keys = d.columns.map((x) => x.name).filter((n) => ks.has(n)); dirty(); draw();
            } })),
            h("td", { class: "full", "data-l": "Default" }, h("input", { class: "input sm mono", value: c.default ?? "", placeholder: "none", "aria-label": `${c.name} default`, style: "min-width:90px", oninput: (e) => { c.default = e.target.value || undefined; dirty(); } })),
            h("td", { class: "c", "data-l": "Personal" }, h("input", { type: "checkbox", checked: !!c.personal, "aria-label": `${c.name} personal`, onchange: (e) => { c.personal = e.target.checked; dirty(); } })),
            h("td", { class: "full acts" }, h("div", { class: "row nowrap", style: "gap:2px" },
              h("button", { class: "btn ghost sm icon", "aria-label": "Move up", disabled: i === 0, onclick: () => move(i, -1) }, icon("arrow-up")),
              h("button", { class: "btn ghost sm icon", "aria-label": "Move down", disabled: i === d.columns.length - 1, onclick: () => move(i, 1) }, icon("arrow-down")),
              h("button", { class: "btn ghost sm icon", "aria-label": `Remove ${c.name}`, onclick: () => { d.columns.splice(i, 1); d.keys = (d.keys || []).filter((k) => k !== c.name); dirty(); draw(); } }, icon("trash")))));
        })))) : h("p", { class: "faint small" }, "No columns yet."),
      d.columns.length > 10 ? pager(colPage, d.columns.length, () => draw()) : null,
      h("div", { class: "row" },
        h("button", { class: "btn sm", onclick: () => { d.columns.push({ name: `column_${d.columns.length + 1}`, type: "string(255)", nullable: true }); colPage.offset = Math.floor((d.columns.length - 1) / colPage.limit) * colPage.limit; dirty(); draw(); } }, icon("plus"), "Add column"),
        h("button", { class: "btn sm ghost", onclick: fromSource }, icon("magic-wand"), "Start from a source")));
  }

  async function fromSource() {
    const sources = conns.filter((c) => (c.role || "source") !== "target");
    const src = { connection: sources[0] ? sources[0].id : "", table: "", files: "", format: "" };
    const dl = dialog("Start from a source");
    const e2 = h("div", { class: "err", role: "alert" });
    const drawDl = () => {
      const c = conns.find((x) => x.id === src.connection);
      const files = c && isFileKind(c.kind);
      put(dl.box, h("h2", {}, "Start from a source"),
        lab("Connection", h("select", { class: "input", onchange: (e) => { src.connection = e.target.value; drawDl(); } },
          sources.map((x) => h("option", { value: x.id, selected: x.id === src.connection }, `${x.id} (${kindLabel(x.kind)})`)))),
        files ? h("div", { class: "grid2" },
          lab("Files", h("input", { class: "input mono", value: src.files, placeholder: "bank/*.txt", oninput: (e) => { src.files = e.target.value; } })),
          lab("Format", h("input", { class: "input mono", value: src.format, placeholder: "mt940", oninput: (e) => { src.format = e.target.value; } })))
          : lab("Table", h("input", { class: "input mono", value: src.table, placeholder: "main.customers", oninput: (e) => { src.table = e.target.value; } })),
        e2,
        h("div", { class: "row" }, h("span", { class: "grow" }), h("button", { class: "btn ghost", onclick: dl.close }, "Cancel"),
          h("button", { class: "btn primary", onclick: async () => {
            const source = { connection: src.connection };
            if (files) { source.files = src.files; if (src.format) source.format = src.format; } else source.table = src.table;
            try {
              const cols = await api("/destinations/design-from-source", { method: "POST", body: { source, connection: d.connection } });
              if (d.columns.length && !(await confirmBox("Replace the columns?", `${d.columns.length} columns are designed now.`, "Replace"))) return;
              d.columns = cols.map((c) => ({ name: c.name, type: c.type, nullable: c.nullable !== false, personal: !!c.personal }));
              d.design = "create"; dirty(); dl.close(); draw();
            } catch (e) { e2.textContent = e.message; }
          } }, icon("check"), "Use these columns")));
    };
    drawDl();
  }

  async function readLive() {
    if (!d.connection || !d.table) return;
    liveCols = "loading"; draw();
    try { liveCols = await api(`/connections/${enc(d.connection)}/columns?table=${enc(d.table)}`); } catch (e) { liveCols = e; }
    draw();
  }

  function existingBlock() {
    return h("div", { class: "stack" },
      h("div", { class: "row nowrap", style: "align-items:flex-end" },
        h("div", { class: "grow" }, lab("Table", h("input", { class: "input mono", value: d.table || "", placeholder: "schema.table", oninput: (e) => { d.table = e.target.value; dirty(); } }))),
        h("button", { class: "btn", onclick: browse }, icon("magnifying-glass"), "Browse"),
        h("button", { class: "btn", onclick: readLive }, icon("arrows-clockwise"), "Read columns")),
      liveCols === "loading" ? h("div", { class: "sk", style: "height:100px" })
        : liveCols instanceof Error ? h("div", { class: "err" }, liveCols.message)
          : Array.isArray(liveCols) || (d.columns || []).length ? h("div", { class: "scroll-x" }, h("table", { class: "t" },
            h("thead", {}, h("tr", {}, h("th", {}, "Column"), h("th", {}, "Type"), h("th", {}, "Nulls"))),
            h("tbody", {}, (Array.isArray(liveCols) ? liveCols : d.columns).map((c) => h("tr", {}, h("td", { class: "mono small" }, c.name), h("td", { class: "mono small muted" }, c.type),
              h("td", { class: "small faint" }, c.nullable === false ? "No" : "Yes"))))))
            : h("p", { class: "faint small" }, "Columns are read from the database."));
  }

  async function browse() {
    const c = conn();
    if (!c) return;
    const dl = dialog(`Tables in ${c.id}`, true);
    put(dl.box, h("div", { class: "sk", style: "height:240px" }));
    put(dl.box, await tableBrowser(c, (name, cols) => { d.table = name; liveCols = cols; dirty(); dl.close(); draw(); }),
      h("div", { class: "row" }, h("span", { class: "grow" }), h("button", { class: "btn", onclick: dl.close }, "Close")));
  }

  function ddlBlock() {
    const out = h("div", { class: "stack tight" });
    return sect("dest-ddl", "SQL to create it", { icon: "code", onOpen: async () => {
      if (!saved || !d.id) { put(out, h("p", { class: "faint small" }, "Save to see the SQL.")); return; }
      put(out, h("div", { class: "sk", style: "height:90px" }));
      try {
        const r = await api(`/destinations/${enc(d.id)}/ddl`);
        put(out,
          h("div", { class: "row" }, h("span", { class: "badge info" }, r.dialect || ""), r.exists ? h("span", { class: "badge ok" }, icon("check"), "Exists") : h("span", { class: "badge warn" }, "Not created")),
          (r.checks || []).map((c) => h("div", { class: c.level === "error" ? "err" : "small", style: c.level === "warn" ? "color:var(--gold)" : null },
            icon(c.level === "error" ? "x-circle" : c.level === "warn" ? "warning" : "info"), " ", c.column ? `${c.column}: ` : "", c.message)),
          h("pre", { class: "code" }, r.sql || ""));
      } catch (e) { put(out, h("div", { class: "err" }, e.message)); }
    } }, out);
  }

  function draw() {
    const c = conn();
    const file = fileDest();
    if (typeof createBtn !== "undefined") createBtn.classList.toggle("hidden", file || d.design === "existing");
    put(dr.body,
      h("div", { class: "grid2" },
        isNew ? lab("Name", h("input", { class: "input mono", value: d.id, placeholder: "reporting_customers", autocomplete: "off", oninput: (e) => { d.id = e.target.value; dirty(); } }), "Letters, digits and _.")
          : lab("Name", h("input", { class: "input mono", value: d.id, disabled: true })),
        lab("Connection", h("select", { class: "input", onchange: (e) => { d.connection = e.target.value; liveCols = null; dirty(); draw(); } },
          h("option", { value: "" }, targets.length ? "Pick one" : "No target connections yet"),
          targets.map((x) => h("option", { value: x.id, selected: x.id === d.connection }, `${x.id} (${kindLabel(x.kind)})`))),
          targets.length ? "Connections with the role target or both." : h("a", { href: "#/sources/add" }, "Add a target connection"))),
      !c ? null : file ? [
        h("div", { class: "grid3" },
          lab("Folder", h("input", { class: "input mono", value: d.path || "", placeholder: "customers", oninput: (e) => { d.path = e.target.value; dirty(); } }), "Inside the location."),
          lab("File name", h("input", { class: "input mono", value: d.file || "", placeholder: "customers.{format}", oninput: (e) => { d.file = e.target.value; dirty(); } }), "{date} {run_id} {job} {format}"),
          seg("Format", FILE_FORMATS, d.format || "parquet", (v) => { d.format = v; dirty(); draw(); })),
        sect("dest-file-cols", "Columns", { right: h("span", { class: "faint small" }, d.columns.length ? `${d.columns.length}` : "Optional") }, designTable()),
      ] : [
        seg("Table", [["existing", "Existing table"], ["create", "Design columns"]], d.design || "create", (v) => { d.design = v; dirty(); draw(); }),
        d.design === "existing" ? existingBlock() : [
          lab("Table to create", h("input", { class: "input mono", value: d.table || "", placeholder: "main.customers", oninput: (e) => { d.table = e.target.value; dirty(); } })),
          designTable(),
        ],
        ddlBlock(),
      ],
      lab("Description", h("input", { class: "input", value: d.description || "", placeholder: "Optional", oninput: (e) => { d.description = e.target.value; dirty(); } })),
      err, result);
  }

  function payload() {
    const p = clone(d);
    const file = fileDest();
    if (file) { delete p.table; delete p.design; p.format = p.format || "parquet"; } else { delete p.path; delete p.file; delete p.format; }
    if (!file && p.design === "existing" && !p.columns.length) delete p.columns;
    if (file && !p.columns.length) delete p.columns;
    for (const c of p.columns || []) { if (c.nullable !== false) delete c.nullable; if (!c.personal) delete c.personal; if (c.default === undefined || c.default === "") delete c.default; }
    if (!p.keys || !p.keys.length) delete p.keys;
    if (!p.description) delete p.description;
    return p;
  }

  function showProblems(e) {
    const probs = (e.data && e.data.problems) || [e.message];
    put(err, probs.map((x) => h("div", { class: "err" }, typeof x === "string" ? x : `${x.column ? `${x.column}: ` : ""}${x.message}`)));
  }

  async function save() {
    put(err);
    const p = payload();
    if (!p.id) { put(err, h("div", { class: "err" }, "Give the destination a name.")); return false; }
    if (!p.connection) { put(err, h("div", { class: "err" }, "Pick a connection.")); return false; }
    try {
      await api(`/destinations/${enc(p.id)}`, { method: "PUT", body: p });
      toast(`${p.id} saved`);
      saved = true;
      if (onSaved) onSaved(p.id);
      return true;
    } catch (e) { showProblems(e); return false; }
  }

  async function createTable() {
    if (!saved && !(await save())) return;
    try {
      const r = await api(`/destinations/${enc(d.id)}/create`, { method: "POST" });
      put(result, h("div", { class: "banner ok" }, icon("check-circle"), h("div", { class: "grow" }, r.created ? "Table created." : "Nothing to create.")));
    } catch (e) {
      put(result, h("div", { class: `banner ${e.status === 409 ? "info" : "bad"}` }, icon(e.status === 409 ? "info" : "warning-octagon"), h("div", { class: "grow" }, e.message)));
    }
  }

  async function test() {
    put(result, h("div", { class: "sk", style: "height:64px" }));
    try {
      const r = await api(`/destinations/${enc(d.id || "draft")}/test`, { method: "POST", body: payload() });
      put(result, testResult({ ok: r.ok, checks: r.checks }));
    } catch (e) { put(result, inlineError(e)); }
  }

  async function remove() {
    if (!(await confirmBox(`Delete ${id}?`, "The table or files stay where they are.", "Delete", true))) return;
    try { await api(`/destinations/${enc(id)}`, { method: "DELETE" }); toast(`${id} deleted`); dr.close(); location.hash = "#/destinations"; }
    catch (e) {
      const jobs = e.data && e.data.jobs;
      put(err, h("div", { class: "err" }, e.message, jobs && jobs.length ? `: ${jobs.join(", ")}` : ""));
    }
  }

  const createBtn = h("button", { class: "btn", onclick: createTable, title: "Runs only when the table is missing" }, icon("table"), "Create table");
  put(dr.foot,
    h("button", { class: "btn", onclick: test }, icon("plug-charging"), "Test"),
    h("button", { class: "btn primary", onclick: async () => { if (await save()) { dr.close(); if (!onSaved) window.dispatchEvent(new CustomEvent("etl:reroute", { detail: { keep: true } })); } } }, icon("floppy-disk"), "Save"),
    createBtn,
    h("span", { class: "grow" }),
    !isNew ? h("button", { class: "btn ghost danger", onclick: remove }, icon("trash"), "Delete") : null);
  draw();
  dr.focus();
  return dr;
}
