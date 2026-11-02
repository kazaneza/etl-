"""Fixes from the v2 review: each test failed on the code before its fix."""
import datetime as dt
import decimal
import http.server
import json
import re
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from etlplus import api, config, ddl, guard, mapping, projects, remote, secrets, sources, store, transform
from etlplus.drivers import mongo, odbc
from etlplus.formats import check as fcheck

CONSOLE = Path(__file__).resolve().parent.parent / "etlplus" / "console"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("ETLPLUS_TOKEN", "ETLPLUS_START", "ETLPLUS_HOME", "ETLPLUS_PROJECT", "ETLPLUS_ALLOWED_HOSTS",
              "ETLPLUS_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def home(tmp_path):
    h = projects.Home.open(tmp_path / "home")
    h.choose("real", run_jobs=False)
    return h


@pytest.fixture
def client(home):
    return TestClient(api.create_app(home=home, start_scheduler=False), base_url="http://localhost")


# ---------------------------------------------------------------- 1. ODBC and Mongo secrets

ODBC_CS = ("DRIVER={ODBC Driver 18 for SQL Server};SERVER=db.example;UID=svc;PWD=pw-AAA;"
           "KeyStoreSecret=ks-BBB;AuthToken=tok-CCC;PRIV_KEY_FILE_PWD=pk-DDD;Encrypt=yes;")


def test_odbc_every_secret_part_is_taken_out():
    for name in ("PWD", "KeyStoreSecret", "AuthToken", "PRIV_KEY_FILE_PWD", "AccountKey", "ApiKey",
                 "SAS", "ClientSecret", "Passphrase"):
        assert odbc.is_secret_part(name), name
    for name in ("DRIVER", "SERVER", "UID", "Encrypt", "KeyStoreAuthentication", "KeyStorePrincipalId",
                 "Database", "TrustServerCertificate"):
        assert not odbc.is_secret_part(name), name
    kept, secret = odbc.separate("SERVER=db.example;UID=svc;PWD=pw-AAA;KeyStoreSecret=ks-BBB;"
                                 "AuthToken=tok-CCC;PRIV_KEY_FILE_PWD=pk-DDD;")
    for v in ("pw-AAA", "ks-BBB", "tok-CCC", "pk-DDD"):
        assert v not in kept
    assert set(secret.values()) == {"pw-AAA", "ks-BBB", "tok-CCC", "pk-DDD"}


def test_odbc_secret_parts_never_reach_yaml_or_responses(client, home, monkeypatch):
    from etlplus.drivers import mssql
    monkeypatch.setattr(mssql, "check_driver_name", lambda v: None)
    monkeypatch.setattr(odbc, "check_driver_name", lambda v: None)
    r = client.put("/api/connections/o", json={"kind": "odbc", "connection_string": ODBC_CS})
    assert r.status_code == 200, r.text
    texts = [r.text, client.get("/api/connections/o").text, client.get("/api/connections").text,
             (Path(home.active().path) / "connections.yaml").read_text(encoding="utf-8")]
    for t in texts:
        for v in ("pw-AAA", "ks-BBB", "tok-CCC", "pk-DDD"):
            assert v not in t
    built = odbc.build(dict(config.Project.load(home.active().path).connections["o"],
                            _secrets=secrets.get(home.active(), "o")), None)
    for v in ("pw-AAA", "ks-BBB", "tok-CCC", "pk-DDD"):
        assert v in built


def test_mongo_uri_secret_options_are_taken_out():
    uri = ("mongodb://u:pw@h/x?tlsCertificateKeyFilePassword=kp-BBB&"
           "authMechanismProperties=AWS_SESSION_TOKEN:tok-CCC&replicaSet=rs0")
    clean, user, pw = mongo.separate_uri(uri)
    assert user == "u" and pw == "pw"
    assert "kp-BBB" not in clean and "tok-CCC" not in clean and "pw" not in clean.split("@")[0]
    assert "replicaSet=rs0" in clean
    kept, opts = mongo.separate_options(clean if "?" in clean else uri)
    assert "kp-BBB" not in kept


def test_mongo_secret_options_go_to_the_store_and_back(client, home):
    uri = ("mongodb://u:pw-X@h.example/x?tlsCertificateKeyFilePassword=kp-BBB&"
           "authMechanismProperties=AWS_SESSION_TOKEN:tok-CCC&replicaSet=rs0")
    r = client.put("/api/connections/m", json={"kind": "mongodb", "uri": uri, "database": "x"})
    assert r.status_code == 200, r.text
    yaml_text = (Path(home.active().path) / "connections.yaml").read_text(encoding="utf-8")
    for t in (r.text, client.get("/api/connections/m").text, yaml_text):
        for v in ("pw-X", "kp-BBB", "tok-CCC"):
            assert v not in t
    stored = secrets.get(home.active(), "m")
    spec = dict(config.Project.load(home.active().path).connections["m"], _secrets=stored)
    host, kw = mongo.client_args(spec, stored.get("password"))
    assert "kp-BBB" in host and "tok-CCC" in host and kw["password"] == "pw-X"


# ---------------------------------------------------------------- 2. webhook headers

def test_webhook_headers_are_write_only(client, home):
    body = {"notify": {"on": "failure", "webhook": {"url": "https://hooks.example.invalid/x",
                                                     "headers": {"Authorization": "Bearer tok-ZZZ",
                                                                 "X-Api-Key": "ak-YYY"}}}}
    r = client.put("/api/settings", json=body)
    assert r.status_code == 200, r.text
    g = client.get("/api/settings")
    disk = (Path(home.active().path) / "etlplus.yaml").read_text(encoding="utf-8")
    for t in (r.text, g.text, disk):
        assert "tok-ZZZ" not in t and "ak-YYY" not in t
    hook = g.json()["settings"]["notify"]["webhook"]
    assert hook["url"] == "https://hooks.example.invalid/x"
    assert sorted(hook["headers_set"]) == ["Authorization", "X-Api-Key"]
    from etlplus import notify
    p = config.Project.load(home.active().path)
    url, headers = notify._webhook(notify.settings(p).get("webhook"), p)
    assert headers == {"Authorization": "Bearer tok-ZZZ", "X-Api-Key": "ak-YYY"}
    # saving again without values keeps them; null removes one
    client.put("/api/settings", json={"notify": {"webhook": {"url": "https://hooks.example.invalid/x",
                                                              "headers": {"X-Api-Key": None}}}})
    p = config.Project.load(home.active().path)
    assert notify._webhook(notify.settings(p).get("webhook"), p)[1] == {"Authorization": "Bearer tok-ZZZ"}


def test_secret_key_rules_cover_auth_headers():
    for k in ("authorization", "Authorization", "x-api-key", "x-auth-token", "proxy-authorization"):
        assert config.is_secret_key(k), k


# ---------------------------------------------------------------- 3. HTTP pickup without Content-Length

class _NoLen(http.server.BaseHTTPRequestHandler):
    body = b"id,v\n1,a\n2,b\n"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Last-Modified", "Sat, 31 Oct 2026 10:00:00 GMT")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Last-Modified", "Sat, 31 Oct 2026 10:00:00 GMT")
        self.end_headers()
        self.wfile.write(self.body)


