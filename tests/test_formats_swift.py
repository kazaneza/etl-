"""SWIFT MT940 / MT950 / MT103 / MT202 on made-up files (section 5.2, 5.3)."""

import datetime as dt
import random
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

from etlplus import formats
from etlplus.formats import FormatError, samples
from etlplus.formats._common import parse_amount

FIX = Path(__file__).parent / "fixtures" / "bank"
TODAY = dt.date(2026, 11, 1)


def rows(fmt, path, options=None, **kw):
    batches = list(formats.read(fmt, Path(path), options or {}, **kw))
    return pa.Table.from_batches(batches).to_pylist()


@pytest.fixture(scope="module")
def bank(tmp_path_factory):
    folder = tmp_path_factory.mktemp("bank")
    samples.write_all(folder, TODAY)
    return folder / "bank"


def test_mt940_sample_rows_and_signed_amounts(bank):
    r = rows("mt940", bank / "statement_mt940.txt")
    assert len(r) == 12
    assert [x["message_index"] for x in r].count(1) == 7
    assert [x["message_index"] for x in r].count(2) == 5
    assert {x["account"] for x in r} == {"DEMO000000001"}
    assert {x["currency"] for x in r} == {"USD"}
    assert r[0]["sender_bic"] == "TESTZZ33XXX" and r[0]["receiver_bic"] == "DEMOZZ22XXX"
    for msg in (1, 2):
        m = [x for x in r if x["message_index"] == msg]
        assert [x["line_number"] for x in m] == list(range(1, len(m) + 1))
        assert m[0]["opening_balance"] + sum(x["signed_amount"] for x in m) == m[0]["closing_balance"]
    for x in r:
        assert x["amount"] > 0
        assert x["signed_amount"] == (x["amount"] if x["mark"] in ("C", "RD") else -x["amount"])
        assert x["info"].startswith("Demo transfer")
    # :86: continuation lines are joined with a space
    assert r[0]["info"] == "Demo transfer 0001 Demo Customer 0007 reference"
    # the second statement opens where the first closed
    assert r[7]["opening_balance"] == r[0]["closing_balance"]


def test_mt940_schema_matches_batches(bank):
    batches = list(formats.read("mt940", bank / "statement_mt940.txt", {}))
    assert batches[0].schema == formats.schema("mt940", {})
    assert batches[0].schema.field("amount").type == pa.decimal128(18, 3)


def test_mt940_bare_year_turn_and_multiple_messages():
    r = rows("mt940", FIX / "mt940_bare_yearturn.txt")
    assert len(r) == 3
    assert r[0]["sender_bic"] is None and r[0]["message_type"] == "940"
    assert [x["message_index"] for x in r] == [1, 1, 2]
    # value 2026-12-31, entry 0102 -> 2027-01-02
    assert r[0]["value_date"] == dt.date(2026, 12, 31)
    assert r[0]["entry_date"] == dt.date(2027, 1, 2)
    assert r[1]["entry_date"] == dt.date(2026, 12, 31)
    # value 2027-01-02, entry 1231 -> 2026-12-31
    assert r[2]["value_date"] == dt.date(2027, 1, 2)
    assert r[2]["entry_date"] == dt.date(2026, 12, 31)
    assert r[0]["info"] == "Demo transfer 0001 second line third line"
    assert r[0]["supplementary_details"] == "Demo supplementary"
    assert r[0]["bank_reference"] == "B0001" and r[0]["customer_reference"] == "DEMO0001"
    # comma decimals, including a trailing comma
    assert r[0]["amount"] == Decimal("250.5") and r[1]["amount"] == Decimal("100")
    # RD (reversal of a debit) is positive
    assert r[2]["mark"] == "RD" and r[2]["signed_amount"] == Decimal("0.5")
    assert r[0]["statement_number"] == 42 and r[0]["sequence_number"] == 1


def test_block_and_bare_forms_give_the_same_lines(tmp_path):
    bare = tmp_path / "bare.txt"
    blocks = tmp_path / "blocks.txt"
    bare.write_text(samples.mt940_text(TODAY, random.Random(1), blocks=False), encoding="utf-8")
    blocks.write_text(samples.mt940_text(TODAY, random.Random(1), blocks=True), encoding="utf-8")
    a, b = rows("mt940", bare), rows("mt940", blocks)
    keys = ("message_index", "line_number", "value_date", "signed_amount", "info",
            "closing_balance")
    assert [[x[k] for k in keys] for x in a] == [[x[k] for k in keys] for x in b]
    assert a[0]["sender_bic"] is None and b[0]["sender_bic"] == "TESTZZ33XXX"


def test_unbalanced_statement_fails_under_error():
    with pytest.raises(FormatError) as e:
        rows("mt940", FIX / "mt940_unbalanced.txt")
    msg = str(e.value)
    assert "balance" in msg and "statement 1" in msg and "line 1" in msg
    assert e.value.line == 1


