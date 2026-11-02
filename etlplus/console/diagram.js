// The animated job diagram (section 13.5): SVG plus CSS, no library.
// Source -> Stage -> Transform -> Mapping -> Destination, horizontal at 720px and wider,
// one column below. Status shows as an icon and a word as well as a colour.
import { h, s, put, icon, api, enc, fmt, every, panel, sect, inlineError, kv } from "./ui.js";

const W = 168;
const H = 72;
const M = 10;
const GAP_X = 64;
const GAP_Y = 40;
const ORDER = ["source", "stage", "transform", "mapping", "destination"];
const TYPE_NAME = { source: "Source", stage: "Stage", transform: "Transform", mapping: "Mapping", destination: "Destination" };
const DETAIL_LABEL = { source: "Read", stage: "File", transform: "Steps", mapping: "Columns", destination: "Load" };
const GLYPH = { source: "database", stage: "stack", transform: "funnel", mapping: "arrows-left-right", destination: "tray-arrow-down" };
const STATUS_ICON = { ok: "check-circle", failed: "x-circle", running: "circle-notch", skipped: "minus-circle", cancelled: "stop-circle", idle: "circle-dashed" };
const STATUS_WORD = { ok: "ok", failed: "failed", running: "running", skipped: "skipped", cancelled: "cancelled", idle: "not run" };
const FLOW_COLOR = { ok: "var(--ok)", running: "var(--primary)" };
export const STATUSES = Object.keys(STATUS_ICON);

const wide = () => matchMedia("(min-width: 720px)").matches;

// geometry for n nodes; pure, so the static test can check it
export function layout(horizontal, n = 5) {
  const nodes = [];
  for (let i = 0; i < n; i++) {
    nodes.push(horizontal ? { x: M + i * (W + GAP_X), y: M } : { x: M, y: M + i * (H + GAP_Y) });
  }
  const width = horizontal ? 2 * M + n * W + (n - 1) * GAP_X : 2 * M + W;
  const height = horizontal ? 2 * M + H : 2 * M + n * H + (n - 1) * GAP_Y;
  const edges = [];
  for (let i = 0; i < n - 1; i++) {
    const a = nodes[i];
    const b = nodes[i + 1];
    if (horizontal) {
      const y = a.y + H / 2;
      const x1 = a.x + W;
      const x2 = b.x;
      edges.push({ d: `M${x1},${y} C${x1 + GAP_X / 2},${y} ${x2 - GAP_X / 2},${y} ${x2},${y}`, len: x2 - x1, mx: (x1 + x2) / 2, my: y });
    } else {
      const x = a.x + W / 2;
      const y1 = a.y + H;
      const y2 = b.y;
      edges.push({ d: `M${x},${y1} C${x},${y1 + GAP_Y / 2} ${x},${y2 - GAP_Y / 2} ${x},${y2}`, len: y2 - y1, mx: x, my: (y1 + y2) / 2 });
    }
  }
  return { nodes, edges, width, height };
}

export function ariaLabel(node) {
  const st = STATUS_WORD[node.status] || node.status;
  const rows = node.rows !== undefined && node.rows !== null ? `, ${fmt.int(node.rows)} rows` : "";
  const label = node.label && node.label !== TYPE_NAME[node.id] ? ` (${node.label})` : "";
  return `${TYPE_NAME[node.id] || node.id}${label}: ${st}${rows}`;
}

// the diagram of an unsaved draft: every node idle
export function draftFlow(job) {
  const src = job.source || {};
  const t = job.target || {};
  const files = Array.isArray(src.files) ? src.files.join(", ") : src.files;
  const mapped = job.mapping && job.mapping.columns ? Object.keys(job.mapping.columns).length : 0;
  const steps = (job.transform || []).length;
  return {
    live: false, status: null,
    nodes: [
      { id: "source", label: src.connection || "Source", detail: [src.table || files || (src.query ? "query" : ""), src.format].filter(Boolean).join(" · "), status: "idle" },
      { id: "stage", label: "Stage", detail: "extract.parquet", status: "idle" },
      { id: "transform", label: "Transform", detail: steps ? `${steps} ${steps === 1 ? "step" : "steps"}` : "No steps", status: "idle" },
      { id: "mapping", label: "Mapping", detail: mapped ? `${mapped} ${mapped === 1 ? "column" : "columns"}` : "Not mapped", status: "idle" },
      { id: "destination", label: t.destination || t.connection || "Destination", detail: t.mode || "replace", status: "idle" },
    ],
    edges: [],
  };
}

