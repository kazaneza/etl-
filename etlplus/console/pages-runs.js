// Runs (paginated, filtered by job), one run with its steps and live log, and Stage:
// the files each run leaves behind plus the file ledger.
import {
  h, put, icon, api, apiPage, enc, fmt, head, panel, empty, toast, pager, pageState, sect, statusBadge, inlineError,
  every, kv, rowsOf, stepMs, runMs, store,
} from "./ui.js";
import { refreshSide, startRun } from "./state.js";

const STEP_NAMES = ["extract", "transform", "map", "load"];
const STEP_LABEL = { extract: "Extract", transform: "Transform", map: "Map", load: "Load" };
const STEP_ICON = { extract: "download-simple", transform: "tree-structure", map: "arrows-left-right", load: "upload-simple" };

export async function pageRuns(args) {
  if (args[0]) return pageRun(args[0]);
  const jobs = await api("/jobs").catch(() => []);
  refreshSide();
  let job = store.get("runs-job", "");
  const st = pageState("runs", 25);
  const box = h("div");
  async function draw() {
    let page;
    try { page = await apiPage(`/runs${job ? `?job=${enc(job)}` : ""}`, st); } catch (e) { put(box, h("div", { class: "pb" }, inlineError(e))); return; }
    put(box, page.total ? h("div", { class: "scroll-x" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "Job"), h("th", {}, "Status"), h("th", { class: "hide-sm" }, "Started"), h("th", { class: "r hide-sm" }, "Took"), h("th", { class: "r hide-sm" }, "Read"), h("th", { class: "r" }, "Loaded"), h("th", { class: "hide-sm" }, "How"))),
      h("tbody", {}, page.items.map((r) => h("tr", { class: "click", onclick: () => (location.hash = `#/runs/${enc(r.run_id)}`) },
        h("td", {}, h("a", { class: "mono", href: `#/runs/${enc(r.run_id)}`, onclick: (e) => e.stopPropagation() }, r.job), h("div", { class: "faint small mono hide-sm" }, r.run_id)),
        h("td", {}, statusBadge(r.status)),
        h("td", { class: "small hide-sm" }, fmt.time(r.started_at)),
        h("td", { class: "r num small hide-sm" }, fmt.dur(runMs(r))),
        h("td", { class: "r num hide-sm" }, fmt.int(rowsOf(r, "extract"))),
        h("td", { class: "r num" }, fmt.int(rowsOf(r, "load"))),
        h("td", { class: "small muted hide-sm" }, r.trigger || "-"))))))
      : empty("clock-counter-clockwise", "No runs yet.", h("a", { class: "btn", href: "#/jobs" }, icon("flow-arrow"), "Jobs")),
    page.total > 10 ? h("div", { class: "pf" }, pager(st, page.total, draw)) : null);
    if (page.items.some((r) => r.status === "running") && !st.polling) { st.polling = true; every(3000, () => { if (box.isConnected) draw(); }); }
  }
  draw();
  return [
    head("Runs", null, h("select", { class: "input", "aria-label": "Job", style: "width:220px", onchange: (e) => { job = e.target.value; store.set("runs-job", job); st.offset = 0; draw(); } },
      h("option", { value: "" }, "All jobs"), (Array.isArray(jobs) ? jobs : []).map((j) => h("option", { value: j.name, selected: j.name === job }, j.name)))),
    panel("History", null, box),
  ];
}

