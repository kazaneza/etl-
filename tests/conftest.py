"""Shared test settings.

Default runs leave out `realdb` and `slow` (pyproject addopts). A `realdb` test also skips
unless ETLPLUS_TESTDB=1, so `pytest -m realdb` without the testdb profile up is a clean skip.
"""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("ETLPLUS_TESTDB") == "1":
        return
    skip = pytest.mark.skip(reason="needs the testdb compose profile and ETLPLUS_TESTDB=1")
    for item in items:
        if "realdb" in item.keywords:
            item.add_marker(skip)
