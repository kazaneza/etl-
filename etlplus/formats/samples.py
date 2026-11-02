"""Made-up bank files for the demo and the tests. Generated, never stored; every check passes.

Everything here is invented: BICs use the country code ZZ, accounts start with DEMO,
currencies are USD, EUR and XTS (the ISO test code), people are "Demo Customer 0007", and
card numbers are generated already masked, so no full card number ever exists in the demo.

    write(folder, today)      -> the five files the demo uses (section 5.9)
    write_all(folder, today)  -> those plus one file for every other format
"""

from __future__ import annotations

import datetime as dt
import os
import random
import uuid
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from . import layouts

SEED = 20261101
BIC_A = "DEMOZZ22XXX"
BIC_B = "TESTZZ33XXX"
LT_A = "DEMOZZ22AXXX"     # 12-character logical terminal addresses
LT_B = "TESTZZ33AXXX"
ACCOUNT = "DEMO000000001"
IBAN = "ZZ00DEMO0000000001"


def _publish(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".partial")
    with open(part, "w", encoding=encoding, newline="") as f:
        f.write(text)
    os.replace(part, path)
    return path


def _swift_amount(d: Decimal) -> str:
    return f"{abs(d):.2f}".replace(".", ",")


def _yymmdd(d: dt.date) -> str:
    return d.strftime("%y%m%d")


def _cents(rng, lo, hi) -> Decimal:
    return Decimal(rng.randint(lo, hi)) / 100


def _uetr(rng) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


# --------------------------------------------------------------------------- MT940 / MT950

def mt940_text(today: dt.date, rng: random.Random, *, mt: str = "940",
               counts=(7, 5), blocks: bool = True) -> str:
    out = []
    balance = Decimal("25000.00")
    line_no = 0
    for k, n in enumerate(counts):
        day = today - dt.timedelta(days=len(counts) - k)
        msg = []
        if blocks:
            b2 = f"O{mt}1200{_yymmdd(day)}{LT_B}0000000000{_yymmdd(day)}1200N"
            msg.append(f"{{1:F01{LT_A}0000000000}}{{2:{b2}}}{{4:")
        msg.append(f":20:STMT{day:%Y%m%d}{k + 1:02d}")
        msg.append(f":25:{ACCOUNT}")
        msg.append(f":28C:{k + 1:05d}/001")
        msg.append(f":60F:{'C' if balance >= 0 else 'D'}{_yymmdd(day)}USD{_swift_amount(balance)}")
        for i in range(n):
            line_no += 1
            credit = i % 3 != 2
            amt = _cents(rng, 1000, 250000)
            mark = "C" if credit else "D"
            ttype = "NTRF" if credit else ("NCHG" if i % 2 else "NMSC")
            balance += amt if credit else -amt
            msg.append(f":61:{_yymmdd(day)}{day:%m%d}{mark}{_swift_amount(amt)}{ttype}"
                       f"DEMOREF{line_no:04d}//BANKREF{line_no:04d}")
            if mt == "940":
                msg.append(f":86:Demo transfer {line_no:04d}")
                if i == 0:
                    msg.append("Demo Customer 0007 reference")
        msg.append(f":62F:{'C' if balance >= 0 else 'D'}{_yymmdd(day)}USD{_swift_amount(balance)}")
        msg.append(f":64:{'C' if balance >= 0 else 'D'}{_yymmdd(day)}USD{_swift_amount(balance)}")
        msg.append("-}" if blocks else "-")
        out.extend(msg)
    return "\r\n".join(out) + "\r\n"


# --------------------------------------------------------------------------- MT103 / MT202

