// Jobs: the paginated list, the job detail (animated diagram, last runs) and the builder
// wizard (Source, Transform, Destination, Mapping, Schedule and save).
import {
  h, put, icon, api, apiPage, enc, fmt, head, panel, empty, toast, pager, pageState, sect, drawer, dialog, confirmBox,
  statusBadge, statusCell, dataGrid, inlineError, lab, seg, inp, clone, cleanObj, describeCron, every, hashQuery,
  kindIcon, rowsOf, runMs, store,
} from "./ui.js";
import { app, loadConnections, loadKinds, kindOf, kindLabel, isFileKind, startRun, refreshSide, sourceText, targetText } from "./state.js";
import { diagramView, diagramPreview } from "./diagram.js";
import { mappingStep } from "./mapping.js";
import { galleryView, pickFromGallery, openConnection, filesBrowser, tableBrowser, checksRow } from "./pages-sources.js";
import { openDestination } from "./pages-destinations.js";

export async function pageJobs(args) {
  const [a, b, c] = args;
  if (a === "new") return pageBuilder(null, b);
  if (a && b === "edit") return pageBuilder(a, c);
  if (a) return pageJob(a);
  return pageList();
}

// ------------------------------------------------------------------ list
function pageList() {
  const box = h("div");
  const st = pageState("jobs", 25);
  async function draw() {
    if (!box.childElementCount) put(box, h("div", { class: "pb" }, h("div", { class: "sk", style: "height:200px" })));
    let page;
    try { page = await apiPage("/jobs", st); } catch (e) { put(box, h("div", { class: "pb" }, inlineError(e))); return; }
    if (!page.total) { put(box, empty("flow-arrow", "No jobs yet.", h("a", { class: "btn primary", href: "#/jobs/new" }, icon("plus"), "New job"))); return; }
    put(box, h("div", { class: "scroll-x" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", { style: "width:36px" }, h("span", { class: "sr" }, "Status")), h("th", {}, "Job"), h("th", { class: "hide-sm" }, "From"), h("th", { class: "hide-sm" }, "To"),
        h("th", {}, "Last run"), h("th", { class: "hide-sm" }, "Next run"), h("th", {}))),
      h("tbody", {}, page.items.map((j) => {
        const r = j.last_run || {};
        return h("tr", { class: "click", onclick: () => (location.hash = `#/jobs/${enc(j.name)}`) },
          h("td", {}, statusCell(r.status)),
          h("td", {}, h("div", { class: "row nowrap", style: "gap:6px" }, h("a", { class: "mono", href: `#/jobs/${enc(j.name)}`, onclick: (e) => e.stopPropagation() }, j.name),
            j.problems && j.problems.length ? h("span", { class: "badge bad", title: j.problems.join(" ") }, icon("warning"), String(j.problems.length)) : null),
          j.description ? h("div", { class: "faint small", style: "max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, j.description) : null),
          h("td", { class: "mono small hide-sm" }, sourceText(j)),
          h("td", { class: "mono small hide-sm" }, targetText(j)),
          h("td", { class: "small" }, r.started_at ? fmt.ago(r.started_at) : h("span", { class: "faint" }, "Never")),
          h("td", { class: "muted small hide-sm" }, j.next_run ? fmt.ago(j.next_run) : "By hand"),
          h("td", { class: "r" }, h("button", { class: "btn sm", disabled: r.status === "running", "aria-label": `Run ${j.name}`,
            onclick: async (e) => { e.stopPropagation(); if (await startRun(j.name)) draw(); } }, icon("play"), "Run")));
      })))),
    page.total > 10 ? h("div", { class: "pf" }, pager(st, page.total, draw)) : null);
    if (page.items.some((j) => j.last_run && j.last_run.status === "running") && !st.polling) { st.polling = true; every(3000, () => { if (box.isConnected) draw(); }); }
  }
  draw();
  return [head("Jobs", null, h("a", { class: "btn primary", href: "#/jobs/new" }, icon("plus"), "New job")), panel("All jobs", null, box)];
}

// ------------------------------------------------------------------ job detail
async function pageJob(name) {
  const job = await api(`/jobs/${enc(name)}`);
  refreshSide();
  const runsBox = h("div");
  const st = pageState(`job-runs-${name}`, 10);
  const runBtn = h("button", { class: "btn primary", onclick: run, disabled: !!(job.last_run && job.last_run.status === "running") }, icon("play"), "Run");
  // while a run is running, Last runs and the Run button are re-read every 2 s; the poll stops when nothing runs
  let poll = null;
  function watch(busy) {
    runBtn.disabled = busy;
    if (busy && !poll) poll = every(2000, () => { if (runsBox.isConnected) drawRuns(); else { clearInterval(poll); poll = null; } });
    if (!busy && poll) { clearInterval(poll); poll = null; }
  }
  async function drawRuns() {
    let page;
    try { page = await apiPage(`/runs?job=${enc(name)}`, st); } catch (e) { put(runsBox, h("div", { class: "pb" }, inlineError(e))); return; }
    const busy = page.items.some((r) => r.status === "running");
    if (!busy && runBtn.disabled) { try { const j = await api(`/jobs/${enc(name)}`); watch(!!(j.last_run && j.last_run.status === "running")); } catch { watch(false); } }
    else watch(busy);
    put(runsBox, page.total ? h("div", { class: "scroll-x" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "Status"), h("th", {}, "Started"), h("th", { class: "r" }, "Took"), h("th", { class: "r hide-sm" }, "Read"), h("th", { class: "r" }, "Loaded"), h("th", { class: "hide-sm" }, "How"))),
      h("tbody", {}, page.items.map((r) => h("tr", { class: "click", onclick: () => (location.hash = `#/runs/${enc(r.run_id)}`) },
        h("td", {}, statusBadge(r.status)),
        h("td", { class: "small" }, h("a", { href: `#/runs/${enc(r.run_id)}`, onclick: (e) => e.stopPropagation() }, fmt.time(r.started_at))),
        h("td", { class: "r num small" }, fmt.dur(runMs(r))),
        h("td", { class: "r num hide-sm" }, fmt.int(rowsOf(r, "extract"))),
        h("td", { class: "r num" }, fmt.int(rowsOf(r, "load"))),
        h("td", { class: "small muted hide-sm" }, r.trigger || "-"))))))
      : empty("clock-counter-clockwise", "Not run yet.", h("button", { class: "btn primary", onclick: run }, icon("play"), "Run now")),
    page.total > 10 ? h("div", { class: "pf" }, pager(st, page.total, drawRuns)) : null);
  }
  async function run() { if (await startRun(name)) window.dispatchEvent(new CustomEvent("etl:reroute", { detail: { keep: true } })); }
  drawRuns();
  const view = clone(job);
  delete view.last_run; delete view.next_run;
  return [
    head(name, job.description || `${sourceText(job)} to ${targetText(job)}`,
      runBtn,
      h("a", { class: "btn", href: `#/jobs/${enc(name)}/edit` }, icon("pencil-simple"), "Edit")),
    (job.problems || []).length ? h("div", { class: "banner bad" }, icon("warning-octagon"), h("div", { class: "stack tight" }, job.problems.map((p) => h("div", { class: "small" }, typeof p === "string" ? p : p.message)))) : null,
    diagramView(name, job),
    panel("Last runs", h("a", { class: "small", href: `#/stage/${enc(name)}` }, "Staged files"), runsBox),
    sect("job-yaml", "Job settings", { icon: "code" }, h("pre", { class: "code" }, JSON.stringify(view, null, 2))),
  ];
}

