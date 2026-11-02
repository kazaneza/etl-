"""ISO 20022 XML: camt.053 / camt.054 (one row per Ntry, or per TxDtls), pain.001 and
pacs.008 (one row per CdtTrfTxInf).

Safety and memory:

- A DOCTYPE or an entity declaration is refused by the parser itself (expat's
  StartDoctypeDeclHandler and EntityDeclHandler raise), after a fast byte check of the prolog.
- The file is fed to expat in 64 KB pieces; elements are built as in iterparse, with local
  names only (namespaces are dropped, so every message version is accepted).
- Each finished Ntry / CdtTrfTxInf is cleared and removed from its parent once its row is
  out, and so is each finished Stmt / Ntfctn / PmtInf, so memory stays flat.
- Whole-file checks keep running counters; they never hold rows.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from xml.parsers import expat

from ._common import (Batcher, Checks, FormatError, col, fit, iso_date, iso_timestamp,
                      parse_amount, read_head, schema_of)

REFUSED = "XML with a DOCTYPE or entities is refused."

# --------------------------------------------------------------------------- columns

_FILE = [col("file_name", "string", help="The file the row came from"),
         col("record_line", "integer", help="Line of the row's element in the file")]

CAMT_COLUMNS = tuple(_FILE + [
    col("message_id", "string", help="GrpHdr/MsgId"),
    col("creation_time", "timestamptz", help="GrpHdr/CreDtTm"),
    col("statement_id", "string", help="Stmt/Id or Ntfctn/Id"),
    col("electronic_sequence", "integer", help="ElctrncSeqNb"),
    col("account", "string", True, "IBAN or Othr/Id"),
    col("account_currency", "string(3)"),
    col("opening_balance", "decimal(18,3)", help="OPBD or PRCD, signed; null in camt.054"),
    col("closing_balance", "decimal(18,3)", help="CLBD, signed; null in camt.054"),
    col("entry_reference", "string", help="NtryRef"),
    col("amount", "decimal(18,3)", help="Ntry/Amt, unsigned; the entry's amount, repeated on each transaction row"),
    col("currency", "string(3)", help="Amt/@Ccy"),
    col("credit_debit", "string", help="CRDT or DBIT"),
    col("signed_amount", "decimal(18,3)", help="Entry amount, CRDT positive, DBIT negative"),
    col("transaction_amount", "decimal(18,3)",
        help="TxDtls/AmtDtls/TxAmt/Amt or TxDtls/Amt, unsigned; sum this at level transaction"),
    col("transaction_currency", "string(3)", help="The transaction amount's Ccy"),
    col("reversal", "boolean", help="RvslInd"),
    col("status", "string", help="Sts, for example BOOK or PDNG"),
    col("booking_date", "date"),
    col("value_date", "date"),
    col("account_servicer_reference", "string", help="AcctSvcrRef"),
    col("bank_tx_code", "string", help="Domain/Family/Sub-family"),
    col("proprietary_code", "string", help="BkTxCd/Prtry/Cd"),
    col("end_to_end_id", "string"),
    col("counterparty_name", "string", True, "Debtor on a credit, creditor on a debit"),
    col("counterparty_account", "string", True),
    col("counterparty_agent_bic", "string"),
    col("remittance_info", "string", True, "Ustrd joined with a space"),
    col("additional_info", "string", help="AddtlNtryInf"),
])

PAIN_COLUMNS = tuple(_FILE + [
    col("message_id", "string"), col("creation_time", "timestamptz"),
    col("number_of_transactions", "integer", help="GrpHdr/NbOfTxs"),
    col("control_sum", "decimal(18,3)", help="GrpHdr/CtrlSum"),
    col("initiating_party", "string", True),
    col("payment_info_id", "string"), col("payment_method", "string"),
    col("requested_execution_date", "date"),
    col("debtor_name", "string", True), col("debtor_account", "string", True),
    col("debtor_agent_bic", "string"),
    col("instruction_id", "string"), col("end_to_end_id", "string"),
    col("amount", "decimal(18,3)", help="InstdAmt"), col("currency", "string(3)"),
    col("charge_bearer", "string"),
    col("creditor_agent_bic", "string"),
    col("creditor_name", "string", True), col("creditor_account", "string", True),
    col("remittance_info", "string", True), col("purpose_code", "string"),
])

PACS_COLUMNS = tuple(_FILE + [
    col("message_id", "string"), col("creation_time", "timestamptz"),
    col("number_of_transactions", "integer"),
    col("settlement_method", "string"), col("settlement_date", "date"),
    col("instruction_id", "string"), col("end_to_end_id", "string"),
    col("transaction_id", "string"), col("uetr", "string"),
    col("amount", "decimal(18,3)", help="IntrBkSttlmAmt"), col("currency", "string(3)"),
    col("instructed_amount", "decimal(18,3)"), col("instructed_currency", "string(3)"),
    col("charge_bearer", "string"),
    col("instructing_agent_bic", "string"), col("instructed_agent_bic", "string"),
    col("debtor_name", "string", True), col("debtor_account", "string", True),
    col("debtor_agent_bic", "string"),
    col("creditor_agent_bic", "string"),
    col("creditor_name", "string", True), col("creditor_account", "string", True),
    col("remittance_info", "string", True),
])


# --------------------------------------------------------------------------- pull parser

def _local(name: str) -> str:
    return name.rsplit(" ", 1)[-1]


def _refuse(*_a, **_k):
    raise FormatError(REFUSED)


class Pull:
    """iterparse over expat, with local names, line numbers and DOCTYPE/entity refusal.

    `on_end(elem, parent, line)` is called for every finished element; `on_start(elem, line)`
    for every started one.
    """

    def __init__(self, on_start, on_end, encoding: str | None = None):
        p = expat.ParserCreate(encoding, namespace_separator=" ")
        p.StartDoctypeDeclHandler = _refuse
        p.EntityDeclHandler = _refuse
        p.UnparsedEntityDeclHandler = _refuse
        p.ExternalEntityRefHandler = _refuse
        p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        p.StartElementHandler = self._start
        p.EndElementHandler = self._end
        p.CharacterDataHandler = self._text
        p.buffer_text = True
        self.p = p
        self.stack: list[tuple[ET.Element, int]] = []
        self.root: ET.Element | None = None
        self.root_ns: str | None = None
        self.on_start = on_start
        self.on_end = on_end

    def _start(self, name, attrs):
        line = self.p.CurrentLineNumber
        el = ET.Element(_local(name), {_local(k): v for k, v in attrs.items()})
        if self.stack:
            self.stack[-1][0].append(el)
        else:
            self.root = el
            self.root_ns = name.rsplit(" ", 1)[0] if " " in name else ""
        self.stack.append((el, line))
        self.on_start(el, line)

    def _end(self, name):
        el, line = self.stack.pop()
        parent = self.stack[-1][0] if self.stack else None
        self.on_end(el, parent, line)

    def _text(self, data):
        if self.stack:
            el = self.stack[-1][0]
            if len(el):            # mixed content after a child: ignore (tail text)
                return
            el.text = (el.text or "") + data

    def feed_file(self, path: Path):
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        self.p.Parse(b"", True)
                        break
                    self.p.Parse(chunk, False)
        except expat.ExpatError as e:
            raise FormatError(f"The XML does not parse: {expat.errors.messages[e.code]} "
                              f"(line {e.lineno}).", line=e.lineno) from None


def prolog_refused(head: bytes) -> bool:
    """Fast first check; the parser handlers are the real guard (UTF-16 hides these bytes)."""
    up = head.upper()
    return b"<!DOCTYPE" in up or b"<!ENTITY" in up


# --------------------------------------------------------------------------- helpers

def _t(el, path: str) -> str | None:
    if el is None:
        return None
    x = el.find(path)
    if x is None or x.text is None:
        return None
    v = x.text.strip()
    return v or None


def _first(el, *paths) -> str | None:
    for p in paths:
        v = _t(el, p)
        if v is not None:
            return v
    return None


def _acct(el) -> str | None:
    """Acct/Id/IBAN or Acct/Id/Othr/Id."""
    return _first(el, "Id/IBAN", "Id/Othr/Id")


def _bic(el) -> str | None:
    return _first(el, "FinInstnId/BICFI", "FinInstnId/BIC")


def _name(el) -> str | None:
    return _first(el, "Nm", "Pty/Nm")


def _date(el) -> str | None:
    """Dt, or the date part of DtTm; or the element's own text (older versions)."""
    if el is None:
        return None
    v = _first(el, "Dt", "DtTm")
    if v is None and el.text and el.text.strip():
        v = el.text.strip()
    return v[:10] if v else None