def mt103_text(today: dt.date, rng: random.Random, n: int = 3) -> str:
    out = []
    for i in range(1, n + 1):
        amt = _cents(rng, 10000, 900000)
        ccy = ("USD", "EUR", "USD")[(i - 1) % 3]
        uetr = _uetr(rng)
        charges = ("SHA", "OUR", "BEN")[(i - 1) % 3]
        out.append(f"{{1:F01{LT_A}0000{i:06d}}}{{2:I103TESTZZ33XXXXN}}"
                   f"{{3:{{108:DEMOMUR{i:04d}}}{{121:{uetr}}}}}{{4:")
        out += [f":20:PAY{today:%Y%m%d}{i:04d}", ":23B:CRED",
                f":32A:{_yymmdd(today)}{ccy}{_swift_amount(amt)}",
                f":33B:{ccy}{_swift_amount(amt)}",
                f":50K:/DEMO{i:09d}", f"Demo Customer {i:04d}", f"Demo Street {i}",
                f":52A:{BIC_A}",
                f":57A:{BIC_B}",
                f":59:/DEMO{100 + i:09d}", f"Demo Customer {100 + i:04d}",
                f":70:Demo invoice {i:04d}",
                f":71A:{charges}"]
        if charges == "BEN":
            out.append(f":71F:{ccy}5,00")
        if charges == "OUR":
            out.append(f":71G:{ccy}7,50")
        out += [":72:/INS/DEMO TEST ONLY", "-}"]
    return "\r\n".join(out) + "\r\n"


def mt202_text(today: dt.date, rng: random.Random, n: int = 2) -> str:
    out = []
    for i in range(1, n + 1):
        amt = _cents(rng, 1000000, 90000000)
        out.append(f"{{1:F01{LT_A}0000{i:06d}}}{{2:I202TESTZZ33XXXXN}}"
                   f"{{3:{{121:{_uetr(rng)}}}}}{{4:")
        out += [f":20:COV{today:%Y%m%d}{i:04d}", f":21:REL{today:%Y%m%d}{i:04d}",
                f":32A:{_yymmdd(today)}USD{_swift_amount(amt)}",
                f":52A:{BIC_A}", f":53A:{BIC_A}", f":57A:{BIC_B}", f":58A:{BIC_B}",
                ":72:/BNF/DEMO COVER", "-}"]
    return "\r\n".join(out) + "\r\n"


# --------------------------------------------------------------------------- ISO 20022

def _x(v) -> str:
    return escape(str(v))


