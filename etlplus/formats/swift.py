"""SWIFT MT messages: MT940 / MT950 statements (one row per :61: line) and MT103 / MT202
payments (one row per message).

A file may hold many messages, either with blocks ({1:..}{2:..}{3:..}{4: .. -}) or as bare
field text. A field starts with ':NNa:' at the start of a line; its continuation lines are
joined. Statements are buffered one message at a time (to fill the closing balance on every
row); nothing else is held.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

from ._common import (Batcher, Checks, FormatError, col, fit, iter_lines, parse_amount,
                      pick_encoding, schema_of, yymmdd)

FIELD_RE = re.compile(r"^:(\d\d[A-Z]?):")
LINE61_RE = re.compile(
    r"^(\d{6})(\d{4})?(R?[CD])([A-Z])?([\d,]+)([A-Z]\w{3})((?:(?!//)[^\n]){0,16})(//[^\n]{0,16})?(\n.{0,34})?$")
BAL_RE = re.compile(r"^([CD])(\d{6})([A-Z]{3})([\d,]+)$")
B1_RE = re.compile(r"\{1:([^}]*)\}")
B2_RE = re.compile(r"\{2:([^}]*)\}")
B3_RE = re.compile(r"\{3:((?:\{[^}]*\})*)\}")
UETR_RE = re.compile(r"\{121:([0-9A-Fa-f-]{36})\}")

COMMON = [
    col("file_name", "string", help="The file the row came from"),
    col("record_line", "integer", help="Line of the record in the file"),
    col("message_index", "integer", help="Position of the message in the file, from 1"),
]

MT940_COLUMNS = tuple(COMMON + [
    col("message_type", "string", help="940 or 950"),
    col("sender_bic", "string", help="Block 1 or 2; null in bare files"),
    col("receiver_bic", "string", help="Block 1 or 2; null in bare files"),
    col("transaction_reference", "string", help=":20:"),
    col("related_reference", "string", help=":21:"),
    col("account", "string", True, ":25:"),
    col("statement_number", "integer", help=":28C: before the slash"),
    col("sequence_number", "integer", help=":28C: after the slash"),
    col("opening_balance_type", "string", help="F (first) or M (intermediate)"),
    col("opening_balance_date", "date", help=":60a:"),
    col("currency", "string(3)", help=":60a:"),
    col("opening_balance", "decimal(18,3)", help="Signed; D is negative"),
    col("line_number", "integer", help="1..n within the statement"),
    col("value_date", "date", help=":61:"),
    col("entry_date", "date", help=":61: MMDD; the year follows the value date"),
    col("mark", "string", help="C, D, RC or RD"),
    col("funds_code", "string"),
    col("amount", "decimal(18,3)", help="Unsigned"),
    col("signed_amount", "decimal(18,3)", help="C and RD positive; D and RC negative"),
    col("transaction_type", "string", help="For example NTRF, NMSC, FCHG"),
    col("customer_reference", "string"),
    col("bank_reference", "string"),
    col("supplementary_details", "string"),
    col("info", "string", True, ":86:, lines joined with a space"),
    col("closing_balance_date", "date", help=":62a:"),
    col("closing_balance", "decimal(18,3)", help=":62a:, signed"),
    col("closing_available", "decimal(18,3)", help=":64:, signed"),
])

MT103_COLUMNS = tuple(COMMON + [
    col("sender_bic", "string"), col("receiver_bic", "string"),
    col("uetr", "string", help="Block 3 {121:}"),
    col("transaction_reference", "string", help=":20:"),
    col("bank_operation_code", "string", help=":23B:"),
    col("value_date", "date", help=":32A:"),
    col("currency", "string(3)", help=":32A:"),
    col("amount", "decimal(18,3)", help=":32A:"),
    col("instructed_currency", "string(3)", help=":33B:"),
    col("instructed_amount", "decimal(18,3)", help=":33B:"),
    col("ordering_customer_account", "string", True, ":50a: first line starting with /"),
    col("ordering_customer", "string", True, ":50a:"),
    col("ordering_institution", "string", help=":52a:"),
    col("sender_correspondent", "string", help=":53a:"),
    col("receiver_correspondent", "string", help=":54a:"),
    col("intermediary", "string", help=":56a:"),
    col("account_with_institution", "string", help=":57a:"),
    col("beneficiary_account", "string", True, ":59a: first line starting with /"),
    col("beneficiary", "string", True, ":59a:"),
    col("remittance_info", "string", True, ":70:"),
    col("charges", "string", help=":71A: OUR, SHA or BEN"),
    col("sender_charges", "string", help=":71F: joined as 'CCY amount; ...'"),
    col("receiver_charges", "string", help=":71G:"),
    col("sender_to_receiver_info", "string", help=":72:"),
])

MT202_COLUMNS = tuple(COMMON + [
    col("sender_bic", "string"), col("receiver_bic", "string"),
    col("uetr", "string", help="Block 3 {121:}"),
    col("transaction_reference", "string", help=":20:"),
    col("related_reference", "string", help=":21:"),
    col("value_date", "date", help=":32A:"),
    col("currency", "string(3)", help=":32A:"),
    col("amount", "decimal(18,3)", help=":32A:"),
    col("ordering_institution", "string", help=":52a:"),
    col("sender_correspondent", "string", help=":53a:"),
    col("receiver_correspondent", "string", help=":54a:"),
    col("intermediary", "string", help=":56a:"),
    col("account_with_institution", "string", help=":57a:"),
    col("beneficiary_institution", "string", help=":58a:"),
    col("sender_to_receiver_info", "string", help=":72:"),
])


# --------------------------------------------------------------------------- messages

class Message:
    __slots__ = ("index", "line", "blocks", "fields", "has_blocks")

    def __init__(self, index: int, line: int, header: str = ""):
        self.index = index
        self.line = line
        self.fields: list[list] = []          # [tag, value, line]
        self.blocks: dict[str, str] = {}
        self.has_blocks = bool(header)
        if header:
            for key, rx in (("1", B1_RE), ("2", B2_RE), ("3", B3_RE)):
                m = rx.search(header)
                if m:
                    self.blocks[key] = m.group(1)

    def has(self, tag: str) -> bool:
        return any(f[0] == tag for f in self.fields)

    def first(self, *tags) -> tuple | None:
        """The first field whose tag is one of `tags` (a tag ending in 'a' means any letter)."""
        for f in self.fields:
            for t in tags:
                if f[0] == t or (t.endswith("a") and f[0][:2] == t[:2]):
                    return f
        return None

    def value(self, *tags) -> str | None:
        f = self.first(*tags)
        return f[1] if f else None

    @property
    def mt(self) -> str | None:
        b2 = self.blocks.get("2") or ""
        m = re.match(r"^[IO](\d{3})", b2)
        return m.group(1) if m else None

    def bics(self) -> tuple[str | None, str | None]:
        """(sender, receiver) as BIC11 from the 12-character logical terminal addresses."""
        b1 = self.blocks.get("1") or ""
        b2 = self.blocks.get("2") or ""
        lt1 = _bic(b1[3:15]) if len(b1) >= 15 else None
        if b2.startswith("I"):
            return lt1, _bic(b2[4:16]) if len(b2) >= 16 else None
        if b2.startswith("O"):
            # O + MT(3) + input time(4) + MIR: date(6) + LT address(12) + session + ISN ...
            return (_bic(b2[14:26]) if len(b2) >= 26 else None), lt1
        return None, None


def _bic(lt: str) -> str | None:
    lt = (lt or "").strip()
    if len(lt) == 12:
        return lt[:8] + lt[9:12]
    return lt or None


def iter_messages(lines):
    """Split numbered lines into Message objects, as they complete."""
    cur: Message | None = None
    in_block4 = False
    header = ""
    header_line = 0
    index = 0
    field = None

    def start(line_no, hdr):
        nonlocal index
        index += 1
        return Message(index, line_no, hdr)

    for n, raw in lines:
        s = raw
        if not in_block4:
            if header or s.lstrip().startswith("{1:") or s.lstrip().startswith("{2:"):
                if not header:
                    if cur is not None and cur.fields:
                        yield cur
                    cur = None
                    header_line = n
                header += s.strip()
                if "{4:" not in header:
                    continue
                before, _, after = header.partition("{4:")
                cur = start(header_line, before)
                header = ""
                field = None
                in_block4 = True
                s = after
                if not s.strip():
                    continue
        else:
            if s.startswith("-}"):
                in_block4 = False
                field = None
                rest = s[2:].strip()
                if cur is not None:
                    yield cur
                cur = None
                if "{1:" in rest or "{2:" in rest:
                    k = min(i for i in (rest.find("{1:"), rest.find("{2:")) if i >= 0)
                    rest = rest[k:]
                    header_line = n
                    if "{4:" in rest:
                        before, _, after = rest.partition("{4:")
                        cur = start(header_line, before)
                        in_block4 = True
                        if after.strip():
                            s = after
                        else:
                            continue
                    else:
                        header = rest
                        continue
                else:
                    continue
        if not in_block4 and s.strip() == "-":
            if cur is not None and cur.fields:
                yield cur
            cur = None
            field = None
            continue
        m = FIELD_RE.match(s)
        if m:
            tag = m.group(1)
            if cur is None:
                cur = start(n, "")
            elif not cur.has_blocks and tag == "20" and cur.has("20"):
                yield cur
                cur = start(n, "")
            field = [tag, s[m.end():], n]
            cur.fields.append(field)
        elif field is not None and s.strip():
            field[1] += "\n" + s
    if header:
        raise FormatError("The file ends inside a message header.", line=header_line)
    if cur is not None and cur.fields:
        yield cur


# --------------------------------------------------------------------------- MT940 / MT950

def _balance(text: str | None):
    """':60F:' style value -> (date, currency, signed amount)."""
    if text is None:
        return None
    m = BAL_RE.match(text.strip().replace("\n", ""))
    if not m:
        raise ValueError(f"'{text.strip()}' is not a balance (mark, YYMMDD, currency, amount)")
    amount = fit(parse_amount(m.group(4)))
    if m.group(1) == "D":
        amount = -amount
    return yymmdd(m.group(2)), m.group(3), amount


def _entry_date(value_date: dt.date, mmdd: str) -> dt.date:
    month, day = int(mmdd[:2]), int(mmdd[2:])
    best = None
    for year in (value_date.year, value_date.year - 1, value_date.year + 1):
        try:
            d = dt.date(year, month, day)
        except ValueError:
            continue
        if year == value_date.year and abs((d - value_date).days) <= 183:
            return d
        if best is None or abs((d - value_date).days) < abs((best - value_date).days):
            best = d
    if best is None:
        raise ValueError(f"'{mmdd}' is not a valid MMDD entry date")
    return best


def read_mt940(path: Path, options: dict, checks: Checks, chunk_rows: int, fmt: str = "mt940"):
    batcher = Batcher(schema_of(MT940_COLUMNS), chunk_rows)
    enc = pick_encoding(path, options.get("encoding"))
    seen_statements: dict[tuple, int] = {}
    any_batch = False
    for msg in iter_messages(iter_lines(path, enc)):
        checks.count("messages")
        rows = _mt940_rows(path, msg, checks, fmt, seen_statements)
        for r in rows:
            b = batcher.add(r)
            if b is not None:
                any_batch = True
                yield b
    b = batcher.flush()
    if b is not None:
        yield b
    elif not any_batch:
        yield batcher.empty()
    checks.count("rows", batcher.count)


def _mt940_rows(path, msg: Message, checks: Checks, fmt: str, seen: dict) -> list[dict]:
    where = f"statement {msg.index} (line {msg.line})"
    sender, receiver = msg.bics()
    mt = msg.mt or fmt[2:]
    for tag in ("20", "25", "60a", "62a"):
        if msg.first(tag) is None:
            checks.fail("mandatory", f"{where}: field :{tag.replace('a', 'F')}: is missing.",
                        line=msg.line, record=tag)
    num = seq = None
    stmt_no = msg.value("28C", "28")
    if stmt_no:
        m = re.match(r"^\s*(\d+)(?:/(\d+))?", stmt_no)
        if m:
            num = int(m.group(1))
            seq = int(m.group(2)) if m.group(2) else None
    account = (msg.value("25", "25P") or "").strip().split("\n")[0] or None

    def bal(tags, name):
        f = msg.first(*tags)
        if f is None:
            return None, None
        try:
            return _balance(f[1]), f[0]
        except ValueError as e:
            checks.fail("dates" if "date" in str(e).lower() or "month" in str(e).lower()
                        else "balance_field", f"{where}: :{f[0]}: {e}.", line=f[2], record=f[0])
            return None, f[0]

    opening, otag = bal(("60F", "60M"), "opening")
    closing, _ = bal(("62F", "62M"), "closing")
    avail, _ = bal(("64",), "available")
    currency = opening[1] if opening else (closing[1] if closing else None)

    # currency: every balance carries the statement's currency
    curr_ok = True
    for b in (closing, avail):
        if b and currency and b[1] != currency:
            curr_ok = False
            checks.fail("currency", f"{where}: balance in {b[1]}, statement in {currency}.",
                        line=msg.line)
    if curr_ok:
        checks.ok("currency")

    key = (account, num, seq)
    if num is not None:
        if key in seen:
            checks.fail("sequence", f"{where}: statement number {num}"
                        + (f"/{seq}" if seq is not None else "")
                        + f" repeats (first at line {seen[key]}).", line=msg.line, level="warn")
        else:
            seen[key] = msg.line
            checks.ok("sequence")

    rows = []
    total = Decimal(0)
    dates_ok = True
    fields = msg.fields
    for i, (tag, value, line) in enumerate(fields):
        if tag != "61":
            continue
        # the whole field must parse: text that does not fit fails the line check, never dropped
        m = LINE61_RE.match(value.rstrip())
        if not m:
            checks.fail("line", f"{where}: :61: at line {line} does not parse.", line=line,
                        record="61")
            continue
        vd_s, ed_s, mark, funds, amt_s, ttype, cref, bref, supp = m.groups()
        value_date = entry_date = None
        try:
            value_date = yymmdd(vd_s)
            if ed_s:
                entry_date = _entry_date(value_date, ed_s)
        except ValueError as e:
            dates_ok = False
            checks.fail("dates", f"{where}: :61: at line {line}: {e}.", line=line, record="61")
        try:
            amount = fit(parse_amount(amt_s))
        except ValueError as e:
            checks.fail("line", f"{where}: :61: at line {line}: {e}.", line=line, record="61")
            amount = None
        signed = None
        if amount is not None:
            signed = amount if mark in ("C", "RD") else -amount
            total += signed
        info = None
        if fmt != "mt950" and mt != "950":
            nxt = fields[i + 1] if i + 1 < len(fields) else None
            if nxt and nxt[0] == "86":
                info = " ".join(p.strip() for p in nxt[1].split("\n") if p.strip()) or None
        rows.append({
            "record_line": line,
            "line_number": len(rows) + 1,
            "value_date": value_date, "entry_date": entry_date,
            "mark": mark, "funds_code": funds, "amount": amount, "signed_amount": signed,
            "transaction_type": ttype,
            "customer_reference": (cref or "").strip() or None,
            "bank_reference": (bref or "")[2:].strip() or None,
            "supplementary_details": (supp or "").strip() or None,
            "info": info,
        })
    if dates_ok:
        checks.ok("dates")
    if opening and closing:
        expected = opening[2] + total
        if expected != closing[2]:
            checks.fail("balance", f"{where}, account {account}: opening {opening[2]} + lines "
                        f"{total} = {expected}, but the closing balance is {closing[2]}.",
                        line=msg.line, record="62")
        else:
            checks.ok("balance")
    checks.count("statements")
    common = {
        "file_name": path.name, "message_index": msg.index, "message_type": mt,
        "sender_bic": sender, "receiver_bic": receiver,
        "transaction_reference": (msg.value("20") or "").strip() or None,
        "related_reference": (msg.value("21") or "").strip() or None,
        "account": account, "statement_number": num, "sequence_number": seq,
        "opening_balance_type": otag[2] if otag else None,
        "opening_balance_date": opening[0] if opening else None,
        "currency": currency,
        "opening_balance": opening[2] if opening else None,
        "closing_balance_date": closing[0] if closing else None,
        "closing_balance": closing[2] if closing else None,
        "closing_available": avail[2] if avail else None,
    }
    for r in rows:
        r.update(common)
    return rows


# --------------------------------------------------------------------------- MT103 / MT202

MANDATORY = {"103": ("20", "23B", "32A", "50a", "59a", "71A"),
             "202": ("20", "21", "32A", "58a")}


def _party(text: str | None) -> tuple[str | None, str | None]:
    """(account, rest): the first line starting with '/' is the account; the rest joined."""
    if text is None:
        return None, None
    lines = [x.strip() for x in text.split("\n") if x.strip()]
    account = None
    rest = []
    for x in lines:
        if account is None and x.startswith("/") and not rest:
            account = x.lstrip("/").strip() or None
        else:
            rest.append(x)
    return account, " ".join(rest) or None


def _join(text: str | None) -> str | None:
    if text is None:
        return None
    return " ".join(x.strip() for x in text.split("\n") if x.strip()) or None


def _ccy_amount(text: str):
    m = re.match(r"^([A-Za-z]{3})([\d,]+)$", text.strip())
    if not m:
        raise ValueError(f"'{text.strip()}' is not a currency and amount")
    return m.group(1), fit(parse_amount(m.group(2)))


def read_payment(path: Path, options: dict, checks: Checks, chunk_rows: int, fmt: str):
    mt = fmt[2:]
    columns = MT103_COLUMNS if mt == "103" else MT202_COLUMNS
    batcher = Batcher(schema_of(columns), chunk_rows)
    enc = pick_encoding(path, options.get("encoding"))
    any_batch = False
    for msg in iter_messages(iter_lines(path, enc)):
        checks.count("messages")
        row = _payment_row(path, msg, checks, mt)
        b = batcher.add(row)
        if b is not None:
            any_batch = True
            yield b
    b = batcher.flush()
    if b is not None:
        yield b
    elif not any_batch:
        yield batcher.empty()
    checks.count("rows", batcher.count)


def _payment_row(path, msg: Message, checks: Checks, mt: str) -> dict:
    where = f"message {msg.index} (line {msg.line})"
    missing = [t for t in MANDATORY[mt] if msg.first(t) is None]
    if missing:
        checks.fail("mandatory", f"{where}: missing "
                    + ", ".join(f":{t}:" for t in missing) + ".",
                    line=msg.line)
    else:
        checks.ok("mandatory")
    sender, receiver = msg.bics()
    uetr = None
    m = UETR_RE.search(msg.blocks.get("3") or "")
    if m:
        uetr = m.group(1).lower()
    row = {"file_name": path.name, "record_line": msg.line, "message_index": msg.index,
           "sender_bic": sender, "receiver_bic": receiver, "uetr": uetr,
           "transaction_reference": (msg.value("20") or "").strip() or None}

    f32 = msg.first("32A")
    if f32:
        v = f32[1].strip()
        mm = re.match(r"^(\d{6})([A-Za-z]{3})([\d,]+)$", v)
        if not mm:
            checks.fail("amount", f"{where}: :32A: '{v}' does not parse.", line=f32[2])
        else:
            try:
                row["value_date"] = yymmdd(mm.group(1))
                checks.ok("date")
            except ValueError as e:
                checks.fail("date", f"{where}: :32A: {e}.", line=f32[2])
            ccy = mm.group(2)
            row["currency"] = ccy
            if re.fullmatch(r"[A-Z]{3}", ccy):
                checks.ok("currency")
            else:
                checks.fail("currency", f"{where}: currency '{ccy}' is not 3 capital letters.",
                            line=f32[2])
            try:
                amt = fit(parse_amount(mm.group(3)))
                row["amount"] = amt
                if amt is None or amt <= 0:
                    checks.fail("amount", f"{where}: the amount must be positive.", line=f32[2])
                else:
                    checks.ok("amount")
            except ValueError as e:
                checks.fail("amount", f"{where}: {e}.", line=f32[2])

    for key, tag in (("ordering_institution", "52a"), ("sender_correspondent", "53a"),
                     ("receiver_correspondent", "54a"), ("intermediary", "56a"),
                     ("account_with_institution", "57a"),
                     ("sender_to_receiver_info", "72")):
        row[key] = _join(msg.value(tag))

    if mt == "202":
        row["related_reference"] = (msg.value("21") or "").strip() or None
        row["beneficiary_institution"] = _join(msg.value("58a"))
        return row

    row["bank_operation_code"] = (msg.value("23B") or "").strip() or None
    f33 = msg.first("33B")
    if f33:
        try:
            row["instructed_currency"], row["instructed_amount"] = _ccy_amount(f33[1])
        except ValueError as e:
            checks.fail("amount", f"{where}: :33B: {e}.", line=f33[2])
    row["ordering_customer_account"], row["ordering_customer"] = _party(msg.value("50a"))
    f59 = msg.first("59", "59a")
    row["beneficiary_account"], row["beneficiary"] = _party(f59[1] if f59 else None)
    row["remittance_info"] = _join(msg.value("70"))
    row["charges"] = (msg.value("71A") or "").strip() or None
    sc = []
    for tag, value, line in msg.fields:
        if tag == "71F":
            try:
                c, a = _ccy_amount(value)
                sc.append(f"{c} {a}")
            except ValueError as e:
                checks.fail("amount", f"{where}: :71F: {e}.", line=line)
    row["sender_charges"] = "; ".join(sc) or None
    f71g = msg.first("71G")
    if f71g:
        try:
            c, a = _ccy_amount(f71g[1])
            row["receiver_charges"] = f"{c} {a}"
        except ValueError as e:
            checks.fail("amount", f"{where}: :71G: {e}.", line=f71g[2])
    return row