def _ustrd(el) -> str | None:
    if el is None:
        return None
    parts = [x.text.strip() for x in el.iter("Ustrd") if x.text and x.text.strip()]
    return " ".join(parts) or None


class _Row:
    """Build a row while recording a failed conversion as a check, not a crash."""

    def __init__(self, checks: Checks, where: str, line: int):
        self.checks, self.where, self.line = checks, where, line
        self.bad = False

    def amount(self, text, name="amount"):
        try:
            return fit(parse_amount(text, comma=False))
        except ValueError as e:
            self.bad = True
            self.checks.fail("amount", f"{self.where}: {name} {e}.", line=self.line)
            return None

    def date(self, text, name="date"):
        try:
            return iso_date(text)
        except ValueError:
            self.bad = True
            self.checks.fail("dates", f"{self.where}: {name} '{text}' is not a date.",
                             line=self.line)
            return None

    def ts(self, text, name="time"):
        try:
            return iso_timestamp(text)
        except ValueError:
            self.bad = True
            self.checks.fail("dates", f"{self.where}: {name} '{text}' is not a timestamp.",
                             line=self.line)
            return None

    def integer(self, text, name="number"):
        if text is None:
            return None
        try:
            return int(text)
        except ValueError:
            self.bad = True
            self.checks.fail("values", f"{self.where}: {name} '{text}' is not a whole number.",
                             line=self.line)
            return None