def test_http_pickup_skips_a_seen_file_without_content_length(tmp_path):
    proj = config.Project.init(tmp_path / "proj")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _NoLen)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/daily.csv"
        proj.save_connection("web", {"kind": "http", "url": url, "options": {"allow_private": True}})
        spec = proj.connections["web"]
        got = []
        found, _ = sources.local_files(proj, "web", spec, "*", fetch_dir=tmp_path / "r1", files_out=got,
                                       pickup={"min_age_seconds": 0}, job="j")
        assert len(found) == 1
        store.mark_files(proj, "j", "run1", "web", got)
        found2, _ = sources.local_files(proj, "web", spec, "*", fetch_dir=tmp_path / "r2", files_out=[],
                                        pickup={"min_age_seconds": 0}, job="j")
        assert found2 == []
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- 4. dblink

@pytest.mark.parametrize("sql", [
    "SELECT dblink_connect('c','dbname=app'), dblink_send_query('c','DELETE FROM accounts')",
    "SELECT * FROM dblink_get_result('c') AS t(x text)",
    "SELECT dblink_open('c','cur','SELECT 1')",
    "SELECT postgres_fdw_disconnect_all()",
])
def test_guard_refuses_every_dblink_call(sql):
    with pytest.raises(guard.GuardError):
        guard.check_select(sql, "postgres")


# ---------------------------------------------------------------- 5. MT940 references with '/'

MT940 = ("{1:F01DEMOBANKAXXX0000000000}{2:O9401200261001DEMOBANKXXXX00000000002610011200N}{4:\n"
         ":20:STMT1\n:25:DEMO-001\n:28C:1/1\n:60F:C261001EUR100,00\n"
         ":61:2610011001C50,00NTRFINV/26/0001//BREF1\n:86:Demo\n"
         ":62F:C261001EUR150,00\n-}\n")


