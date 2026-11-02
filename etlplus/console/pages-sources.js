// Sources: saved connections (cards), the Add source gallery, the connection drawer,
// and the file and table browsers.
import {
  h, put, icon, pill, api, apiPage, enc, fmt, head, panel, empty, toast, pager, pageState, sect, drawer, dialog, confirmBox,
  copyBtn, CARD_SIZES, kindIcon, kindStatus, withSecrets, parseOptions, optionsText, clone, dataGrid, inlineError, store, debounce,
} from "./ui.js";
import { app, loadConnections, loadKinds, kindOf, kindLabel, isFileKind } from "./state.js";

const GROUP_ORDER = ["Databases", "Files", "Bank files", "Remote locations"];
const GROUP_ICON = { Databases: "database", Files: "file-text", "Bank files": "bank", "Remote locations": "cloud" };

export async function pageSources(args) {
  const [first] = args;
  await loadKinds();
  const saved = await loadConnections(true).catch(() => []);
  const tabs = h("div", { class: "tabs", role: "tablist" },
    h("a", { href: "#/sources", role: "tab", class: first === "add" ? "" : "on", "aria-selected": String(first !== "add") }, "Saved", pill(saved.length)),
    h("a", { href: "#/sources/add", role: "tab", class: first === "add" ? "on" : "", "aria-selected": String(first === "add") }, "Gallery"));
  const top = head("Sources", null, first === "add" ? null : h("a", { class: "btn primary", href: "#/sources/add" }, icon("plus"), "Add source"));
  if (first === "add") return [top, tabs, await galleryView({ onPick: pickFromGallery })];
  if (first) {
    const c = saved.find((x) => x.id === first);
    if (!c) return [top, tabs, inlineError(new Error(`No connection named ${first}.`))];
    return [top, tabs, h("div", { class: "row" }, h("a", { class: "btn ghost sm", href: "#/sources" }, icon("arrow-left"), "All sources"), h("span", { class: "grow" }),
      h("button", { class: "btn sm", onclick: () => openConnection({ conn: c }) }, icon("pencil-simple"), "Edit")),
    isFileKind(c.kind) ? filesBrowser(c) : await tableBrowser(c)];
  }
  return [top, tabs, savedGrid()];
}

// ------------------------------------------------------------------ saved connections
function where(c) {
  if (c.host) return `${c.host}${c.port ? `:${c.port}` : ""}${c.database ? ` / ${c.database}` : ""}`;
  return c.path || c.url || c.bucket || c.account_url || c.dsn || "-";
}

function savedGrid() {
  const box = h("div", { class: "stack" });
  const st = pageState("connections", 12, CARD_SIZES);
  async function draw() {
    put(box, h("div", { class: "cards" }, [1, 2, 3].map(() => h("div", { class: "sk", style: "height:132px" }))));
    let page;
    try { page = await apiPage("/connections", st); } catch (e) { put(box, inlineError(e)); return; }
    if (!page.total) {
      put(box, panel("Connections", null, empty("plugs", "No connections yet.", h("a", { class: "btn primary", href: "#/sources/add" }, icon("plus"), "Add source"))));
      return;
    }
    put(box, h("div", { class: "cards" }, page.items.map(connCard)), page.total > 12 ? pager(st, page.total, draw) : null);
  }
  draw();
  return box;
}

function connCard(c) {
  const k = kindOf(c.kind) || { kind: c.kind };
  const role = c.role || "source";
  return h("div", { class: "card" },
    h("div", { class: "ch" }, h("span", { class: "ico" }, icon(kindIcon(k))), h("span", { class: "ttl mono", title: c.id }, c.id),
      h("span", { class: `badge${role === "source" ? "" : " info"}` }, { source: "Source", target: "Target", both: "Both" }[role] || role)),
    h("div", { class: "cb" }, h("div", {}, kindLabel(c.kind)), h("div", { class: "mono", title: where(c) }, where(c))),
    h("div", { class: "cf" },
      h("a", { class: "btn sm", href: `#/sources/${enc(c.id)}` }, icon(isFileKind(c.kind) ? "folder-open" : "table"), isFileKind(c.kind) ? "Files" : "Tables"),
      h("button", { class: "btn ghost sm", onclick: () => quickTest(c.id) }, icon("plug-charging"), "Test"),
      h("span", { class: "grow" }),
      h("button", { class: "btn ghost sm icon", "aria-label": `Edit ${c.id}`, title: "Edit", onclick: () => openConnection({ conn: c }) }, icon("pencil-simple"))));
}