def _ccy_ok(checks, ccy, where, line):
    if ccy is None or len(ccy) != 3 or not ccy.isalpha() or not ccy.isupper():
        checks.fail("amount", f"{where}: currency '{ccy}' is not 3 capital letters.", line=line)
        return False
    return True


# --------------------------------------------------------------------------- runner

def _run(path: Path, options: dict, checks: Checks, chunk_rows: int, columns, handler_cls,
         fmt: str):
    head = read_head(path)
    if prolog_refused(head):
        raise FormatError(REFUSED, line=1)
    batcher = Batcher(schema_of(columns), chunk_rows)
    out: list = []
    h = handler_cls(path, options, checks, fmt, lambda r: out.append(r))
    enc = options.get("encoding")
    enc = None if not enc or str(enc).lower() == "auto" else str(enc)
    pull = Pull(h.start, h.end, enc)
    h.pull = pull
    produced = False
    # feed in pieces and hand out rows as they come, so rows never pile up
    try:
        f = open(path, "rb")
    except OSError as e:
        raise FormatError(f"Cannot open {path.name}: {e}") from None
    with f:
        try:
            while True:
                chunk = f.read(65536)
                final = not chunk
                pull.p.Parse(chunk, final)
                for r in out:
                    b = batcher.add(r)
                    if b is not None:
                        produced = True
                        yield b
                out.clear()
                if final:
                    break
        except expat.ExpatError as e:
            raise FormatError(f"The XML does not parse: {expat.errors.messages[e.code]} "
                              f"(line {e.lineno}).", line=e.lineno) from None
    h.finish(pull)
    b = batcher.flush()
    if b is not None:
        yield b
    elif not produced:
        yield batcher.empty()
    checks.count("rows", batcher.count)


class _Base:
    ROOTS: tuple = ()

    def __init__(self, path, options, checks, fmt, emit):
        self.path, self.options, self.checks, self.fmt, self.emit = path, options, checks, fmt, emit
        self.pull = None
        self.grp = None
        self.started = False

    def start(self, el, line):
        pass

    def finish(self, pull):
        root = pull.root
        if root is None:
            raise FormatError("The file holds no XML element.")
        names = {c.tag for c in root} | {root.tag}
        if not (names & set(self.ROOTS)):
            raise FormatError(f"This is not a {self.fmt} file: none of "
                              f"{', '.join(self.ROOTS)} was found.")


# --------------------------------------------------------------------------- camt