def test_mt940_reference_with_slash(tmp_path):
    from etlplus.formats import read
    f = tmp_path / "s.mt940"
    f.write_text(MT940, encoding="utf-8")
    rows = [r for b in read("mt940", f, {}) for r in b.to_pylist()]
    assert rows[0]["customer_reference"] == "INV/26/0001" and rows[0]["bank_reference"] == "BREF1"
    assert fcheck("mt940", f)["ok"] is True
    bad = tmp_path / "bad.mt940"
    bad.write_text(MT940.replace("//BREF1", "//BREF1 TOO LONG FOR THE FIELD XX"), encoding="utf-8")
    assert fcheck("mt940", bad)["ok"] is False


# ---------------------------------------------------------------- 6. BAI2 16 continued before the text

BAI2 = ("01,DEMOSEND,DEMORECV,261001,1200,1,,,2/\n02,DEMORECV,DEMOSEND,1,261001,,USD,2/\n"
        "03,DEMO1,USD,010,10000,,/\n16,195,5000,Z,BREF1/\n88,CREF1,Demo text/\n"
        "49,15000,4/\n98,15000,1,6/\n99,15000,1,8/\n")


def test_bai2_16_continued_before_the_text(tmp_path):
    from etlplus.formats import read
    f = tmp_path / "a.bai"
    f.write_text(BAI2, encoding="utf-8")
    rows = [r for b in read("bai2", f, {}) for r in b.to_pylist()]
    assert rows[0]["bank_reference"] == "BREF1"
    assert rows[0]["customer_reference"] == "CREF1"
    assert rows[0]["text"] == "Demo text"
    # text started on the 16 line still joins its 88 parts with a space
    g = tmp_path / "b.bai"
    g.write_text(BAI2.replace("16,195,5000,Z,BREF1/\n88,CREF1,Demo text/",
                              "16,195,5000,Z,BREF1,CREF1,Demo\n88,text/"), encoding="utf-8")
    rows = [r for b in read("bai2", g, {}) for r in b.to_pylist()]
    assert (rows[0]["customer_reference"], rows[0]["text"]) == ("CREF1", "Demo text")


# ---------------------------------------------------------------- 7. exists_sql folds like quote_ident

def test_exists_sql_uses_the_stored_name():
    assert ddl.exists_sql("oracle", "MyTable")[1] == (None, "MyTable")
    assert ddl.exists_sql("oracle", "mytable")[1] == (None, "MYTABLE")
    assert ddl.exists_sql("oracle", 'sales."Daily Totals"')[1] == ("SALES", "Daily Totals")
    assert ddl.exists_sql("db2", "MyTable")[1] == (None, "MyTable")
    assert ddl.exists_sql("snowflake", "MyTable")[1] == ("MyTable", None)
    assert ddl.exists_sql("postgres", '"ORDERS"')[1] == ("ORDERS", None)
    assert ddl.exists_sql("postgres", "ORDERS")[1] == ("orders", None)
    for kind in ("oracle", "db2", "snowflake", "hana", "firebird", "postgres"):
        for t in ("MyTable", "orders", "ORDERS", '"Mixed Case"'):
            created = ddl.quote_table(kind, t).split(".")[-1].strip('"')
            params = ddl.exists_sql(kind, t)[1]
            vals = params if isinstance(params, tuple) else tuple(params.values())
            assert created in vals, (kind, t, created, vals)


# ---------------------------------------------------------------- 8. mapping warns before rounding

def test_mapping_warns_on_a_smaller_decimal_scale():
    job = {"mapping": {"columns": {"amt": {"from": "amount"}, "n": {"from": "big"}}}}
    src = [{"name": "amount", "type": "DECIMAL(18,3)"}, {"name": "big", "type": "BIGINT"}]
    dest = [{"name": "amt", "type": "decimal(18,2)"}, {"name": "n", "type": "integer"}]
    msgs = {(p["column"], p["level"]): p["message"] for p in mapping.validate(job, src, dest)}
    assert ("amt", "warn") in msgs and "3" in msgs[("amt", "warn")]
    assert ("n", "warn") in msgs
    dest2 = [{"name": "amt", "type": "decimal(10,3)"}, {"name": "n", "type": "bigint"}]
    msgs2 = {(p["column"], p["level"]): p["message"] for p in mapping.validate(job, src, dest2)}
    assert ("amt", "warn") in msgs2 and "whole" in msgs2[("amt", "warn")]
    dest3 = [{"name": "amt", "type": "decimal(20,3)"}, {"name": "n", "type": "bigint"}]
    assert mapping.validate(job, src, dest3) == []


# ---------------------------------------------------------------- 9. check_expr and comments