async function quickTest(id) {
  const d = dialog(`Test ${id}`);
  put(d.box, h("h2", {}, `Test ${id}`), h("div", { class: "sk", style: "height:80px" }));
  try {
    const r = await api(`/connections/${enc(id)}/test`, { method: "POST" });
    put(d.box, h("h2", {}, `Test ${id}`), testResult(r), h("div", { class: "row" }, h("span", { class: "grow" }), h("button", { class: "btn", onclick: d.close }, "Close")));
  } catch (e) {
    put(d.box, h("h2", {}, `Test ${id}`), inlineError(e), h("div", { class: "row" }, h("span", { class: "grow" }), h("button", { class: "btn", onclick: d.close }, "Close")));
  }
  const b = d.box.querySelector("button"); if (b) b.focus();
}

export function testResult(r) {
  return h("div", { class: `banner ${r.ok ? "ok" : "bad"}` }, icon(r.ok ? "check-circle" : "warning-octagon"),
    h("div", { class: "grow stack tight" },
      h("strong", {}, r.ok ? `Connected${r.server ? `: ${r.server}` : ""}` : "Could not connect"),
      h("div", { class: "steplist" }, (r.steps || r.checks || []).map((x) => {
        const ok = x.ok !== undefined ? x.ok : x.level !== "error";
        const warn = x.level === "warn";
        return h("div", {},
          icon(warn ? "warning" : ok ? "check-circle" : "x-circle", warn ? "w" : ok ? "y" : "x"),
          h("div", { class: "grow" }, h("span", {}, x.name || x.message), x.detail ? h("span", { class: "faint" }, `  ${x.detail}`) : null),
          x.ms !== undefined && x.ms !== null ? h("span", { class: "faint small num" }, fmt.dur(x.ms)) : null);
      }))));
}

// ------------------------------------------------------------------ gallery
function groupsOf(data) {
  let rows = Array.isArray(data) ? data : (data && (data.groups || data.cards)) || [];
  if (rows.length && (rows[0].cards || rows[0].items)) {
    return rows.map((g) => ({ name: g.group || g.label || g.name, cards: g.cards || g.items || [] }));
  }
  const by = new Map();
  for (const c of rows) {
    const g = c.group || "Databases";
    if (!by.has(g)) by.set(g, []);
    by.get(g).push(c);
  }
  return [...by.keys()].sort((a, b) => (GROUP_ORDER.indexOf(a) + 99) % 99 - (GROUP_ORDER.indexOf(b) + 99) % 99).map((g) => ({ name: g, cards: by.get(g) }));
}

function cardIcon(c) {
  if (c.kind) return kindIcon(kindOf(c.kind) || { kind: c.kind, category: c.group === "Remote locations" ? "remote" : null });
  return GROUP_ICON[c.group] || "file-text";
}

