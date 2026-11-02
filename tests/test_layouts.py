"""Layout-driven fixed-width files (section 5.7)."""

import copy as _copy
import datetime as dt
import os
import random
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest
import yaml

from etlplus import formats
from etlplus.formats import FormatError, layouts, samples

TODAY = dt.date(2026, 11, 1)
NI = "card_processor_ni_style"


def rows(path, options, **kw):
    return pa.Table.from_batches(list(formats.read("layout", Path(path), options, **kw))).to_pylist()


@pytest.fixture()
def card(tmp_path):
    p = tmp_path / "card.txt"
    p.write_text(samples.card_ni_text(TODAY, random.Random(1)), encoding="latin-1", newline="")
    return p


def small_layout(**over):
    d = {"layout": 1, "name": "tiny", "label": "Tiny", "line_length": 20,
         "record_type": {"start": 1, "length": 1},
         "records": [
             {"type": "header", "match": "H", "occurs": "first",
              "fields": [{"name": "day", "start": 2, "length": 8, "type": "date"}]},
             {"type": "detail", "match": "D", "output": True,
              "fields": [{"name": "pan", "start": 2, "length": 16, "type": "string",
                          "mask": "pan"},
                         {"name": "amt", "start": 18, "length": 3, "type": "decimal",
                          "scale": 0, "implied_decimal": True}]},
             {"type": "trailer", "match": "T", "occurs": "last",
              "fields": [{"name": "n", "start": 2, "length": 3, "type": "integer"},
                         {"name": "total", "start": 5, "length": 5, "type": "decimal",
                          "scale": 0, "implied_decimal": True}]}],
         "carry": ["header.day"],
         "checks": [{"kind": "count", "field": "trailer.n", "of": "detail"},
                    {"kind": "sum", "field": "trailer.total", "of": "detail.amt"},
                    {"kind": "unknown_records", "level": "error"}]}
    d.update(over)
    return d


def test_every_builtin_validates():
    names = [x["name"] for x in layouts.list()]
    assert {NI, "payroll_batch_example", "statement_fixed_example"} <= set(names)
    for n in names:
        warnings = []
        assert layouts.validate_layout(layouts.load(n), warnings) == [], n
        assert warnings == [], n
    ni = [x for x in layouts.list() if x["name"] == NI][0]
    assert ni["template"] is True and ni["source"] == "builtin"
    assert "Network International" in ni["verify"]
    assert ni["label"] == "Card processor daily file (NI-style)"


def test_ni_template_parses_its_sample(card):
    r = rows(card, {"layout": NI})
    assert len(r) == 12
    a = r[0]
    assert a["card_number"].startswith("400000") and "******" in a["card_number"]
    assert a["header_file_date"] == TODAY
    assert a["settlement_date"] == TODAY - dt.timedelta(days=1)
    assert len(a["arn"]) == 23 and a["arn"].isdigit()
    assert a["merchant_name"].startswith("Demo Merchant")
    assert isinstance(a["transaction_time"], dt.time)
    refunds = [x for x in r if x["transaction_type"] == "20"]
    assert refunds and all(x["amount"] < 0 for x in refunds)
    res = formats.check("layout", card, {"layout": NI})
    assert res["ok"], res
    assert {"count", "sum", "occurs", "line_length"} <= {c["name"] for c in res["checks"]}
    assert res["counts"]["records_detail"] == 12
    cols = {c["name"]: c for c in formats.preview("layout", card, {"layout": NI})["columns"]}
    assert cols["card_number"]["personal"] is True
    assert cols["amount"]["type"] == "decimal(18,2)"


def _replace_trailer(path, **values):
    L = layouts.Compiled(layouts.load(NI))
    lines = path.read_text(encoding="latin-1").splitlines()
    cur = {f.name: layouts.convert(f, lines[-1][f.a:f.b], {}, "right", True)
           for f in L.rec_by_type["trailer"]["fields"]}
    cur.update(values)
    lines[-1] = layouts.format_record(L, "trailer", cur)
    path.write_text("\r\n".join(lines) + "\r\n", encoding="latin-1", newline="")


