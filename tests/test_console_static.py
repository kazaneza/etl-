"""Static checks for the console (SPEC_V2 section 13.8), plus the pure console functions run
under Node when it is installed. Nothing here starts a server or a browser."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CONSOLE = Path(__file__).resolve().parent.parent / "etlplus" / "console"
JS = sorted(CONSOLE.glob("*.js"))
TEXT_FILES = [CONSOLE / "index.html", CONSOLE / "app.css", *JS]


def read(p):
    return p.read_text(encoding="utf-8")


# ------------------------------------------------------------------ layout of the package
def test_console_is_flat_modules():
    names = {p.name for p in JS}
    for need in ("app.js", "ui.js", "diagram.js", "mapping.js", "layouts.js"):
        assert need in names
    assert any(n.startswith("pages-") for n in names)
    # the package-data glob is console/*.js: no sub-folders except assets
    assert {p.name for p in CONSOLE.iterdir() if p.is_dir()} <= {"assets"}
    html = read(CONSOLE / "index.html")
    assert '<script type="module" src="app.js"></script>' in html


def test_nav_has_the_v2_pages_and_icons():
    html = read(CONSOLE / "index.html")
    # the design's own line icons, inline: 18 px, stroke 1.6, the same shapes as ui.js LINES
    nav = re.findall(r'data-page="([a-z]+)"><svg class="ic" width="18" height="18" viewBox="0 0 24 24" fill="none" '
                     r'stroke="currentColor" stroke-width="1.6"[^>]*>(.*?)</svg><span>([A-Za-z]+)</span>', html)
    assert [(p, label) for p, _, label in nav] == [
        ("overview", "Overview"), ("sources", "Sources"), ("destinations", "Destinations"), ("jobs", "Jobs"),
        ("runs", "Runs"), ("stage", "Stage"), ("layouts", "Layouts"), ("settings", "Settings")]
    ui = read(CONSOLE / "ui.js")
    for page, shapes, _ in nav:
        for d in re.findall(r' d="([^"]+)"', shapes):
            assert f'd: "{d}"' in ui, (page, d)
    assert re.findall(r'<div class="grp">([A-Za-z]+)</div>', html) == ["Build", "Operate", "Setup"]
    assert 'class="ph ' not in html.split('id="nav"')[1].split("</nav>")[0]


def test_top_bar_scope_replaces_the_project_tag():
    html = read(CONSOLE / "index.html")
    assert 'id="project-badge"' not in html
    assert 'id="scope"' in html and 'aria-haspopup="menu"' in html
    assert 'placeholder="Find a job or source"' in html
    state = read(CONSOLE / "state.js")
    # the switch keeps both projects: it only makes the chosen one active through /start
    assert 'api("/start", { method: "POST", body: { choice: id' in state
    assert '"Demo data"' in state and '"My data"' in state


def test_empty_state_and_getting_set_up():
    ui = read(CONSOLE / "ui.js")
    for words in ('"Add a source"', '"Add a destination"', '"Create a job"', '"Getting set up"'):
        assert words in ui, words
    for page, title in (("pages-sources.js", "No sources yet"), ("pages-destinations.js", "No destinations yet"),
                        ("pages-jobs.js", "No jobs yet"), ("pages-runs.js", "No runs yet"), ("pages-runs.js", "Nothing staged yet"),
                        ("layouts.js", "No layouts yet"), ("pages-home.js", "Nothing here yet")):
        assert f'title: "{title}"' in read(CONSOLE / page), (page, title)
    for page in ("pages-sources.js", "pages-destinations.js", "pages-jobs.js"):
        assert "stepsStrip()" in read(CONSOLE / page), page
    assert "setupSteps(o.setup)" in read(CONSOLE / "pages-home.js")


# ------------------------------------------------------------------ no remote resources
def test_no_http_urls():
    for p in TEXT_FILES:
        found = re.findall(r"https?://[^\s\"'`)]*", read(p))
        assert not found, f"{p.name}: {found}"


def test_no_em_or_en_dashes():
    for p in TEXT_FILES:
        t = read(p)
        assert chr(0x2014) not in t and chr(0x2013) not in t, p.name


# ------------------------------------------------------------------ icons
def _first_arg(call):
    """The first argument of a call's text: up to the first comma outside brackets and quotes."""
    depth, quote = 0, None
    for i, ch in enumerate(call):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return call[:i]
    return call