// draw one flow into box; opts: {animate, selected, onNode(node)}
export function renderDiagram(box, flow, opts = {}) {
  const horizontal = wide();
  const g = layout(horizontal);
  const byId = Object.fromEntries((flow.nodes || []).map((n) => [n.id, n]));
  const nodes = ORDER.map((id) => byId[id] || { id, label: TYPE_NAME[id], status: "idle" });
  const edgeRows = Object.fromEntries((flow.edges || []).map((e) => [`${e.from}>${e.to}`, e.rows]));
  const anim = opts.animate !== false;
  const svg = s("svg", { viewBox: `0 0 ${g.width} ${g.height}`, role: "group", "aria-label": "Job flow" });

  // edges first, so nodes sit on top
  g.edges.forEach((e, i) => {
    const from = nodes[i];
    const to = nodes[i + 1];
    const st = from.status;
    const broken = st === "failed";
    const base = s("path", { d: e.d, class: `edge-base${broken ? " broken" : ""}${anim ? " draw-in" : ""}`, style: `--len:${e.len};--d:${i * 60 + 120}ms` });
    svg.append(base);
    if ((st === "ok" || st === "running") && to) {
      svg.append(s("path", { d: e.d, class: "edge-flow", stroke: FLOW_COLOR[st], style: `--flow-delay:${anim ? i * 60 + 540 : 0}ms` }));
    }
    const rows = edgeRows[`${from.id}>${to.id}`] ?? (st === "ok" || st === "running" ? from.rows : undefined);
    if (rows !== undefined && rows !== null) {
      const text = fmt.int(rows);
      const tw = text.length * 6.8 + 10;
      const lbl = s("g", { class: "edge-label" + (anim ? " node-in" : ""), style: `--d:${i * 60 + 300}ms` },
        s("rect", { x: e.mx - tw / 2, y: e.my - (horizontal ? 21 : 8), width: tw, height: 16, rx: 4 }),
        s("text", { x: e.mx, y: e.my - (horizontal ? 9 : -4), "text-anchor": "middle" }, text));
      svg.append(lbl);
    }
  });

  nodes.forEach((n, i) => {
    const p = g.nodes[i];
    const st = STATUSES.includes(n.status) ? n.status : "idle";
    const grp = s("g", {
      class: `node s-${st}${anim ? " node-in" : ""}${opts.selected === n.id ? " sel" : ""}`, role: "button", tabindex: "0",
      "aria-label": ariaLabel(n), style: `--d:${i * 60}ms`, "data-node": n.id,
    });
    grp.append(s("title", {}, [n.label, n.detail].filter(Boolean).join(": ")));
    grp.append(s("rect", { class: "focus-ring", x: p.x - 4, y: p.y - 4, width: W + 8, height: H + 8, rx: 13 }));
    if (st === "running") grp.append(s("rect", { class: "node-pulse", x: p.x, y: p.y, width: W, height: H, rx: 10 }));
    grp.append(s("rect", { class: "node-under", x: p.x, y: p.y, width: W, height: H, rx: 10 }));
    grp.append(s("rect", { class: "node-box", x: p.x, y: p.y, width: W, height: H, rx: 10 }));
    const fo = s("foreignObject", { x: p.x, y: p.y, width: W, height: H });
    const rowText = n.rows !== undefined && n.rows !== null ? `${fmt.int(n.rows)} rows` : st === "skipped" ? "Skipped" : "-";
    fo.append(h("div", { class: "nd", "aria-hidden": "true" },
      h("span", { class: "gl" }, icon(GLYPH[n.id] || "circle")),
      h("span", { class: "lb" }, n.label || TYPE_NAME[n.id]),
      icon(STATUS_ICON[st], "si"),
      h("span", { class: "dt" }, n.detail || ""),
      h("span", { class: "rw" }, h("span", {}, rowText), n.ms !== undefined && n.ms !== null ? h("span", { class: "faint" }, fmt.dur(n.ms)) : null, h("span", { class: "word" }, "Running"))));
    grp.append(fo);
    const act = () => { if (opts.onNode) opts.onNode(n); };
    grp.addEventListener("click", act);
    grp.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); act(); } });
    svg.append(grp);
  });

  box.classList.add("diagram");
  box.classList.toggle("vertical", !horizontal);
  put(box, svg);
  return svg;
}

function legend() {
  return h("div", { class: "legend", "aria-hidden": "true" },
    [["ok", "Done"], ["running", "Running"], ["failed", "Failed"], ["skipped", "Skipped"], ["idle", "Not run"]].map(([k, l]) =>
      h("span", { class: `s-${k}` }, h("span", { style: `color:var(${{ ok: "--ok", running: "--primary", failed: "--danger" }[k] || "--ink-3"})` }, icon(STATUS_ICON[k])), l)));
}

// a static preview (wizard): every node idle, no polling
export function diagramPreview(job) {
  const box = h("div");
  const draw = (animate) => renderDiagram(box, draftFlow(job), { animate });
  draw(true);
  const mq = matchMedia("(min-width: 720px)");
  const onMq = () => { if (box.isConnected) draw(false); else mq.removeEventListener("change", onMq); };
  mq.addEventListener("change", onMq);
  return box;
}