// the Add source gallery; onPick(card). Groups are tabs with counts; one page of cards at a time.
export async function galleryView({ onPick, roles }) {
  const box = h("div", { class: "stack" });
  let groups;
  try { groups = groupsOf(await api("/gallery")); } catch (e) { return inlineError(e); }
  if (roles) groups = groups.map((g) => ({ ...g, cards: g.cards.filter((c) => !c.roles || c.roles.some((r) => roles.includes(r))) })).filter((g) => g.cards.length);
  let q = store.get("gallery-q", "");
  let tab = store.get("gallery-tab", groups.length ? groups[0].name : "");
  if (!groups.some((g) => g.name === tab) && groups.length) tab = groups[0].name;
  const st = pageState("gallery", 12, CARD_SIZES);
  const tabsEl = h("div", { class: "tabs", role: "tablist", "aria-label": "Kinds of source" });
  const list = h("div", { class: "stack" });
  const search = h("input", { class: "input", type: "search", placeholder: "Search sources", "aria-label": "Search sources", value: q,
    oninput: debounce((e) => { q = e.target.value; store.set("gallery-q", q); st.offset = 0; draw(); }, 120) });

  function draw() {
    const f = q.trim().toLowerCase();
    const shown = groups.map((g) => ({ ...g, cards: g.cards.filter((c) => !f || [c.label, c.needs, c.kind, c.format, c.id].join(" ").toLowerCase().includes(f)) }));
    let cur = shown.find((g) => g.name === tab);
    if ((!cur || !cur.cards.length) && f) { const first = shown.find((g) => g.cards.length); if (first) { cur = first; tab = first.name; } }
    put(tabsEl, shown.map((g) => h("button", { type: "button", role: "tab", class: g.name === tab ? "on" : "", "aria-selected": String(g.name === tab),
      onclick: () => { tab = g.name; store.set("gallery-tab", tab); st.offset = 0; draw(); } }, icon(GROUP_ICON[g.name] || "plugs"), g.name, pill(g.cards.length))));
    if (!cur || !cur.cards.length) { put(list, empty("magnifying-glass", "Nothing matches.", h("button", { class: "btn", onclick: () => { q = ""; search.value = ""; store.set("gallery-q", ""); draw(); } }, "Clear search"))); return; }
    put(list, h("div", { class: "cards" }, cur.cards.slice(st.offset, st.offset + st.limit).map((c) => galleryCard(c, onPick))),
      cur.cards.length > st.limit || st.offset ? pager(st, cur.cards.length, draw) : null);
  }
  draw();
  put(box, h("div", { class: "row" }, h("div", { class: "grow", style: "max-width:420px" }, search)), tabsEl, list);
  return box;
}

// the whole card picks; the one button is quiet, so a page of cards does not read as a wall of buttons
function galleryCard(c, onPick) {
  const installed = c.installed !== false;
  return h("div", { class: "card pick", onclick: (e) => { if (!e.target.closest("button, a, code")) onPick(c); } },
    h("div", { class: "ch" }, h("span", { class: "ico" }, icon(cardIcon(c))), h("span", { class: "ttl wrap", title: c.label }, c.label), c.status ? kindStatus(c.status) : null),
    h("div", { class: "cb", title: c.verify_note || c.needs || "" }, c.needs || ""),
    h("div", { class: "cf" },
      installed ? null : h("div", { class: "cmd grow", title: c.install || "" }, h("code", {}, c.install || "Not installed"), c.install ? copyBtn(c.install, "Copy the install command") : null),
      installed ? h("span", { class: "grow" }) : null,
      h("button", { class: "btn ghost sm add", "aria-label": `Add ${c.label}`, onclick: () => onPick(c) }, icon("plus"), "Add")));
}