// ------------------------------------------------------------------ builder wizard
const STEPS = [["source", "Source"], ["transform", "Transform"], ["destination", "Destination"], ["mapping", "Mapping"], ["schedule", "Schedule and save"]];
const PLAIN_FORMATS = [["csv", "CSV"], ["txt", "Delimited text"], ["fixed", "Fixed width"], ["parquet", "Parquet"], ["json", "JSON"], ["jsonl", "JSON lines"], ["excel", "Excel"]];
const STEP_TYPES = [
  { t: "rename", l: "Rename", ic: "textbox" }, { t: "cast", l: "Change type", ic: "code" }, { t: "filter", l: "Keep rows", ic: "funnel" },
  { t: "derive", l: "Add column", ic: "plus" }, { t: "drop", l: "Drop columns", ic: "trash" }, { t: "dedupe", l: "Remove duplicates", ic: "broom" }, { t: "sql", l: "SQL", ic: "file-sql" },
];
const TYPES = ["VARCHAR", "BIGINT", "INTEGER", "DOUBLE", "DECIMAL(18,2)", "DATE", "TIMESTAMP", "BOOLEAN"];
const PRESETS = [["0 2 * * *", "Nightly 02:00"], ["0 * * * *", "Hourly"], ["0 6 * * 1-5", "Weekdays 06:00"], ["0 3 1 * *", "Monthly"]];
const newStep = (t) => ({ rename: { rename: {} }, cast: { cast: {} }, filter: { filter: "" }, derive: { derive: {} }, drop: { drop: [] },
  dedupe: { dedupe: { key: [], order_by: "" } }, sql: { sql: "SELECT * FROM input" } }[t]);
const stepType = (x) => Object.keys(x)[0];