async function pageRun(runId) {
  let run = await api(`/runs/${enc(runId)}`);
  refreshSide();
  const stepsBox = h("div", { class: "stats" });
  const info = h("div");
  const logBox = h("pre", { class: "log", tabindex: "0", "aria-label": "Log" });
  const actions = h("div", { class: "row" });
  const status = h("span");
  let follow = true;
  logBox.addEventListener("scroll", () => { follow = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 8; });

  function draw() {
    const running = run.status === "running";
    put(status, statusBadge(run.status));
    const steps = run.steps || {};
    const names = STEP_NAMES.filter((x) => x !== "map" || steps.map);
    put(stepsBox, names.map((x) => {
      const st = steps[x] || {};
      const stt = st.status || (typeof st === "string" ? st : null);
      return h("div", { class: `stat${stt === "failed" ? " failed" : stt === "running" ? " running" : ""}` },
        h("div", { class: "k" }, icon(STEP_ICON[x]), h("span", { class: "grow" }, STEP_LABEL[x]), statusBadge(stt || (running ? "pending" : "skipped"))),
        h("div", { class: "v" }, fmt.int(rowsOf(run, x))),
        h("div", { class: "s" }, stt || running ? `rows, ${fmt.dur(stepMs(st))}` : x === "transform" ? "No transform steps" : "Not run this time"));
    }));
    const w = run.window;
    put(info, h("div", { class: "stack" },
      run.error ? h("div", { class: "banner bad", role: "alert" }, icon("warning-octagon"), h("div", { class: "mono small", style: "white-space:pre-wrap;overflow-wrap:anywhere" }, run.error)) : null,
      h("div", { class: "kv" },
        kv("Started", fmt.time(run.started_at)), kv("Finished", running ? "Still running" : fmt.time(run.finished_at)), kv("Took", fmt.dur(runMs(run))),
        kv("Window", w ? `${w[0]} to ${w[1]}` : "All rows"), kv("How", run.trigger || "-"), kv("Run", run.run_id))));
    const lines = run.log || [];
    put(logBox, lines.length ? lines.map((l) => h("span", { class: /error|failed|traceback/i.test(l) ? "e" : "" }, `${l}\n`)) : h("span", { class: "faint" }, running ? "Waiting for the first lines." : "No log."));
    if (follow) logBox.scrollTop = logBox.scrollHeight;
    put(actions,
      running ? h("button", { class: "btn danger", onclick: cancel }, icon("stop"), "Cancel") : null,
      !running ? h("button", { class: "btn", onclick: async () => { const r = await startRun(run.job); if (r && r.run_id) location.hash = `#/runs/${enc(r.run_id)}`; } }, icon("arrows-clockwise"), "Run again") : null,
      h("a", { class: "btn ghost", href: `#/jobs/${enc(run.job)}` }, icon("flow-arrow"), "Job"));
  }
  async function cancel() {
    try { const r = await api(`/runs/${enc(runId)}/cancel`, { method: "POST" }); toast(r.cancelled ? "Cancelling" : "It already finished"); } catch (e) { toast(e.message, true); }
  }
  if (run.status === "running") {
    const timer = every(2000, async () => {
      if (!logBox.isConnected) return;
      try { run = await api(`/runs/${enc(runId)}`); draw(); if (run.status !== "running") { clearInterval(timer); refreshSide(); } } catch (e) { /* keep the last view */ }
    });
  }
  draw();
  return [
    head(run.job, null, status, actions),
    stepsBox,
    info,
    sect("run-log", "Log", { icon: "file-text", right: h("span", { class: "faint small" }, run.status === "running" ? "Live, every 2 s" : "Last 200 lines") }, logBox),
  ];
}

