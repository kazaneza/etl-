// Layouts for fixed-width multi-record files: the list, the editor with record sections,
// the field table, a ruler preview, a test on a real file and the YAML (JSON form) view.
import {
  h, put, icon, api, enc, head, panel, empty, emptyState, toast, pager, pageState, sect, dialog, confirmBox, inlineError, lab, clone, dataGrid, pagedGrid,
} from "./ui.js";
import { loadConnections, isFileKind, loadKinds } from "./state.js";
import { checksRow } from "./pages-sources.js";

const FIELD_TYPES = ["string", "integer", "decimal", "date", "time", "timestamp", "boolean"];
const NAME_RE = /^[a-z0-9_]{1,60}$/;

export async function pageLayouts(args) {
  if (args[0]) return pageLayout(args[0]);
  let list;
  const sub = "Fixed-width files with several record types.";
  try { list = await api("/layouts"); } catch (e) { return [head("Layouts", sub), inlineError(e)]; }
  if (!list.length) {
    return [head("Layouts", sub), emptyState({ art: "layouts", title: "No layouts yet",
      text: "A layout describes a fixed-width file: its record types and where each field starts and ends." })];
  }
  const st = pageState("layouts", 25);
  const box = h("div");
  function draw() {
    if (!list.length) { put(box, empty("rows", "No layouts yet.")); return; }
    put(box, h("div", { class: "scroll-x" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "Layout"), h("th", {}, "Kind"), h("th", { class: "hide-sm" }, "Name"))),
      h("tbody", {}, list.slice(st.offset, st.offset + st.limit).map((l) => h("tr", { class: "click", onclick: () => (location.hash = `#/layouts/${enc(l.name)}`) },
        h("td", {}, h("div", { class: "row nowrap", style: "gap:8px" }, h("a", { href: `#/layouts/${enc(l.name)}`, onclick: (e) => e.stopPropagation() }, l.label || l.name),
          l.verify ? h("span", { style: "color:var(--gold)", title: l.verify }, icon("warning"), h("span", { class: "sr" }, "Check before use")) : null)),
        h("td", {}, l.template || l.source === "builtin" ? h("span", { class: "badge warn" }, "Template") : h("span", { class: "badge info" }, "Project")),
        h("td", { class: "mono small muted hide-sm" }, l.name)))))),
    list.length > 10 ? h("div", { class: "pf" }, pager(st, list.length, draw)) : null);
  }
  draw();
  return [head("Layouts", sub), panel("All layouts", null, box)];
}

// ------------------------------------------------------------------ pure helpers (tested)
export function fieldLength(f) {
  if (f.length !== undefined && f.length !== null && f.length !== "") return Number(f.length);
  if (f.end !== undefined && f.end !== null && f.end !== "") return Number(f.end) - Number(f.start) + 1;
  return 0;
}
// indexes of fields that overlap another field of the same record
export function overlaps(fields) {
  const bad = new Set();
  const spans = fields.map((f, i) => ({ i, a: Number(f.start), b: Number(f.start) + fieldLength(f) - 1 }));
  for (const x of spans) for (const y of spans) if (x.i < y.i && x.a <= y.b && y.a <= x.b) { bad.add(x.i); bad.add(y.i); }
  return bad;
}
// a made-up sample line that fits the fields: the record code, digits for numbers, the name for text
export function sampleLine(layout, rec) {
  const fields = rec.fields || [];
  const end = Math.max(0, ...fields.map((f) => Number(f.start) + fieldLength(f) - 1), layout.record_type ? layout.record_type.start + layout.record_type.length - 1 : 0);
  const width = Math.min(layout.line_length || end, 400);
  const chars = Array(Math.max(width, end)).fill(" ");
  const putAt = (start, text, len) => { for (let i = 0; i < len; i++) chars[start - 1 + i] = text[i] ?? " "; };
  for (const f of fields) {
    const len = fieldLength(f);
    let t;
    if (["integer", "decimal"].includes(f.type)) t = "0".repeat(Math.max(0, len - 3)) + "125".slice(0, len);
    else if (f.type === "date") t = "20261101".padEnd(len, " ");
    else if (f.type === "time") t = "143000".padEnd(len, " ");
    else if (f.type === "boolean") t = "Y";
    else t = String(f.name || "").toUpperCase().replace(/[^A-Z0-9]/g, "").padEnd(len, ".");
    putAt(Number(f.start), t, len);
  }
  if (layout.record_type) {
    const code = Array.isArray(rec.match) ? rec.match[0] : rec.match;
    if (code) putAt(layout.record_type.start, String(code), layout.record_type.length);
  }
  return chars.join("").slice(0, Math.max(width, end));
}