// a file or bank-file card asks where the file comes from, then opens the job builder
export async function pickFromGallery(card, after) {
  if (card.kind) { openConnection({ kind: card.kind, onSaved: after || ((saved) => { location.hash = `#/sources/${enc(saved.id)}`; }) }); return; }
  const conns = (await loadConnections(true)).filter((c) => isFileKind(c.kind));
  const d = dialog(`Read ${card.label} from`);
  let pick = conns.length ? conns[0].id : "";
  const go = () => {
    if (!pick) return;
    d.close();
    const qs = new URLSearchParams({ connection: pick, format: card.format || "" });
    if (card.layout) qs.set("layout", card.layout);
    if (after) after({ id: pick, format: card.format, layout: card.layout }); else location.hash = `#/jobs/new?${qs}`;
  };
  put(d.box, h("h2", {}, `Read ${card.label} from`),
    conns.length ? h("label", { class: "f" }, "Folder or remote location",
      h("select", { class: "input", onchange: (e) => { pick = e.target.value; } }, conns.map((c) => h("option", { value: c.id }, `${c.id} (${kindLabel(c.kind)})`))))
      : h("p", { class: "muted" }, "No folder or remote location yet."),
    h("div", { class: "row" },
      h("button", { class: "btn", onclick: () => { d.close(); openConnection({ kind: "folder", onSaved: (x) => { location.hash = `#/jobs/new?${new URLSearchParams({ connection: x.id, format: card.format || "" })}`; } }); } }, icon("folder-open"), "New folder"),
      h("a", { class: "btn ghost", href: "#/sources/add", onclick: () => { d.close(); store.set("gallery-q", ""); store.set("gallery-tab", "Remote locations"); } }, icon("cloud"), "Remote location"),
      h("span", { class: "grow" }),
      conns.length ? h("button", { class: "btn primary", onclick: go }, "Next", icon("arrow-right")) : null));
}

