"""ISO 20022 camt.053 / camt.054 / pain.001 / pacs.008 on made-up files (sections 5.4, 5.5)."""

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


def rows(fmt, path, options=None, **kw):
    batches = list(formats.read(fmt, Path(path), options or {}, **kw))
    return pa.Table.from_batches(batches).to_pylist()


def write(tmp_path, name, text, encoding="utf-8"):
    p = tmp_path / name
    p.write_text(text, encoding=encoding)
    return p


@pytest.mark.parametrize("version", ["02", "08"])
def test_camt053_entries_and_balances(tmp_path, version):
    p = write(tmp_path, "s.xml", samples.camt053_xml(TODAY, random.Random(3), version=version))
    r = rows("camt053", p)
    assert len(r) == 6
    assert [x["credit_debit"] for x in r].count("CRDT") == 4
    assert [x["credit_debit"] for x in r].count("DBIT") == 2
    a = r[0]
    assert a["account"] == "ZZ00DEMO0000000001" and a["account_currency"] == "USD"
    assert a["opening_balance"] + sum(x["signed_amount"] for x in r) == a["closing_balance"]
    assert a["status"] == "BOOK" and a["reversal"] is False
    assert a["bank_tx_code"] == "PMNT/RCDT/DMCT" and a["proprietary_code"] == "DEMO01"
    assert a["counterparty_name"] == "Demo Customer 0001"
    assert a["counterparty_account"] == "DEMO000000201"
    assert a["counterparty_agent_bic"] == "TESTZZ33XXX"
    assert a["remittance_info"] == "Demo transfer 0001 part 1"
    assert a["booking_date"] == TODAY - dt.timedelta(days=1)
    assert a["creation_time"].tzinfo is not None
    assert a["end_to_end_id"] == "E2E00010"
    dbit = [x for x in r if x["credit_debit"] == "DBIT"][0]
    assert dbit["signed_amount"] == -dbit["amount"]
    # record_line is the line of <Ntry>
    lines = p.read_text().splitlines()
    assert all("<Ntry>" in lines[x["record_line"] - 1] for x in r)
    res = formats.check("camt053", p, {})
    assert res["ok"], res
    assert {"balance", "summary", "amount"} <= {c["name"] for c in res["checks"]}


def test_camt053_level_transaction(tmp_path):
    p = write(tmp_path, "s.xml", samples.camt053_xml(TODAY, random.Random(3)))
    r = rows("camt053", p, {"level": "transaction"})
    assert len(r) == 7                                  # the first entry has two TxDtls
    assert r[0]["entry_reference"] == r[1]["entry_reference"] == "0001"
    assert r[0]["amount"] == r[1]["amount"]             # entry fields repeat
    assert [r[0]["end_to_end_id"], r[1]["end_to_end_id"]] == ["E2E00010", "E2E00011"]
    assert r[1]["remittance_info"] == "Demo transfer 0001 part 2"
    with pytest.raises(FormatError):
        rows("camt053", p, {"level": "booking"})


def test_camt053_prefixed_namespace_v02():
    r = rows("camt053", FIX / "camt053_prefixed_v02.xml")
    assert len(r) == 2
    assert r[0]["account"] == "DEMO000000009" and r[0]["currency"] == "XTS"
    assert r[0]["opening_balance"] == Decimal("-5") and r[0]["closing_balance"] == Decimal("15")
    assert r[0]["booking_date"] == dt.date(2026, 10, 31)       # date part of DtTm
    assert r[0]["counterparty_name"] == "Demo Customer 0007"
    assert r[0]["remittance_info"] == "Demo transfer 0001"
    assert r[1]["reversal"] is True and r[1]["counterparty_name"] is None


@pytest.mark.parametrize("version", ["02", "08"])
def test_camt054(tmp_path, version):
    p = write(tmp_path, "n.xml",
              samples.camt053_xml(TODAY, random.Random(4), version=version, kind="054"))
    r = rows("camt054", p)
    assert len(r) == 2
    assert all(x["opening_balance"] is None and x["closing_balance"] is None for x in r)
    assert r[0]["statement_id"].startswith("DEMOSTMT")
    assert formats.check("camt054", p, {})["ok"]