def _literals(expr):
    """String literals that can be the value, not the ones a comparison tests against."""
    return re.findall(r'(?<![=!]=\s)(?<![=!]==\s)"([a-z][a-z0-9-]*)"', expr)


def _icon_names():
    names = set()
    for p in JS:
        t = read(p)
        # icon("x"), icon(a ? "x" : "y"): every quoted token inside the call
        for m in re.finditer(r"\bicon\(", t):
            depth, i = 1, m.end()
            while i < len(t) and depth:
                depth += {"(": 1, ")": -1}.get(t[i], 0)
                i += 1
            names.update(_literals(_first_arg(t[m.end():i - 1])))
        # options objects: icon: "x", ic: "x"
        for m in re.finditer(r"\b(?:icon|ic):\s*([^,}\n]+)", t):
            names.update(_literals(m.group(1)))
        # icon maps: const SOMETHING_ICON = {...} / GLYPH = {...}
        for m in re.finditer(r"const\s+[A-Z_]*(?:ICON|GLYPH)[A-Z_]*\s*=\s*\{(.*?)\};", t, re.S):
            names.update(re.findall(r':\s*"([a-z][a-z0-9-]*)"', m.group(1)))
    for p in [CONSOLE / "index.html", *JS]:
        names.update(n for n in re.findall(r"\bph-([a-z][a-z0-9-]*)", read(p)))
    return names


def test_every_icon_exists_in_phosphor():
    css = read(CONSOLE / "assets" / "phosphor.css")
    have = set(re.findall(r"\.ph-([a-z0-9-]+):", css))
    used = _icon_names()
    assert len(used) > 60, used
    missing = sorted(n for n in used if n not in have)
    assert not missing, missing


# ------------------------------------------------------------------ tokens and contrast (13.6)
SPEC_TOKENS = {
    # the final design (2 Nov 2026): dark values are the owner's tokens, light is derived from the same hues
    "bg": ("#e4ebef", "#19222a"), "surface": ("#fcfdfd", "#10171c"), "raised": ("#eef3f5", "#16202a"), "input": ("#ffffff", "#131c23"),
    "line": ("#d3dde2", "#24323d"), "line-strong": ("#6a8190", "#567082"), "ink": ("#0e1a21", "#eef3f5"), "ink-2": ("#34474f", "#c5d0d6"),
    "ink-3": ("#4f626c", "#93a4af"), "primary": ("#17698a", "#3aa6c8"), "primary-ink": ("#f6fbfd", "#04212c"), "link": ("#15617f", "#63c0de"),
    "primary-soft": ("#dcedf4", "#15303c"), "primary-soft-ink": ("#0f4f67", "#8fd3ea"), "nav-active": ("#d4e9f1", "#1d3442"),
    "nav-ink": ("#0f4f67", "#9bd8ec"), "gold": ("#83590c", "#e2b04f"), "gold-soft": ("#f7ecd4", "#352a12"),
    "danger": ("#b23a26", "#f07862"), "danger-soft": ("#fae3de", "#3d1c17"), "ok": ("#18724d", "#6fd3a2"), "ok-soft": ("#dcf1e6", "#13332a"),
}
TEXT_PAIRS = [(fg, bg) for fg in ("ink", "ink-2", "ink-3") for bg in ("bg", "surface", "raised", "input", "nav-active")] + [
    (fg, bg) for fg in ("primary", "link", "gold", "danger", "ok", "nav-ink") for bg in ("bg", "surface", "raised")] + [
    ("primary-ink", "primary"), ("primary-ink", "danger"), ("primary-soft-ink", "primary-soft"), ("gold", "gold-soft"), ("danger", "danger-soft"),
    ("ok", "ok-soft"), ("nav-ink", "nav-active"), ("ink", "primary-soft"), ("ink", "danger-soft"), ("ink", "gold-soft"), ("ink-2", "input")]
# gold-bar is the brand colour for decoration beside other cues (cancelled node outline, ruler
# fields), so it is not checked as a carrier of meaning. Required dots use --gold.
NONTEXT_PAIRS = [("line-strong", bg) for bg in ("bg", "surface", "raised", "input")] + [("primary-ink", "ok"), ("gold", "surface"), ("primary", "surface")]


