"""Shared helpers for the comics_strips tests: loading the widget's modules
from this folder, test pictures and xkcd answers, and a fake for the host's
guarded fetch (app.net_guard), so no test touches the network.

Loaded by path (``helpers()`` in each test module): the host runs pytest with
``--import-mode=importlib``, where test folders aren't importable by name.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import urllib.error
from pathlib import Path
from typing import Any

from PIL import Image

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def load(name: str, file: str = "server.py") -> Any:
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def png(size: tuple[int, int] = (90, 30), mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, "white").save(buf, format="PNG")
    return buf.getvalue()


def jpeg(size: tuple[int, int] = (95, 110)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 200, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def comic(
    num: int,
    title: str = "Title",
    alt: str = "Alt text",
    img: str | None = None,
    day: tuple[int, int, int] = (2026, 10, 23),
) -> dict[str, Any]:
    """An answer the way xkcd's info.0.json gives it (date parts as strings)."""
    year, month, dom = day
    return {
        "num": num,
        "title": title,
        "safe_title": title,
        "alt": alt,
        "img": img or f"https://imgs.xkcd.com/comics/comic_{num}.png",
        "year": str(year),
        "month": str(month),
        "day": str(dom),
        "link": "",
        "news": "",
        "transcript": "",
    }


def http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", None, None)  # type: ignore[arg-type]


class FakeNet:
    """Stands in for app.net_guard.fetch_bytes / fetch_json. A route's value
    is the answer (bytes for fetch_bytes, an object for fetch_json) or an
    exception to raise; a list is served in turn (the last repeats). Every
    call is recorded with its keyword arguments."""

    def __init__(self) -> None:
        self.bytes: dict[str, Any] = {}
        self.json: dict[str, Any] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _answer(self, table: dict[str, Any], url: str) -> Any:
        if url not in table:
            raise urllib.error.URLError(f"no route for {url}")
        value = table[url]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, BaseException):
            raise value
        return value

    def fetch_bytes(self, url: str, **kw: Any) -> tuple[bytes, str]:
        self.calls.append(("bytes", url, kw))
        return self._answer(self.bytes, url), "image/png"

    def fetch_json(self, url: str, **kw: Any) -> Any:
        self.calls.append(("json", url, kw))
        return self._answer(self.json, url)

    def urls(self, kind: str | None = None) -> list[str]:
        return [url for k, url, _ in self.calls if kind is None or k == kind]