def test_camt053_balance_check_fails(tmp_path):
    text = samples.camt053_xml(TODAY, random.Random(3))
    text = text.replace("<Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy=\"USD\">",
                        "<Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy=\"USD\">1")
    p = write(tmp_path, "bad.xml", text)
    with pytest.raises(FormatError) as e:
        rows("camt053", p)
    assert "balance" in str(e.value)
    out = []
    r = rows("camt053", p, {"checks": "warn"}, checks_out=out)
    assert len(r) == 6 and any(c["name"] == "balance" and c["level"] == "warn" for c in out)


def test_camt053_summary_check_fails(tmp_path):
    text = samples.camt053_xml(TODAY, random.Random(3)).replace(
        "<NbOfNtries>6</NbOfNtries>", "<NbOfNtries>7</NbOfNtries>")
    p = write(tmp_path, "bad.xml", text)
    res = formats.check("camt053", p, {})
    assert not res["ok"]
    bad = [c for c in res["checks"] if c["name"] == "summary"][0]
    assert "NbOfNtries says 7" in bad["message"]


def test_camt_amount_check(tmp_path):
    text = samples.camt053_xml(TODAY, random.Random(3))
    i = text.index("<Ntry>")
    text = text[:i] + text[i:].replace('<Amt Ccy="USD">', '<Amt Ccy="usd">', 1)
    p = write(tmp_path, "bad.xml", text)
    res = formats.check("camt053", p, {})
    assert any(c["name"] == "amount" and c["level"] == "error" for c in res["checks"])


def test_pain001(tmp_path):
    p = write(tmp_path, "p.xml", samples.pain001_xml(TODAY, random.Random(5)))
    r = rows("pain001", p)
    assert len(r) == 3
    a = r[0]
    assert a["number_of_transactions"] == 3
    assert a["control_sum"] == sum(x["amount"] for x in r)
    assert a["initiating_party"] == "Demo Company 0001"
    assert a["payment_info_id"] == "DEMOPMT0001" and a["payment_method"] == "TRF"
    assert a["requested_execution_date"] == TODAY
    assert a["debtor_account"] == "ZZ00DEMO0000000001" and a["debtor_agent_bic"] == "DEMOZZ22XXX"
    assert a["creditor_name"] == "Demo Customer 0301" and a["currency"] == "EUR"
    assert a["charge_bearer"] == "SLEV" and a["purpose_code"] == "SUPP"
    assert formats.check("pain001", p, {})["ok"]


def test_pain001_control_checks(tmp_path):
    text = samples.pain001_xml(TODAY, random.Random(5))
    bad_n = write(tmp_path, "n.xml", text.replace("<NbOfTxs>3</NbOfTxs>", "<NbOfTxs>4</NbOfTxs>", 1))
    with pytest.raises(FormatError) as e:
        rows("pain001", bad_n)
    assert "NbOfTxs says 4" in str(e.value)
    # the PmtInf-level CtrlSum (the second CtrlSum in the file)
    k = text.index("<CtrlSum>", text.index("<PmtInf>"))
    bad_s = write(tmp_path, "s.xml", text[:k] + "<CtrlSum>1.00</CtrlSum>"
                  + text[text.index("</CtrlSum>", k) + len("</CtrlSum>"):])
    res = formats.check("pain001", bad_s, {})
    bad = [c for c in res["checks"] if c["level"] == "error"]
    assert bad and "payment DEMOPMT0001" in bad[0]["message"]