def test_count_check_fails(card):
    _replace_trailer(card, record_count=13)
    with pytest.raises(FormatError) as e:
        rows(card, {"layout": NI})
    assert "record_count says 13" in str(e.value) and "12" in str(e.value)
    out = []
    assert len(rows(card, {"layout": NI, "checks": "warn"}, checks_out=out)) == 12
    assert any(c["name"] == "count" and c["level"] == "warn" for c in out)


def test_sum_check_fails(card):
    _replace_trailer(card, total_amount=Decimal("1.00"))
    res = formats.check("layout", card, {"layout": NI})
    assert not res["ok"]
    assert any(c["name"] == "sum" and c["level"] == "error" for c in res["checks"])
    assert not any(c["name"] == "count" and c["level"] == "error" for c in res["checks"])


def test_unknown_record_and_occurs(card):
    text = card.read_text(encoding="latin-1")
    card.write_text(text + "ZZ" + " " * 198 + "\r\n", encoding="latin-1", newline="")
    res = formats.check("layout", card, {"layout": NI})
    errs = {c["name"] for c in res["checks"] if c["level"] == "error"}
    assert {"unknown_records", "occurs"} <= errs        # the trailer is no longer last


def test_mask_pan_masks(tmp_path):
    L = small_layout()
    p = tmp_path / "t.txt"
    p.write_text("\n".join([
        layouts.format_record(L, "header", {"day": TODAY}),
        layouts.format_record(L, "detail", {"pan": "4000001234567899", "amt": 5}),
        layouts.format_record(L, "trailer", {"n": 1, "total": 5})]) + "\n")
    r = rows(p, {"layout": L})
    assert r[0]["pan"] == "400000******7899"
    assert r[0]["header_day"] == TODAY
    L2 = _copy.deepcopy(L)
    L2["records"][1]["fields"][0]["mask"] = "none"
    assert rows(p, {"layout": L2})[0]["pan"] == "4000001234567899"


def test_overlap_is_a_warning_and_two_outputs_an_error():
    L = small_layout()
    L["records"][1]["fields"][1]["start"] = 16          # overlaps pan (2..17)
    warnings = []
    assert layouts.validate_layout(L, warnings) == []
    assert warnings and "overlap" in warnings[0] and "'detail'" in warnings[0]
    L = small_layout()
    L["records"][2]["output"] = True
    problems = layouts.validate_layout(L)
    assert any("Exactly one record has output: true" in p for p in problems)


@pytest.mark.parametrize("change,needle", [
    (lambda L: L["records"][1]["fields"].append({"name": "pan", "start": 1, "length": 1}),
     "used twice"),
    (lambda L: L["records"][1]["fields"][1].update(start=19), "beyond line_length"),
    (lambda L: L["records"][0].update(match="HH"), "record_type is 1"),
    (lambda L: L["checks"].append({"kind": "sum", "field": "trailer.nope", "of": "detail.amt"}),
     "no field 'nope'"),
    (lambda L: L["records"][0]["fields"][0].update(format="%Q"), "not a valid date format"),
    (lambda L: L.update(encoding="zlib_codec"), "not a text encoding"),
    (lambda L: L.update(line_length=20000), "line_length"),
    (lambda L: L["records"][1]["fields"][1].update(sign="separate:nope"), "sign field 'nope'"),
])
def test_validation_problems_name_record_and_field(change, needle):
    L = small_layout()
    change(L)
    problems = layouts.validate_layout(L)
    assert any(needle in p for p in problems), problems


def test_layout_names_are_checked_before_any_path(tmp_path):
    for bad in ("../x", "A", "x" * 61, "a/b", "", "x.yaml"):
        with pytest.raises(ValueError):
            layouts.load(bad, tmp_path)
        with pytest.raises(ValueError):
            layouts.save(tmp_path, bad, small_layout())
    with pytest.raises(FormatError):
        rows(tmp_path / "x.txt", {"layout": "../../etc"})