def test_unbalanced_statement_warns_under_warn():
    seen, out = [], []
    r = rows("mt940", FIX / "mt940_unbalanced.txt", {"checks": "warn"},
             log=seen.append, checks_out=out)
    assert len(r) == 2
    assert any(c["name"] == "balance" and c["level"] == "warn" for c in out)
    assert any("balance" in s for s in seen)
    res = formats.check("mt940", FIX / "mt940_unbalanced.txt", {})
    assert res["ok"] is False
    assert any(c["name"] == "balance" and c["level"] == "error" for c in res["checks"])


def test_repeated_statement_number_is_a_warning_only(tmp_path):
    text = (FIX / "mt940_unbalanced.txt").read_text()
    good = text.replace("1200,00", "1150,00")
    p = tmp_path / "twice.txt"
    p.write_text(good + good, encoding="utf-8")
    r = rows("mt940", p)              # checks: error, still no raise
    assert len(r) == 4
    res = formats.check("mt940", p, {})
    assert res["ok"] is True
    assert any(c["name"] == "sequence" and c["level"] == "warn" for c in res["checks"])


def test_mt950_has_no_info(bank):
    r = rows("mt950", bank / "statement_mt950.txt")
    assert len(r) == 4 and all(x["info"] is None for x in r)
    assert r[0]["message_type"] == "950"
    r = rows("mt950", FIX / "mt950_bare.txt")
    assert len(r) == 2 and r[0]["opening_balance"] == Decimal("-10")
    assert r[0]["opening_balance_type"] == "M" and r[0]["currency"] == "XTS"
    assert r[0]["statement_number"] == 7


def test_mt103_fields(bank):
    r = rows("mt103", bank / "payments_mt103.txt")
    assert len(r) == 3
    a = r[0]
    assert a["sender_bic"] == "DEMOZZ22XXX" and a["receiver_bic"] == "TESTZZ33XXX"
    assert len(a["uetr"]) == 36 and a["uetr"][14] == "4"
    assert a["bank_operation_code"] == "CRED"
    assert a["value_date"] == TODAY and a["currency"] == "USD" and a["amount"] > 0
    assert a["ordering_customer_account"] == "DEMO000000001"
    assert a["ordering_customer"] == "Demo Customer 0001 Demo Street 1"
    assert a["beneficiary_account"] == "DEMO000000101"
    assert a["beneficiary"] == "Demo Customer 0101"
    assert a["remittance_info"] == "Demo invoice 0001"
    assert a["ordering_institution"] == "DEMOZZ22XXX"
    assert a["account_with_institution"] == "TESTZZ33XXX"
    assert [x["charges"] for x in r] == ["SHA", "OUR", "BEN"]
    assert r[1]["receiver_charges"] == "EUR 7.500"
    assert r[2]["sender_charges"] == "USD 5.000"
    assert len({x["uetr"] for x in r}) == 3
    assert [x["record_line"] for x in r] == sorted(x["record_line"] for x in r)


def test_mt103_samples_are_deterministic(tmp_path):
    a = samples.mt103_text(TODAY, random.Random(samples.SEED))
    b = samples.mt103_text(TODAY, random.Random(samples.SEED))
    assert a == b


def test_mt103_missing_mandatory_fields():
    with pytest.raises(FormatError) as e:
        rows("mt103", FIX / "mt103_missing.txt")
    assert ":59a:" in str(e.value) and ":71A:" in str(e.value)
    res = formats.check("mt103", FIX / "mt103_missing.txt", {})
    assert not res["ok"]
    r = rows("mt103", FIX / "mt103_missing.txt", {"checks": "warn"})
    assert r[0]["uetr"] == "00000000-0000-4000-8000-000000000001"
    assert r[0]["beneficiary"] is None


def test_mt202_fields(bank):
    r = rows("mt202", bank / "cover_mt202.txt")
    assert len(r) == 2
    a = r[0]
    assert a["related_reference"].startswith("REL")
    assert a["beneficiary_institution"] == "TESTZZ33XXX"
    assert a["sender_correspondent"] == "DEMOZZ22XXX"
    assert a["uetr"] and a["currency"] == "USD"


def test_mt103_bad_amount_and_currency(tmp_path):
    text = (FIX / "mt103_missing.txt").read_text().replace("USD100,00", "usd0,00")
    p = tmp_path / "bad.txt"
    p.write_text(text)
    res = formats.check("mt103", p, {})
    names = {c["name"] for c in res["checks"] if c["level"] == "error"}
    assert {"amount", "currency", "mandatory"} <= names


def test_comma_decimals():
    assert parse_amount("1234,56") == Decimal("1234.56")
    assert parse_amount("100,") == Decimal("100")
    assert parse_amount("0,5") == Decimal("0.5")
    with pytest.raises(ValueError):
        parse_amount("12a,0")


def test_missing_file_and_unknown_format(tmp_path):
    with pytest.raises(FormatError):
        list(formats.read("mt999", tmp_path / "x.txt", {}))
    res = formats.check("mt940", tmp_path / "nothing.txt", {})
    assert res["ok"] is False


def test_long_line_is_refused(tmp_path):
    p = tmp_path / "long.txt"
    p.write_text(":20:" + "A" * 1_100_000 + "\n")
    with pytest.raises(FormatError):
        rows("mt940", p)