def test_pacs008(tmp_path):
    p = write(tmp_path, "x.xml", samples.pacs008_xml(TODAY, random.Random(6)))
    r = rows("pacs008", p)
    assert len(r) == 2
    a = r[0]
    assert a["settlement_method"] == "CLRG" and a["settlement_date"] == TODAY
    assert len(a["uetr"]) == 36 and a["transaction_id"] == "TX000001"
    assert a["instructing_agent_bic"] == "DEMOZZ22XXX" and a["instructed_agent_bic"] == "TESTZZ33XXX"
    assert a["instructed_amount"] == a["amount"] and a["instructed_currency"] == "USD"
    assert a["debtor_account"] == "DEMO000000401"
    assert formats.check("pacs008", p, {})["ok"]
    text = p.read_text()
    i = text.index("<TtlIntrBkSttlmAmt")
    j = text.index("</TtlIntrBkSttlmAmt>")
    bad = write(tmp_path, "bad.xml",
                text[:i] + '<TtlIntrBkSttlmAmt Ccy="USD">1.00' + text[j:])
    res = formats.check("pacs008", bad, {})
    assert not res["ok"]
    assert any("TtlIntrBkSttlmAmt says 1.000" in c["message"] for c in res["checks"])


def test_doctype_is_refused():
    with pytest.raises(FormatError) as e:
        rows("camt053", FIX / "camt053_doctype.xml")
    assert "DOCTYPE" in str(e.value)
    assert not formats.check("camt053", FIX / "camt053_doctype.xml", {})["ok"]


def test_doctype_refused_by_the_parser_in_utf16(tmp_path):
    text = (FIX / "camt053_doctype.xml").read_text().replace('encoding="UTF-8"', 'encoding="UTF-16"')
    p = tmp_path / "u16.xml"
    p.write_bytes(text.encode("utf-16"))
    assert b"<!DOCTYPE" not in p.read_bytes()       # the byte check cannot see it
    with pytest.raises(FormatError) as e:
        rows("camt053", p)
    assert "DOCTYPE" in str(e.value)


def test_entity_without_doctype_bytes_check(tmp_path, monkeypatch):
    from etlplus.formats import iso
    monkeypatch.setattr(iso, "prolog_refused", lambda head: False)
    with pytest.raises(FormatError) as e:
        rows("camt053", FIX / "camt053_doctype.xml")
    assert "refused" in str(e.value)


def test_wrong_message_kind_and_broken_xml(tmp_path):
    p = write(tmp_path, "p.xml", samples.pain001_xml(TODAY, random.Random(5)))
    with pytest.raises(FormatError):
        rows("camt053", p)
    q = write(tmp_path, "broken.xml", "<Document><BkToCstmrStmt>")
    with pytest.raises(FormatError):
        rows("camt053", q)


def test_entries_are_removed_after_their_row(tmp_path, monkeypatch):
    """Flat memory: each finished Ntry leaves the tree (the 200k RSS test is the slow one)."""
    from etlplus.formats import iso
    seen = []
    orig = iso._Camt._entry

    def spy(self, e, stmt, line):
        seen.append(len(stmt.findall("Ntry")))
        return orig(self, e, stmt, line)
    monkeypatch.setattr(iso._Camt, "_entry", spy)
    p = write(tmp_path, "s.xml", samples.camt053_xml(TODAY, random.Random(3)))
    rows("camt053", p)
    assert seen == [1] * 6


def _rss() -> int:
    """Resident memory of this process, without extra packages."""
    import os
    import sys
    if sys.platform.startswith("linux"):
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.WorkingSetSize
    pytest.skip("no way to read the resident memory here")


def test_rss_reader_works():
    assert _rss() > 1024 * 1024


@pytest.mark.slow
def test_large_camt053_flat_memory(tmp_path):
    one = samples.camt053_xml(TODAY, random.Random(3))
    head, rest = one.split("<Ntry>", 1)
    entry = "<Ntry>" + rest.split("</Ntry>", 1)[0] + "</Ntry>\n"
    tail = "</Stmt>\n" + one.rsplit("</Stmt>", 1)[1]
    head = head.replace("<NbOfNtries>6</NbOfNtries>", "<NbOfNtries>200000</NbOfNtries>")
    p = tmp_path / "big.xml"
    with open(p, "w", encoding="utf-8") as f:
        f.write(head)
        for _ in range(200_000):
            f.write(entry)
        f.write(tail)
    before = _rss()
    peak = before
    n = 0
    for b in formats.read("camt053", p, {"checks": "warn"}, chunk_rows=10_000):
        n += b.num_rows
        peak = max(peak, _rss())
    assert n == 200_000
    assert peak - before < 150 * 1024 * 1024
