"""Integrator: cross-role checks settled from SPEC_V2_CHANGES.md."""
import pytest

from etlplus import config, runner


@pytest.fixture
def proj(tmp_path):
    p = config.Project.init(tmp_path / "p")
    p = config.Project.load(p.path)
    p.connections["mg"] = {"kind": "mongodb", "host": "db.example.test", "database": "d", "role": "source"}
    p.connections["web"] = {"kind": "http", "url": "https://files.example.test/{date}.csv", "role": "source"}
    return p


def _job(src):
    return {"name": "j", "source": src,
            "target": {"connection": "mg", "table": "t", "mode": "replace"}}


def _src_problems(proj, src):
    return [x for x in runner.validate_job(proj, _job(src))
            if "source" in x.lower() or "query" in x.lower() or "collection" in x.lower()
            or "format" in x.lower()]


def test_mongodb_query_is_json_not_sql(proj):
    ok = {"connection": "mg", "table": "accounts", "query": '{"filter": {"status": "open"}}'}
    assert not any("refused" in x for x in _src_problems(proj, ok))
    assert not any("exactly one of" in x for x in _src_problems(proj, ok))
    bad = {"connection": "mg", "table": "accounts", "query": '{"pipeline": [{"$out": "x"}]}'}
    assert any("refused" in x for x in _src_problems(proj, bad))
    nojson = {"connection": "mg", "table": "accounts", "query": "SELECT 1"}
    assert any("refused" in x for x in _src_problems(proj, nojson))


def test_mongodb_needs_a_collection(proj):
    probs = _src_problems(proj, {"connection": "mg", "query": '{"filter": {}}'})
    assert any("collection" in x for x in probs)


def test_http_source_needs_no_files_pattern(proj):
    probs = _src_problems(proj, {"connection": "web", "format": "csv"})
    assert not any("exactly one of" in x for x in probs)
    assert not any("format" in x for x in probs)
    probs = _src_problems(proj, {"connection": "web"})
    assert any("format" in x for x in probs)