class _Camt(_Base):
    ROOTS = ("BkToCstmrStmt", "BkToCstmrDbtCdtNtfctn", "BkToCstmrAcctRpt")

    def __init__(self, *a):
        super().__init__(*a)
        level = str(self.options.get("level") or "entry").lower()
        if level not in ("entry", "transaction"):
            raise FormatError("options.level must be entry or transaction.")
        self.level = level
        self._new_stmt()

    def _new_stmt(self):
        self.n = 0
        self.total = Decimal(0)
        self.abs_total = Decimal(0)
        self.booked = Decimal(0)
        self.stmt_line = None
        self.bad_amount = False

    def start(self, el, line):
        if el.tag in ("Stmt", "Ntfctn", "Rpt"):
            self._new_stmt()
            self.stmt_line = line

    def end(self, el, parent, line):
        tag = el.tag
        if tag == "GrpHdr":
            self.grp = el
        elif tag == "Ntry" and parent is not None and parent.tag in ("Stmt", "Ntfctn", "Rpt"):
            self._entry(el, parent, line)
            el.clear()
            parent.remove(el)
        elif tag in ("Stmt", "Ntfctn", "Rpt"):
            self._statement_checks(el, line)
            el.clear()
            if parent is not None:
                parent.remove(el)

    def _balances(self, stmt):
        opening = closing = None
        for bal in stmt.findall("Bal"):
            code = _first(bal, "Tp/CdOrPrtry/Cd", "Tp/CdOrPrtry/Prtry")
            r = _Row(self.checks, f"statement {_t(stmt, 'Id')}", self.stmt_line)
            amt = r.amount(_t(bal, "Amt"), "balance")
            if amt is not None and _t(bal, "CdtDbtInd") == "DBIT":
                amt = -amt
            if code in ("OPBD", "PRCD") and opening is None:
                opening = amt
            elif code == "CLBD":
                closing = amt
        return opening, closing

    def _entry(self, e, stmt, line):
        sid = _t(stmt, "Id")
        where = f"statement {sid}, entry at line {line}"
        r = _Row(self.checks, where, line)
        opening, closing = self._balances(stmt) if self.fmt == "camt053" else (None, None)
        amt_el = e.find("Amt")
        amount = r.amount(_t(e, "Amt"))
        ccy = amt_el.get("Ccy") if amt_el is not None else None
        cd = _t(e, "CdtDbtInd")
        ok = True
        if amount is None or amount <= 0:
            if amount is not None:
                self.checks.fail("amount", f"{where}: the amount must be positive.", line=line)
            ok = False
        if not _ccy_ok(self.checks, ccy, where, line):
            ok = False
        if ok:
            self.checks.ok("amount")
        signed = None
        if amount is not None:
            signed = amount if cd == "CRDT" else -amount
            self.total += signed
            self.abs_total += amount
        status = _first(e, "Sts/Cd", "Sts/Prtry", "Sts")
        if signed is not None and (status in (None, "BOOK")):
            self.booked += signed
        self.n += 1
        rv = (_t(e, "RvslInd") or "").lower()
        domn = _t(e, "BkTxCd/Domn/Cd")
        fmly = _t(e, "BkTxCd/Domn/Fmly/Cd")
        sub = _t(e, "BkTxCd/Domn/Fmly/SubFmlyCd")
        code = "/".join(x for x in (domn, fmly, sub) if x) or None
        acct = stmt.find("Acct")
        base = {
            "file_name": self.path.name, "record_line": line,
            "message_id": _t(self.grp, "MsgId"),
            "creation_time": r.ts(_t(self.grp, "CreDtTm"), "CreDtTm"),
            "statement_id": sid,
            "electronic_sequence": r.integer(_t(stmt, "ElctrncSeqNb"), "ElctrncSeqNb"),
            "account": _acct(acct), "account_currency": _t(acct, "Ccy"),
            "opening_balance": opening, "closing_balance": closing,
            "entry_reference": _t(e, "NtryRef"),
            "amount": amount, "currency": ccy, "credit_debit": cd, "signed_amount": signed,
            "reversal": rv in ("true", "1") if rv else None,
            "status": status,
            "booking_date": r.date(_date(e.find("BookgDt")), "BookgDt"),
            "value_date": r.date(_date(e.find("ValDt")), "ValDt"),
            "account_servicer_reference": _t(e, "AcctSvcrRef"),
            "bank_tx_code": code,
            "proprietary_code": _t(e, "BkTxCd/Prtry/Cd"),
            "additional_info": _t(e, "AddtlNtryInf"),
        }
        txs = e.findall("NtryDtls/TxDtls")
        if self.level == "entry" or not txs:
            # at entry level a transaction amount is shown only when the entry holds one transaction
            self.emit({**base, **self._tx(txs[0] if txs else None, cd, r if len(txs) == 1 else None)})
        else:
            for tx in txs:
                self.emit({**base, **self._tx(tx, cd, r)})

    @staticmethod
    def _tx(tx, cd, r=None):
        if tx is None:
            return {"end_to_end_id": None, "counterparty_name": None,
                    "counterparty_account": None, "counterparty_agent_bic": None,
                    "remittance_info": None, "transaction_amount": None, "transaction_currency": None}
        amt_el = tx.find("AmtDtls/TxAmt/Amt")
        if amt_el is None:
            amt_el = tx.find("Amt")
        tx_amount = tx_ccy = None
        if amt_el is not None and r is not None:
            tx_amount = r.amount((amt_el.text or "").strip(), "TxAmt")
            tx_ccy = amt_el.get("Ccy")
        party, acct, agent = (("Dbtr", "DbtrAcct", "DbtrAgt") if cd == "CRDT"
                              else ("Cdtr", "CdtrAcct", "CdtrAgt"))
        return {
            "end_to_end_id": _t(tx, "Refs/EndToEndId"),
            "counterparty_name": _name(tx.find(f"RltdPties/{party}")),
            "counterparty_account": _acct(tx.find(f"RltdPties/{acct}")),
            "counterparty_agent_bic": _bic(tx.find(f"RltdAgts/{agent}")),
            "remittance_info": _ustrd(tx.find("RmtInf")),
            "transaction_amount": tx_amount, "transaction_currency": tx_ccy,
        }

    def _statement_checks(self, stmt, line):
        sid = _t(stmt, "Id")
        self.checks.count("statements")
        if self.fmt == "camt053":
            opening, closing = self._balances(stmt)
            if opening is not None and closing is not None:
                expected = opening + self.booked
                if expected != closing:
                    self.checks.fail("balance", f"statement {sid}: opening {opening} + entries "
                                     f"{self.booked} = {expected}, but the closing balance is "
                                     f"{closing}.", line=self.stmt_line)
                else:
                    self.checks.ok("balance")
        summ = stmt.find("TxsSummry/TtlNtries")
        if summ is not None:
            r = _Row(self.checks, f"statement {sid}", self.stmt_line)
            n = r.integer(_t(summ, "NbOfNtries"), "NbOfNtries")
            s = r.amount(_t(summ, "Sum"), "Sum") if _t(summ, "Sum") else None
            problems = []
            if n is not None and n != self.n:
                problems.append(f"NbOfNtries says {n}, the file holds {self.n}")
            if s is not None and s != self.abs_total:
                problems.append(f"Sum says {s}, the entries add up to {self.abs_total}")
            net = _t(summ, "TtlNetNtry/Amt")
            if net is not None:
                nv = r.amount(net, "TtlNetNtry")
                if nv is not None and _t(summ, "TtlNetNtry/CdtDbtInd") == "DBIT":
                    nv = -nv
                if nv is not None and nv != self.total:
                    problems.append(f"TtlNetNtry says {nv}, the entries net to {self.total}")
            if problems:
                self.checks.fail("summary", f"statement {sid}: " + "; ".join(problems) + ".",
                                 line=self.stmt_line)
            else:
                self.checks.ok("summary")