def test_save_is_atomic_and_never_into_the_package(tmp_path, monkeypatch):
    layouts.save(tmp_path, "tiny", small_layout())
    target = tmp_path / "layouts" / "tiny.yaml"
    assert yaml.safe_load(target.read_text())["name"] == "tiny"
    before = target.read_text()

    def boom(a, b):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    changed = small_layout(label="Changed")
    with pytest.raises(OSError):
        layouts.save(tmp_path, "tiny", changed)
    monkeypatch.undo()
    assert target.read_text() == before                  # the old file is intact
    # a project inside the package is refused
    pkg = Path(layouts.__file__).resolve().parent
    with pytest.raises(ValueError):
        layouts.save(pkg, "tiny", small_layout())
    with pytest.raises(ValueError):
        layouts.save(None, "tiny", small_layout())
    assert not (pkg / "layouts" / "tiny.yaml").exists()
    # an invalid layout is refused with its problems
    bad = small_layout()
    bad["records"][2]["output"] = True
    with pytest.raises(layouts.LayoutError) as e:
        layouts.save(tmp_path, "tiny", bad)
    assert e.value.problems


def test_project_copy_overrides_builtin(tmp_path, card):
    layouts.copy(tmp_path, NI, NI)
    data = layouts.load(NI, tmp_path)
    assert data["template"] is False
    data["label"] = "Ours"
    data["checks"] = [c for c in data["checks"] if c["kind"] != "sum"]
    layouts.save(tmp_path, NI, data)
    assert layouts.load(NI, tmp_path)["label"] == "Ours"
    assert layouts.load(NI)["label"] != "Ours"
    listed = {x["name"]: x for x in layouts.list(tmp_path)}
    assert listed[NI]["source"] == "project" and listed[NI]["template"] is False
    _replace_trailer(card, total_amount=Decimal("1.00"))
    assert formats.check("layout", card, {"layout": NI}, project=tmp_path)["ok"]
    assert not formats.check("layout", card, {"layout": NI})["ok"]
    layouts.delete(tmp_path, NI)
    assert layouts.load(NI, tmp_path)["label"] != "Ours"
    with pytest.raises(ValueError):
        layouts.delete(tmp_path, NI)                     # built-ins are never deleted
    layouts.copy(tmp_path, NI, "my_cards")
    with pytest.raises(ValueError):
        layouts.copy(tmp_path, NI, "my_cards")


def test_project_object_with_path(tmp_path):
    class P:
        path = tmp_path
    layouts.save(P(), "tiny", small_layout())
    assert "tiny" in {x["name"] for x in layouts.list(P())}


def test_payroll_and_signed_statement_samples(tmp_path):
    paths = samples.write_all(tmp_path, TODAY)
    by = {p.name: p for p in paths}
    r = rows(by["payroll_batch.txt"], {"layout": "payroll_batch_example"})
    assert len(r) == 8 and r[0]["header_company_code"] == "DEMOCO0001"
    s = rows(by["statement_fixed.txt"], {"layout": "statement_fixed_example"})
    assert any(x["amount"] < 0 for x in s) and any(x["amount"] > 0 for x in s)
    for x in s:
        assert (x["fee"] <= 0) if x["fee_sign"] == "D" else (x["fee"] >= 0)


def test_bad_value_is_reported_with_line_and_field(card):
    lines = card.read_text(encoding="latin-1").splitlines()
    lines[3] = lines[3][:23] + "2026XX31" + lines[3][31:]
    card.write_text("\r\n".join(lines) + "\r\n", encoding="latin-1", newline="")
    with pytest.raises(FormatError) as e:
        rows(card, {"layout": NI})
    assert "Line 4" in str(e.value) and "transaction_date" in str(e.value)
    assert e.value.line == 4


def test_unsaved_layout_dict_is_accepted(card):
    data = layouts.load(NI)
    p = formats.preview("layout", card, {"layout": data}, limit=3)
    assert len(p["rows"]) == 3
    assert all(not c.get("level") == "error" for c in p["checks"])
