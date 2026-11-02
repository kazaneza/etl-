// Settings: the Project panel (demo or my data, reset demo), notifications, drivers, access.
import { h, put, icon, api, head, panel, toast, pager, pageState, sect, confirmBox, clone, cleanObj, lab, inp, seg, kindStatus, copyBtn } from "./ui.js";
import { app, loadStart, loadKinds, refreshSide, drawProjectBadge } from "./state.js";

export async function pageSettings() {
  const [d, start, kinds] = await Promise.all([api("/settings"), loadStart(), loadKinds()]);
  refreshSide();
  const s = clone(d.settings);
  const n = s.notify = s.notify || {};
  const email = n.email = n.email && typeof n.email === "object" ? n.email : {};
  let pw = "";
  let clearPw = false;
  const err = h("div", { class: "err", role: "alert" });

  // ---- project choice
  const projectBox = h("div", { class: "pb stack" });
  function drawProject() {
    const st = app.start || start;
    const fixed = st.mode === "fixed";
    const active = st.active;
    const proj = Object.fromEntries((st.projects || []).map((p) => [p.id, p]));
    const choice = (id, label, ic, line) => {
      const p = proj[id] || {};
      const on = active === id;
      return h("div", { class: `choice ${id}${on ? " on" : ""}` },
        h("div", { class: "row nowrap" }, h("div", { class: "ico" }, icon(ic)), h("div", { class: "grow" }, h("h2", {}, label),
          h("div", { class: "small faint" }, p.exists ? `${p.jobs ?? 0} jobs, ${p.connections ?? 0} sources` : "Not created")),
          on ? h("span", { class: "badge info" }, icon("check"), "In use") : null),
        h("p", { class: "small" }, line),
        h("div", { class: "row" }, on ? null : h("button", { class: "btn sm", onclick: () => switchTo(id, label) }, icon("arrows-left-right"), `Use ${label.toLowerCase()}`),
          on && id === "demo" ? h("button", { class: "btn sm ghost danger", onclick: resetDemo }, icon("arrow-counter-clockwise"), "Reset demo") : null));
    };
    put(projectBox,
      fixed ? null : h("div", { class: "choices" },
        choice("demo", "Demo data", "flask", "Made-up files and ready jobs."),
        choice("real", "My data", "plugs", "Your own sources and jobs.")),
      h("div", { class: "grid3" },
        lab("Name", inp(s, "name")),
        lab("Time zone", inp(s, "timezone", { mono: true, placeholder: "UTC" }), "Schedules run in this zone."),
        lab("Staged runs to keep", inp(s, "keep_runs", { type: "number", mono: true, min: 1 }), "Per job.")));
  }
  async function switchTo(id, label) {
    const st = app.start || start;
    if (st.active === id) return;
    const ok = await confirmBox(`Switch to ${label}?`, "The other project is kept as it is.", "Switch");
    if (!ok) { drawProject(); return; }
    try {
      app.start = await api("/start", { method: "POST", body: { choice: id, run_jobs: true } });
      app.connections = null;
      drawProjectBadge();
      toast(`Now on ${label}`);
      location.hash = "#/overview";
    } catch (e) { toast(e.message, true); drawProject(); }
  }
  async function resetDemo() {
    if (!(await confirmBox("Reset the demo?", "Its runs, stage and changes go. My data is not touched.", "Reset demo", true))) return;
    try { await api("/projects/demo/reset", { method: "POST", body: { confirm: "demo" } }); app.start = null; toast("Demo reset"); location.hash = "#/overview"; window.dispatchEvent(new CustomEvent("etl:reroute")); }
    catch (e) { toast(e.message, true); }
  }
  drawProject();

  // ---- notifications
  async function save() {
    err.textContent = "";
    const body = clone(s);
    body.notify.email = cleanObj(body.notify.email);
    if (!body.notify.email.host) body.notify.email = null;
    if (!body.notify.webhook) body.notify.webhook = null;
    delete body.notify.smtp_password_set;
    if (pw) body.notify.smtp_password = pw;
    if (clearPw) body.notify.clear_smtp_password = true;
    try { await api("/settings", { method: "PUT", body }); toast("Settings saved"); pw = ""; }
    catch (e) { err.textContent = e.message; }
  }
  async function testNotify() {
    try {
      const r = await api("/notify/test", { method: "POST" });
      const why = (r.channels || []).filter((c) => !c.ok).map((c) => `${c.channel}: ${c.detail}`).join(" ");
      toast(r.ok === false ? (why || r.detail || "Not sent") : "Test sent", r.ok === false);
    } catch (e) { toast(e.message, true); }
  }
  const hook = n.webhook && typeof n.webhook === "object" ? n.webhook : n;
  const hookKey = n.webhook && typeof n.webhook === "object" ? "url" : "webhook";

  // ---- drivers (paginated)
  const drvBox = h("div");
  const dst = pageState("drivers", 10);
  const rows = Array.isArray(kinds) ? kinds : [];
  function drawDrivers() {
    put(drvBox, h("div", { class: "scroll-x" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "Kind"), h("th", {}, "Status"), h("th", {}, "Installed"), h("th", { class: "hide-sm" }, "Read only by"))),
      h("tbody", {}, rows.slice(dst.offset, dst.offset + dst.limit).map((k) => h("tr", {},
        h("td", {}, h("div", {}, k.label), h("div", { class: "faint small mono" }, k.kind)),
        h("td", {}, kindStatus(k.status)),
        h("td", {}, k.installed !== false ? h("span", { class: "badge ok" }, icon("check"), "Yes")
          : h("div", { class: "cmd", style: "max-width:260px" }, h("code", {}, k.install || "Not installed"), k.install ? copyBtn(k.install, "Copy the install command") : null)),
        h("td", { class: "small muted hide-sm", style: "max-width:320px" }, k.read_only || "-")))))),
    rows.length > 10 ? h("div", { class: "pf" }, pager(dst, rows.length, drawDrivers)) : null);
  }
  drawDrivers();

  return [
    head("Settings", null, h("button", { class: "btn primary", onclick: save }, icon("floppy-disk"), "Save")),
    err,
    panel(h("div", { class: "row" }, icon("folder-simple"), h("h2", {}, "Project")), null, projectBox),
    sect("settings-notify", "Notifications", { icon: "bell" },
      seg("Default for new jobs", [["never", "Never"], ["failure", "When it fails"], ["always", "Every run"]], n.on || "failure", (v) => { n.on = v; }),
      lab("Webhook", inp(hook, hookKey, { mono: true, placeholder: "hooks.example.com/etl" }), "Gets a JSON post for each run."),
      h("div", { class: "grid2" },
        lab("Email to", inp(email, "to", { placeholder: "data-team@example.com" }), "Separate several with commas."),
        lab("From", inp(email, "from", { placeholder: "etl@example.com" }))),
      h("div", { class: "grid3" },
        lab("SMTP server", inp(email, "host", { mono: true, placeholder: "smtp.example.com" })),
        lab("Port", inp(email, "port", { type: "number", mono: true, placeholder: "587" })),
        lab("User", inp(email, "user", { mono: true, autocomplete: "off" }))),
      lab("SMTP password", h("input", { class: "input", type: "password", autocomplete: "new-password",
        placeholder: n.smtp_password_set ? "Saved; leave empty to keep" : "", oninput: (e) => { pw = e.target.value; } }), "Stored in the secrets store, never shown again."),
      h("div", { class: "row" },
        h("label", { class: "f", style: "flex-direction:row;align-items:center;gap:8px" }, "Security",
          h("select", { class: "input", style: "width:auto", onchange: (e) => { email.security = e.target.value; } },
            [["starttls", "STARTTLS"], ["ssl", "SSL"], ["none", "None"]].map(([v, l]) => h("option", { value: v, selected: (email.security || "starttls") === v }, l)))),
        n.smtp_password_set ? h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => { clearPw = e.target.checked; } }), "Remove the saved password") : null),
      h("div", { class: "row" }, h("button", { class: "btn", onclick: testNotify }, icon("bell"), "Send a test"))),
    sect("settings-drivers", "Drivers", { icon: "plugs", right: h("span", { class: "faint small" }, `${rows.filter((k) => k.installed !== false).length} of ${rows.length} installed`) }, drvBox),
    sect("settings-access", "Access", { icon: "lock-key" }, h("p", { class: "small muted" }, d.auth
      ? "The API asks for a token (ETLPLUS_TOKEN). This tab keeps it until it closes."
      : "No token is set. Anyone who can reach this address can use the console. Set ETLPLUS_TOKEN to require one."),
      h("a", { class: "small", href: "/api/docs" }, "API reference")),
  ];
}
