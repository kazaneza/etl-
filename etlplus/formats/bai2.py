"""BAI2 cash management files: one row per 16 (transaction detail) record.

Records are comma separated and end with '/'. An 88 record continues the one before it.
Amounts have no decimal point and `implied_decimals` (default 2) implied decimals.
Totals and counts at 49, 98 and 99 are checked with running counters.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ._common import (Batcher, Checks, FormatError, col, iter_lines, pick_encoding, schema_of,
                      yymmdd)

BAI2_COLUMNS = (
    col("file_name", "string", help="The file the row came from"),
    col("record_line", "integer", help="Line of the 16 record"),
    col("sender_id", "string", help="01 record"),
    col("receiver_id", "string", help="01 record"),
    col("file_creation_date", "date", help="01 record"),
    col("file_id", "string", help="01 record"),
    col("originator_id", "string", help="02 record"),
    col("as_of_date", "date", help="02 record"),
    col("currency", "string(3)", help="03 record, else 02"),
    col("account", "string", True, "03 record"),
    col("opening_ledger", "decimal(18,3)", help="03 summary type 010"),
    col("closing_ledger", "decimal(18,3)", help="03 summary type 015"),
    col("type_code", "string", help="3 digits"),
    col("credit_debit", "string", help="C for 100-399, D for 400-699"),
    col("amount", "decimal(18,3)"),
    col("signed_amount", "decimal(18,3)"),
    col("funds_type", "string"),
    col("bank_reference", "string"),
    col("customer_reference", "string"),
    col("text", "string", help="16 text plus its 88 continuations"),
)


def _strip_end(f: str) -> str:
    f = f.strip()
    return f[:-1] if f.endswith("/") else f


def _funds_skip(fields: list[str], i: int) -> int:
    """Index after a funds type and its extra fields, starting at the funds type at i."""
    ft = fields[i].strip().upper() if i < len(fields) else ""
    i += 1
    if ft == "S":
        return i + 3
    if ft == "V":
        return i + 2
    if ft == "D":
        try:
            n = int(fields[i].strip() or 0)
        except (ValueError, IndexError):
            return i + 1
        return i + 1 + 2 * n
    return i


def _ascii_digits(t: str) -> bool:
    """0-9 only: str.isdigit() also takes superscripts such as the Latin-1 bytes B2, B3, B9,
    which Decimal refuses."""
    return bool(t) and t.isascii() and t.isdigit()


def _fields_16(text: str, conts: list[str]) -> tuple[list[str], list[str]]:
    """(fields, extra text parts) of a 16 record and its 88 continuations.

    An 88 may continue any field: until the text field has started, its fields are appended
    (the '/' that ends each physical record dropped), so a reference moved to the 88 stays a
    reference. Once the text has started, each 88 is more text, joined with a space later."""
    fields = _strip_end(text).split(",")
    extra = []
    for c in conts:
        i = _funds_skip(fields, 3)
        if len(fields) > i + 2:
            extra.append(c)
        else:
            fields.extend(_strip_end(c).split(","))
    return fields, extra


class _State:
    def __init__(self):
        self.header: dict = {}
        self.group: dict = {}
        self.account: dict = {}
        self.n01 = self.n99 = 0
        self.file_records = 0
        self.group_records = 0
        self.account_records = 0
        self.account_total = Decimal(0)
        self.group_total = Decimal(0)
        self.file_total = Decimal(0)
        self.group_accounts = 0
        self.file_groups = 0
        self.open_account_line = None
        self.open_group_line = None


def read_bai2(path: Path, options: dict, checks: Checks, chunk_rows: int, fmt: str = "bai2"):
    try:
        scale = int(options.get("implied_decimals", 2))
    except (TypeError, ValueError):
        raise FormatError("options.implied_decimals must be a whole number.") from None
    if not 0 <= scale <= 3:
        raise FormatError("options.implied_decimals must be between 0 and 3.")
    div = Decimal(10) ** scale
    batcher = Batcher(schema_of(BAI2_COLUMNS), chunk_rows)
    enc = pick_encoding(path, options.get("encoding"))
    st = _State()
    produced = False

    def amount(text, line, what):
        t = (text or "").strip()
        if not t:
            return None
        neg = t.startswith("-")
        digits = t.lstrip("+-")
        if not _ascii_digits(digits):
            checks.fail("values", f"{what} at line {line}: '{t}' is not an amount.", line=line)
            return None
        v = (Decimal(digits) / div).quantize(Decimal(1).scaleb(-3))
        return -v if neg else v

    def records():
        """(line, code, fields, continuations) with 88 lines folded in."""
        cur = None
        for n, raw in iter_lines(path, enc):
            s = raw.strip()
            if not s:
                continue
            code = s.split(",", 1)[0].strip()
            if code == "88":
                if cur is None:
                    checks.fail("structure", f"Line {n}: an 88 record continues nothing.", line=n)
                    continue
                cur[3].append(s.split(",", 1)[1] if "," in s else "")
                continue
            if cur is not None:
                yield cur
            cur = [n, code, s, []]
        if cur is not None:
            yield cur

    for line, code, text, conts in records():
        n_physical = 1 + len(conts)
        st.file_records += n_physical
        st.group_records += n_physical
        st.account_records += n_physical
        if code == "16":
            fields, conts = _fields_16(text, conts)
            # 16,type,amount,funds,[funds extras],bank_ref,cust_ref,text
            i = _funds_skip(fields, 3)
            type_code = fields[1].strip() if len(fields) > 1 else ""
            bank_ref = _strip_end(fields[i]) if i < len(fields) else ""
            cust_ref = _strip_end(fields[i + 1]) if i + 1 < len(fields) else ""
            txt = ",".join(fields[i + 2:]) if i + 2 < len(fields) else ""
            parts = [_strip_end(txt)] + [_strip_end(c) for c in conts]
            body = " ".join(p.strip() for p in parts if p.strip()) or None
            amt = amount(fields[2] if len(fields) > 2 else "", line, "16 amount")
            st.account_total += amt or 0
            cd = None
            if type_code.isdigit():
                tc = int(type_code)
                cd = "C" if 100 <= tc <= 399 else "D" if 400 <= tc <= 699 else None
            signed = None if amt is None else (amt if cd != "D" else -amt)
            if st.open_account_line is None:
                checks.fail("structure", f"Line {line}: a 16 record outside an account.",
                            line=line)
            row = {"file_name": path.name, "record_line": line, **st.header, **st.group,
                   **st.account, "type_code": type_code or None, "credit_debit": cd,
                   "amount": amt, "signed_amount": signed,
                   "funds_type": (fields[3].strip() or None) if len(fields) > 3 else None,
                   "bank_reference": bank_ref.strip() or None,
                   "customer_reference": cust_ref.strip() or None, "text": body}
            b = batcher.add(row)
            if b is not None:
                produced = True
                yield b
            continue

        allf = ",".join([text] + conts)
        allf = allf[:-1] if allf.endswith("/") else allf
        fields = [f.strip() for f in allf.split(",")]
        if fields and fields[-1].endswith("/"):
            fields[-1] = fields[-1][:-1]

        def date(v, what):
            try:
                return yymmdd(v) if v else None
            except ValueError:
                checks.fail("dates", f"{what} at line {line}: '{v}' is not a YYMMDD date.",
                            line=line)
                return None

        if code == "01":
            st.n01 += 1
            if st.n01 > 1:
                checks.fail("structure", f"Line {line}: a second 01 file header.", line=line)
            if st.file_records != 1:
                checks.fail("structure", f"Line {line}: the 01 file header is not first.",
                            line=line)
            st.header = {"sender_id": fields[1] if len(fields) > 1 else None,
                         "receiver_id": fields[2] if len(fields) > 2 else None,
                         "file_creation_date": date(fields[3] if len(fields) > 3 else "",
                                                    "01 creation date"),
                         "file_id": fields[5] if len(fields) > 5 else None}
        elif code == "02":
            if st.open_group_line is not None:
                checks.fail("structure", f"Line {line}: a 02 group header before the 98 of the "
                            f"group at line {st.open_group_line}.", line=line)
            st.open_group_line = line
            st.group_records = n_physical
            st.group_total = Decimal(0)
            st.group_accounts = 0
            st.group = {"originator_id": fields[2] if len(fields) > 2 else None,
                        "as_of_date": date(fields[4] if len(fields) > 4 else "", "02 as-of date"),
                        "currency": (fields[6] if len(fields) > 6 else "") or None}
            st.file_groups += 1
        elif code == "03":
            if st.open_account_line is not None:
                checks.fail("structure", f"Line {line}: a 03 account before the 49 of the account"
                            f" at line {st.open_account_line}.", line=line)
            st.open_account_line = line
            st.account_records = n_physical
            st.account_total = Decimal(0)
            st.group_accounts += 1
            acc = {"account": fields[1] if len(fields) > 1 else None,
                   "opening_ledger": None, "closing_ledger": None}
            ccy = fields[2] if len(fields) > 2 else ""
            acc["currency"] = ccy or st.group.get("currency")
            i = 3
            while i < len(fields) and fields[i]:
                tc = fields[i]
                amt = amount(fields[i + 1] if i + 1 < len(fields) else "", line, f"03 type {tc}")
                if amt is not None:
                    st.account_total += amt
                if tc == "010":
                    acc["opening_ledger"] = amt
                elif tc == "015":
                    acc["closing_ledger"] = amt
                # type, amount, item count, funds type (+ extras)
                i = _funds_skip(fields, i + 3) if i + 3 < len(fields) else len(fields)
            st.account = acc
        elif code == "49":
            if st.open_account_line is None:
                checks.fail("structure", f"Line {line}: a 49 without its 03.", line=line)
            said = amount(fields[1] if len(fields) > 1 else "", line, "49 control total")
            cnt = _int(fields, 2)
            where = f"account {st.account.get('account')} (49 at line {line})"
            _tally(checks, "account_trailer", where, line, said, st.account_total,
                   cnt, st.account_records)
            st.group_total += said if said is not None else st.account_total
            st.open_account_line = None
            st.account = {}
        elif code == "98":
            if st.open_account_line is not None:
                checks.fail("structure", f"Line {line}: the account at line "
                            f"{st.open_account_line} has no 49.", line=line)
                st.open_account_line = None
            said = amount(fields[1] if len(fields) > 1 else "", line, "98 control total")
            where = f"group at line {st.open_group_line} (98 at line {line})"
            _tally(checks, "group_trailer", where, line, said, st.group_total,
                   _int(fields, 3), st.group_records, _int(fields, 2), st.group_accounts)
            st.file_total += said if said is not None else st.group_total
            st.open_group_line = None
            st.group = {}
        elif code == "99":
            st.n99 += 1
            if st.open_group_line is not None:
                checks.fail("structure", f"Line {line}: the group at line {st.open_group_line} "
                            f"has no 98.", line=line)
            said = amount(fields[1] if len(fields) > 1 else "", line, "99 control total")
            _tally(checks, "file_trailer", f"file (99 at line {line})", line, said,
                   st.file_total, _int(fields, 3), st.file_records, _int(fields, 2),
                   st.file_groups, units="groups")
        else:
            checks.fail("structure", f"Line {line}: unknown record type '{code}'.", line=line)

    if st.open_account_line is not None:
        checks.fail("structure", f"The account at line {st.open_account_line} has no 49.",
                    line=st.open_account_line)
    if st.n01 != 1 or st.n99 != 1:
        checks.fail("structure", f"A BAI2 file has exactly one 01 and one 99; this one has "
                    f"{st.n01} and {st.n99}.")
    else:
        checks.ok("structure")
    b = batcher.flush()
    if b is not None:
        yield b
    elif not produced:
        yield batcher.empty()
    checks.count("rows", batcher.count)
    checks.count("groups", st.file_groups)


def _int(fields, i):
    try:
        v = fields[i].strip()
        return int(v) if v else None
    except (IndexError, ValueError):
        return None


def _tally(checks, name, where, line, said_total, total, said_records, records,
           said_n=None, n=None, units="accounts"):
    problems = []
    if said_total is not None and said_total != total:
        problems.append(f"the control total says {said_total}, the records add up to {total}")
    if said_records is not None and said_records != records:
        problems.append(f"the record count says {said_records}, there are {records}")
    if said_n is not None and n is not None and said_n != n:
        problems.append(f"the number of {units} says {said_n}, there are {n}")
    if problems:
        checks.fail(name, f"{where}: " + "; ".join(problems) + ".", line=line)
    else:
        checks.ok(name)