def _parse_color(v):
    v = v.strip()
    m = re.fullmatch(r"#([0-9a-f]{6})", v, re.I)
    if m:
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (1.0,)
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)", v)
    assert m, v
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4) or 1))


def _tokens():
    css = read(CONSOLE / "app.css")
    root = re.search(r":root\s*\{(.*?)\n\}", css, re.S).group(1)
    out = {}
    for name, a, b in re.findall(r"--([a-z0-9-]+):\s*light-dark\(\s*(rgba?\([^)]*\)|[^,]+?)\s*,\s*(rgba?\([^)]*\)|[^;]+?)\s*\)\s*;", root):
        out[name] = (_parse_color(a), _parse_color(b))
    return out


def _over(fg, bg):
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3)) + (1.0,)


def _lum(c):
    def ch(x):
        x = x / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def _ratio(a, b):
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_tokens_match_the_spec_and_are_written_once():
    css = read(CONSOLE / "app.css")
    t = _tokens()
    for name, (light, dark) in SPEC_TOKENS.items():
        assert name in t, name
        assert t[name][0][:3] == _parse_color(light)[:3], (name, "light")
        assert t[name][1][:3] == _parse_color(dark)[:3], (name, "dark")
    # one source of truth: every colour token is defined exactly once
    for name in SPEC_TOKENS:
        assert len(re.findall(rf"--{name}:", css)) == 1, name
    assert '[data-theme="dark"]' in css and '[data-theme="light"]' in css
    # dark is the default: the root scheme is dark until the light theme is chosen
    root = re.search(r":root\s*\{(.*?)\n\}", css, re.S).group(1)
    assert "color-scheme: dark;" in root


def test_fonts_are_self_hosted_plex():
    css = read(CONSOLE / "app.css")
    assert "Geist" not in css and "fonts.googleapis" not in css
    for f in re.findall(r'url\("(assets/[^"]+)"\)', css):
        assert (CONSOLE / f).is_file(), f
    assert '"IBM Plex Sans"' in css and '"IBM Plex Mono"' in css
    assert (CONSOLE / "assets" / "PLEX-LICENSE.txt").is_file()
    for p in TEXT_FILES:
        assert "Geist" not in read(p), p.name


@pytest.mark.parametrize("mode", [0, 1], ids=["light", "dark"])
def test_token_contrast(mode):
    t = {k: v[mode] for k, v in _tokens().items()}

    def solid(name):
        c = t[name]
        return c if c[3] == 1 else _over(c, t["surface"])

    bad = []
    for fg, bg in TEXT_PAIRS:
        for base in ("surface", "bg"):
            b = _over(t[bg], t[base]) if t[bg][3] < 1 else t[bg]
            r = _ratio(solid(fg), b)
            if r < 4.5:
                bad.append(f"{fg} on {bg} ({base}): {r:.2f}")
    for fg, bg in NONTEXT_PAIRS:
        r = _ratio(solid(fg), solid(bg))
        if r < 3:
            bad.append(f"{fg} vs {bg}: {r:.2f}")
    assert not bad, bad