def test_check_expr_comment_before_paren():
    for e in ("current_setting/**/('allowed_paths')", "getvariable/**/('x')", "current_setting -- c\n('x')"):
        with pytest.raises(ValueError):
            transform.check_expr(e, ["a"])
    transform.check_expr("upper/**/(a)", ["a"])


# ---------------------------------------------------------------- 10. superscript digits

def test_superscript_digits_fail_a_check_not_crash(tmp_path):
    f = tmp_path / "s.bai"
    f.write_bytes(BAI2.replace("03,DEMO1,USD,010,10000", "03,DEMO1,USD,010,1\xb200").encode("latin-1"))
    res = fcheck("bai2", f)
    assert res["ok"] is False
    lay = {"layout": 1, "name": "sup", "label": "Sup", "line_length": 8,
           "records": [{"type": "detail", "output": True,
                        "fields": [{"name": "amt", "start": 1, "length": 8, "type": "decimal",
                                    "scale": 2, "implied_decimal": True}]}]}
    g = tmp_path / "f.txt"
    g.write_bytes("00001\xb200\n".encode("latin-1"))
    res = fcheck("fixed_layout", g, {"layout": lay, "encoding": "latin-1"})
    assert res["ok"] is False


# ---------------------------------------------------------------- 11. capped decimal keeps whole digits

def test_capped_decimal_keeps_whole_digits():
    assert ddl.generic_to_duck("decimal(50,40)") == "DECIMAL(38,28)"
    assert ddl.native_type("sqlserver", "decimal(50,40)") == "decimal(38,28)"
    assert ddl.generic_to_duck("decimal(60,2)") == "DECIMAL(38,0)"
    assert ddl.generic_to_duck("decimal(18,3)") == "DECIMAL(18,3)"


# ---------------------------------------------------------------- 12. camt transaction amounts

CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt>
<GrpHdr><MsgId>M1</MsgId><CreDtTm>2026-10-01T10:00:00</CreDtTm></GrpHdr>
<Stmt><Id>S1</Id><Acct><Id><IBAN>XX00DEMO0001</IBAN></Id><Ccy>EUR</Ccy></Acct>
<Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">0.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-10-01</Dt></Dt></Bal>
<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">300.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-10-01</Dt></Dt></Bal>
<Ntry><Amt Ccy="EUR">300.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
<BookgDt><Dt>2026-10-01</Dt></BookgDt><ValDt><Dt>2026-10-01</Dt></ValDt>
<NtryDtls><Btch><NbOfTxs>2</NbOfTxs></Btch>
<TxDtls><AmtDtls><TxAmt><Amt Ccy="EUR">100.00</Amt></TxAmt></AmtDtls></TxDtls>
<TxDtls><Amt Ccy="EUR">200.00</Amt></TxDtls>
</NtryDtls></Ntry></Stmt></BkToCstmrStmt></Document>
"""


def test_camt_transaction_level_carries_the_transaction_amount(tmp_path):
    from etlplus.formats import read
    f = tmp_path / "c.xml"
    f.write_text(CAMT, encoding="utf-8")
    rows = [r for b in read("camt053", f, {"level": "transaction"}) for r in b.to_pylist()]
    assert len(rows) == 2
    assert [r["transaction_amount"] for r in rows] == [Decimal("100.000"), Decimal("200.000")]
    assert [r["transaction_currency"] for r in rows] == ["EUR", "EUR"]
    assert all(r["amount"] == Decimal("300.000") for r in rows)


# ---------------------------------------------------------------- console

def _js(name):
    return (CONSOLE / name).read_text(encoding="utf-8")


def test_job_page_polls_runs_until_nothing_runs():
    src = _js("pages-jobs.js")
    body = src[src.index("async function pageJob("):src.index("// ------------------------------------------------------------------ builder wizard")]
    assert "every(2000" in body and "runBtn.disabled" in body


def test_step_detail_label_per_node():
    src = _js("diagram.js")
    assert 'n.id === "destination" ? "Load" : "File"' not in src
    for word in ('"Read"', '"Steps"', '"Columns"', '"Load"', '"File"'):
        assert word in src


def test_vertical_diagram_is_not_blown_up():
    css = (CONSOLE / "app.css").read_text(encoding="utf-8")
    m = re.search(r"\.diagram\.vertical svg \{[^}]*max-width:\s*(\d+)px", css)
    assert m and int(m.group(1)) <= 250


def test_durations_never_wrap():
    css = (CONSOLE / "app.css").read_text(encoding="utf-8")
    assert re.search(r"td\.num[^{]*\{[^}]*white-space:\s*nowrap", css)


def test_runs_list_keeps_loaded_on_a_phone():
    src = _js("pages-runs.js")
    assert 'h("th", { class: "r hide-sm" }, "Took")' in src
