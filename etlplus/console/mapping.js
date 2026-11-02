// The mapping step (section 13.7): destination columns filled from a source column, a
// constant or an expression, with auto-match, a live preview and per-column problems.
import { h, s, put, icon, api, enc, sect, seg, pager, pageState, dataGrid, debounce, inlineError, confirmBox } from "./ui.js";

const REQUIRED = "Required: map a column, a constant or an expression.";
const norm = (x) => String(x || "").toLowerCase();
const NUMERIC = /^(integer|bigint|decimal|double)/;
const ROW_ICON = { bad: "x-circle", warn: "warning", ok: "check-circle", idle: "circle-dashed" };
const ROW_WORD = { bad: "Error", warn: "Warning", ok: "Mapped", idle: "Not mapped" };

export function isRequired(c) {
  return c.nullable === false && (c.default === undefined || c.default === null || c.default === "");
}

// errors the console can see without the server: unmapped required columns, unknown sources,
// mappings to columns the destination does not have
export function localProblems(mapping, destCols, srcCols) {
  const out = [];
  const cols = (mapping && mapping.columns) || {};
  const srcNames = new Set((srcCols || []).map((c) => norm(c.name)));
  const destNames = new Set((destCols || []).map((c) => norm(c.name)));
  for (const c of destCols || []) {
    const m = cols[c.name];
    if (!m && isRequired(c)) out.push({ column: c.name, level: "error", message: REQUIRED });
    if (m && m.from !== undefined && srcCols && srcCols.length && !srcNames.has(norm(m.from))) out.push({ column: c.name, level: "error", message: `No source column ${m.from}.` });
    if (m && m.expr !== undefined && !String(m.expr).trim()) out.push({ column: c.name, level: "error", message: "Write an expression." });
  }
  for (const d of Object.keys(cols)) if (destCols && destCols.length && !destNames.has(norm(d))) out.push({ column: d, level: "error", message: `The destination has no column ${d}.` });
  return out;
}

export function mappingCounts(mapping, destCols, problems) {
  const cols = (mapping && mapping.columns) || {};
  const mapped = (destCols || []).filter((c) => cols[c.name]).length;
  const errors = new Set((problems || []).filter((p) => p.level === "error").map((p) => p.column)).size;
  return { mapped, total: (destCols || []).length, errors };
}

export function countText(c) {
  return `${c.mapped} of ${c.total} mapped · ${c.errors} ${c.errors === 1 ? "error" : "errors"}`;
}

function constValue(text, type) {
  const t = String(text);
  if (NUMERIC.test(type || "") && /^-?\d+(\.\d+)?$/.test(t.trim())) return Number(t.trim());
  if (/^boolean/.test(type || "") && /^(true|false)$/i.test(t.trim())) return t.trim().toLowerCase() === "true";
  return t;
}

