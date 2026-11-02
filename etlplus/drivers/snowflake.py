"""Snowflake through snowflake-connector-python. Read-only rests on the SQL guard: use a role with
SELECT only."""

from __future__ import annotations

from ._common import TIMEOUT, module


def connect(spec, password, role, project=None):
    sf = module("snowflake")
    kw = {"account": spec.get("account"), "user": spec.get("user"), "password": password,
          "warehouse": spec.get("warehouse"), "role": spec.get("db_role"), "database": spec.get("database"),
          "schema": spec.get("schema"), "login_timeout": TIMEOUT, "application": "etlplus",
          "autocommit": False}
    return sf.connect(**{k: v for k, v in kw.items() if v not in (None, "")})
