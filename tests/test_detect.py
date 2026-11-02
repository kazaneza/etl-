"""Detection (section 5.8), the catalogue, and the made-up samples (section 5.9)."""

import datetime as dt
import random
import re
from pathlib import Path

import pytest

from etlplus import formats
from etlplus.formats import samples

FIX = Path(__file__).parent / "fixtures" / "bank"
TODAY = dt.date(2026, 11, 1)
FORMATS_DIR = Path(formats.__file__).resolve().parent


@pytest.fixture(scope="module")
def written(tmp_path_factory):
    folder = tmp_path_factory.mktemp("landing")
    return samples.write_all(folder, TODAY)


def test_write_gives_the_five_demo_files(tmp_path):
    paths = samples.write(tmp_path, TODAY)
    assert sorted(p.name for p in paths) == sorted([
        "statement_mt940.txt", "payments_mt103.txt", "statement_camt053.xml",
        "card_daily_ni_style.txt", "payroll_batch.txt"])
    assert all(p.parent == tmp_path / "bank" for p in paths)
    assert not list(tmp_path.rglob("*.partial"))


def test_samples_are_deterministic(tmp_path):
    a = samples.write_all(tmp_path / "a", TODAY)
    b = samples.write_all(tmp_path / "b", TODAY)
    for x, y in zip(a, b):
        assert x.read_bytes() == y.read_bytes(), x.name


def test_every_sample_passes_every_check(written):
    for p in written:
        fmt, opts = samples.SAMPLE_FORMATS[p.name]
        res = formats.check(fmt, p, opts)
        assert res["ok"], (p.name, res)
        assert res["counts"]["rows"] > 0
        assert not [c for c in res["checks"] if c["level"] != "ok"], p.name


def test_sample_sizes(written):
    by = {p.name: p for p in written}
    assert formats.check("mt940", by["statement_mt940.txt"])["counts"]["rows"] == 12
    assert formats.check("mt103", by["payments_mt103.txt"])["counts"]["rows"] == 3
    assert formats.check("camt053", by["statement_camt053.xml"])["counts"]["rows"] == 6
    card = formats.check("layout", by["card_daily_ni_style.txt"],
                         {"layout": "card_processor_ni_style"})
    assert card["counts"]["records_detail"] == 12
    assert "Demo transfer 0001" in by["statement_mt940.txt"].read_text()
    assert "ZZ00DEMO0000000001" in by["statement_camt053.xml"].read_text()
    assert "camt.053.001.08" in by["statement_camt053.xml"].read_text()


def test_each_sample_is_detected_as_its_own_format(written):
    for p in written:
        fmt, opts = samples.SAMPLE_FORMATS[p.name]
        got = formats.detect(p)
        assert got, p.name
        best = got[0]
        assert best["format"] == fmt, (p.name, got)
        assert best["options"] == opts, (p.name, got)
        floor = 0.7 if p.name == "statement_fixed.txt" else 0.9   # no record codes there
        assert best["confidence"] >= floor, (p.name, got)


def test_bare_swift_detection(tmp_path):
    p = tmp_path / "bare.txt"
    p.write_text(samples.mt940_text(TODAY, random.Random(1), blocks=False))
    got = formats.detect(p)
    assert got[0]["format"] == "mt940" and got[0]["confidence"] >= 0.7
    got = formats.detect(FIX / "mt950_bare.txt")
    assert got[0]["format"] == "mt950" and got[0]["confidence"] >= 0.7
    bare103 = tmp_path / "p.txt"
    text = samples.mt103_text(TODAY, random.Random(1))
    bare103.write_text("\n".join(x for x in text.splitlines()
                                 if not x.startswith("{") and x != "-}"))
    got = formats.detect(bare103)
    assert got[0]["format"] == "mt103" and got[0]["confidence"] >= 0.7