def camt053_xml(today: dt.date, rng: random.Random, *, version: str = "08",
                kind: str = "053") -> str:
    ns = f"urn:iso:std:iso:20022:tech:xsd:camt.{kind}.001.{version}"
    day = today - dt.timedelta(days=1)
    v2 = version == "02"
    entries = []
    opening = Decimal("10000.00")
    total = Decimal(0)
    abs_total = Decimal(0)
    pattern = (["CRDT", "CRDT", "DBIT", "CRDT", "DBIT", "CRDT"] if kind == "053"
               else ["CRDT", "DBIT"])
    for i, cd in enumerate(pattern, 1):
        amt = _cents(rng, 500, 300000)
        total += amt if cd == "CRDT" else -amt
        abs_total += amt
        party, acct, agent = (("Dbtr", "DbtrAcct", "DbtrAgt") if cd == "CRDT"
                              else ("Cdtr", "CdtrAcct", "CdtrAgt"))
        name = (f"<{party}><Nm>Demo Customer {i:04d}</Nm></{party}>" if v2 else
                f"<{party}><Pty><Nm>Demo Customer {i:04d}</Nm></Pty></{party}>")
        bic = "BIC" if v2 else "BICFI"
        sts = "<Sts>BOOK</Sts>" if v2 else "<Sts><Cd>BOOK</Cd></Sts>"
        two_tx = kind == "053" and i == 1
        txs = []
        for t in range(2 if two_tx else 1):
            tx_amt = (amt / 2).quantize(Decimal("0.01")) if two_tx else amt
            if two_tx and t == 1:
                tx_amt = amt - (amt / 2).quantize(Decimal("0.01"))
            txs.append(
                f"<TxDtls><Refs><EndToEndId>E2E{i:04d}{t}</EndToEndId></Refs>"
                f"<Amt Ccy=\"USD\">{tx_amt:.2f}</Amt>"
                f"<RltdPties>{name}<{acct}><Id><Othr><Id>DEMO{200 + i:09d}</Id></Othr></Id>"
                f"</{acct}></RltdPties>"
                f"<RltdAgts><{agent}><FinInstnId><{bic}>{BIC_B}</{bic}></FinInstnId></{agent}>"
                f"</RltdAgts>"
                f"<RmtInf><Ustrd>Demo transfer {i:04d}</Ustrd><Ustrd>part {t + 1}</Ustrd></RmtInf>"
                f"</TxDtls>")
        entries.append(
            f"<Ntry><NtryRef>{i:04d}</NtryRef><Amt Ccy=\"USD\">{amt:.2f}</Amt>"
            f"<CdtDbtInd>{cd}</CdtDbtInd><RvslInd>false</RvslInd>{sts}"
            f"<BookgDt><Dt>{day.isoformat()}</Dt></BookgDt>"
            f"<ValDt><Dt>{day.isoformat()}</Dt></ValDt>"
            f"<AcctSvcrRef>SVC{i:06d}</AcctSvcrRef>"
            f"<BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>{'RCDT' if cd == 'CRDT' else 'ICDT'}</Cd>"
            f"<SubFmlyCd>{'DMCT' if cd == 'CRDT' else 'AUTT'}</SubFmlyCd></Fmly></Domn>"
            f"<Prtry><Cd>DEMO{i:02d}</Cd><Issr>DEMO</Issr></Prtry></BkTxCd>"
            f"<NtryDtls>{''.join(txs)}</NtryDtls>"
            f"<AddtlNtryInf>Demo entry {i:04d}</AddtlNtryInf></Ntry>")
    closing = opening + total
    acct = (f"<Acct><Id><IBAN>{IBAN}</IBAN></Id><Ccy>USD</Ccy>"
            f"<Svcr><FinInstnId><{'BIC' if v2 else 'BICFI'}>{BIC_B}</{'BIC' if v2 else 'BICFI'}>"
            f"</FinInstnId></Svcr></Acct>")

    def bal(code, amount):
        cd = "CRDT" if amount >= 0 else "DBIT"
        return (f"<Bal><Tp><CdOrPrtry><Cd>{code}</Cd></CdOrPrtry></Tp>"
                f"<Amt Ccy=\"USD\">{abs(amount):.2f}</Amt><CdtDbtInd>{cd}</CdtDbtInd>"
                f"<Dt><Dt>{day.isoformat()}</Dt></Dt></Bal>")
    net_cd = "CRDT" if total >= 0 else "DBIT"
    summary = (f"<TxsSummry><TtlNtries><NbOfNtries>{len(pattern)}</NbOfNtries>"
               f"<Sum>{abs_total:.2f}</Sum><TtlNetNtry><Amt>{abs(total):.2f}</Amt>"
               f"<CdtDbtInd>{net_cd}</CdtDbtInd></TtlNetNtry></TtlNtries></TxsSummry>")
    if kind == "053":
        body_tag, outer = "Stmt", "BkToCstmrStmt"
        bals = bal("OPBD", opening) + bal("CLBD", closing)
    else:
        body_tag, outer = "Ntfctn", "BkToCstmrDbtCdtNtfctn"
        bals = ""
    created = dt.datetime.combine(today, dt.time(6, 0), dt.timezone.utc)
    return (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            f"<Document xmlns=\"{ns}\">\n<{outer}>\n"
            f"<GrpHdr><MsgId>DEMOMSG{today:%Y%m%d}{kind}</MsgId>"
            f"<CreDtTm>{created.isoformat()}</CreDtTm></GrpHdr>\n"
            f"<{body_tag}><Id>DEMOSTMT{day:%Y%m%d}</Id><ElctrncSeqNb>{day.toordinal() % 1000}"
            f"</ElctrncSeqNb><CreDtTm>{created.isoformat()}</CreDtTm>\n{acct}\n{bals}\n"
            f"{summary}\n" + "\n".join(entries) + f"\n</{body_tag}>\n</{outer}>\n</Document>\n")