function ruler(layout, rec, line) {
  const fields = rec.fields || [];
  const over = overlaps(fields);
  const n = Math.max(line.length, 10);
  const sc = Array(n).fill(" ");
  let ticks = "";
  for (let i = 1; i <= n; i++) {
    ticks += i % 10 === 0 ? "|" : i % 5 === 0 ? "+" : ".";
    // each number ends on its own position: 10 sits over column 10
    if (i % 10 === 0 || i === 1) { const t = String(i); for (let k = 0; k < t.length; k++) sc[i - t.length + k] = t[k]; }
  }
  const scale = sc.join("");
  return h("div", { class: "ruler-wrap" }, h("div", { class: "ruler", style: `width:${n}ch` },
    h("div", { class: "scale", "aria-hidden": "true" }, scale),
    h("div", { class: "ticks", "aria-hidden": "true" }, ticks),
    h("div", { class: "line" },
      fields.map((f, i) => {
        const len = fieldLength(f);
        const label = `${f.name}: ${f.start} to ${Number(f.start) + len - 1}, ${f.type || "string"}${over.has(i) ? ", overlaps" : ""}`;
        return h("span", { class: `fld${i % 2 ? " alt" : ""}${over.has(i) ? " over" : ""}`, tabindex: "0", role: "img", "aria-label": label,
          style: `left:${Number(f.start) - 1}ch;width:${Math.max(len, 1)}ch` }, h("span", { class: "tip" }, label));
      }),
      h("span", { class: "txt" }, line))));
}

