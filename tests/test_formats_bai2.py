"""BAI2 on made-up files (section 5.6)."""

import datetime as dt
import random
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

from etlplus import formats
from etlplus.formats import FormatError, samples

FIX = Path(__file__).parent / "fixtures" / "bank"
TODAY = dt.date(2026, 11, 1)


def rows(path, options=None, **kw):
    return pa.Table.from_batches(list(formats.read("bai2", Path(path), options or {}, **kw))).to_pylist()


@pytest.fixture()
def sample(tmp_path):
    p = tmp_path / "cash.bai"
    p.write_text(samples.bai2_text(TODAY, random.Random(7)))
    return p


def test_sample_rows_and_checks(sample):
    r = rows(sample)
    assert len(r) == 9                       # 4 + 5 detail records
    a = r[0]
    assert a["sender_id"] == "DEMOZZ22" and a["receiver_id"] == "TESTZZ33"
    assert a["file_creation_date"] == TODAY and a["file_id"] == "DEMOFILE01"
    assert a["as_of_date"] == TODAY - dt.timedelta(days=1) and a["currency"] == "USD"
    assert a["account"] == "DEMO000000001"
    assert a["opening_ledger"] is not None and a["closing_ledger"] is not None
    assert a["type_code"] == "195" and a["credit_debit"] == "C"
    assert r[1]["type_code"] == "475" and r[1]["credit_debit"] == "D"
    assert r[1]["signed_amount"] == -r[1]["amount"]
    # ledger: opening + net detail = closing (the sample is built that way)
    acct1 = [x for x in r if x["account"] == "DEMO000000001"]
    assert a["opening_ledger"] + sum(x["signed_amount"] for x in acct1) == a["closing_ledger"]
    assert a["bank_reference"] == "BREF10001" and a["customer_reference"] == "CREF10001"
    res = formats.check("bai2", sample, {})
    assert res["ok"], res
    names = {c["name"] for c in res["checks"]}
    assert {"account_trailer", "group_trailer", "file_trailer", "structure"} <= names


def test_88_continuation_joins_text(sample):
    r = rows(sample)
    assert r[0]["text"] == "Demo transfer 1001 Demo Customer 0007 continued"
    assert r[1]["text"] == "Demo transfer 1002"


def test_implied_decimals(tmp_path):
    p = tmp_path / "x.bai"
    p.write_text("\n".join([
        "01,DEMOZZ22,TESTZZ33,261101,0600,F1,,,2/",
        "02,DEMOZZ22,TESTZZ33,1,261031,,EUR,2/",
        "03,DEMO000000003,,010,12345,,/",
        "16,399,1050,Z,B1,C1,Demo/",
        "49,13395,3/",
        "98,13395,1,5/",
        "99,13395,1,7/"]) + "\n")
    r = rows(p)
    assert r[0]["amount"] == Decimal("10.50") and r[0]["opening_ledger"] == Decimal("123.45")
    assert r[0]["currency"] == "EUR"           # from 02 when 03 leaves it blank
    r = rows(p, {"implied_decimals": 0})
    assert r[0]["amount"] == Decimal("1050")
    with pytest.raises(FormatError):
        rows(p, {"implied_decimals": 7})


def test_funds_type_extras_are_skipped(tmp_path):
    p = tmp_path / "f.bai"
    p.write_text("\n".join([
        "01,DEMOZZ22,TESTZZ33,261101,0600,F1,,,2/",
        "02,DEMOZZ22,TESTZZ33,1,261031,,USD,2/",
        "03,DEMO000000003,USD,010,100,,S,10,20,30,015,200,,V,261031,1200/",
        "16,475,500,D,2,0,250,1,250,BREF,CREF,Demo split/",
        "16,195,700,V,261031,0900,BREF2,CREF2,Demo value dated/",
        "49,1500,4/",
        "98,1500,1,6/",
        "99,1500,1,8/"]) + "\n")
    r = rows(p)
    assert r[0]["opening_ledger"] == Decimal("1.00") and r[0]["closing_ledger"] == Decimal("2.00")
    assert r[0]["bank_reference"] == "BREF" and r[0]["text"] == "Demo split"
    assert r[1]["bank_reference"] == "BREF2" and r[1]["funds_type"] == "V"


def test_broken_trailer_is_reported():
    with pytest.raises(FormatError) as e:
        rows(FIX / "bai2_broken_trailer.bai")
    assert "49" in str(e.value) and "control total" in str(e.value)
    res = formats.check("bai2", FIX / "bai2_broken_trailer.bai", {})
    assert not res["ok"]
    bad = {c["name"] for c in res["checks"] if c["level"] == "error"}
    assert {"account_trailer"} <= bad
    out = []
    r = rows(FIX / "bai2_broken_trailer.bai", {"checks": "warn"}, checks_out=out)
    assert len(r) == 1 and any(c["level"] == "warn" for c in out)


@pytest.mark.parametrize("old,new,name", [
    (",17/", ",18/", "group_trailer"),            # 98 record count
    ("99,{t},1,19/", "99,{t},1,20/", "file_trailer"),   # 99 record count
    ("98,{t},2,", "98,{t},3,", "group_trailer"),  # 98 number of accounts
])
def test_counts_at_98_and_99(sample, old, new, name):
    text = sample.read_text()
    total = text.strip().splitlines()[-1].split(",")[1]
    text = text.replace(old.format(t=total), new.format(t=total))
    sample.write_text(text)
    res = formats.check("bai2", sample, {})
    assert any(c["name"] == name and c["level"] == "error" for c in res["checks"]), res


def test_49_must_follow_03_and_one_01_one_99(sample):
    lines = sample.read_text().splitlines()
    no49 = [x for i, x in enumerate(lines) if not (x.startswith("49,") and i < 10)]
    sample.write_text("\n".join(no49) + "\n")
    res = formats.check("bai2", sample, {})
    assert any("has no 49" in c["message"] or "before the 49" in c["message"]
               for c in res["checks"])
    sample.write_text("\n".join(lines[:-1]) + "\n")       # no 99
    res = formats.check("bai2", sample, {})
    assert any("exactly one 01 and one 99" in c["message"] for c in res["checks"])