// ------------------------------------------------------------------ connection drawer
// openConnection({conn}) edits; openConnection({kind}) adds; onSaved(saved) after a save
export async function openConnection({ conn, kind, onSaved } = {}) {
  await loadKinds();
  const isNew = !conn;
  const k = kindOf(isNew ? kind : conn.kind) || { kind: kind || conn.kind, label: kind || conn.kind, fields: [], roles: ["source", "target"] };
  const draft = conn ? clone(conn) : { id: "", kind: k.kind, role: (k.roles || ["source"]).includes("source") ? "source" : "target", options: {} };
  if (isNew) for (const f of k.fields || []) if (f.default !== null && f.default !== undefined && !f.secret) draft[f.name] = f.default;
  if (isNew && k.default_port && (k.fields || []).some((f) => f.name === "port") && !draft.port) draft.port = k.default_port;
  const original = clone(draft);
  const secrets = {};
  const clears = {};
  let optText = optionsText(draft.options);
  const d = drawer({ title: isNew ? `New ${k.label}` : conn.id, label: "Connection" });
  const err = h("div", { class: "err", role: "alert" });
  const result = h("div");

  function fieldControl(f) {
    const id = `cf-${f.name}`;
    const set = (v) => { draft[f.name] = v; if (f.endpoint) drawNotes(); };
    if (f.secret || f.type === "password") {
      const saved = !!draft[`${f.name}_set`];
      return h("input", { id, class: "input", type: "password", autocomplete: "new-password", placeholder: saved ? "Saved; leave empty to keep" : "",
        oninput: (e) => { secrets[f.name] = e.target.value; } });
    }
    if (f.type === "select") {
      const choices = (f.choices || []).map((x) => (Array.isArray(x) ? x : [x, x]));
      return h("select", { id, class: "input", onchange: (e) => set(e.target.value) },
        !f.required ? h("option", { value: "" }, "Default") : null,
        choices.map(([v, l]) => h("option", { value: v, selected: String(draft[f.name] ?? f.default ?? "") === String(v) }, l)));
    }
    if (f.type === "bool") {
      return h("label", { class: "check" }, h("input", { id, type: "checkbox", checked: draft[f.name] ?? f.default ?? false, onchange: (e) => { set(e.target.checked); drawNotes(); } }), f.label);
    }
    if (f.type === "textarea") return h("textarea", { id, class: "input mono", rows: 3, oninput: (e) => set(e.target.value) }, draft[f.name] ?? "");
    return h("input", { id, class: `input${["path", "text", "int"].includes(f.type) ? " mono" : ""}`, type: f.type === "int" ? "number" : "text", value: draft[f.name] ?? "",
      autocomplete: "off", placeholder: f.default !== null && f.default !== undefined ? String(f.default) : "",
      oninput: (e) => set(f.type === "int" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value) });
  }

  function fieldBlock(f) {
    const label = h("span", { class: "row nowrap", style: "gap:6px" }, f.type === "bool" ? null : f.label,
      f.required ? h("span", { class: "dot req", title: "Required" }, h("span", { class: "sr" }, "required")) : null);
    const wide = ["textarea", "path"].includes(f.type) || f.name === "url" || f.name === "connection_string";
    const ctl = fieldControl(f);
    return h(f.type === "bool" ? "div" : "label", { class: "f", style: wide ? "grid-column:1/-1" : null },
      f.type === "bool" ? null : label, ctl,
      f.help ? h("span", { class: "help" }, f.help) : null,
      (f.secret || f.type === "password") && draft[`${f.name}_set`]
        ? h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!clears[f.name], onchange: (e) => { clears[f.name] = e.target.checked; } }), "Remove the saved value") : null);
  }

  const notes = h("div", { class: "stack tight" });
  function drawNotes() {
    const out = [];
    if (k.kind === "ftp" && draft.tls === false) out.push(h("div", { class: "banner bad" }, icon("warning-octagon"), h("div", {}, "Password sent in clear.")));
    if (!isNew) {
      const changed = (k.fields || []).some((f) => f.endpoint && String(draft[f.name] ?? "") !== String(original[f.name] ?? ""));
      const hasSecret = Object.keys(original).some((x) => x.endsWith("_set") && original[x]);
      if (changed && hasSecret) out.push(h("div", { class: "banner" }, icon("warning"), h("div", { class: "small" }, "A new address clears the saved secrets. Enter them again.")));
    }
    put(notes, out);
  }

  function draw() {
    const roles = k.roles || ["source", "target"];
    const roleOpts = [["source", "Source"], ["target", "Target"], ["both", "Both"]].filter(([r]) => r === "both" ? roles.includes("source") && roles.includes("target") : roles.includes(r));
    put(d.body,
      h("div", { class: "row" }, h("span", { class: "badge info" }, icon(kindIcon(k)), k.label), k.status ? kindStatus(k.status) : null,
        k.installed === false ? h("div", { class: "cmd", style: "flex:1 1 220px" }, h("code", {}, k.install || ""), k.install ? copyBtn(k.install, "Copy the install command") : null) : null),
      isNew ? h("label", { class: "f" }, "Name", h("input", { class: "input mono", value: draft.id, placeholder: `${k.kind}_1`, autocomplete: "off", oninput: (e) => { draft.id = e.target.value; } }),
        h("span", { class: "help" }, "Letters, digits, - and _. Jobs use this name.")) : null,
      roleOpts.length > 1 ? h("div", { class: "f" }, h("span", {}, "Role"),
        h("div", { class: "seg", role: "radiogroup", "aria-label": "Role" }, roleOpts.map(([r, l]) =>
          h("button", { type: "button", role: "radio", "aria-checked": String(draft.role === r), class: draft.role === r ? "on" : "", onclick: () => { draft.role = r; draw(); } }, l))),
        h("span", { class: "help" }, draft.role === "source" ? (k.read_only && !/^read only$/i.test(k.read_only) ? `Read only: ${k.read_only}.` : "Read only. Every query is checked.") : "Jobs may write here.")) : null,
      h("div", { class: "grid2" }, (k.fields || []).map(fieldBlock)),
      notes,
      sect("conn-advanced", "Advanced", { flat: true },
        h("label", { class: "f" }, "Options", h("textarea", { class: "input mono", rows: 3, oninput: (e) => { optText = e.target.value; } }, optText),
          h("span", { class: "help" }, "One key=value per line.")),
        (k.fields || []).some((f) => f.name === "password") ? h("label", { class: "f" }, "Or read the password from",
          h("input", { class: "input mono", value: draft.password_env || "", placeholder: "CORE_PASSWORD", oninput: (e) => { draft.password_env = e.target.value; } }),
          h("span", { class: "help" }, "An environment variable on the server.")) : null),
      err, result);
    drawNotes();
  }

  function payload() {
    const p = { ...draft, options: parseOptions(optText) };
    for (const [x, v] of Object.entries(p)) if (v === "" || v === null) delete p[x];
    const body = withSecrets(p, secrets);
    for (const [name, on] of Object.entries(clears)) if (on) body[`clear_${name}`] = true;
    return body;
  }

  async function test() {
    err.textContent = "";
    let p;
    try { p = payload(); } catch (e) { err.textContent = e.message; return; }
    put(result, h("div", { class: "sk", style: "height:72px" }));
    try { put(result, testResult(await api(`/connections/${enc(p.id || "draft")}/test`, { method: "POST", body: p }))); }
    catch (e) { put(result); err.textContent = e.message; }
  }

  async function save(thenTest) {
    err.textContent = "";
    let p;
    try { p = payload(); } catch (e) { err.textContent = e.message; return; }
    if (!p.id) { err.textContent = "Give the connection a name."; return; }
    try {
      const saved = await api(`/connections/${enc(p.id)}`, { method: "PUT", body: p });
      toast(`${saved.id || p.id} saved`);
      app.connections = null;
      for (const x of Object.keys(secrets)) delete secrets[x];
      Object.assign(draft, saved);
      if (thenTest) {
        draw();
        put(result, h("div", { class: "sk", style: "height:72px" }));
        try { put(result, testResult(await api(`/connections/${enc(saved.id || p.id)}/test`, { method: "POST" }))); } catch (e) { err.textContent = e.message; }
      } else d.close();
      if (onSaved) onSaved(saved.id ? saved : { ...p });
      else window.dispatchEvent(new CustomEvent("etl:reroute", { detail: { keep: true } }));
    } catch (e) {
      const probs = e.data && e.data.problems;
      err.textContent = probs ? probs.map((x) => (typeof x === "string" ? x : x.message)).join(" ") : e.message;
    }
  }

  async function remove() {
    if (!(await confirmBox(`Delete ${conn.id}?`, "Its saved secrets go too.", "Delete", true))) return;
    try {
      await api(`/connections/${enc(conn.id)}`, { method: "DELETE" });
      toast(`${conn.id} deleted`);
      app.connections = null;
      d.close();
      location.hash = "#/sources";
    } catch (e) { err.textContent = e.message; }
  }

  put(d.foot,
    h("button", { class: "btn", onclick: test }, icon("plug-charging"), "Test"),
    h("button", { class: "btn", onclick: () => save(false) }, icon("floppy-disk"), "Save"),
    h("button", { class: "btn primary", onclick: () => save(true) }, icon("check"), "Save and test"),
    h("span", { class: "grow" }),
    !isNew ? h("button", { class: "btn ghost danger", onclick: remove }, icon("trash"), "Delete") : null);
  draw();
  d.focus();
  return d;
}