# --------------------------------------------------------------------------- pain.001

def _ctrl(checks, name, where, line, n_said, n, sum_said, s):
    problems = []
    if n_said is not None and n_said != n:
        problems.append(f"NbOfTxs says {n_said}, the file holds {n}")
    if sum_said is not None and sum_said != s:
        problems.append(f"{name} says {sum_said}, the transactions add up to {s}")
    if problems:
        checks.fail("control", f"{where}: " + "; ".join(problems) + ".", line=line)
    else:
        checks.ok("control")


class _Pain(_Base):
    ROOTS = ("CstmrCdtTrfInitn",)

    def __init__(self, *a):
        super().__init__(*a)
        self.n = 0
        self.sum = Decimal(0)
        self.pmt_n = 0
        self.pmt_sum = Decimal(0)
        self.pmt_line = None
        self.grp_line = None

    def start(self, el, line):
        if el.tag == "PmtInf":
            self.pmt_n, self.pmt_sum, self.pmt_line = 0, Decimal(0), line
        elif el.tag == "GrpHdr":
            self.grp_line = line

    def end(self, el, parent, line):
        if el.tag == "GrpHdr":
            self.grp = el
        elif el.tag == "CdtTrfTxInf" and parent is not None and parent.tag == "PmtInf":
            self._tx(el, parent, line)
            el.clear()
            parent.remove(el)
        elif el.tag == "PmtInf":
            r = _Row(self.checks, f"payment {_t(el, 'PmtInfId')}", self.pmt_line)
            _ctrl(self.checks, "CtrlSum", f"payment {_t(el, 'PmtInfId')}", self.pmt_line,
                  r.integer(_t(el, "NbOfTxs")), self.pmt_n,
                  r.amount(_t(el, "CtrlSum"), "CtrlSum"), self.pmt_sum)
            el.clear()
            if parent is not None:
                parent.remove(el)

    def _tx(self, tx, pmt, line):
        where = f"payment {_t(pmt, 'PmtInfId')}, transaction at line {line}"
        r = _Row(self.checks, where, line)
        amt_el = tx.find("Amt/InstdAmt")
        amount = r.amount(_t(tx, "Amt/InstdAmt"))
        ccy = amt_el.get("Ccy") if amt_el is not None else None
        ok = _ccy_ok(self.checks, ccy, where, line)
        if amount is not None:
            self.n_add(amount)
            if amount <= 0:
                self.checks.fail("amount", f"{where}: the amount must be positive.", line=line)
                ok = False
        if ok and amount is not None:
            self.checks.ok("amount")
        g = self.grp
        self.emit({
            "file_name": self.path.name, "record_line": line,
            "message_id": _t(g, "MsgId"), "creation_time": r.ts(_t(g, "CreDtTm"), "CreDtTm"),
            "number_of_transactions": r.integer(_t(g, "NbOfTxs"), "NbOfTxs"),
            "control_sum": r.amount(_t(g, "CtrlSum"), "CtrlSum"),
            "initiating_party": _name(g.find("InitgPty")) if g is not None else None,
            "payment_info_id": _t(pmt, "PmtInfId"), "payment_method": _t(pmt, "PmtMtd"),
            "requested_execution_date": r.date(_date(pmt.find("ReqdExctnDt")), "ReqdExctnDt"),
            "debtor_name": _name(pmt.find("Dbtr")), "debtor_account": _acct(pmt.find("DbtrAcct")),
            "debtor_agent_bic": _bic(pmt.find("DbtrAgt")),
            "instruction_id": _t(tx, "PmtId/InstrId"), "end_to_end_id": _t(tx, "PmtId/EndToEndId"),
            "amount": amount, "currency": ccy,
            "charge_bearer": _t(tx, "ChrgBr") or _t(pmt, "ChrgBr"),
            "creditor_agent_bic": _bic(tx.find("CdtrAgt")),
            "creditor_name": _name(tx.find("Cdtr")), "creditor_account": _acct(tx.find("CdtrAcct")),
            "remittance_info": _ustrd(tx.find("RmtInf")),
            "purpose_code": _t(tx, "Purp/Cd"),
        })

    def n_add(self, amount):
        self.n += 1
        self.sum += amount
        self.pmt_n += 1
        self.pmt_sum += amount

    def finish(self, pull):
        super().finish(pull)
        g = self.grp
        r = _Row(self.checks, "group header", self.grp_line)
        _ctrl(self.checks, "CtrlSum", "group header", self.grp_line,
              r.integer(_t(g, "NbOfTxs")), self.n, r.amount(_t(g, "CtrlSum"), "CtrlSum"), self.sum)