// mappingStep({job, destCols, previewName, onChange}) -> {el, errors(), focusFirstError(), refresh()}
export function mappingStep({ job, destCols: destColsIn, previewName, onChange, payload, autoMatch = false }) {
  job.mapping = job.mapping || { columns: {} };
  const M = job.mapping;
  M.columns = M.columns || {};
  let destCols = destColsIn || [];
  let srcCols = [];
  let sugg = {};
  let server = [];
  let preview = null;
  let previewErr = null;
  let busy = false;
  let filter = "";
  let hot = null;
  const rowsSt = pageState("mapped-rows", 10);

  const root = h("div", { class: "stack" });
  const tools = h("div", { class: "map-tools" });
  const suggBox = h("div", { class: "sugg" });
  const mapper = h("div", { class: "mapper" });
  const srcCol = h("div", { class: "map-col src" });
  const gutter = h("div", { class: "gutter", "aria-hidden": "true" });
  const dstCol = h("div", { class: "map-col dst" });
  mapper.append(srcCol, gutter, dstCol);
  const previewBox = h("div", { class: "stack tight" });
  const counter = h("span", { class: "map-count", role: "status", "aria-live": "polite" });

  const problems = () => {
    const loc = localProblems(M, destCols, srcCols);
    const seen = new Set(loc.map((p) => `${p.column}|${p.message}`));
    return loc.concat(server.filter((p) => !seen.has(`${p.column}|${p.message}`)));
  };
  const problemsFor = (name) => problems().filter((p) => norm(p.column) === norm(name));

  const runPreview = debounce(() => doPreview(), 400);
  async function doPreview() {
    if (busy) { runPreview(); return; }
    busy = true;
    try {
      const body = payload ? payload() : job;
      preview = await api(`/jobs/${enc(previewName() || "draft")}/mapping-preview`, { method: "POST", body });
      previewErr = null;
      if (preview.source_columns) srcCols = preview.source_columns;
      if (preview.destination_columns && preview.destination_columns.length) destCols = preview.destination_columns;
      server = preview.problems || [];
      if (!Object.keys(sugg).length && srcCols.length && destCols.length) await loadSuggestions();
      // a destination made like the source: its columns carry the source's names, so match them now
      if (autoMatch && srcCols.length && destCols.length && !Object.keys(M.columns).length) { autoMatch = false; busy = false; matchByName(); return; }
    } catch (e) {
      if (e.status === 429) { busy = false; setTimeout(runPreview, 600); return; }
      previewErr = e;
      server = (e.data && e.data.problems) || [];
    }
    busy = false;
    draw();
  }

  async function loadSuggestions() {
    try {
      const r = await api("/mapping/suggest", { method: "POST", body: { source_columns: srcCols, destination_columns: destCols } });
      sugg = (r && r.columns) || {};
    } catch (e) { sugg = {}; }
  }

  const changed = () => { if (onChange) onChange(); draw(); runPreview(); };
  const usedSources = () => new Set(Object.values(M.columns).filter((m) => m.from !== undefined).map((m) => norm(m.from)));

  function matchByName() {
    const used = usedSources();
    let n = 0;
    for (const c of destCols) {
      const x = sugg[c.name];
      if (M.columns[c.name] || !x || !x.from || !["exact", "alias"].includes(x.how) || used.has(norm(x.from))) continue;
      M.columns[c.name] = { from: x.from };
      used.add(norm(x.from));
      n++;
    }
    if (!n) {
      // without the suggest route: exact names only
      const byNorm = new Map(srcCols.map((c) => [norm(c.name).replace(/[^a-z0-9]/g, ""), c.name]));
      for (const c of destCols) {
        const src = byNorm.get(norm(c.name).replace(/[^a-z0-9]/g, ""));
        if (!M.columns[c.name] && src && !used.has(norm(src))) { M.columns[c.name] = { from: src }; used.add(norm(src)); }
      }
    }
    changed();
  }
  const similar = () => {
    const used = usedSources();
    return destCols.map((c) => [c.name, sugg[c.name]]).filter(([d, x]) => x && x.how === "similar" && !M.columns[d] && !used.has(norm(x.from)));
  };
  const accept = (pairs) => { for (const [d, x] of pairs) M.columns[d] = { from: x.from }; changed(); };

  function setHow(c, value) {
    if (!value) delete M.columns[c.name];
    else if (value === "const") M.columns[c.name] = { const: (M.columns[c.name] && M.columns[c.name].const) ?? "" };
    else if (value === "expr") M.columns[c.name] = { expr: (M.columns[c.name] && M.columns[c.name].expr) ?? "" };
    else M.columns[c.name] = { from: value.slice(5) };
    changed();
    if (value === "const" || value === "expr") {
      const inp = dstCol.querySelector(`[data-col="${CSS.escape(c.name)}"] .how input`);
      if (inp) inp.focus();
    }
  }

  function dstRow(c) {
    const m = M.columns[c.name];
    const probs = problemsFor(c.name);
    const bad = probs.find((p) => p.level === "error");
    const warn = probs.find((p) => p.level === "warn");
    const value = !m ? "" : m.const !== undefined ? "const" : m.expr !== undefined ? "expr" : `from:${m.from}`;
    const x = sugg[c.name];
    const suggested = x && x.from ? srcCols.filter((sc) => norm(sc.name) === norm(x.from)) : [];
    const sel = h("select", { class: "input sm", "aria-label": `${c.name}: fill from`, onchange: (e) => setHow(c, e.target.value) },
      h("option", { value: "" }, "Not mapped"),
      suggested.length ? h("optgroup", { label: "Suggested" }, suggested.map((sc) => h("option", { value: `from:${sc.name}`, selected: value === `from:${sc.name}` }, `${sc.name} (${x.how})`))) : null,
      h("optgroup", { label: "Source columns" }, srcCols.filter((sc) => !suggested.includes(sc)).map((sc) => h("option", { value: `from:${sc.name}`, selected: norm(value) === norm(`from:${sc.name}`) }, sc.name)),
        m && m.from !== undefined && !srcCols.some((sc) => norm(sc.name) === norm(m.from)) ? h("option", { value, selected: true }, `${m.from} (missing)`) : null),
      h("option", { value: "const", selected: value === "const" }, "Constant…"),
      h("option", { value: "expr", selected: value === "expr" }, "Expression…"));
    const extra = value === "const"
      ? h("input", { class: "input sm mono", value: m.const ?? "", placeholder: "MT940", "aria-label": `${c.name} constant`,
        oninput: (e) => { m.const = constValue(e.target.value, c.type); if (onChange) onChange(); runPreview(); } })
      : value === "expr"
        ? h("input", { class: `input sm mono${bad ? " bad" : ""}`, value: m.expr ?? "", placeholder: "upper(name)", "aria-label": `${c.name} expression`, spellcheck: "false",
          oninput: (e) => { m.expr = e.target.value; if (onChange) onChange(); }, onblur: () => doPreview() })
        : null;
    const k = bad ? "bad" : warn ? "warn" : m ? "ok" : "idle";
    const st = [k, ROW_ICON[k], ROW_WORD[k]];
    const msg = bad || warn;
    return h("div", { class: `dst-row${bad ? " err" : ""}${hot === c.name ? " hot" : ""}`, "data-col": c.name,
      onmouseenter: () => hover(c.name), onmouseleave: () => hover(null) },
      h("div", { class: "stack", style: "gap:3px;min-width:0" },
        h("div", { class: "nm" }, h("b", { title: c.name }, c.name),
          isRequired(c) ? h("span", { class: "dot req", title: "Required" }, h("span", { class: "sr" }, "required")) : null),
        h("div", { class: "nm" }, h("span", { class: "tchip" }, c.type || ""), c.personal ? h("span", { class: "pchip" }, "Personal") : null)),
      h("div", { class: "how" }, sel, extra),
      h("span", { class: `stt ${st[0]}`, title: st[2] }, icon(st[1]), h("span", { class: "sr" }, st[2])),
      msg ? h("div", { class: `msg${bad ? " bad" : ""}` }, msg.message) : null);
  }

  function srcRow(c) {
    const used = Object.entries(M.columns).some(([, m]) => m.from !== undefined && norm(m.from) === norm(c.name));
    return h("div", { class: `src-row${used ? " used" : ""}`, "data-src": c.name },
      h("div", { class: "nm" }, h("b", { title: c.name }, c.name), h("span", { class: "tchip" }, c.type || ""), c.personal ? h("span", { class: "pchip" }, "Personal") : null),
      h("div", { class: "smp", title: (c.samples || []).map(String).join(", ") }, (c.samples || []).slice(0, 3).map((v) => (v === null ? "null" : String(v))).join("  ·  ") || " "));
  }

  function hover(name) {
    hot = name;
    dstCol.querySelectorAll(".dst-row").forEach((r) => r.classList.toggle("hot", r.dataset.col === name));
    const m = name && M.columns[name];
    srcCol.querySelectorAll(".src-row").forEach((r) => r.classList.toggle("hot", !!(m && m.from !== undefined && norm(r.dataset.src) === norm(m.from))));
    gutter.querySelectorAll("path").forEach((p) => p.classList.toggle("hot", p.dataset.col === name));
  }

  function drawGutter() {
    if (!mapper.isConnected || getComputedStyle(gutter).display === "none") return;
    const box = gutter.getBoundingClientRect();
    if (!box.width) return;
    const svg = s("svg", {});
    const mid = (el) => { const r = el.getBoundingClientRect(); return r.top + r.height / 2 - box.top; };
    for (const row of dstCol.querySelectorAll(".dst-row")) {
      const name = row.dataset.col;
      const m = M.columns[name];
      if (!m) continue;
      const y2 = Math.min(mid(row), Math.max(0, row.getBoundingClientRect().top - box.top + 29));
      if (m.from !== undefined) {
        const srcEl = [...srcCol.querySelectorAll(".src-row")].find((r) => norm(r.dataset.src) === norm(m.from));
        if (!srcEl) continue;
        const y1 = mid(srcEl);
        const w = box.width;
        svg.append(s("path", { d: `M0,${y1} C${w * 0.5},${y1} ${w * 0.5},${y2} ${w},${y2}`, "data-col": name, class: hot === name ? "hot" : "" }));
      } else {
        const w = box.width;
        svg.append(s("path", { d: `M${w - 34},${y2} L${w},${y2}`, class: `stub${hot === name ? " hot" : ""}`, "data-col": name }));
        svg.append(s("text", { x: w - 48, y: y2 + 4, "text-anchor": "middle" }, m.const !== undefined ? "=" : "ƒ"));
      }
    }
    put(gutter, svg);
  }

  function drawPreview() {
    const all = problems();
    const label = preview && preview.based_on ? (String(preview.based_on).startsWith("staged") ? "From the last staged run" : "Live sample") : null;
    const rows = (preview && preview.rows) || [];
    const cols = (preview && preview.columns) || [];
    const shown = rows.slice(rowsSt.offset, rowsSt.offset + rowsSt.limit);
    put(previewBox,
      label ? h("div", { class: "row" }, h("span", { class: "chip muted" }, icon("info"), label), busy ? h("span", { class: "spin", "aria-label": "Updating" }) : null) : null,
      previewErr && !all.length ? inlineError(previewErr) : null,
      all.length ? h("div", { class: "stack tight", role: "list", "aria-label": "Problems" }, all.slice(0, 12).map((p) =>
        h("div", { class: p.level === "error" ? "err" : "small", role: "listitem", style: p.level === "warn" ? "color:var(--gold)" : null },
          icon(p.level === "error" ? "x-circle" : "warning"), " ", h("span", { class: "mono" }, p.column || ""), p.column ? ": " : "", p.message)),
        all.length > 12 ? h("div", { class: "faint small" }, `${all.length - 12} more`) : null) : null,
      cols.length ? dataGrid({ columns: cols, rows: shown }) : !previewErr ? h("div", { class: "sk", style: "height:120px" }) : null,
      rows.length > 10 ? pager(rowsSt, rows.length, drawPreview) : null);
  }

  function draw() {
    const all = problems();
    const c = mappingCounts(M, destCols, all);
    counter.replaceChildren(document.createTextNode(`${c.mapped} of ${c.total} mapped · `), h("span", { class: c.errors ? "e" : "" }, `${c.errors} ${c.errors === 1 ? "error" : "errors"}`));
    const sim = similar();
    put(suggBox, sim.length ? [h("span", { class: "muted" }, "Suggested:"),
      sim.slice(0, 6).map(([d, x]) => h("button", { class: "chip", onclick: () => accept([[d, x]]), "aria-label": `Accept ${x.from} into ${d}` },
        h("span", { class: "mono" }, `${x.from} → ${d}`), icon("check"))),
      sim.length > 6 ? h("span", { class: "faint small" }, `${sim.length - 6} more`) : null,
      h("button", { class: "btn sm ghost", onclick: () => accept(sim) }, "Accept all")] : null);
    const f = norm(filter);
    put(srcCol, h("h3", {}, `Source (${srcCols.length})`), srcCols.filter((x) => !f || norm(x.name).includes(f)).map(srcRow));
    put(dstCol, h("h3", {}, `Destination (${destCols.length})`),
      destCols.length ? destCols.filter((x) => !f || norm(x.name).includes(f) || (M.columns[x.name] && norm(M.columns[x.name].from).includes(f))).map(dstRow)
        : h("div", { class: "faint small" }, "Pick a destination with columns."));
    drawPreview();
    requestAnimationFrame(drawGutter);
  }

  put(tools,
    h("button", { class: "btn sm", onclick: matchByName }, icon("magic-wand"), "Match by name"),
    h("button", { class: "btn sm ghost", onclick: async () => { if (!Object.keys(M.columns).length || await confirmBox("Clear the mapping?", null, "Clear")) { M.columns = {}; changed(); } } }, icon("eraser"), "Clear"),
    h("input", { class: "input sm", type: "search", placeholder: "Filter columns", "aria-label": "Filter columns", oninput: (e) => { filter = e.target.value; draw(); } }),
    h("span", { class: "grow" }), counter);

  const ro = new ResizeObserver(() => drawGutter());
  ro.observe(mapper);

  put(root, tools, suggBox, mapper,
    sect("map-options", "Options", { flat: true },
      h("div", { class: "grid2" },
        seg("Unmapped columns", [["default", "Default or empty"], ["error", "Error"]], M.unmapped || "default", (v) => { M.unmapped = v; changed(); }),
        seg("A value that does not convert", [["error", "Fails the run"], ["null", "Becomes empty"]], M.on_cast_error || "error", (v) => { M.on_cast_error = v; changed(); }))),
    sect("map-preview", "Preview", { open: true, icon: "eye" }, previewBox));
  draw();
  doPreview();

  return {
    el: root,
    errors: () => problems().filter((p) => p.level === "error"),
    counts: () => mappingCounts(M, destCols, problems()),
    setDestination(cols) { destCols = cols || []; sugg = {}; draw(); runPreview(); },
    focusFirstError() {
      const p = problems().find((x) => x.level === "error");
      if (!p) return false;
      const row = dstCol.querySelector(`[data-col="${CSS.escape(p.column)}"]`);
      if (row) { row.scrollIntoView({ block: "center", behavior: "smooth" }); const ctl = row.querySelector("select, input"); if (ctl) ctl.focus({ preventScroll: true }); }
      return true;
    },
    refresh: () => doPreview(),
  };
}