def pain001_xml(today: dt.date, rng: random.Random, n: int = 3) -> str:
    ns = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"
    txs = []
    total = Decimal(0)
    for i in range(1, n + 1):
        amt = _cents(rng, 1000, 500000)
        total += amt
        txs.append(
            f"<CdtTrfTxInf><PmtId><InstrId>INSTR{i:04d}</InstrId>"
            f"<EndToEndId>E2E{i:04d}</EndToEndId></PmtId>"
            f"<Amt><InstdAmt Ccy=\"EUR\">{amt:.2f}</InstdAmt></Amt>"
            f"<CdtrAgt><FinInstnId><BIC>{BIC_B}</BIC></FinInstnId></CdtrAgt>"
            f"<Cdtr><Nm>Demo Customer {300 + i:04d}</Nm></Cdtr>"
            f"<CdtrAcct><Id><IBAN>ZZ00DEMO{300 + i:010d}</IBAN></Id></CdtrAcct>"
            f"<Purp><Cd>SUPP</Cd></Purp>"
            f"<RmtInf><Ustrd>Demo invoice {i:04d}</Ustrd></RmtInf></CdtTrfTxInf>")
    created = dt.datetime.combine(today, dt.time(7, 30))
    return (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<Document xmlns=\"{ns}\">"
            f"<CstmrCdtTrfInitn>\n<GrpHdr><MsgId>DEMOPAIN{today:%Y%m%d}</MsgId>"
            f"<CreDtTm>{created.isoformat()}</CreDtTm><NbOfTxs>{n}</NbOfTxs>"
            f"<CtrlSum>{total:.2f}</CtrlSum><InitgPty><Nm>Demo Company 0001</Nm></InitgPty>"
            f"</GrpHdr>\n<PmtInf><PmtInfId>DEMOPMT0001</PmtInfId><PmtMtd>TRF</PmtMtd>"
            f"<NbOfTxs>{n}</NbOfTxs><CtrlSum>{total:.2f}</CtrlSum>"
            f"<ReqdExctnDt>{today.isoformat()}</ReqdExctnDt>"
            f"<Dbtr><Nm>Demo Company 0001</Nm></Dbtr>"
            f"<DbtrAcct><Id><IBAN>{IBAN}</IBAN></Id></DbtrAcct>"
            f"<DbtrAgt><FinInstnId><BIC>{BIC_A}</BIC></FinInstnId></DbtrAgt>"
            f"<ChrgBr>SLEV</ChrgBr>\n" + "\n".join(txs) +
            "\n</PmtInf></CstmrCdtTrfInitn></Document>\n")


def pacs008_xml(today: dt.date, rng: random.Random, n: int = 2) -> str:
    ns = "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"
    txs = []
    total = Decimal(0)
    for i in range(1, n + 1):
        amt = _cents(rng, 10000, 2000000)
        total += amt
        txs.append(
            f"<CdtTrfTxInf><PmtId><InstrId>INSTR{i:04d}</InstrId>"
            f"<EndToEndId>E2E{i:04d}</EndToEndId><TxId>TX{i:06d}</TxId>"
            f"<UETR>{_uetr(rng)}</UETR></PmtId>"
            f"<IntrBkSttlmAmt Ccy=\"USD\">{amt:.2f}</IntrBkSttlmAmt>"
            f"<IntrBkSttlmDt>{today.isoformat()}</IntrBkSttlmDt>"
            f"<InstdAmt Ccy=\"USD\">{amt:.2f}</InstdAmt><ChrgBr>SHAR</ChrgBr>"
            f"<InstgAgt><FinInstnId><BICFI>{BIC_A}</BICFI></FinInstnId></InstgAgt>"
            f"<InstdAgt><FinInstnId><BICFI>{BIC_B}</BICFI></FinInstnId></InstdAgt>"
            f"<Dbtr><Nm>Demo Customer {400 + i:04d}</Nm></Dbtr>"
            f"<DbtrAcct><Id><Othr><Id>DEMO{400 + i:09d}</Id></Othr></Id></DbtrAcct>"
            f"<DbtrAgt><FinInstnId><BICFI>{BIC_A}</BICFI></FinInstnId></DbtrAgt>"
            f"<CdtrAgt><FinInstnId><BICFI>{BIC_B}</BICFI></FinInstnId></CdtrAgt>"
            f"<Cdtr><Nm>Demo Customer {500 + i:04d}</Nm></Cdtr>"
            f"<CdtrAcct><Id><Othr><Id>DEMO{500 + i:09d}</Id></Othr></Id></CdtrAcct>"
            f"<RmtInf><Ustrd>Demo transfer {i:04d}</Ustrd></RmtInf></CdtTrfTxInf>")
    created = dt.datetime.combine(today, dt.time(9, 15), dt.timezone.utc)
    return (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<Document xmlns=\"{ns}\">"
            f"<FIToFICstmrCdtTrf>\n<GrpHdr><MsgId>DEMOPACS{today:%Y%m%d}</MsgId>"
            f"<CreDtTm>{created.isoformat()}</CreDtTm><NbOfTxs>{n}</NbOfTxs>"
            f"<TtlIntrBkSttlmAmt Ccy=\"USD\">{total:.2f}</TtlIntrBkSttlmAmt>"
            f"<IntrBkSttlmDt>{today.isoformat()}</IntrBkSttlmDt>"
            f"<SttlmInf><SttlmMtd>CLRG</SttlmMtd></SttlmInf></GrpHdr>\n"
            + "\n".join(txs) + "\n</FIToFICstmrCdtTrf></Document>\n")