# --------------------------------------------------------------------------- pacs.008

class _Pacs(_Base):
    ROOTS = ("FIToFICstmrCdtTrf",)

    def __init__(self, *a):
        super().__init__(*a)
        self.n = 0
        self.sum = Decimal(0)
        self.grp_line = None

    def start(self, el, line):
        if el.tag == "GrpHdr":
            self.grp_line = line

    def end(self, el, parent, line):
        if el.tag == "GrpHdr":
            self.grp = el
        elif el.tag == "CdtTrfTxInf" and parent is not None:
            self._tx(el, line)
            el.clear()
            parent.remove(el)

    def _tx(self, tx, line):
        where = f"transaction at line {line}"
        r = _Row(self.checks, where, line)
        g = self.grp
        amt_el = tx.find("IntrBkSttlmAmt")
        amount = r.amount(_t(tx, "IntrBkSttlmAmt"))
        ccy = amt_el.get("Ccy") if amt_el is not None else None
        ins_el = tx.find("InstdAmt")
        ok = _ccy_ok(self.checks, ccy, where, line)
        if amount is not None:
            self.n += 1
            self.sum += amount
            if amount <= 0:
                self.checks.fail("amount", f"{where}: the amount must be positive.", line=line)
                ok = False
        if ok and amount is not None:
            self.checks.ok("amount")
        self.emit({
            "file_name": self.path.name, "record_line": line,
            "message_id": _t(g, "MsgId"), "creation_time": r.ts(_t(g, "CreDtTm"), "CreDtTm"),
            "number_of_transactions": r.integer(_t(g, "NbOfTxs"), "NbOfTxs"),
            "settlement_method": _t(g, "SttlmInf/SttlmMtd"),
            "settlement_date": r.date(_t(tx, "IntrBkSttlmDt") or _t(g, "IntrBkSttlmDt"),
                                      "IntrBkSttlmDt"),
            "instruction_id": _t(tx, "PmtId/InstrId"), "end_to_end_id": _t(tx, "PmtId/EndToEndId"),
            "transaction_id": _t(tx, "PmtId/TxId"), "uetr": _t(tx, "PmtId/UETR"),
            "amount": amount, "currency": ccy,
            "instructed_amount": r.amount(_t(tx, "InstdAmt"), "InstdAmt"),
            "instructed_currency": ins_el.get("Ccy") if ins_el is not None else None,
            "charge_bearer": _t(tx, "ChrgBr"),
            "instructing_agent_bic": _bic(tx.find("InstgAgt")) or _bic(
                g.find("InstgAgt") if g is not None else None),
            "instructed_agent_bic": _bic(tx.find("InstdAgt")) or _bic(
                g.find("InstdAgt") if g is not None else None),
            "debtor_name": _name(tx.find("Dbtr")), "debtor_account": _acct(tx.find("DbtrAcct")),
            "debtor_agent_bic": _bic(tx.find("DbtrAgt")),
            "creditor_agent_bic": _bic(tx.find("CdtrAgt")),
            "creditor_name": _name(tx.find("Cdtr")), "creditor_account": _acct(tx.find("CdtrAcct")),
            "remittance_info": _ustrd(tx.find("RmtInf")),
        })

    def finish(self, pull):
        super().finish(pull)
        g = self.grp
        r = _Row(self.checks, "group header", self.grp_line)
        said = _t(g, "CtrlSum")
        name = "CtrlSum"
        if said is None and _t(g, "TtlIntrBkSttlmAmt") is not None:
            said, name = _t(g, "TtlIntrBkSttlmAmt"), "TtlIntrBkSttlmAmt"
        _ctrl(self.checks, name, "group header", self.grp_line,
              r.integer(_t(g, "NbOfTxs")), self.n, r.amount(said, name), self.sum)


HANDLERS = {"camt053": (_Camt, CAMT_COLUMNS), "camt054": (_Camt, CAMT_COLUMNS),
            "pain001": (_Pain, PAIN_COLUMNS), "pacs008": (_Pacs, PACS_COLUMNS)}


def read_iso(path: Path, options: dict, checks: Checks, chunk_rows: int, fmt: str):
    cls, columns = HANDLERS[fmt]
    yield from _run(path, options, checks, chunk_rows, columns, cls, fmt)
