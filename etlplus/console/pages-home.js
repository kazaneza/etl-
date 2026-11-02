// The first-start chooser (demo or real) and the Overview page with the Getting set up steps.
import { h, put, icon, line, api, enc, fmt, head, panel, stat, pill, emptyState, setupSteps, statusCell, pager, pageState, sect, every, toast, rowsOf } from "./ui.js";
import { app, refreshSide, startRun, loadStart, drawProjectBadge } from "./state.js";

// ------------------------------------------------------------------ chooser (section 13.3)
export async function pageChooser(onReady) {
  const box = h("div", { class: "chooser" });
  const inner = h("div", { class: "chooser-in" });
  box.append(inner);
  const brand = h("div", { class: "brand" }, h("img", { src: "assets/crane-128.png", alt: "" }),
    h("div", {}, h("div", { class: "name" }, "etl+"), h("div", { class: "by" }, "Crested Crane Intelligence")));
  const msg = h("div", { role: "status", "aria-live": "polite" });
  let polling = false;

  function choices() {
    const demoBtn = h("button", { class: "btn primary", onclick: () => choose("demo") }, icon("flask"), "Start with demo data");
    const realBtn = h("button", { class: "btn", onclick: () => choose("real") }, line("sources", 16), "Connect my own data");
    put(inner, brand,
      h("div", { class: "stack tight" }, h("h1", {}, "How do you want to start?"), h("p", { class: "muted" }, "You can switch later in Settings. Both are kept.")),
      h("div", { class: "choices" },
        h("div", { class: "choice demo" }, h("div", { class: "ico" }, icon("flask")), h("h2", {}, "Start with demo data"),
          h("p", {}, "Made-up bank files, a database and ready jobs."), demoBtn),
        h("div", { class: "choice real" }, h("div", { class: "ico" }, line("sources", 20)), h("h2", {}, "Connect my own data"),
          h("p", {}, "Start empty. Add a connection, a destination, a job."), realBtn)),
      msg);
    setTimeout(() => { if (demoBtn.isConnected) demoBtn.focus(); }, 30);
  }

  function progress(st) {
    const run = st && st.setup_run;
    const total = run ? run.total || 0 : 0;
    const done = run ? run.done || 0 : 0;
    const pct = total ? Math.round((done / total) * 100) : 6;
    const text = !run || !total || /^creating/i.test(run.running || "") ? "Creating demo…" : `Running jobs ${done}/${total}`;
    put(inner, brand,
      h("div", { class: "stack tight" }, h("h1", {}, "Setting up the demo"), h("p", { class: "muted" }, "Everything in it is made up.")),
      h("div", { class: "panel" }, h("div", { class: "pb progress" },
        h("div", { class: "row" }, h("span", { class: "spin", "aria-hidden": "true" }), h("strong", { role: "status" }, text),
          run && run.running ? h("span", { class: "faint small mono" }, run.running) : null),
        h("div", { class: "bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(total || 1), "aria-valuenow": String(done), "aria-label": "Demo setup" },
          h("span", { style: `width:${pct}%` })),
        h("div", { class: "row" }, h("span", { class: "grow" }),
          st && st.state === "ready" ? h("button", { class: "btn ghost sm", onclick: () => { polling = false; onReady(); } }, "Open now", icon("arrow-right")) : null))));
  }

  async function poll() {
    if (polling) return;
    polling = true;
    const tick = async () => {
      if (!polling) return;
      try {
        const st = await loadStart();
        const run = st.setup_run;
        if (st.state === "ready" && (!run || run.finished || (run.total && run.done >= run.total))) {
          polling = false;
          toast("Demo ready");
          onReady();
          return;
        }
        progress(st);
      } catch (e) { put(msg, h("div", { class: "err" }, e.message)); }
    };
    every(1500, tick);
    tick();
  }

  async function choose(choice) {
    put(msg, h("div", { class: "row faint small" }, h("span", { class: "spin", "aria-hidden": "true" }), choice === "demo" ? "Creating demo…" : "Creating your project…"));
    if (choice === "demo") progress(null);
    try {
      const st = await api("/start", { method: "POST", body: { choice, run_jobs: true } });
      app.start = st;
      if (choice === "demo") { poll(); return; }
      location.hash = "#/overview";
      onReady();
    } catch (e) {
      if (e.status === 409 && /still being set up/i.test(e.message)) { poll(); return; }
      choices();
      put(msg, h("div", { class: "banner bad", role: "alert" }, icon("warning-octagon"), h("div", { class: "grow" }, e.message)));
    }
  }

  const st = app.start || {};
  if (st.setup_run && !st.setup_run.finished && st.state !== "ready") { progress(st); poll(); } else choices();
  return box;
}

// ------------------------------------------------------------------ overview
export async function pageOverview() {
  const o = await refreshSide();
  if (!o) throw new Error("The server did not answer.");
  drawProjectBadge();
  if (o.counts.running) every(3000, () => { if (location.hash.startsWith("#/overview") || location.hash === "" || location.hash === "#/") window.dispatchEvent(new CustomEvent("etl:reroute", { detail: { keep: true } })); });
  const newJob = h("a", { class: "btn primary", href: "#/jobs/new" }, line("plus", 16), "New job");
  const top = head("Overview", "What ran, what failed and what runs next.", newJob);
  const c = o.counts || {};
  // nothing yet: the empty state and the three steps, nothing else
  if (!c.connections && !c.destinations && !c.jobs) {
    return [head("Overview", "What ran, what failed and what runs next."),
      h("div", {}, emptyState({ art: "overview", title: "Nothing here yet",
        text: "Add a source, then a destination, then a job that moves data from one to the other.",
        actions: [h("a", { class: "btn primary", href: "#/sources/add?setup=1" }, line("plus", 16), "Add source")] }),
      setupSteps(o.setup))];
  }

  const jobsBox = h("div");
  const st = pageState("overview-jobs", 10);
  const jobs = o.jobs || [];
  function drawJobs() {
    if (!jobs.length) {
      put(jobsBox, h("p", { class: "faint", style: "padding:14px 16px" }, "No jobs yet. Create one to move data from a source to a destination."));
      return;
    }
    const shown = jobs.slice(st.offset, st.offset + st.limit);
    put(jobsBox, h("div", { class: "scroll-x" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", { style: "width:36px" }, h("span", { class: "sr" }, "Status")), h("th", {}, "Job"), h("th", {}, "Last run"), h("th", { class: "r hide-sm" }, "Rows loaded"), h("th", { class: "hide-sm" }, "Next run"), h("th", {}))),
      h("tbody", {}, shown.map((j) => {
        const r = j.last_run || {};
        return h("tr", { class: "click", onclick: () => (location.hash = `#/jobs/${enc(j.name)}`) },
          h("td", {}, statusCell(r.status)),
          h("td", {}, h("a", { class: "mono", href: `#/jobs/${enc(j.name)}`, onclick: (e) => e.stopPropagation() }, j.name)),
          h("td", { class: "small" }, r.started_at ? fmt.ago(r.started_at) : h("span", { class: "faint" }, "Never")),
          h("td", { class: "r num hide-sm" }, fmt.int(rowsOf(r, "load"))),
          h("td", { class: "muted small hide-sm" }, j.next_run ? fmt.ago(j.next_run) : "By hand"),
          h("td", { class: "r" }, h("button", { class: "btn sm", "aria-label": `Run ${j.name}`, disabled: r.status === "running",
            onclick: async (e) => { e.stopPropagation(); const x = await startRun(j.name); if (x && x.run_id) location.hash = `#/runs/${enc(x.run_id)}`; } }, icon("play"), h("span", { class: "lbl" }, "Run"))));
      })))),
    jobs.length > 10 ? h("div", { class: "pf" }, pager(st, jobs.length, drawJobs)) : null);
  }
  drawJobs();

  const alerts = o.alerts || [];
  return [
    top,
    setupSteps(o.setup),
    h("div", { class: "stats" },
      stat(fmt.int(o.counts.jobs), "Jobs", { art: "jobs", href: "#/jobs", sub: o.counts.scheduled ? `${fmt.int(o.counts.scheduled)} on a schedule` : "Started by hand" }),
      stat(fmt.int(o.counts.connections), "Sources", { art: "sources", href: "#/sources", sub: `${fmt.int(o.counts.destinations)} destinations` }),
      stat(fmt.int(o.counts.failed), "Failed last run", { icon: "warning-octagon", href: "#/runs", tone: o.counts.failed ? "bad" : "ok", sub: o.counts.running ? `${fmt.int(o.counts.running)} running now` : "None running" }),
      stat(fmt.bytes(o.staged_bytes), "Staged", { art: "stage", href: "#/stage", sub: "Kept for reruns" })),
    (o.running || []).length ? panel("Running now", null, h("div", { class: "pb stack tight" }, o.running.map((r) =>
      h("a", { class: "row", href: `#/runs/${enc(r.run_id)}` }, h("span", { class: "spin", "aria-hidden": "true" }), h("span", { class: "mono" }, r.job),
        h("span", { class: "faint small" }, `started ${fmt.ago(r.started_at)}`), h("span", { class: "grow" }), icon("arrow-right"))))) : null,
    panel(h("div", { class: "row" }, h("h2", {}, "Jobs"), pill(jobs.length)), h("a", { href: "#/jobs", class: "small" }, "All jobs"), jobsBox),
    alerts.length ? sect("overview-alerts", "Alerts", { icon: "warning", right: h("span", { class: "badge bad" }, String(alerts.length)) },
      ...alerts.map(alertRow), h("a", { class: "small", href: "#/runs" }, "All runs")) : null,
  ];
}

export function alertRow(a) {
  const bad = (a.level || a.status || "failed") !== "info";
  return h("div", { class: "row nowrap", style: "align-items:flex-start" },
    h("span", { style: `color:var(${bad ? "--danger" : "--primary"})` }, icon(bad ? "warning-octagon" : "info")),
    h("div", { class: "grow" },
      h("div", {}, h("span", { class: "mono" }, a.job || ""), " ", h("span", { class: bad ? "err" : "muted" }, a.message || a.error || a.status || "")),
      h("div", { class: "faint small" }, fmt.ago(a.at || a.created_at || a.finished_at))),
    a.run_id ? h("a", { class: "btn ghost sm", href: `#/runs/${enc(a.run_id)}` }, "Open") : null);
}
