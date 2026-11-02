// etl+ console: the shell and the router. Every change goes through /api and lands in the
// project's own files, so the console and the command line always agree.
import { h, put, line, api, enc, skeleton, errorBox, stopTimers, closeOverlays } from "./ui.js";
import { app, loadStart, refreshSide, drawProjectBadge, projectInfo, switchProject, KIND_WORD } from "./state.js";
import { pageChooser, pageOverview } from "./pages-home.js";
import { pageSources } from "./pages-sources.js";
import { pageDestinations } from "./pages-destinations.js";
import { pageJobs } from "./pages-jobs.js";
import { pageRuns, pageStage } from "./pages-runs.js";
import { pageLayouts } from "./layouts.js";
import { pageSettings } from "./pages-settings.js";

const $page = document.getElementById("page");
const $shell = document.getElementById("shell");

const PAGES = {
  overview: pageOverview, sources: pageSources, destinations: pageDestinations, jobs: pageJobs,
  runs: pageRuns, stage: pageStage, layouts: pageLayouts, settings: pageSettings,
};
// v1 links keep working
const ALIASES = { connections: "sources" };

function parts() {
  const raw = location.hash.replace(/^#\/?/, "").split("?")[0];
  const p = raw.split("/").filter((x) => x !== "");
  let name = p[0] || "overview";
  if (ALIASES[name]) name = ALIASES[name];
  return { name: PAGES[name] ? name : "overview", args: p.slice(1).map((x) => decodeURIComponent(x)) };
}

let seq = 0;
async function route(keep = false) {
  stopTimers();
  if (!app.start || app.start.state === "choose") {
    try { await loadStart(); } catch (e) { $shell.classList.add("bare"); put($page, errorBox(e, () => route())); return; }
  }
  if (app.start.state === "choose") { showChooser(); return; }
  $shell.classList.remove("bare");
  drawProjectBadge();
  const { name, args } = parts();
  document.querySelectorAll("#nav a").forEach((a) => {
    const on = a.dataset.page === name;
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  const mine = ++seq;
  if (!keep || !$page.childElementCount) put($page, skeleton());
  try {
    const out = await PAGES[name](args, { keep });
    if (mine !== seq) return;
    const y = window.scrollY;
    put($page, ...(Array.isArray(out) ? out : [out]).filter(Boolean));
    if (keep) window.scrollTo(0, y);
  } catch (e) {
    if (mine !== seq) return;
    if (e.status === 409 && e.data && e.data.code === "choose") return;
    put($page, errorBox(e, () => route()));
  }
}

async function showChooser() {
  $shell.classList.add("bare");
  const mine = ++seq;
  const out = await pageChooser(() => { app.start = null; route(); });
  if (mine === seq) put($page, out);
}

window.addEventListener("hashchange", () => { closeOverlays(); window.scrollTo(0, 0); route(); });
window.addEventListener("etl:reroute", (e) => route(!!(e.detail && e.detail.keep)));
// any call answered 409 "choose": the person has not chosen yet
window.addEventListener("etl:choose", async () => {
  if (app.start && app.start.state === "choose") return;
  app.start = null;
  route();
});

// ------------------------------------------------------------------ theme
const themeBtn = document.getElementById("theme");
// dark is the default; light only when chosen
function isDark() { return document.documentElement.dataset.theme !== "light"; }
function themeIcon() {
  put(themeBtn, line(isDark() ? "sun" : "moon"));
  themeBtn.setAttribute("aria-label", isDark() ? "Switch to light theme" : "Switch to dark theme");
}
themeBtn.addEventListener("click", () => {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("etlplus-theme", next); } catch (e) { /* private mode */ }
  themeIcon();
});
themeIcon();


// ------------------------------------------------------------------ find a job or source (top bar)
const $search = document.getElementById("search");
const $q = document.getElementById("search-q");
let jumpItems = null;
let jumpLoad = null;
let jumpAt = 0;
let jumpSel = 0;
const jumpBox = h("div", { class: "jump hidden", id: "jump", role: "listbox", "aria-label": "Matches" });
$search.append(jumpBox);
// read once, kept for 30 s; a focus after that reads again
function loadJump() {
  if (jumpLoad && Date.now() - jumpAt < 30000) return jumpLoad;
  jumpAt = Date.now();
  jumpLoad = readJump();
  return jumpLoad;
}
async function readJump() {
  const [jobs, conns, dests] = await Promise.allSettled([api("/jobs"), api("/connections"), api("/destinations")]);
  const list = (r) => (r.status === "fulfilled" && Array.isArray(r.value) ? r.value : []);
  jumpItems = [
    ...list(jobs).map((j) => ({ label: j.name, kind: "Job", art: "jobs", href: `#/jobs/${enc(j.name)}` })),
    ...list(conns).map((c) => ({ label: c.id, kind: "Source", art: "sources", href: `#/sources/${enc(c.id)}` })),
    ...list(dests).map((x) => ({ label: x.id, kind: "Destination", art: "destinations", href: `#/destinations/${enc(x.id)}` })),
  ];
  return jumpItems;
}
function closeJump() { jumpBox.classList.add("hidden"); $q.setAttribute("aria-expanded", "false"); }
async function drawJump() {
  const f = $q.value.trim().toLowerCase();
  if (!f) { closeJump(); return; }
  if (!jumpItems) {
    put(jumpBox, h("div", { class: "none" }, "Searching..."));
    jumpBox.classList.remove("hidden");
  }
  const items = (await loadJump()).filter((x) => x.label.toLowerCase().includes(f)).slice(0, 8);
  jumpSel = Math.min(jumpSel, Math.max(0, items.length - 1));
  put(jumpBox, items.length ? items.map((x, i) => h("a", { href: x.href, role: "option", class: i === jumpSel ? "on" : "", "aria-selected": String(i === jumpSel),
    onmousedown: (e) => e.preventDefault(), onclick: () => { $q.value = ""; closeJump(); } },
    line(x.art, 16), h("span", {}, x.label), h("span", { class: "faint" }, x.kind))) : h("div", { class: "none" }, "Nothing matches."));
  jumpBox.classList.remove("hidden");
  $q.setAttribute("aria-expanded", "true");
}
$q.addEventListener("input", () => { jumpSel = 0; drawJump(); });
$q.addEventListener("focus", () => { loadJump(); if ($q.value) drawJump(); });
$q.addEventListener("blur", () => setTimeout(closeJump, 120));
$q.addEventListener("keydown", (e) => {
  const opts = [...jumpBox.querySelectorAll("a")];
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!opts.length) return;
    jumpSel = (jumpSel + (e.key === "ArrowDown" ? 1 : opts.length - 1)) % opts.length;
    drawJump();
  } else if (e.key === "Enter" && opts[jumpSel]) {
    location.hash = opts[jumpSel].getAttribute("href"); $q.value = ""; closeJump(); $q.blur();
  } else if (e.key === "Escape") { $q.value = ""; closeJump(); $q.blur(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(input|textarea|select)$/i.test(t.tagName))) return;
  e.preventDefault();
  $q.focus();
});

