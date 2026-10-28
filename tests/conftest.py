"""The comics_strips tests run inside a Tesserae checkout with this folder
dropped in as ``plugins/comics_strips/``; the host's own conftest provides
``app`` and ``client``. This one adds ``net``, a fake for the host's guarded
fetch, so no test touches the network."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_NAME = "_comics_strips_test_helpers"


def helpers() -> Any:
    """tests/_helpers.py, loaded once by path."""
    found = sys.modules.get(_NAME)
    if found is not None:
        return found
    spec = importlib.util.spec_from_file_location(_NAME, Path(__file__).with_name("_helpers.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Any:
    from app import net_guard

    fake = helpers().FakeNet()
    monkeypatch.setattr(net_guard, "fetch_bytes", fake.fetch_bytes)
    monkeypatch.setattr(net_guard, "fetch_json", fake.fetch_json)
    return fake