# --------------------------------------------------------------------------- BAI2

def bai2_text(today: dt.date, rng: random.Random) -> str:
    d = _yymmdd(today - dt.timedelta(days=1))
    lines = [f"01,DEMOZZ22,TESTZZ33,{_yymmdd(today)},0600,DEMOFILE01,,,2/",
             f"02,DEMOZZ22,TESTZZ33,1,{d},,USD,2/"]
    file_total = 0
    group_total = 0
    group_records = 1
    accounts = 0
    for a in range(1, 3):
        opening = rng.randint(100000, 900000)
        details = []
        net = 0
        for i in range(1, 4 + a):
            credit = i % 2 == 1
            amt = rng.randint(1000, 90000)
            net += amt if credit else -amt
            details.append((("195" if credit else "475"), amt, i))
        closing = opening + net
        rec = [f"03,DEMO{a:09d},USD,010,{opening},,,015,{closing},,/"]
        total = opening + closing
        for code, amt, i in details:
            text = f"Demo transfer {a}{i:03d}"
            if i == 1:
                rec.append(f"16,{code},{amt},Z,BREF{a}{i:04d},CREF{a}{i:04d},{text}")
                rec.append("88,Demo Customer 0007 continued/")
            else:
                rec.append(f"16,{code},{amt},Z,BREF{a}{i:04d},CREF{a}{i:04d},{text}/")
            total += amt
        count = len(rec) + 1
        rec.append(f"49,{total},{count}/")
        lines += rec
        group_total += total
        group_records += count
        accounts += 1
    group_records += 1
    lines.append(f"98,{group_total},{accounts},{group_records}/")
    file_total += group_total
    lines.append(f"99,{file_total},1,{len(lines) + 1}/")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- layouts

def card_ni_text(today: dt.date, rng: random.Random, n: int = 12) -> str:
    L = layouts.Compiled(layouts.load("card_processor_ni_style"))
    settle = today - dt.timedelta(days=1)
    lines = [layouts.format_record(L, "header", {
        "processor_id": "PROC00001", "institution_id": "INST0001", "file_date": today,
        "file_sequence": today.timetuple().tm_yday})]
    total = Decimal(0)
    for i in range(1, n + 1):
        ttype = ("00", "00", "01", "00", "20", "00")[(i - 1) % 6]
        amt = _cents(rng, 500, 150000)
        signed = -amt if ttype == "20" else amt
        total += signed
        ccy = "840" if i % 4 else "978"
        settle_amt = signed if ccy == "840" else (signed * Decimal("1.08")).quantize(Decimal("0.01"))
        pan = f"400000******{rng.randint(0, 9999):04d}"     # generated masked
        when = dt.datetime.combine(settle, dt.time(8 + i % 12, (i * 7) % 60, (i * 13) % 60))
        lines.append(layouts.format_record(L, "detail", {
            "card_number": pan, "transaction_type": ttype,
            "transaction_date": settle, "transaction_time": when.time(),
            "terminal_id": f"TERM{i % 5 + 1:04d}", "merchant_id": f"MERCH{i % 7 + 1:010d}",
            "merchant_name": f"Demo Merchant {i % 7 + 1:04d}",
            "mcc": ("5411", "5812", "6011", "5999")[(i - 1) % 4],
            "amount": signed, "currency": ccy,
            "settlement_amount": settle_amt, "settlement_currency": "840",
            "settlement_date": settle,
            "fee": (amt * Decimal("0.015")).quantize(Decimal("0.01")),
            "auth_code": f"A{rng.randint(0, 99999):05d}", "response_code": "00",
            "arn": "".join(str(rng.randint(0, 9)) for _ in range(23)),
        }))
    lines.append(layouts.format_record(L, "trailer", {"record_count": n, "total_amount": total}))
    return "\r\n".join(lines) + "\r\n"