async function pageBuilder(name, stepArg) {
  await loadKinds();
  const q = hashQuery();
  const [conns, saved, destList, formats] = await Promise.all([
    loadConnections(true),
    name ? api(`/jobs/${enc(name)}`) : Promise.resolve(null),
    api("/destinations").catch(() => []),
    api("/formats").catch(() => []),
  ]);
  refreshSide();
  const isNew = !saved;
  let dests = Array.isArray(destList) ? destList : [];
  const job = saved ? clone(saved) : { name: "", description: "", source: { connection: "", table: "" }, transform: [], target: { mode: "replace" }, notify: "failure" };
  delete job.last_run; delete job.next_run; delete job.problems; delete job.timezone;
  job.source = job.source || {};
  job.target = job.target || {};
  job.transform = job.transform || [];
  const S = job.source;
  const T = job.target;
  if (isNew) {
    if (q.get("connection")) S.connection = q.get("connection");
    if (q.get("format")) S.format = q.get("format");
    if (q.get("layout")) S.options = { ...(S.options || {}), layout: q.get("layout") };
    if (q.get("files")) S.files = q.get("files");
    if (q.get("destination")) T.destination = q.get("destination");
    if (q.get("setup")) {
      if (!S.connection) { const c = conns.find((x) => (x.role || "source") !== "target"); if (c) S.connection = c.id; }
      if (!T.destination && dests[0]) T.destination = dests[0].id;
    }
  }
  const inlineTarget = !!(T.connection && !T.destination);
  let srcMode = S.query ? "query" : "table";
  let incMode = S.incremental ? (S.incremental.watermark ? "watermark" : "window") : "none";
  let step = STEPS.some(([x]) => x === stepArg) ? stepArg : "source";
  let srcCols = [];
  let srcColsErr = null;
  let detected = null;
  let destDetail = null;
  let mapper = null;
  let mapperFor = null;
  const topErr = h("div", { class: "stack tight", role: "alert" });
  const rail = h("nav", { class: "rail", "aria-label": "Job steps" });
  const body = h("div", { class: "stack" });
  const conn = (id) => conns.find((c) => c.id === id);
  const fileFormats = Array.isArray(formats) ? formats : (formats && formats.formats) || [];
  const bankFormats = fileFormats.filter((f) => (f.group || "Bank files") === "Bank files");
  const fmtInfo = (id) => fileFormats.find((f) => f.id === id);
  let layouts = null;

  const go = (x) => {
    step = x;
    const base = isNew ? "#/jobs/new" : `#/jobs/${enc(name)}/edit`;
    const qs = location.hash.includes("?") ? location.hash.slice(location.hash.indexOf("?")) : "";
    history.replaceState(null, "", `${base}/${x}${qs}`);
    draw();
    window.scrollTo(0, 0);
  };

  // ---- source
  function formatSelect() {
    return h("select", { class: "input", onchange: (e) => { S.format = e.target.value; S.options = {}; detected = null; srcCols = []; draw(); loadSrcCols(); } },
      h("optgroup", { label: "Files" }, PLAIN_FORMATS.map(([v, l]) => h("option", { value: v, selected: (S.format || "csv") === v }, l))),
      bankFormats.length ? h("optgroup", { label: "Bank files" }, bankFormats.map((f) => h("option", { value: f.id, selected: S.format === f.id }, f.label))) : null);
  }

  function formatOptions() {
    const f = S.format || "csv";
    const o = S.options = S.options || {};
    if (["csv", "txt"].includes(f)) return h("div", { class: "grid3" },
      lab("Delimiter", inp(o, "delimiter", { mono: true, placeholder: f === "txt" ? "|" : "," })),
      lab("Encoding", inp(o, "encoding", { mono: true, placeholder: "utf-8" })),
      lab("Skip rows", inp(o, "skip_rows", { type: "number", mono: true, placeholder: "0" })),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: o.header !== false, onchange: (e) => { o.header = e.target.checked; } }), "First row is the header"));
    if (f === "fixed") return h("div", { class: "grid2" },
      lab("Widths", h("input", { class: "input mono", value: (o.widths || []).join(", "), placeholder: "10, 8, 30", oninput: (e) => { o.widths = e.target.value.split(",").map((x) => x.trim()).filter(Boolean).map(Number); } }), "Characters per column."),
      lab("Names", h("input", { class: "input mono", value: (o.names || []).join(", "), placeholder: "account, branch, name", oninput: (e) => { o.names = e.target.value.split(",").map((x) => x.trim()).filter(Boolean); } }), "One per width."));
    if (f === "excel") return h("div", { class: "grid2" }, lab("Sheet", inp(o, "sheet", { mono: true, placeholder: "First sheet" })), lab("Skip rows", inp(o, "skip_rows", { type: "number", mono: true, placeholder: "0" })));
    const info = fmtInfo(f);
    if (!info || !(info.options || []).length) return null;
    return h("div", { class: "grid2" }, info.options.filter((x) => x.name !== "layout").map((x) => x.type === "select"
      ? lab(x.label || x.name, h("select", { class: "input", onchange: (e) => { o[x.name] = e.target.value || undefined; } }, h("option", { value: "" }, "Default"),
        (x.choices || []).map((cv) => { const [v, l] = Array.isArray(cv) ? cv : [cv, cv]; return h("option", { value: v, selected: String(o[x.name] ?? "") === String(v) }, l); })), x.help)
      : x.type === "bool" ? h("label", { class: "check" }, h("input", { type: "checkbox", checked: o[x.name] ?? x.default ?? false, onchange: (e) => { o[x.name] = e.target.checked; } }), x.label || x.name)
        : lab(x.label || x.name, inp(o, x.name, { mono: true, type: x.type === "int" ? "number" : "text", placeholder: x.default !== undefined && x.default !== null ? String(x.default) : "" }), x.help)));
  }

  function layoutPicker() {
    const o = S.options = S.options || {};
    if (!layouts) { api("/layouts").then((l) => { layouts = Array.isArray(l) ? l : []; draw(); }).catch(() => { layouts = []; draw(); }); return h("div", { class: "sk", style: "height:34px" }); }
    const cur = layouts.find((l) => l.name === o.layout);
    return h("div", { class: "stack tight" },
      lab("Layout", h("select", { class: "input", onchange: (e) => { o.layout = e.target.value; draw(); loadSrcCols(); } }, h("option", { value: "" }, "Pick a layout"),
        layouts.map((l) => h("option", { value: l.name, selected: l.name === o.layout }, `${l.label || l.name}${l.template ? " (template)" : ""}`))),
      h("a", { href: o.layout ? `#/layouts/${enc(o.layout)}` : "#/layouts" }, "Open layouts")),
      cur && cur.verify ? h("div", { class: "banner" }, icon("warning"), h("div", { class: "small" }, cur.verify)) : null);
  }

  async function detect() {
    const c = conn(S.connection);
    if (!c || !S.files) return;
    detected = "loading"; draw();
    try {
      let file = S.files;
      if (/[*?[]/.test(file)) {
        const list = await api(`/connections/${enc(c.id)}/files?pattern=${enc(file)}&limit=1`);
        file = list && list[0] && list[0].path;
        if (!file) throw new Error("No file matches the pattern.");
      }
      const r = await api("/formats/detect", { method: "POST", body: { connection: c.id, file } });
      const best = (Array.isArray(r) ? r : r.detected || r.results || [])[0];
      detected = best || { none: true };
      if (best && best.format && (!S.format || S.format === "csv")) { S.format = best.format; S.options = { ...(best.options || {}) }; }
    } catch (e) { detected = e; }
    draw();
    loadSrcCols();
  }

  function srcBody() {
    const s = { connection: S.connection };
    const c = conn(S.connection);
    if (c && isFileKind(c.kind)) { s.files = S.files; s.format = S.format || "csv"; s.options = cleanObj(S.options); }
    else if (srcMode === "query") s.query = S.query; else s.table = S.table;
    return s;
  }
  async function loadSrcCols() {
    const c = conn(S.connection);
    if (!c || !(S.table || S.query || S.files)) { srcCols = []; draw(); return; }
    srcCols = "loading"; srcColsErr = null; draw();
    try { const r = await api("/sources/columns", { method: "POST", body: srcBody() }); srcCols = r.columns || []; }
    catch (e) { srcCols = []; srcColsErr = e; }
    if (step === "source") draw();
  }

  function srcColumnsBlock() {
    if (srcCols === "loading") return h("div", { class: "sk", style: "height:120px" });
    if (srcColsErr) return h("div", { class: "err" }, srcColsErr.message);
    if (!srcCols.length) return null;
    const st = pageState("src-cols", 10);
    const box = h("div", { class: "stack tight" });
    const drawCols = () => put(box, h("div", { class: "scroll-x" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "Column"), h("th", {}, "Type"), h("th", { class: "hide-sm" }, "Samples"))),
      h("tbody", {}, srcCols.slice(st.offset, st.offset + st.limit).map((x) => h("tr", {},
        h("td", {}, h("span", { class: "mono small" }, x.name), x.personal ? h("span", { class: "pchip", style: "margin-left:6px" }, "Personal") : null),
        h("td", { class: "mono small muted" }, x.type),
        h("td", { class: "mono small faint hide-sm", style: "max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, (x.samples || []).slice(0, 3).map((v) => (v === null ? "null" : String(v))).join(", "))))))),
    srcCols.length > 10 ? pager(st, srcCols.length, drawCols) : null);
    drawCols();
    return h("div", { class: "stack tight" }, h("div", { class: "small muted" }, `${srcCols.length} source columns`), box);
  }

  async function addConnection() {
    const dr = drawer({ title: "Add a source", wide: true });
    put(dr.body, h("div", { class: "sk", style: "height:300px" }));
    put(dr.body, await galleryView({ roles: ["source"], onPick: (card) => {
      dr.close();
      pickFromGallery(card, async (savedConn) => {
        await loadConnections(true).then((l) => { conns.splice(0, conns.length, ...l); });
        S.connection = savedConn.id;
        if (savedConn.format) S.format = savedConn.format;
        if (savedConn.layout) S.options = { ...(S.options || {}), layout: savedConn.layout };
        draw();
      });
    } }));
  }

  async function browseInto() {
    const c = conn(S.connection);
    if (!c) return;
    const dl = dialog(`Browse ${c.id}`, true);
    put(dl.box, h("div", { class: "sk", style: "height:240px" }));
    const close = h("div", { class: "row" }, h("span", { class: "grow" }), h("button", { class: "btn", onclick: dl.close }, "Close"));
    if (isFileKind(c.kind)) {
      put(dl.box, filesBrowser(c, { onUse: (path, det) => { S.files = path; if (det && det.format) { S.format = det.format; detected = det; } dl.close(); draw(); loadSrcCols(); } }), close);
    } else {
      put(dl.box, await tableBrowser(c, (tname) => { S.table = tname; srcMode = "table"; dl.close(); draw(); loadSrcCols(); }), close);
    }
  }

  function sourceStep() {
    const c = conn(S.connection);
    const k = c ? kindOf(c.kind) || {} : {};
    const files = c && isFileKind(c.kind);
    const remote = c && k.category === "remote";
    const sources = conns.filter((x) => (x.role || "source") !== "target");
    const inc = S.incremental = S.incremental || {};
    const pk = S.pickup = S.pickup || {};
    const info = fmtInfo(S.format);
    return [
      h("div", { class: "grid2" },
        lab("Name", inp(job, "name", { mono: true, placeholder: "customers_daily", autocomplete: "off", disabled: !isNew ? true : null }), isNew ? "Letters, digits and _." : null),
        lab("Description", inp(job, "description", { placeholder: "Optional" }))),
      h("div", { class: "row", style: "align-items:flex-end" },
        h("div", { class: "grow", style: "min-width:220px" }, lab("Connection", h("select", { class: "input", onchange: (e) => { S.connection = e.target.value; S.table = ""; S.files = ""; srcCols = []; detected = null; draw(); } },
          h("option", { value: "" }, sources.length ? "Pick one" : "No source connections yet"),
          sources.map((x) => h("option", { value: x.id, selected: x.id === S.connection }, `${x.id} (${kindLabel(x.kind)})`))))),
        h("button", { class: "btn", onclick: addConnection }, icon("plus"), "Add a connection")),
      !c ? null : files ? [
        h("div", { class: "row", style: "align-items:flex-end" },
          h("div", { class: "grow", style: "min-width:200px" }, lab("Files", h("input", { class: "input mono", value: Array.isArray(S.files) ? S.files.join(", ") : S.files || "", placeholder: "bank/statement_*.txt",
            onchange: (e) => { S.files = e.target.value; detected = null; loadSrcCols(); } }), "A name or a pattern.")),
          h("div", { style: "min-width:200px" }, lab("Format", formatSelect())),
          h("button", { class: "btn", onclick: detect }, icon("magnifying-glass"), "Detect"),
          h("button", { class: "btn ghost", onclick: browseInto }, icon("folder-open"), "Browse")),
        detected === "loading" ? h("div", { class: "row faint small" }, h("span", { class: "spin", "aria-hidden": "true" }), "Reading the first 64 KB")
          : detected instanceof Error ? h("div", { class: "err" }, detected.message)
            : detected && detected.none ? h("span", { class: "chip static", style: "align-self:flex-start" }, "No bank format found")
              : detected ? h("span", { class: "chip on static", style: "align-self:flex-start" }, icon("seal-check"), `Detected: ${String(detected.format).toUpperCase()} (${Number(detected.confidence).toFixed(2)})`) : null,
        info && info.verify_note ? h("div", { class: "banner" }, icon("warning"), h("div", { class: "small" }, info.verify_note)) : null,
        S.format === "layout" || (info && info.needs_layout) ? layoutPicker() : null,
        formatOptions() ? sect("job-format-options", "Format options", { flat: true }, formatOptions()) : null,
        remote || k.kind === "share" ? sect("job-pickup", "Pickup", { flat: true },
          h("div", { class: "grid3" },
            h("label", { class: "check" }, h("input", { type: "checkbox", checked: pk.skip_seen !== false, onchange: (e) => { pk.skip_seen = e.target.checked; } }), "Skip files already loaded"),
            lab("Wait after a change (s)", inp(pk, "min_age_seconds", { type: "number", mono: true, placeholder: "60" })),
            lab("At most files", inp(pk, "max_files", { type: "number", mono: true, placeholder: "500" }))),
          h("div", { class: "grid2" },
            lab("After a good load", h("select", { class: "input", onchange: (e) => { pk.after = e.target.value; draw(); } },
              [["keep", "Keep the file"], ["archive", "Move to an archive folder"], ["mark_done", "Write a .done marker"], ["delete", "Delete it"]].map(([v, l]) => h("option", { value: v, selected: (pk.after || "keep") === v }, l))),
              pk.after && pk.after !== "keep" ? "Needs allow_after_actions on the connection." : null),
            pk.after === "archive" ? lab("Archive folder", inp(pk, "archive_to", { mono: true, placeholder: "done/" })) : h("span"))) : null,
      ] : [
        seg("Read", [["table", k.category === "document" ? "A collection" : "A table"], ["query", "A query"]], srcMode, (v) => { srcMode = v; draw(); }),
        srcMode === "query" ? lab("Query", h("textarea", { class: "input mono", rows: 6, placeholder: k.category === "document" ? "{\"filter\": {}}" : "SELECT ... FROM ...",
          onchange: (e) => { S.query = e.target.value; loadSrcCols(); } }, S.query || ""), k.category === "document" ? "A filter or a pipeline, read only." : "One SELECT. Anything else is refused.")
          : h("div", { class: "row", style: "align-items:flex-end" },
            h("div", { class: "grow" }, lab(k.category === "document" ? "Collection" : "Table", h("input", { class: "input mono", value: S.table || "", placeholder: "schema.table", onchange: (e) => { S.table = e.target.value; loadSrcCols(); } }))),
            h("button", { class: "btn", onclick: browseInto }, icon("magnifying-glass"), "Browse")),
        sect("job-rows", "Rows to read", { flat: true },
          seg("Rows", [["none", "All"], ["window", "Last days"], ["watermark", "New since last run"]], incMode, (v) => {
            incMode = v;
            if (v === "watermark") { inc.watermark = true; delete inc.lookback_days; }
            if (v === "window") { delete inc.watermark; inc.lookback_days = inc.lookback_days || 3; }
            draw();
          }),
          incMode !== "none" ? h("div", { class: "grid2" },
            lab("On column", h("input", { class: "input mono", value: inc.column || "", oninput: (e) => { inc.column = e.target.value; } }), "Should be indexed."),
            incMode === "window" ? lab("Days back", inp(inc, "lookback_days", { type: "number", mono: true, min: 1 })) : h("span")) : null,
          lab("Rows per chunk", inp(S, "chunk_rows", { type: "number", mono: true, placeholder: "100000" }))),
      ],
      c ? h("div", { class: "row" }, h("button", { class: "btn sm", onclick: loadSrcCols }, icon("eye"), "Read columns")) : null,
      srcColumnsBlock(),
    ];
  }

  // ---- transform
  const trPreview = h("div");
  function pairEditor(map, keyPh, valPh) {
    const rows = Object.entries(map);
    return h("div", { class: "pairs" }, rows.map(([k, v], i) => h("div", { class: "row" },
      h("input", { class: "input mono", value: k, placeholder: keyPh, "aria-label": keyPh, onchange: (e) => { const en = Object.entries(map); en[i][0] = e.target.value; for (const x of Object.keys(map)) delete map[x]; for (const [a, b] of en) map[a] = b; } }),
      icon("arrow-right", "faint"),
      h("input", { class: "input mono", value: v, placeholder: valPh, "aria-label": valPh, list: keyPh === "column" ? "tr-types" : null, oninput: (e) => { map[k] = e.target.value; } }),
      h("button", { class: "btn ghost sm icon", "aria-label": "Remove", onclick: () => { delete map[k]; draw(); } }, icon("x")))),
    h("div", {}, h("button", { class: "btn sm", onclick: () => { let n = ""; let i = 1; while (n in map) n = `column_${i++}`; map[n] = ""; draw(); } }, icon("plus"), "Add")));
  }
  const listInput = (arr, set, ph) => h("input", { class: "input mono", value: (arr || []).join(", "), placeholder: ph, oninput: (e) => set(e.target.value.split(",").map((x) => x.trim()).filter(Boolean)) });
  function stepEditor(x) {
    const t = stepType(x);
    if (t === "rename") return pairEditor(x.rename, "old name", "new name");
    if (t === "cast") return pairEditor(x.cast, "column", "type");
    if (t === "derive") return pairEditor(x.derive, "new column", "expression");
    if (t === "filter") return h("input", { class: "input mono", value: x.filter, placeholder: "segment IS NOT NULL", "aria-label": "Condition", oninput: (e) => { x.filter = e.target.value; } });
    if (t === "drop") return listInput(x.drop, (v) => { x.drop = v; }, "column_a, column_b");
    if (t === "dedupe") return h("div", { class: "grid2" },
      lab("Key", listInput(x.dedupe.key, (v) => { x.dedupe.key = v; }, "customer_id")),
      lab("Keep first by", h("input", { class: "input mono", value: x.dedupe.order_by || "", placeholder: "updated_at DESC", oninput: (e) => { x.dedupe.order_by = e.target.value; } })));
    if (t === "sql") return h("textarea", { class: "input mono", rows: 4, "aria-label": "SQL", oninput: (e) => { x.sql = e.target.value; } }, x.sql);
    return h("span", { class: "faint" }, JSON.stringify(x));
  }
  function transformStep() {
    const steps = job.transform;
    const move = (i, d) => { const j = i + d; if (j < 0 || j >= steps.length) return; [steps[i], steps[j]] = [steps[j], steps[i]]; draw(); };
    return [
      h("p", { class: "muted small" }, "Optional. Applied in order on the staged file."),
      sect("job-transform", `Steps (${steps.length})`, { open: steps.length > 0, icon: "tree-structure" },
        steps.length ? h("div", { class: "tsteps" }, steps.map((x, i) => {
          const meta = STEP_TYPES.find((y) => y.t === stepType(x)) || { l: stepType(x) };
          return h("div", { class: "tstep" }, h("span", { class: "n" }, String(i + 1)), h("div", { style: "padding-top:6px;font-weight:550;font-size:13px" }, meta.l), stepEditor(x),
            h("div", { class: "acts" },
              h("button", { class: "btn ghost sm icon", "aria-label": "Move up", disabled: i === 0, onclick: () => move(i, -1) }, icon("arrow-up")),
              h("button", { class: "btn ghost sm icon", "aria-label": "Move down", disabled: i === steps.length - 1, onclick: () => move(i, 1) }, icon("arrow-down")),
              h("button", { class: "btn ghost sm icon", "aria-label": "Remove step", onclick: () => { steps.splice(i, 1); draw(); } }, icon("trash"))));
        })) : h("p", { class: "faint small" }, "No steps: the staged rows go on as they are."),
        h("div", { class: "row" }, STEP_TYPES.map((y) => h("button", { class: "btn sm", onclick: () => { steps.push(newStep(y.t)); draw(); } }, icon(y.ic), y.l))),
        h("div", { class: "row" }, h("button", { class: "btn sm", onclick: previewTransform, disabled: isNew }, icon("eye"), "Preview the result"),
          h("span", { class: "help" }, isNew ? "Save and run the extract once to preview." : "Runs on the last staged extract.")),
        h("datalist", { id: "tr-types" }, TYPES.map((x) => h("option", { value: x }))),
        trPreview),
    ];
  }
  async function previewTransform() {
    put(trPreview, h("div", { class: "sk", style: "height:160px" }));
    try {
      const p = await api(`/jobs/${enc(name)}/transform-preview`, { method: "POST", body: payload() });
      put(trPreview, h("div", { class: "stack tight" }, p.rows_in !== undefined ? h("div", { class: "faint small" }, `${fmt.int(p.rows_in)} rows in, ${fmt.int(p.rows_out)} out`) : null, dataGrid(p)));
    } catch (e) { put(trPreview, h("div", { class: "err" }, e.message)); }
  }

  // ---- destination
  async function loadDestDetail() {
    if (!T.destination) { destDetail = null; return; }
    if (destDetail && destDetail.id === T.destination) return;
    destDetail = { id: T.destination, loading: true };
    try { destDetail = { ...(await api(`/destinations/${enc(T.destination)}`)), id: T.destination }; }
    catch (e) { destDetail = { id: T.destination, error: e }; }
    if (mapper && mapperFor !== T.destination) { mapper = null; }
    draw();
  }
  async function createDestination() {
    await openDestination({ onSaved: async (id) => {
      dests = await api("/destinations").catch(() => dests);
      T.destination = id; destDetail = null; mapper = null; loadDestDetail(); draw();
    } });
  }
  function destinationStep() {
    if (inlineTarget && !T.destination) {
      return [h("div", { class: "banner info" }, icon("info"), h("div", { class: "grow small" }, `Writes to ${T.connection}: ${T.table || T.file || T.format || ""}. This older form is kept as it is.`)),
        seg("Mode", [["replace", "Replace"], ["append", "Append"], ["upsert", "Upsert"]], T.mode || "replace", (v) => { T.mode = v; draw(); })];
    }
    const dd = destDetail && destDetail.id === T.destination ? destDetail : null;
    if (T.destination && !dd) loadDestDetail();
    const cols = (dd && dd.columns) || [];
    const keys = new Set(T.key || (dd && dd.keys) || []);
    return [
      h("div", { class: "row", style: "align-items:flex-end" },
        h("div", { class: "grow", style: "min-width:220px" }, lab("Destination", h("select", { class: "input", onchange: (e) => { T.destination = e.target.value; delete T.key; delete T.slice_column; destDetail = null; mapper = null; draw(); } },
          h("option", { value: "" }, dests.length ? "Pick one" : "No destinations yet"),
          dests.map((x) => h("option", { value: x.id, selected: x.id === T.destination }, `${x.id} (${x.connection})`))))),
        h("button", { class: "btn", onclick: createDestination }, icon("plus"), "Create one")),
      dd && dd.loading ? h("div", { class: "sk", style: "height:60px" }) : dd && dd.error ? h("div", { class: "err" }, dd.error.message)
        : dd ? h("div", { class: "kv" },
          h("div", {}, h("span", { class: "k" }, "Connection"), h("span", { class: "v" }, dd.connection || "-")),
          h("div", {}, h("span", { class: "k" }, dd.table ? "Table" : "Path"), h("span", { class: "v", title: dd.table || dd.path || "" }, dd.table || [dd.path, dd.file].filter(Boolean).join("/") || "-")),
          h("div", {}, h("span", { class: "k" }, "Columns"), h("span", { class: "v" }, String(cols.length))),
          h("div", {}, h("span", { class: "k" }, "Exists"), h("span", { class: "v" }, dd.exists ? "Yes" : "Not yet"))) : null,
      T.destination ? seg("Mode", [["replace", "Replace"], ["append", "Append"], ["upsert", "Upsert"]], T.mode || "replace", (v) => { T.mode = v; draw(); }) : null,
      T.destination ? h("p", { class: "help" }, { replace: "Removes the old rows and loads the new ones in one transaction.", append: "Replaces the window's rows, then inserts. Re-runs never duplicate.", upsert: "Updates rows that match the key, inserts the rest." }[T.mode || "replace"]) : null,
      T.mode === "upsert" && cols.length ? h("div", { class: "f" }, h("span", {}, "Key"), h("div", { class: "chips" }, cols.map((c) => h("button", { class: `chip mono${keys.has(c.name) ? " on" : ""}`, "aria-pressed": String(keys.has(c.name)),
        onclick: () => { if (keys.has(c.name)) keys.delete(c.name); else keys.add(c.name); T.key = cols.map((x) => x.name).filter((n) => keys.has(n)); draw(); } }, c.name)))) : null,
      T.mode === "append" && cols.length ? lab("Window column", h("select", { class: "input", onchange: (e) => { T.slice_column = e.target.value || undefined; } }, h("option", { value: "" }, "The source's incremental column"),
        cols.map((c) => h("option", { value: c.name, selected: c.name === T.slice_column }, c.name)))) : null,
    ];
  }

  // ---- mapping
  function mappingView() {
    if (!T.destination) return [empty("arrows-left-right", "Pick a destination first.", h("button", { class: "btn", onclick: () => go("destination") }, "Destination"))];
    const dd = destDetail && destDetail.id === T.destination ? destDetail : null;
    if (!dd || dd.loading) { loadDestDetail(); return [h("div", { class: "sk", style: "height:240px" })]; }
    if (!(dd.columns || []).length) return [empty("arrows-left-right", "This destination takes the columns as they come.")];
    if (!mapper || mapperFor !== T.destination) {
      mapper = mappingStep({ job, destCols: dd.columns, previewName: () => job.name || name, payload, onChange: drawRail });
      mapperFor = T.destination;
    }
    return [mapper.el];
  }

  // ---- schedule
  function scheduleStep() {
    const reading = h("div");
    const show = () => { const d = describeCron(job.schedule); put(reading, h("div", { class: `cron-read${d.ok ? "" : " bad"}` }, icon(d.ok ? "calendar-blank" : "warning"), d.text)); };
    show();
    return [
      h("div", { class: "grid2" },
        lab("When", h("input", { class: "input mono", value: job.schedule || "", placeholder: "0 2 * * *", "aria-label": "Cron", oninput: (e) => { job.schedule = e.target.value; show(); } }), "Cron, in the project's time zone. Empty: by hand."),
        h("div", { class: "f" }, h("span", {}, "Quick picks"), h("div", { class: "chips" }, PRESETS.map(([v, l]) => h("button", { class: `chip${job.schedule === v ? " on" : ""}`, onclick: () => { job.schedule = v; draw(); } }, l)),
          h("button", { class: `chip${!job.schedule ? " on" : ""}`, onclick: () => { job.schedule = ""; draw(); } }, "By hand")))),
      reading,
      seg("Tell me", [["never", "Never"], ["failure", "When it fails"], ["always", "Every run"]], job.notify || "failure", (v) => { job.notify = v; }),
      h("div", { class: "stack tight" }, h("div", { class: "small muted" }, "Flow"), diagramPreview(payload())),
    ];
  }

  // ---- payload and save
  function payload() {
    const j = clone(job);
    j.name = (j.name || "").trim();
    const s = j.source;
    const c = conn(s.connection);
    if (c && isFileKind(c.kind)) {
      delete s.table; delete s.query; delete s.columns; delete s.incremental;
      s.format = s.format || "csv";
      s.options = cleanObj(s.options);
      if (!Object.keys(s.options).length) delete s.options;
      const pk = cleanObj(s.pickup);
      if (pk.after === "keep") delete pk.after;
      if (pk.skip_seen === true) delete pk.skip_seen;
      if (pk.after !== "archive") delete pk.archive_to;
      if (Object.keys(pk).length && kindOf(c.kind) && (kindOf(c.kind).category === "remote" || c.kind === "share")) s.pickup = pk; else delete s.pickup;
    } else {
      delete s.files; delete s.format; delete s.options; delete s.pickup;
      if (srcMode === "query") delete s.table; else delete s.query;
      if (incMode === "none") delete s.incremental;
      else if (incMode === "watermark") s.incremental = { column: s.incremental.column, watermark: true };
      else s.incremental = { column: s.incremental.column, lookback_days: Number(s.incremental.lookback_days) || 1 };
    }
    if (!s.chunk_rows) delete s.chunk_rows;
    const t = j.target;
    if (t.destination) { delete t.connection; delete t.table; delete t.format; delete t.create; }
    if (t.mode !== "upsert") delete t.key;
    if (t.mode !== "append" || !t.slice_column) delete t.slice_column;
    j.transform = (j.transform || []).map(cleanStep).filter(Boolean);
    if (!j.transform.length) delete j.transform;
    if (!j.mapping || !j.mapping.columns || !Object.keys(j.mapping.columns).length || !t.destination) delete j.mapping;
    else {
      if (j.mapping.unmapped === "default") delete j.mapping.unmapped;
      if (j.mapping.on_cast_error === "error") delete j.mapping.on_cast_error;
    }
    for (const k of ["schedule", "description"]) if (!j[k]) delete j[k];
    return j;
  }

  async function save(thenRun) {
    put(topErr);
    const p = payload();
    if (!p.name) { go("source"); put(topErr, h("div", { class: "banner bad" }, icon("warning-octagon"), h("div", {}, "Give the job a name."))); return; }
    if (mapper && mapper.errors().length && p.mapping !== undefined) {
      go("mapping");
      toast("Fix the mapping errors first.", true);
      setTimeout(() => mapper.focusFirstError(), 50);
      return;
    }
    try {
      const out = await api(`/jobs/${enc(isNew ? p.name : name)}`, { method: "PUT", body: p });
      const n = (out && out.name) || p.name;
      toast(`${n} saved`);
      if (thenRun) await startRun(n);
      location.hash = `#/jobs/${enc(n)}`;
    } catch (e) {
      const problems = (e.data && e.data.problems) || [e.message];
      put(topErr, h("div", { class: "banner bad" }, icon("warning-octagon"), h("div", { class: "stack tight" }, problems.map((x) => h("div", { class: "small" }, typeof x === "string" ? x : `${x.column ? `${x.column}: ` : ""}${x.message}`)))));
      window.scrollTo(0, 0);
    }
  }

  async function remove() {
    if (!(await confirmBox(`Delete ${name}?`, "Its run history stays.", "Delete", true))) return;
    try { await api(`/jobs/${enc(name)}`, { method: "DELETE" }); toast(`${name} deleted`); location.hash = "#/jobs"; } catch (e) { toast(e.message, true); }
  }

  // ---- rail and frame
  function stepState(x) {
    if (x === "source") return S.connection && (S.table || S.query || S.files) ? "ok" : null;
    if (x === "destination") return T.destination || inlineTarget ? "ok" : null;
    if (x === "mapping") { if (!mapper) return null; return mapper.errors().length ? "bad" : (mapper.counts().mapped ? "ok" : null); }
    return null;
  }
  function drawRail() {
    put(rail, STEPS.map(([x, l], i) => {
      const st = stepState(x);
      return h("button", { class: step === x ? "on" : "", "aria-current": step === x ? "step" : null, onclick: () => go(x) },
        h("span", { class: "n" }, String(i + 1)), h("span", {}, l),
        st ? h("span", { class: `st ${st}` }, icon(st === "bad" ? "warning-circle" : "check-circle"), h("span", { class: "sr" }, st === "bad" ? "has errors" : "done")) : null);
    }));
  }
  function draw() {
    const i = STEPS.findIndex(([x]) => x === step);
    const content = { source: sourceStep, transform: transformStep, destination: destinationStep, mapping: mappingView, schedule: scheduleStep }[step]();
    const last = i === STEPS.length - 1;
    put(body, topErr,
      panel(h("div", { class: "row" }, h("h2", {}, STEPS[i][1]), step === "transform" ? h("span", { class: "badge" }, "Optional") : null),
        !isNew ? h("button", { class: "btn sm", onclick: () => save(false) }, icon("floppy-disk"), "Save") : null,
        h("div", { class: "pb stack" }, content)),
      h("div", { class: "wiz-foot" },
        i > 0 ? h("button", { class: "btn", onclick: () => go(STEPS[i - 1][0]) }, icon("arrow-left"), "Back") : null,
        h("span", { class: "grow" }),
        last && !isNew ? h("button", { class: "btn ghost danger", onclick: remove }, icon("trash"), "Delete") : null,
        last ? h("button", { class: "btn", onclick: () => save(false) }, icon("floppy-disk"), "Save") : null,
        last ? h("button", { class: "btn primary", onclick: () => save(true) }, icon("play"), "Save and run")
          : h("button", { class: "btn primary", onclick: () => go(STEPS[i + 1][0]) }, "Next", icon("arrow-right"))));
    drawRail();
  }

  if (T.destination) loadDestDetail();
  draw();
  if (S.connection && (S.table || S.files || S.query)) loadSrcCols();
  return [
    head(isNew ? "New job" : name, isNew ? "Source, destination, mapping." : `${sourceText(job)} to ${targetText(job)}`,
      !isNew ? h("a", { class: "btn ghost", href: `#/jobs/${enc(name)}` }, icon("x"), "Close") : h("a", { class: "btn ghost", href: "#/jobs" }, icon("x"), "Cancel")),
    h("div", { class: "wizard" }, rail, body),
  ];
}

function cleanStep(x) {
  const t = stepType(x);
  const v = x[t];
  if (["rename", "cast", "derive"].includes(t)) {
    const m = {};
    for (const [k, y] of Object.entries(v || {})) if (k.trim() && String(y).trim()) m[k.trim()] = String(y).trim();
    return Object.keys(m).length ? { [t]: m } : null;
  }
  if (t === "filter" || t === "sql") return String(v || "").trim() ? { [t]: String(v).trim() } : null;
  if (t === "drop") return v && v.length ? { drop: v } : null;
  if (t === "dedupe") {
    if (!v.key || !v.key.length) return null;
    return { dedupe: v.order_by ? { key: v.key, order_by: v.order_by } : { key: v.key } };
  }
  return x;
}