// a project switch: the lists the search box read belong to the other project
window.addEventListener("etl:project", () => { jumpItems = null; jumpLoad = null; });

// ------------------------------------------------------------------ project menu (top bar scope, side foot name)
// Demo data or My data. POST /api/start { choice } makes one active; the other is kept as it is.
const $scope = document.getElementById("scope");
const $projName = document.getElementById("project-name");
let menu = null;
function closeMenu(focusBack = false) {
  if (!menu) return;
  const { el, trigger, onDoc } = menu;
  menu = null;
  el.remove();
  document.removeEventListener("mousedown", onDoc, true);
  trigger.setAttribute("aria-expanded", "false");
  if (focusBack) trigger.focus();
}
function openMenu(trigger) {
  if (menu) { const same = menu.trigger === trigger; closeMenu(); if (same) return; }
  const st = app.start || {};
  const list = Object.fromEntries((st.projects || []).map((p) => [p.id, p]));
  const busy = h("div", { class: "mnote", role: "status" });
  const items = ["real", "demo"].map((id) => {
    const p = list[id] || {};
    const on = st.active === id;
    const what = p.exists ? `${p.jobs ?? 0} ${p.jobs === 1 ? "job" : "jobs"}, ${p.connections ?? 0} ${p.connections === 1 ? "source" : "sources"}`
      : id === "demo" ? "Made-up files and ready jobs" : "Starts empty";
    return h("button", { type: "button", role: "menuitemradio", "aria-checked": String(on), class: on ? "on" : "",
      onclick: async (e) => {
        if (on) { closeMenu(true); return; }
        e.currentTarget.disabled = true;
        put(busy, h("span", { class: "spin", "aria-hidden": "true" }), id === "demo" && !p.exists ? "Creating the demo..." : "Switching...");
        try {
          await switchProject(id);
          closeMenu();
          if (location.hash.startsWith("#/overview") || location.hash === "" || location.hash === "#/") route(); else location.hash = "#/overview";
        } catch (err) { put(busy, h("span", { class: "err" }, err.message)); e.currentTarget.disabled = false; }
      } },
    h("span", { class: "mi" }, h("span", { class: "ml" }, KIND_WORD[id]), h("span", { class: "cs" }, what)),
    on ? line("check", 16, "mk") : null);
  });
  const el = h("div", { class: "menu", id: "scope-menu", role: "menu", "aria-label": "Data" },
    ...items, busy,
    h("div", { class: "msep" }),
    h("a", { href: "#/settings", role: "menuitem", class: "mlink", onclick: () => closeMenu() }, "Project settings"),
    h("p", { class: "cs mfoot" }, "Both are kept when you switch."));
  el.addEventListener("keydown", (e) => {
    const opts = [...el.querySelectorAll("button, a")];
    const i = opts.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); closeMenu(true); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      opts[(i + (e.key === "ArrowDown" ? 1 : opts.length - 1)) % opts.length].focus();
    } else if (e.key === "Tab") closeMenu();
  });
  document.body.append(el);
  const r = trigger.getBoundingClientRect();
  const w = el.offsetWidth;
  const left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
  // under the trigger, or above it when it sits low (the side foot)
  if (r.bottom + el.offsetHeight + 8 > window.innerHeight) el.style.top = `${Math.max(8, r.top - el.offsetHeight - 6)}px`;
  else el.style.top = `${r.bottom + 6}px`;
  el.style.left = `${left}px`;
  const onDoc = (e) => { if (!el.contains(e.target) && !trigger.contains(e.target)) closeMenu(); };
  document.addEventListener("mousedown", onDoc, true);
  trigger.setAttribute("aria-expanded", "true");
  menu = { el, trigger, onDoc };
  const first = el.querySelector("button.on") || el.querySelector("button");
  if (first) first.focus();
}
$scope.addEventListener("click", () => { if (!projectInfo().fixed) openMenu($scope); });
$projName.addEventListener("click", (e) => {
  if (projectInfo().fixed) return;
  e.preventDefault();
  openMenu($projName);
});
window.addEventListener("hashchange", () => closeMenu());
window.addEventListener("resize", () => closeMenu());

(async () => {
  await route();
  if (app.start && app.start.state !== "choose") { drawProjectBadge(); refreshSide(); }
})();

export { route, h };