def payroll_text(today: dt.date, rng: random.Random, n: int = 8) -> str:
    L = layouts.Compiled(layouts.load("payroll_batch_example"))
    lines = [layouts.format_record(L, "header", {"company_code": "DEMOCO0001",
                                                 "batch_date": today})]
    total = Decimal(0)
    for i in range(1, n + 1):
        amt = _cents(rng, 50000, 900000)
        total += amt
        lines.append(layouts.format_record(L, "detail", {
            "employee_id": f"EMP{i:07d}", "account": f"DEMO{600 + i:010d}", "amount": amt,
            "reference": f"Demo salary {today:%Y%m} {i:04d}"}))
    lines.append(layouts.format_record(L, "trailer", {"record_count": n, "total_amount": total}))
    return "\r\n".join(lines) + "\r\n"


def statement_fixed_text(today: dt.date, rng: random.Random, n: int = 6) -> str:
    L = layouts.Compiled(layouts.load("statement_fixed_example"))
    lines = []
    for i in range(1, n + 1):
        amt = _cents(rng, 100, 500000) * (1 if i % 3 else -1)
        fee = _cents(rng, 0, 500)
        lines.append(layouts.format_record(L, "line", {
            "booking_date": today - dt.timedelta(days=1), "value_date": today,
            "account": ACCOUNT, "amount": amt, "fee": fee, "fee_sign": "D" if i % 2 else "C",
            "currency": ("USD", "EUR", "XTS")[i % 3], "description": f"Demo line {i:04d}"}))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- writers

def write(folder: Path, today: dt.date) -> list[Path]:
    """The demo's bank files under <folder>/bank/ (section 5.9). Deterministic for `today`."""
    rng = random.Random(SEED)
    bank = Path(folder) / "bank"
    return [
        _publish(bank / "statement_mt940.txt", mt940_text(today, rng)),
        _publish(bank / "payments_mt103.txt", mt103_text(today, rng)),
        _publish(bank / "statement_camt053.xml", camt053_xml(today, rng)),
        _publish(bank / "card_daily_ni_style.txt", card_ni_text(today, rng), "latin-1"),
        _publish(bank / "payroll_batch.txt", payroll_text(today, rng), "latin-1"),
    ]


def write_all(folder: Path, today: dt.date) -> list[Path]:
    """`write` plus one made-up file for every other format, for tests and examples."""
    paths = write(folder, today)
    rng = random.Random(SEED + 1)
    bank = Path(folder) / "bank"
    paths += [
        _publish(bank / "statement_mt950.txt", mt940_text(today, rng, mt="950", counts=(4,))),
        _publish(bank / "cover_mt202.txt", mt202_text(today, rng)),
        _publish(bank / "notification_camt054.xml", camt053_xml(today, rng, version="02",
                                                                 kind="054")),
        _publish(bank / "payment_order_pain001.xml", pain001_xml(today, rng)),
        _publish(bank / "credit_transfer_pacs008.xml", pacs008_xml(today, rng)),
        _publish(bank / "cash_report.bai", bai2_text(today, rng)),
        _publish(bank / "statement_fixed.txt", statement_fixed_text(today, rng), "latin-1"),
    ]
    return paths


# format id (and options) for each file `write_all` produces
SAMPLE_FORMATS = {
    "statement_mt940.txt": ("mt940", {}),
    "payments_mt103.txt": ("mt103", {}),
    "statement_camt053.xml": ("camt053", {}),
    "card_daily_ni_style.txt": ("layout", {"layout": "card_processor_ni_style"}),
    "payroll_batch.txt": ("layout", {"layout": "payroll_batch_example"}),
    "statement_mt950.txt": ("mt950", {}),
    "cover_mt202.txt": ("mt202", {}),
    "notification_camt054.xml": ("camt054", {}),
    "payment_order_pain001.xml": ("pain001", {}),
    "credit_transfer_pacs008.xml": ("pacs008", {}),
    "cash_report.bai": ("bai2", {}),
    "statement_fixed.txt": ("layout", {"layout": "statement_fixed_example"}),
}