def test_xml_detection_by_root_without_namespace(tmp_path):
    p = tmp_path / "x.xml"
    p.write_text('<?xml version="1.0"?><Document><FIToFICstmrCdtTrf><GrpHdr/>'
                 '</FIToFICstmrCdtTrf></Document>')
    got = formats.detect(p)
    assert got[0]["format"] == "pacs008" and got[0]["confidence"] == 0.9
    got = formats.detect(FIX / "camt053_prefixed_v02.xml")
    assert got[0]["format"] == "camt053" and got[0]["confidence"] == 0.99


def test_csv_and_json_give_no_bank_detection(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("id,name,amount\n" + "".join(f"{i},Demo {i},{i}.50\n" for i in range(200)))
    assert formats.detect(csv) == []
    js = tmp_path / "x.json"
    js.write_text('[{"a": 1}]')
    assert formats.detect(js) == []


def test_detect_reads_at_most_64kb(tmp_path):
    p = tmp_path / "big.txt"
    head = samples.mt940_text(TODAY, random.Random(1))
    p.write_text(head + ("X" * 100 + "\n") * 5000)
    got = formats.detect(p)
    assert got[0]["format"] == "mt940"
    got = formats.detect(p, head=p.read_bytes()[:1000])
    assert got[0]["format"] == "mt940"


def test_project_layout_is_detected(tmp_path):
    from etlplus.formats import layouts
    data = layouts.load("payroll_batch_example")
    data["records"][0]["match"] = "X"          # a project-only code
    layouts.save(tmp_path, "payroll_x", data)
    text = samples.payroll_text(TODAY, random.Random(2)).replace("\nH", "\nX")
    p = tmp_path / "pay.txt"
    p.write_text("X" + text[1:], newline="")
    got = formats.detect(p, project=tmp_path)
    assert got[0]["options"] == {"layout": "payroll_x"}


def test_catalog():
    cat = formats.catalog()
    ids = [c["id"] for c in cat]
    assert ids == ["mt940", "mt950", "mt103", "mt202", "camt053", "camt054", "pain001",
                   "pacs008", "bai2", "layout"]
    for c in cat:
        assert c["group"] == "Bank files" and c["status"] == "simulated"
        assert c["options"] and c["extensions"]
        if c["id"] == "layout":
            assert c["needs_layout"] and c["columns"] is None
        else:
            names = [x["name"] for x in c["columns"]]
            assert names[:2] == ["file_name", "record_line"]
    personal = {x["name"] for x in cat[0]["columns"] if x["personal"]}
    assert personal == {"account", "info"}
    for fmt in ids[:-1]:
        assert formats.schema(fmt).names == [x["name"] for x in cat[ids.index(fmt)]["columns"]]


def _bad_words(text: str) -> list[str]:
    return re.findall(r"BK|RWF|Kigali", text)


def test_no_bank_names_in_samples_or_the_package(written):
    for p in written:
        assert _bad_words(p.read_text(encoding="latin-1")) == [], p.name
    for p in FORMATS_DIR.rglob("*"):
        if p.is_file() and p.suffix in (".py", ".yaml"):
            assert _bad_words(p.read_text(encoding="utf-8")) == [], p.name
    for p in FIX.rglob("*"):
        if p.is_file():
            assert _bad_words(p.read_text(encoding="utf-8")) == [], p.name


def test_samples_use_only_made_up_codes(written):
    text = "".join(p.read_text(encoding="latin-1") for p in written)
    bics = set(re.findall(r"\b[A-Z]{4}ZZ\d\d(?:[A-Z0-9]{3})?\b", text))
    assert bics <= {"DEMOZZ22XXX", "TESTZZ33XXX", "DEMOZZ22", "TESTZZ33"}
    ccys = set(re.findall(r'Ccy="([A-Z]{3})"', text))
    assert ccys <= {"USD", "EUR", "XTS"}
    # card numbers exist only masked
    assert all("******" in line[2:21] for line in
               (written[3].read_text(encoding="latin-1").splitlines()) if line.startswith("TD"))