# ------------------------------------------------------------------ motion
def test_reduced_motion_covers_the_diagram():
    css = read(CONSOLE / "app.css")
    blocks = re.findall(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", css, re.S)
    assert blocks
    body = "\n".join(blocks)
    for cls in (".edge-flow", ".node-pulse", ".draw-in"):
        assert cls in body, cls
    assert "animation: none" in body
    # and the animations exist to be turned off
    for kf in ("edge-flow", "node-pulse", "draw-in"):
        assert f"@keyframes {kf}" in css


def test_edge_particles_follow_the_spec():
    css = read(CONSOLE / "app.css")
    rule = re.search(r"\.diagram \.edge-flow \{(.*?)\}", css, re.S).group(1)
    assert "stroke-dasharray: 0 14" in rule and "stroke-linecap: round" in rule and "stroke-width: 4" in rule
    assert "1.1s linear" in rule
    assert re.search(r"@keyframes edge-flow \{ from \{ stroke-dashoffset: 0; \} to \{ stroke-dashoffset: -28; \} \}", css)
    assert "cubic-bezier(0.2, 0.7, 0.2, 1)" in re.search(r"\.diagram \.draw-in \{(.*?)\}", css, re.S).group(1)
    assert re.search(r"@keyframes node-pulse \{ from \{ transform: scale\(1\); opacity: 0\.5; \} to \{ transform: scale\(1\.06\); opacity: 0; \} \}", css)


# ------------------------------------------------------------------ requests and markup
def test_every_fetch_goes_through_api():
    calls = [(p.name, m.start()) for p in JS for m in re.finditer(r"\bfetch\(", read(p))]
    assert len(calls) == 1 and calls[0][0] == "ui.js", calls
    ui = read(CONSOLE / "ui.js")
    start = ui.index("export async function api(")
    assert start < calls[0][1] < ui.index("export async function apiPage(")


def test_no_markup_from_data():
    for p in JS + [CONSOLE / "index.html"]:
        t = read(p)
        for m in re.finditer(r"\.(innerHTML|outerHTML)\s*(\+?=)\s*(.)", t):
            assert m.group(3) in "\"'", f"{p.name}: {m.group(0)}"
            rest = t[m.end() - 1:].split("\n", 1)[0]
            assert re.match(r"""(["'])[^"'`$]*\1\s*;?\s*$""", rest), f"{p.name}: {rest}"
        for m in re.finditer(r"insertAdjacentHTML\(|document\.write\(", t):
            line = t[m.start():].split("\n", 1)[0]
            assert re.match(r"""\w+(\.\w+)*\(\s*["'][^"']*["']\s*,\s*["'][^"'`$]*["']\s*\)""", line), f"{p.name}: {line}"


def _calls_with(t, needle):
    """Each h("input", {...}) object literal that contains needle."""
    out = []
    for m in re.finditer(r'h\("input",\s*\{', t):
        depth, i = 1, m.end()
        while i < len(t) and depth:
            depth += {"{": 1, "}": -1}.get(t[i], 0)
            i += 1
        obj = t[m.end():i - 1]
        if needle in obj:
            out.append(obj)
    return out


def test_secret_inputs_are_write_only():
    found = 0
    for p in JS:
        for obj in _calls_with(read(p), 'type: "password"'):
            found += 1
            assert 'autocomplete: "new-password"' in obj, (p.name, obj[:120])
            # never filled from a response
            assert not re.search(r"\bvalue:", obj), (p.name, obj[:120])
    assert found >= 3
    # the connection drawer builds its PUT body through withSecrets, so an empty secret is left out
    src = read(CONSOLE / "pages-sources.js")
    assert "withSecrets(p, secrets)" in src


# ------------------------------------------------------------------ pure functions under Node
NODE = shutil.which("node")

HARNESS = r"""
const ui = await import(process.argv[2] + "/ui.js");
const dg = await import(process.argv[2] + "/diagram.js");
const mp = await import(process.argv[2] + "/mapping.js");
const lay = await import(process.argv[2] + "/layouts.js");
const out = {};
out.secrets = [
  ui.withSecrets({ id: "x", host: "h", password_set: true }, { password: "" }),
  ui.withSecrets({ id: "x", password_set: true }, { password: "s3cret", sas_token: "" }),
];
out.pageText = [ui.pageText(0, 25, 312), ui.pageText(300, 12, 312), ui.pageText(0, 0, 0)];
out.cron = [ui.describeCron("0 6 * * 1-5").text, ui.describeCron("bad").ok, ui.describeCron("").text];
const hz = dg.layout(true), vt = dg.layout(false);
out.layout = { hw: hz.width, hh: hz.height, vw: vt.width, vh: vt.height, edges: hz.edges.length, d0: hz.edges[0].d, vd0: vt.edges[0].d, n0: hz.nodes[1] };
out.aria = dg.ariaLabel({ id: "mapping", label: "Mapping", status: "ok", rows: 1200 });
out.draft = dg.draftFlow({ source: { connection: "landing", files: "bank/x.txt", format: "mt940" }, target: { destination: "d", mode: "upsert" }, mapping: { columns: { a: { from: "b" } } } }).nodes.map((n) => [n.id, n.status, n.label, n.detail]);
const dest = [{ name: "id", type: "string(10)", nullable: false }, { name: "src", type: "string(10)", nullable: false, default: "'X'" }, { name: "amt", type: "decimal(18,2)" }];
out.problems = mp.localProblems({ columns: { amt: { from: "nope" }, ghost: { const: 1 } } }, dest, [{ name: "amount" }]);
out.counts = mp.countText(mp.mappingCounts({ columns: { id: { from: "a" } } }, dest, out.problems));
out.overlaps = [...lay.overlaps([{ start: 1, length: 4 }, { start: 3, length: 2 }, { start: 10, end: 12 }])];
out.sample = lay.sampleLine({ record_type: { start: 1, length: 2 }, line_length: 20 }, { match: "TD", fields: [{ name: "amt", start: 3, length: 6, type: "decimal" }, { name: "nm", start: 9, length: 4, type: "string" }] });
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def node_out(tmp_path_factory):
    if not NODE:
        pytest.skip("node is not installed")
    d = tmp_path_factory.mktemp("consolejs")
    for p in JS:
        # .mjs copies so Node reads them as ES modules whatever its version
        (d / p.name).write_text(read(p), encoding="utf-8")
    (d / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    h = d / "harness.mjs"
    h.write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([NODE, str(h), d.as_uri()], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_empty_secret_is_left_out_of_the_put_body(node_out):
    a, b = node_out["secrets"]
    assert a == {"id": "x", "host": "h"}
    assert b == {"id": "x", "password": "s3cret"}


def test_pager_text(node_out):
    assert node_out["pageText"] == ["1-25 of 312", "301-312 of 312", "0 of 0"]


def test_cron_reading(node_out):
    assert node_out["cron"] == ["Weekdays at 06:00.", False, "Runs only when started by hand."]


def test_diagram_geometry(node_out):
    lay = node_out["layout"]
    # five nodes of 168x72; horizontal is wide and short, vertical is one column
    assert lay["edges"] == 4
    assert lay["hw"] > 5 * 168 and lay["hh"] < 120
    assert lay["vw"] < 200 and lay["vh"] > 5 * 72
    assert lay["d0"].startswith("M") and " C" in lay["d0"]
    assert lay["n0"]["x"] > 168
    assert node_out["aria"] == "Mapping: ok, 1,200 rows"


def test_draft_flow_is_all_idle(node_out):
    nodes = node_out["draft"]
    assert [n[0] for n in nodes] == ["source", "stage", "transform", "mapping", "destination"]
    assert {n[1] for n in nodes} == {"idle"}
    assert nodes[0][2] == "landing" and nodes[4][2] == "d" and nodes[3][3] == "1 column"


def test_mapping_local_problems(node_out):
    probs = {(p["column"], p["message"]) for p in node_out["problems"]}
    assert ("id", "Required: map a column, a constant or an expression.") in probs
    assert ("amt", "No source column nope.") in probs
    assert ("ghost", "The destination has no column ghost.") in probs
    # a not-null column with a default is not required
    assert not any(c == "src" for c, _ in probs)
    assert node_out["counts"] == f"1 of 3 mapped {chr(0xb7)} 3 errors"


def test_layout_helpers(node_out):
    assert sorted(node_out["overlaps"]) == [0, 1]
    line = node_out["sample"]
    assert line.startswith("TD000125NM..") and len(line) == 20


def test_destination_design_starts_with_two_choices_and_jobs_have_the_shortcut():
    dest = read(CONSOLE / "pages-destinations.js")
    for words in ('"Copy from a source table"', '"Same columns and types as a table or file you read from."', '"Start empty"',
                  '"Use these columns"', "function startChoices()", "d.columns.length ? designTable() : startChoices()",
                  "export async function copyColumns(", "detail: true", "/connections/${enc(S.connection)}/tables",
                  "/connections/${enc(S.connection)}/files", "pager(listSt", 'type: "search"'):
        assert words in dest, words
    assert '"Start from a source"' not in dest
    jobs = read(CONSOLE / "pages-jobs.js")
    for words in ('"New destination like this source"', "readDesign(srcBody(), like.connection, (job.transform", 'spec.design = "create"',
                  'go("mapping")', "T.destination = like.id", "autoMatch: matchNext === T.destination"):
        assert words in jobs, words
    assert "autoMatch" in read(CONSOLE / "mapping.js")
    css = read(CONSOLE / "app.css")
    assert ".choice.plain .ico" in css and ".copy-browse" in css
