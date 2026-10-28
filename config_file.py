"""What the comics show, kept in ``<data_dir>/config.json``.

The admin page (/plugins/comics_strips/) is the only place the comics are
chosen, and every comics cell shows them, so they are the widget's own data
like the todo widget's lists, not manifest settings. The file holds::

    strips          the strip rows in order: enabled, label, slug, mode,
                    pick, image_url
    captions        the xkcd title and alt text under a strip (a switch)
    small_screens   "fit" (as many as fit) or "one" (one at a time)
    gap_min, gap, gap_max   the space between comics, in px at 1440 wide
    status_note     the small out-of-date line (a switch)

Two ways in. :func:`check` is the strict save path: a value that doesn't
fit raises :class:`ConfigError` with a message for the page and nothing is
written. :func:`normalise` is the forgiving read path: whatever doesn't fit
takes its default, so a hand-edited file can't break a render.

Writes are atomic (a temp file, then ``os.replace``) and every
read-modify-write runs under one process-wide lock, so an upload that
switches a strip to its picture can't undo an edit saved meanwhile.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import re
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

FILE_NAME = "config.json"
VERSION = 1
MAX_STRIPS = 8
# Where a strip's picture comes from.
MODES = ("xkcd", "url", "upload")
# Which xkcd comic an xkcd strip shows.
PICKS = ("latest", "random")
SMALL_SCREENS = ("fit", "one")
LABEL_MAX = 60
SLUG_MAX = 64
URL_MAX = 2000
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SLUG_HINT = "lower-case letters, digits and -"

DEFAULT_STRIPS: tuple[dict[str, Any], ...] = (
    {
        "enabled": True,
        "label": "xkcd",
        "slug": "xkcd",
        "mode": "xkcd",
        "pick": "latest",
        "image_url": "",
    },
)
# name: (label, default)
SWITCHES: dict[str, tuple[str, bool]] = {
    "captions": ("Captions", True),
    "status_note": ("Out-of-date note", True),
}
# name: (label, default, lowest, highest)
NUMBERS: dict[str, tuple[str, int, int, int]] = {
    "gap_min": ("Smallest space", 6, 0, 200),
    "gap": ("Usual space", 32, 0, 200),
    "gap_max": ("Largest space", 48, 0, 400),
}
CHOICES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "small_screens": ("On small screens", "fit", SMALL_SCREENS),
}

_TRUE = frozenset({"true", "on", "1", "yes"})
_FALSE = frozenset({"false", "off", "0", "no", ""})


def defaults() -> dict[str, Any]:
    """A fresh copy of every value's default."""
    values: dict[str, Any] = {"strips": [dict(row) for row in DEFAULT_STRIPS]}
    values.update({name: spec[1] for name, spec in SWITCHES.items()})
    values.update({name: spec[1] for name, spec in NUMBERS.items()})
    values.update({name: spec[1] for name, spec in CHOICES.items()})
    return values


class ConfigError(ValueError):
    """A value that can't be saved. The message is fit for the page."""


def new_slug(text: str, taken: set[str] | frozenset[str]) -> str:
    """A row's id from what it is called: lower-case words joined by -, made
    unique among ``taken`` with a number. Rows keep their id for good (it
    names the strip's records on disk), whatever they are renamed to."""
    base = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[: SLUG_MAX - 4] or "strip"
    slug, n = base, 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


# -- strict: what a save may store ------------------------------------------------