// the job detail diagram: GET /api/jobs/{name}/flow, polled every 2 s while live
export function diagramView(name, job) {
  const box = h("div");
  const detail = h("div", { class: "step-detail hidden", "aria-live": "polite" });
  const note = h("div");
  let flow = null;
  let selected = null;
  let last = "";
  let timer = null;

  function draw(animate) {
    renderDiagram(box, flow, { animate, selected, onNode: (n) => { selected = n.id; draw(false); showDetail(n); const el = box.querySelector(`[data-node="${n.id}"]`); if (el) el.focus(); } });
  }

  async function load(first) {
    try {
      const f = await api(`/jobs/${enc(name)}/flow`);
      const key = JSON.stringify(f);
      if (key !== last) {
        last = key;
        flow = f;
        draw(first);
        if (selected) { const n = (flow.nodes || []).find((x) => x.id === selected); if (n) showDetail(n); }
      }
      if (f.live && !timer) timer = every(2000, () => { if (box.isConnected) load(false); });
      if (!f.live && timer) { clearInterval(timer); timer = null; }
      put(note);
    } catch (e) {
      if (first) { flow = draftFlow(job || {}); draw(true); }
      put(note, inlineError(e));
    }
  }

  async function showDetail(n) {
    detail.classList.remove("hidden");
    const kindFor = { source: "extract", stage: "extract", transform: "transformed", mapping: "mapped", destination: null }[n.id];
    const manifestBox = h("div", { class: "stack tight" });
    const mapCols = n.id === "mapping" && job && job.mapping && job.mapping.columns ? Object.entries(job.mapping.columns) : [];
    put(detail, h("div", { class: "stack" },
      h("div", { class: "row" }, h("span", { class: `badge ${{ ok: "ok", failed: "bad", running: "info", cancelled: "warn" }[n.status] || ""}` }, icon(STATUS_ICON[n.status] || "circle-dashed"), STATUS_WORD[n.status] || n.status),
        h("h3", {}, TYPE_NAME[n.id], n.label && n.label !== TYPE_NAME[n.id] ? [": ", h("span", { class: "mono" }, n.label)] : null), h("span", { class: "grow" }),
        h("button", { class: "btn ghost sm icon", "aria-label": "Close the step detail", onclick: () => { selected = null; detail.classList.add("hidden"); draw(false); } }, icon("x"))),
      h("div", { class: "kv" },
        kv("Rows", fmt.int(n.rows)),
        kv("Took", fmt.dur(n.ms)),
        kv(DETAIL_LABEL[n.id] || "Detail", n.detail || "-"),
        kv("Run", (flow && flow.run_id) || "-")),
      mapCols.length ? h("div", { class: "stack tight" }, h("div", { class: "small muted" }, "Columns"),
        h("div", { class: "scroll-x" }, h("table", { class: "t" }, h("tbody", {}, mapCols.map(([dest, how]) => h("tr", {},
          h("td", { class: "mono small", style: "overflow-wrap:anywhere" }, dest),
          h("td", { class: "small muted hide-sm" }, how.from !== undefined ? "from" : how.const !== undefined ? "constant" : "expression"),
          h("td", { class: "mono small", style: "overflow-wrap:anywhere" }, how.from ?? (how.const !== undefined ? JSON.stringify(how.const) : how.expr)))))))) : null,
      n.id === "destination" && job && job.target && job.target.destination
        ? h("a", { class: "btn sm", style: "align-self:flex-start", href: `#/destinations/${enc(job.target.destination)}/preview` }, icon("eye"), "Rows in the destination") : null,
      kindFor && flow && flow.run_id ? sect("diagram-manifest", "Manifest", { flat: true, onOpen: () => loadManifest(kindFor, manifestBox) }, manifestBox) : null));
  }

  async function loadManifest(kind, out) {
    put(out, h("div", { class: "sk", style: "height:80px" }));
    try {
      const staged = await api(`/stage/${enc(name)}`);
      const run = staged.find((x) => x.run_id === flow.run_id) || staged[0];
      const m = run && (run.manifests || []).find((x) => (x.kind === "transform" ? "transformed" : x.kind || "extract") === kind);
      put(out, m ? h("pre", { class: "code", style: "max-height:260px" }, JSON.stringify(m, null, 2)) : h("p", { class: "faint small" }, "No manifest for this step."));
    } catch (e) { put(out, h("div", { class: "err" }, e.message)); }
  }

  const mq = matchMedia("(min-width: 720px)");
  const onMq = () => { if (!box.isConnected) { mq.removeEventListener("change", onMq); return; } if (flow) draw(false); };
  mq.addEventListener("change", onMq);
  load(true);
  return panel(h("div", { class: "row" }, icon("flow-arrow"), h("h2", {}, "Flow")), legend(), h("div", { class: "pb stack" }, note, box, detail));
}