// ------------------------------------------------------------------ files browser (folder, share, remote)
export function filesBrowser(conn, { onUse } = {}) {
  const st = pageState(`files-${conn.id}`, 25);
  let pattern = "";
  const listBox = h("div");
  const detail = h("div", { class: "pb stack" }, empty("file-text", "Pick a file to preview or detect its format."));
  let current = null;

  async function draw() {
    put(listBox, h("div", { class: "pb" }, h("div", { class: "sk", style: "height:160px" })));
    let page;
    try { page = await apiPage(`/connections/${enc(conn.id)}/files?pattern=${enc(pattern)}`, st); }
    catch (e) {
      if (e.status !== 404) { put(listBox, h("div", { class: "pb" }, inlineError(e))); return; }
      // a server without the files route: the v1 listing
      try {
        const all = (await api(`/connections/${enc(conn.id)}/tables`)).map((t) => ({ path: t.name, size: t.bytes, modified: t.modified }))
          .filter((x) => !pattern || x.path.includes(pattern.replace(/\*/g, "")));
        page = { items: all.slice(st.offset, st.offset + st.limit), total: all.length };
      } catch (e2) { put(listBox, h("div", { class: "pb" }, inlineError(e2))); return; }
    }
    put(listBox, page.total ? h("div", { class: "scroll-x" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", { class: "r" }, "Size"), h("th", { class: "hide-sm" }, "Modified"))),
      h("tbody", {}, page.items.map((f) => h("tr", { class: `click${current === f.path ? " sel" : ""}`, tabindex: "0",
        onclick: () => pick(f.path), onkeydown: (e) => { if (e.key === "Enter") pick(f.path); } },
        h("td", { class: "mono small", style: "overflow-wrap:anywhere" }, f.path),
        h("td", { class: "r num small" }, fmt.bytes(f.size)),
        h("td", { class: "small muted hide-sm" }, fmt.time(f.modified)))))))
      : empty("folder-open", pattern ? "No file matches." : "No files."),
    page.total > 10 ? h("div", { class: "pf" }, pager(st, page.total, draw)) : null);
  }

  async function pick(path) {
    current = path;
    listBox.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel"));
    const chip = h("div", { class: "row" });
    const prev = h("div");
    put(detail,
      h("div", { class: "row" }, h("h3", { class: "mono grow", style: "overflow-wrap:anywhere" }, path),
        h("button", { class: "btn sm", onclick: detect }, icon("magnifying-glass"), "Detect format")),
      chip, prev);
    let detected = null;
    async function detect() {
      put(chip, h("span", { class: "spin", "aria-hidden": "true" }), h("span", { class: "faint small" }, "Reading the first 64 KB"));
      try {
        const r = await api("/formats/detect", { method: "POST", body: { connection: conn.id, file: path } });
        const best = (Array.isArray(r) ? r : r.detected || r.results || [])[0];
        detected = best || null;
        put(chip, best ? h("span", { class: "chip on static" }, icon("seal-check"), `Detected: ${(best.format || "").toUpperCase()} (${Number(best.confidence).toFixed(2)})`)
          : h("span", { class: "chip static" }, "No bank format found"),
        best && best.reason ? h("span", { class: "faint small" }, best.reason) : null,
        h("span", { class: "grow" }),
        onUse ? h("button", { class: "btn sm primary", onclick: () => onUse(path, detected) }, icon("check"), "Use")
          : h("a", { class: "btn sm primary", href: `#/jobs/new?${new URLSearchParams({ connection: conn.id, files: path, format: detected ? detected.format : "" })}` }, icon("flow-arrow"), "New job"));
        preview(detected && detected.format);
      } catch (e) { put(chip, h("span", { class: "err" }, e.message)); }
    }
    async function preview(format) {
      put(prev, h("div", { class: "sk", style: "height:140px" }));
      try {
        const body = { files: path };
        if (format) body.format = format;
        const p = await api(`/connections/${enc(conn.id)}/preview`, { method: "POST", body });
        put(prev, checksRow(p.checks), dataGrid(p));
      } catch (e) { put(prev, h("div", { class: "err" }, e.message)); }
    }
    put(chip, onUse ? h("button", { class: "btn sm primary", onclick: () => onUse(path, null) }, icon("check"), "Use") : null);
    preview(null);
  }

  draw();
  return panel(h("div", { class: "row" }, icon("folder-open"), h("h2", {}, `Files in ${conn.id}`)),
    h("input", { class: "input sm mono", style: "width:220px", placeholder: "*.txt", "aria-label": "File pattern",
      oninput: debounce((e) => { pattern = e.target.value.trim(); st.offset = 0; draw(); }, 250) }),
    h("div", { class: "browse" }, listBox, detail));
}