// ------------------------------------------------------------------ editor
async function pageLayout(name) {
  let layout;
  try { layout = await api(`/layouts/${enc(name)}`); } catch (e) { return [head(name, "Fixed-width layout"), inlineError(e)]; }
  const listRow = (await api("/layouts").catch(() => [])).find((l) => l.name === name) || {};
  const readOnly = !!(layout.template || listRow.template || listRow.source === "builtin");
  const L = clone(layout);
  L.records = L.records || [];
  const samples = {};
  const err = h("div", { class: "stack tight", role: "alert" });
  const recBox = h("div", { class: "stack" });
  const dis = readOnly ? true : null;

  // each record's fields page by ten; the page is kept while the editor redraws
  const fieldPages = {};
  function fieldsTable(rec, ri) {
    const over = overlaps(rec.fields || []);
    const set = (f, k, v) => { f[k] = v; drawRecords(); };
    const fst = fieldPages[ri] || (fieldPages[ri] = pageState(`layout-fields`, 10));
    const all = rec.fields || [];
    if (fst.offset >= all.length) fst.offset = Math.max(0, Math.floor((all.length - 1) / fst.limit) * fst.limit);
    const shown = all.map((f, i) => [f, i]).slice(fst.offset, fst.offset + fst.limit);
    return h("div", { class: "stack tight" }, h("div", { class: "scroll-x" }, h("table", { class: "t fields-t" },
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Start"), h("th", {}, "Length"), h("th", {}, "Type"), h("th", {}, "Options"), h("th", {}, h("span", { class: "sr" }, "Remove")))),
      h("tbody", {}, shown.map(([f, i]) => h("tr", { style: over.has(i) ? "background:var(--danger-soft)" : null },
        h("td", {}, h("input", { class: "input sm mono", value: f.name || "", disabled: dis, "aria-label": `Field ${i + 1} name`, onchange: (e) => set(f, "name", e.target.value) })),
        h("td", {}, h("input", { class: "input sm mono", type: "number", min: 1, value: f.start ?? "", disabled: dis, "aria-label": `${f.name} start`, style: "width:72px", onchange: (e) => set(f, "start", Number(e.target.value)) })),
        h("td", {}, h("input", { class: "input sm mono", type: "number", min: 1, value: fieldLength(f) || "", disabled: dis, "aria-label": `${f.name} length`, style: "width:72px", onchange: (e) => { delete f.end; set(f, "length", Number(e.target.value)); } })),
        h("td", {}, h("select", { class: "input sm", disabled: dis, "aria-label": `${f.name} type`, onchange: (e) => set(f, "type", e.target.value) }, FIELD_TYPES.map((t) => h("option", { value: t, selected: (f.type || "string") === t }, t)))),
        h("td", {}, h("details", {}, h("summary", { class: "small", style: "cursor:pointer;color:var(--ink-2)" }, optionSummary(f)),
          h("div", { class: "stack tight", style: "padding:8px 0;min-width:220px" },
            ["date", "time", "timestamp"].includes(f.type) ? lab("Format", h("input", { class: "input sm mono", value: f.format || "", placeholder: "%Y%m%d", disabled: dis, onchange: (e) => { f.format = e.target.value || undefined; } })) : null,
            f.type === "decimal" ? h("div", { class: "grid2" },
              lab("Scale", h("input", { class: "input sm mono", type: "number", value: f.scale ?? "", disabled: dis, onchange: (e) => { f.scale = e.target.value === "" ? undefined : Number(e.target.value); } })),
              lab("Sign", h("input", { class: "input sm mono", value: f.sign || "", placeholder: "none", disabled: dis, onchange: (e) => { f.sign = e.target.value || undefined; } }))) : null,
            f.type === "decimal" ? h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!f.implied_decimal, disabled: dis, onchange: (e) => { f.implied_decimal = e.target.checked || undefined; } }), "Implied decimal") : null,
            h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!f.personal, disabled: dis, onchange: (e) => { f.personal = e.target.checked || undefined; } }), "Personal"),
            f.type === "string" ? lab("Mask", h("select", { class: "input sm", disabled: dis, onchange: (e) => { f.mask = e.target.value || undefined; drawRecords(); } },
              [["", "No mask"], ["pan", "Card number (first 6, last 4)"], ["none", "Keep the full value"]].map(([v, l]) => h("option", { value: v, selected: (f.mask || "") === v }, l)))) : null,
            f.mask === "none" ? h("div", { class: "err" }, "The full number is kept.") : null))),
        h("td", {}, readOnly ? null : h("button", { class: "btn ghost sm icon", "aria-label": `Remove ${f.name}`, onclick: () => { rec.fields.splice(i, 1); drawRecords(); } }, icon("trash")))))))),
      all.length > 10 ? pager(fst, all.length, drawRecords) : null);
  }
  const optionSummary = (f) => [f.format, f.type === "decimal" && f.scale !== undefined ? `scale ${f.scale}` : null, f.mask ? `mask ${f.mask}` : null, f.personal ? "personal" : null].filter(Boolean).join(", ") || "More";

  function drawRecords() {
    put(recBox, L.records.map((rec, ri) => {
      const code = Array.isArray(rec.match) ? rec.match.join(", ") : rec.match || "";
      if (samples[ri] === undefined) samples[ri] = null;
      const line = samples[ri] ?? sampleLine(L, rec);
      const over = overlaps(rec.fields || []);
      return sect(`layout-${name}-${ri}`, `${rec.type || "record"} ${code}`, { open: ri === 0 || !!rec.output, icon: rec.output ? "rows" : "minus",
        right: h("span", { class: "row", style: "gap:6px" }, rec.output ? h("span", { class: "badge info" }, "Rows") : null,
          over.size ? h("span", { class: "badge bad" }, icon("warning"), "Overlap") : null, h("span", { class: "faint small" }, `${(rec.fields || []).length} fields`)) },
        h("div", { class: "grid3" },
          lab("Code", h("input", { class: "input sm mono", value: code, disabled: dis, onchange: (e) => { const v = e.target.value.split(",").map((x) => x.trim()).filter(Boolean); rec.match = v.length === 1 ? v[0] : v; drawRecords(); } })),
          lab("Occurs", h("select", { class: "input sm", disabled: dis, onchange: (e) => { rec.occurs = e.target.value; } }, ["first", "last", "once", "many"].map((o) => h("option", { value: o, selected: (rec.occurs || "many") === o }, o)))),
          h("label", { class: "check", style: "align-self:end" }, h("input", { type: "radio", name: "output", checked: !!rec.output, disabled: dis, onchange: () => { L.records.forEach((r) => { delete r.output; }); rec.output = true; drawRecords(); } }), "Gives the rows")),
        h("div", { class: "stack tight" }, h("div", { class: "row" }, h("span", { class: "small muted grow" }, "Ruler"),
          h("input", { class: "input sm mono", style: "max-width:360px", placeholder: "Paste a line to check", "aria-label": "Sample line", value: samples[ri] || "",
            oninput: (e) => { samples[ri] = e.target.value || null; drawRecords(); } })),
        ruler(L, rec, line)),
        fieldsTable(rec, ri),
        readOnly ? null : h("div", {}, h("button", { class: "btn sm", onclick: () => {
          const last = (rec.fields || []).reduce((m, f) => Math.max(m, Number(f.start) + fieldLength(f)), (L.record_type ? L.record_type.start + L.record_type.length : 1));
          rec.fields = rec.fields || []; rec.fields.push({ name: `field_${rec.fields.length + 1}`, start: last, length: 8, type: "string" }); drawRecords();
        } }, icon("plus"), "Add field")));
    }));
    const ta = document.getElementById("layout-json");
    if (ta && document.activeElement !== ta) ta.value = JSON.stringify(L, null, 2);
  }

  // test on a file
  const testOut = h("div", { class: "stack tight" });
  await loadKinds();
  const conns = (await loadConnections(true)).filter((c) => isFileKind(c.kind));
  const t = { connection: conns[0] ? conns[0].id : "", file: "" };
  async function runTest() {
    put(testOut, h("div", { class: "sk", style: "height:140px" }));
    try {
      const r = await api(`/layouts/${enc(name)}/test`, { method: "POST", body: { connection: t.connection, file: t.file, layout: L } });
      put(testOut, checksRow(r.checks), pagedGrid(r, "layout-test", 10));
    } catch (e) { put(testOut, inlineError(e)); }
  }

  async function save() {
    put(err);
    try { await api(`/layouts/${enc(name)}`, { method: "PUT", body: L }); toast(`${name} saved`); }
    catch (e) { put(err, ((e.data && e.data.problems) || [e.message]).map((p) => h("div", { class: "err" }, typeof p === "string" ? p : p.message))); }
  }
  async function copy() {
    const d = dialog("Copy template to edit");
    let to = `${name}_copy`.slice(0, 60);
    const e2 = h("div", { class: "err" });
    put(d.box, h("h2", {}, "Copy template to edit"),
      lab("New name", h("input", { class: "input mono", value: to, oninput: (e) => { to = e.target.value; } }), "Lower case, digits and _."), e2,
      h("div", { class: "row" }, h("span", { class: "grow" }), h("button", { class: "btn ghost", onclick: d.close }, "Cancel"),
        h("button", { class: "btn primary", onclick: async () => {
          if (!NAME_RE.test(to)) { e2.textContent = "Use lower case letters, digits and _ (60 at most)."; return; }
          try { await api(`/layouts/${enc(name)}/copy`, { method: "POST", body: { to } }); d.close(); toast("Copied"); location.hash = `#/layouts/${enc(to)}`; }
          catch (e) { e2.textContent = e.message; }
        } }, icon("copy"), "Copy")));
    d.box.querySelector("input").focus();
  }
  async function remove() {
    if (!(await confirmBox(`Delete ${name}?`, "Only this project's copy is removed.", "Delete", true))) return;
    try { await api(`/layouts/${enc(name)}`, { method: "DELETE" }); toast(`${name} deleted`); location.hash = "#/layouts"; } catch (e) { toast(e.message, true); }
  }

  const jsonErr = h("div", { class: "err" });
  drawRecords();
  return [
    head(L.label || name, L.label && L.label !== name ? name : "Fixed-width layout",
      h("a", { class: "btn ghost", href: "#/layouts" }, icon("arrow-left"), "Layouts"),
      readOnly ? h("button", { class: "btn primary", onclick: copy }, icon("copy"), "Copy template to edit") : h("button", { class: "btn primary", onclick: save }, icon("floppy-disk"), "Save")),
    L.verify || readOnly ? h("div", { class: "banner", title: L.verify || "" }, icon("warning"),
      h("div", { class: "grow" }, readOnly ? h("strong", {}, "Template. ") : null,
        L.verify ? `${String(L.verify).split(/(?<=\.)\s/)[0]} Check every position before use.` : "Read only. Copy it to edit.")) : null,
    err,
    panel("File", null, h("div", { class: "pb" }, h("div", { class: "grid3" },
      lab("Encoding", h("input", { class: "input mono", value: L.encoding || "", placeholder: "utf-8", disabled: dis, onchange: (e) => { L.encoding = e.target.value || undefined; } })),
      lab("Line length", h("input", { class: "input mono", type: "number", value: L.line_length ?? "", placeholder: "Any", disabled: dis, onchange: (e) => { L.line_length = e.target.value ? Number(e.target.value) : undefined; drawRecords(); } })),
      lab("Record code at", h("div", { class: "row nowrap" },
        h("input", { class: "input mono", type: "number", value: (L.record_type || {}).start ?? "", "aria-label": "Code start", disabled: dis, onchange: (e) => { L.record_type = { ...(L.record_type || {}), start: Number(e.target.value) }; drawRecords(); } }),
        h("input", { class: "input mono", type: "number", value: (L.record_type || {}).length ?? "", "aria-label": "Code length", disabled: dis, onchange: (e) => { L.record_type = { ...(L.record_type || {}), length: Number(e.target.value) }; drawRecords(); } })), "Start and length.")))),
    recBox,
    sect("layout-test", "Test on a file", { icon: "flask" },
      conns.length ? h("div", { class: "row", style: "align-items:flex-end" },
        h("div", { style: "min-width:180px" }, lab("Connection", h("select", { class: "input", onchange: (e) => { t.connection = e.target.value; } }, conns.map((c) => h("option", { value: c.id }, c.id))))),
        h("div", { class: "grow", style: "min-width:200px" }, lab("File", h("input", { class: "input mono", placeholder: "bank/card_daily.txt", oninput: (e) => { t.file = e.target.value; } }))),
        h("button", { class: "btn", onclick: runTest }, icon("play"), "Test")) : h("p", { class: "faint small" }, "Add a folder connection to test on a file."),
      testOut),
    sect("layout-yaml", "YAML", { icon: "code" },
      h("textarea", { id: "layout-json", class: "input mono", rows: 16, spellcheck: "false", disabled: dis, "aria-label": "Layout as YAML (JSON form)" }, JSON.stringify(L, null, 2)),
      h("span", { class: "help" }, "The JSON form of YAML."), jsonErr,
      readOnly ? null : h("div", {}, h("button", { class: "btn sm", onclick: () => {
        try { const v = JSON.parse(document.getElementById("layout-json").value); for (const k of Object.keys(L)) delete L[k]; Object.assign(L, v); L.records = L.records || []; jsonErr.textContent = ""; drawRecords(); toast("Applied"); }
        catch (e) { jsonErr.textContent = `Not valid: ${e.message}`; }
      } }, icon("check"), "Apply"))),
    readOnly ? null : h("div", {}, h("button", { class: "btn ghost danger", onclick: remove }, icon("trash"), "Delete this layout")),
  ];
}