def is_web_address(text: str) -> bool:
    """An http(s) address with a host that parses. Where it points is
    judged elsewhere (the net guard)."""
    if any(c.isspace() for c in text):
        return False
    try:
        parts = urlsplit(text)
        _ = parts.port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _switch(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    raise ConfigError(f"{label} must be on or off.")


def _number(value: Any, label: str, low: int, high: int) -> int:
    number: int | None = None
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        with contextlib.suppress(ValueError):
            number = int(value.strip())
    if number is None:
        raise ConfigError(f"{label} must be a whole number.")
    if not low <= number <= high:
        raise ConfigError(f"{label} must be between {low} and {high}.")
    return number


def _text(value: Any, label: str, limit: int, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {label} must be text.")
    text = value.strip()
    if "\n" in text or "\r" in text:
        raise ConfigError(f"{where}: {label} must be one line.")
    if len(text) > limit:
        raise ConfigError(f"{where}: {label} is longer than {limit} characters.")
    return text


def check_row(raw: Any, where: str) -> dict[str, Any]:
    """One strip row as it may be stored, or ConfigError."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a strip.")
    slug = _text(raw.get("slug"), "Id", SLUG_MAX, where)
    if not slug:
        raise ConfigError(f"{where}: Id is required.")
    if not SLUG_RE.match(slug):
        raise ConfigError(f"{where}: Id: {SLUG_HINT}.")
    mode = raw.get("mode") or "xkcd"
    if mode not in MODES:
        raise ConfigError(f"{where}: {mode!r} isn't one of the choices for Source.")
    pick = raw.get("pick") or "latest"
    if pick not in PICKS:
        raise ConfigError(f"{where}: {pick!r} isn't one of the choices for Which comic.")
    image_url = _text(raw.get("image_url"), "Image URL", URL_MAX, where)
    if image_url and not is_web_address(image_url):
        raise ConfigError(
            f"{where}: Image URL must be a web address starting with https:// or http://."
        )
    return {
        "enabled": _switch(raw.get("enabled", True), f"{where}: Enabled"),
        "label": _text(raw.get("label"), "Label", LABEL_MAX, where),
        "slug": slug,
        "mode": str(mode),
        "pick": str(pick),
        "image_url": image_url,
    }


def check_rows(raw: Any) -> list[dict[str, Any]]:
    """The strip rows as they may be stored: at most MAX_STRIPS, each one
    checked, no strip twice."""
    if not isinstance(raw, list):
        raise ConfigError("Strips: expected a list of strips.")
    if len(raw) > MAX_STRIPS:
        raise ConfigError(f"The list holds at most {MAX_STRIPS} strips.")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        row = check_row(item, f"Strip {index + 1}")
        if row["slug"] in seen:
            raise ConfigError(f"{row['slug']} is already in the list.")
        seen.add(row["slug"])
        rows.append(row)
    return rows


def check(values: dict[str, Any]) -> dict[str, Any]:
    """Every value as it may be stored, or ConfigError naming the first that
    doesn't fit. A missing value takes its default; unknown keys go."""
    out: dict[str, Any] = {"strips": check_rows(values.get("strips", defaults()["strips"]))}
    for name, (label, default) in SWITCHES.items():
        out[name] = _switch(values.get(name, default), label)
    for name, (label, number, low, high) in NUMBERS.items():
        out[name] = _number(values.get(name, number), label, low, high)
    for name, (label, choice, choices) in CHOICES.items():
        value = values.get(name, choice)
        if value not in choices:
            raise ConfigError(f"{value!r} isn't one of the choices for {label}.")
        out[name] = value
    return out


# -- forgiving: what a read shows ---------------------------------------------------


def _lenient_switch(value: Any, default: bool) -> bool:
    try:
        return _switch(value, "")
    except ConfigError:
        return default


def _lenient_number(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, number))


def _line(value: Any, limit: int) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def normalise_rows(value: Any) -> list[dict[str, Any]]:
    """``value`` as strip rows, never raising. A row without a usable id, or
    naming one already listed, is dropped; other values that don't fit take
    their defaults."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or len(rows) >= MAX_STRIPS:
            continue
        slug = _line(item.get("slug"), SLUG_MAX + 1).lower()
        if len(slug) > SLUG_MAX or not SLUG_RE.match(slug) or slug in seen:
            continue
        seen.add(slug)
        mode, pick = item.get("mode"), item.get("pick")
        rows.append(
            {
                "enabled": _lenient_switch(item.get("enabled", True), True),
                "label": _line(item.get("label"), LABEL_MAX),
                "slug": slug,
                "mode": mode if mode in MODES else "xkcd",
                "pick": pick if pick in PICKS else "latest",
                "image_url": _line(item.get("image_url"), URL_MAX),
            }
        )
    return rows


def normalise(values: Any) -> dict[str, Any]:
    """Every value, never raising: what doesn't fit takes its default."""
    values = values if isinstance(values, dict) else {}
    base = defaults()
    out: dict[str, Any] = {
        "strips": normalise_rows(values["strips"]) if "strips" in values else base["strips"]
    }
    for name, (_, default) in SWITCHES.items():
        out[name] = _lenient_switch(values.get(name, default), default)
    for name, (_, number, low, high) in NUMBERS.items():
        out[name] = _lenient_number(values.get(name, number), number, low, high)
    for name, (_, choice, choices) in CHOICES.items():
        out[name] = values.get(name) if values.get(name) in choices else choice
    return out


# -- the file ---------------------------------------------------------------------

# One writer at a time per process, and the same lock after an in-process
# reload of the plugin's modules (see images.shared).
_RUNTIME = f"_tesserae_plugins.{Path(__file__).resolve().parent.name}.runtime"
_LOCK: threading.Lock = sys.modules.setdefault(_RUNTIME, ModuleType(_RUNTIME)).__dict__.setdefault(
    "config_lock", threading.Lock()
)


class ConfigFile:
    """``config.json`` in a widget's data_dir."""

    def __init__(self, data_dir: Path | str) -> None:
        self.path = Path(data_dir) / FILE_NAME

    def read(self) -> dict[str, Any] | None:
        """The values on disk (normalised), or None when there is no file.
        A file that can't be read or parsed reads as the defaults, and is
        left for the next save to replace."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("comics_strips: can't read %s", self.path, exc_info=True)
            return defaults()
        try:
            data = json.loads(text)
        except ValueError:
            logger.warning("comics_strips: %s isn't JSON; showing the defaults", self.path)
            return defaults()
        return normalise(data)

    def load(self) -> dict[str, Any]:
        """The values, starting the file from the defaults when there is
        none (written only if no save got in first)."""
        found = self.read()
        if found is not None:
            return found
        with _LOCK:
            found = self.read()
            if found is not None:
                return found
            values = defaults()
            self._write(values)
        return values

    def update(
        self, change: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> dict[str, Any] | None:
        """Read, change and write in one step under the lock: ``change``
        gets a copy of the values as stored now and returns the new values,
        or None to leave them. The new values go through :func:`check`
        (ConfigError, and nothing written, when they don't fit). Returns
        what was written, or None."""
        with _LOCK:
            current = self.read() or defaults()
            new = change(copy.deepcopy(current))
            if new is None:
                return None
            checked = check(new)
            self._write(checked)
            return checked

    def _write(self, values: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"version": VERSION, **values}, indent=1, sort_keys=True)
        fd, tmp = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