export function checksRow(checks) {
  if (!checks || !checks.length) return null;
  return h("div", { class: "chips" }, checks.map((c) => {
    const lv = c.level || (c.ok === false ? "error" : "ok");
    return h("span", { class: `badge ${lv === "error" ? "bad" : lv === "warn" ? "warn" : "ok"}`, title: c.message || "" },
      icon(lv === "error" ? "x-circle" : lv === "warn" ? "warning" : "check-circle"), c.name || c.message);
  }));
}

// ------------------------------------------------------------------ table browser (databases)
export async function tableBrowser(conn, onPick) {
  let tables = [];
  try { tables = await api(`/connections/${enc(conn.id)}/tables`); } catch (e) { return panel(`Tables in ${conn.id}`, null, h("div", { class: "pb" }, inlineError(e))); }
  const st = pageState(`tables-${conn.id}`, 25);
  let filter = "";
  let current = null;
  const fullName = (t) => (t.schema ? `${t.schema}.${t.name}` : t.name);
  const listBox = h("div");
  const right = h("div", { class: "pb stack" }, empty("table", "Pick a table to see its columns and rows."));
  function drawList() {
    const f = filter.toLowerCase();
    const shown = tables.filter((t) => fullName(t).toLowerCase().includes(f));
    const pageRows = shown.slice(st.offset, st.offset + st.limit);
    put(listBox, h("div", { class: "list" }, pageRows.length ? pageRows.map((t) => h("button", { class: current === fullName(t) ? "on" : "", onclick: () => pick(fullName(t)) },
      icon(t.kind === "view" ? "eye" : "table"),
      h("span", { class: "grow mono", style: "font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, fullName(t)),
      t.rows !== undefined && t.rows !== null ? h("span", { class: "faint small num" }, fmt.int(t.rows)) : null))
      : h("div", { class: "empty" }, h("p", { class: "small" }, tables.length ? "Nothing matches." : "No tables."))),
    shown.length > 10 ? h("div", { class: "pf" }, pager(st, shown.length, drawList)) : null);
  }
  async function pick(name) {
    current = name;
    drawList();
    put(right, h("div", { class: "sk", style: "height:120px" }), h("div", { class: "sk", style: "height:180px" }));
    const [cols, prev] = await Promise.allSettled([
      api(`/connections/${enc(conn.id)}/columns?table=${enc(name)}`),
      api(`/connections/${enc(conn.id)}/preview`, { method: "POST", body: { table: name } }),
    ]);
    if (current !== name) return;
    put(right,
      h("div", { class: "row" }, h("h3", { class: "mono grow", style: "overflow-wrap:anywhere" }, name),
        onPick ? h("button", { class: "btn sm primary", onclick: () => onPick(name, cols.value || []) }, icon("check"), "Use") : null),
      cols.status === "fulfilled" ? sect("browse-columns", `Columns (${cols.value.length})`, { flat: true, open: true },
        h("div", { class: "scroll-x" }, h("table", { class: "t" },
          h("thead", {}, h("tr", {}, h("th", {}, "Column"), h("th", {}, "Type"), h("th", {}, "Nulls"))),
          h("tbody", {}, cols.value.map((c) => h("tr", {}, h("td", { class: "mono small" }, c.name), h("td", { class: "mono small muted" }, c.type), h("td", { class: "small faint" }, c.nullable === false ? "No" : "Yes")))))))
        : h("div", { class: "err" }, cols.reason.message),
      h("div", { class: "faint small" }, "First rows"),
      prev.status === "fulfilled" ? dataGrid(prev.value) : h("div", { class: "err" }, prev.reason.message));
  }
  drawList();
  return panel(h("div", { class: "row" }, icon("table"), h("h2", {}, `Tables in ${conn.id}`), h("span", { class: "faint small" }, fmt.int(tables.length))), null,
    h("div", { class: "browse" }, h("div", {}, h("div", { class: "filter" }, h("input", { class: "input", type: "search", placeholder: "Find a table", "aria-label": "Find a table",
      oninput: (e) => { filter = e.target.value; st.offset = 0; drawList(); } })), listBox), right));
}