// ------------------------------------------------------------------ stage
export async function pageStage(args) {
  const jobs = await api("/jobs");
  refreshSide();
  const name = args[0] || (jobs[0] && jobs[0].name);
  const picker = h("select", { class: "input", "aria-label": "Job", style: "width:220px", onchange: (e) => (location.hash = `#/stage/${enc(e.target.value)}`) },
    jobs.map((j) => h("option", { value: j.name, selected: j.name === name }, j.name)));
  if (!name) return [head("Stage"), panel("Staged files", null, empty("stack", "No jobs yet."))];
  const staged = await api(`/stage/${enc(name)}`);
  const total = staged.reduce((a, x) => a + (x.bytes || 0), 0);
  const kindOf = (m) => ({ transform: "transformed", map: "mapped" }[m.kind] || m.kind || "extract");
  const manifestOf = (x, kind) => (x.manifests || []).find((m) => kindOf(m) === kind);
  const st = pageState(`stage-${name}`, 10);
  const list = h("div", { class: "stack" });
  function drawList() {
    put(list, staged.length ? staged.slice(st.offset, st.offset + st.limit).map((x, i) => {
      const ex = manifestOf(x, "extract");
      const tr = manifestOf(x, "transformed");
      const mp = manifestOf(x, "mapped");
      const m = mp || tr || ex || {};
      return sect(`stage-${name}-${i === 0 && st.offset === 0 ? "first" : "older"}`, x.run_id, {
        open: i === 0 && st.offset === 0,
        right: h("span", { class: "row", style: "gap:6px" },
          ex ? h("span", { class: "badge ok" }, icon("check"), "Extract") : h("span", { class: "badge warn" }, icon("hourglass-medium"), "Incomplete"),
          tr ? h("span", { class: "badge ok hide-sm" }, icon("check"), "Transform") : null,
          mp ? h("span", { class: "badge ok hide-sm" }, icon("check"), "Map") : null,
          h("span", { class: "faint small num" }, fmt.bytes(x.bytes))),
      },
      h("div", { class: "kv" },
        kv("Rows extracted", fmt.int(ex && ex.rows)), kv("After transform", tr ? fmt.int(tr.rows) : "-"), kv("Mapped", mp ? fmt.int(mp.rows) : "-"),
        kv("Window", m.window ? `${m.window[0]} to ${m.window[1]}` : "All rows"), kv("Finished", fmt.time(m.finished_at || m.started_at)), kv("Checksum", m.sha256 ? m.sha256.slice(0, 16) : "-")),
      m.columns && m.columns.length ? h("div", { class: "chips" }, m.columns.map((c) => h("span", { class: "chip static mono", title: c.type }, `${c.name} ${c.type ? String(c.type).toLowerCase() : ""}`))) : null,
      h("div", { class: "scroll-x" }, h("table", { class: "t" }, h("tbody", {}, (x.files || []).map((f) => h("tr", {},
        h("td", { class: "mono small", style: "overflow-wrap:anywhere" }, icon(f.complete ? "file-text" : "circle-dashed"), " ", f.name),
        h("td", { class: "small" }, f.complete ? "" : h("span", { class: "badge warn" }, "Being written")),
        h("td", { class: "r num small" }, fmt.bytes(f.bytes))))))),
      sect("stage-manifest", "Manifest", { flat: true }, h("pre", { class: "code", style: "max-height:300px" }, JSON.stringify(m, null, 2))));
    }) : panel("Staged files", null, empty("stack", "Nothing staged yet.", h("button", { class: "btn", onclick: () => startRun(name, ["extract"]) }, icon("play"), "Extract now"))),
    staged.length > 10 ? pager(st, staged.length, drawList) : null);
  }
  drawList();

  const ledgerBox = h("div");
  const lst = pageState(`ledger-${name}`, 25);
  async function drawLedger() {
    put(ledgerBox, h("div", { class: "sk", style: "height:80px" }));
    let page;
    try { page = await apiPage(`/jobs/${enc(name)}/files`, lst); } catch (e) { put(ledgerBox, e.status === 404 ? h("p", { class: "faint small" }, "No file ledger on this server.") : inlineError(e)); return; }
    put(ledgerBox, page.total ? h("div", { class: "scroll-x" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", { class: "r" }, "Size"), h("th", { class: "hide-sm" }, "Modified"), h("th", {}, "Loaded"), h("th", { class: "hide-sm" }, "Run"))),
      h("tbody", {}, page.items.map((f) => h("tr", {},
        h("td", { class: "mono small", style: "overflow-wrap:anywhere" }, f.path), h("td", { class: "r num small" }, fmt.bytes(f.size)),
        h("td", { class: "small hide-sm" }, fmt.time(f.modified)), h("td", { class: "small" }, fmt.time(f.loaded_at)),
        h("td", { class: "mono small hide-sm" }, f.run_id ? h("a", { href: `#/runs/${enc(f.run_id)}` }, f.run_id) : "-"))))))
      : h("p", { class: "faint small" }, "No files loaded yet."),
    page.total > 10 ? pager(lst, page.total, drawLedger) : null);
  }

  return [
    head("Stage", null, picker),
    h("div", { class: "row" },
      h("span", { class: "muted small grow" }, `${staged.length} staged ${staged.length === 1 ? "run" : "runs"}, ${fmt.bytes(total)}`),
      h("button", { class: "btn", disabled: !staged.length, onclick: () => startRun(name, ["transform", "map", "load"]) }, icon("tree-structure"), "Transform and load"),
      h("button", { class: "btn primary", disabled: !staged.length, onclick: () => startRun(name, ["load"]) }, icon("upload-simple"), "Load again")),
    list,
    sect(`ledger`, "Files already loaded", { icon: "list", onOpen: drawLedger }, ledgerBox),
  ];
}
