"""comics_strips, today's comics on one page: full-width strips, in your order.

The sources, chosen per strip on the widget's admin page:

* **xkcd**, from xkcd's official JSON API (xkcd.py): the latest comic, or a
  random one from the archive, a new one each day. Shown with its title and
  its alt text as the caption, and credited to xkcd.com.
* **Direct image URL**: any picture address, downloaded again each day.
* **Manual upload**: a picture uploaded on the admin page, shown until the
  strip is switched to another source.

Every download goes through the host's SSRF-guarded fetch (``app.net_guard``):
only public web addresses, each redirect checked again, the size capped.

``fetch()`` never downloads: it answers from the last good pictures on disk
and, when a source is due, queues it for one background worker, reporting
the work in the job tray and as ``live_refresh_s`` so editors repaint and a
push is followed up once the new pictures land. ``render.warm_ahead_s``
starts that work ahead of a scheduled send, and ``updates.on_schedule`` lets
a placement refresh daily.

A source is due once per local day. A failure keeps the last good picture,
backs off (media_lifecycle.py), and is reported in the data so the page can
say what is stale.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import importlib.util
import logging
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.wrappers import Response

try:
    from app import net_guard
except ImportError:  # a Tesserae from before the guarded fetch
    net_guard = None  # type: ignore[assignment]

try:
    from app import jobs
except ImportError:  # an older Tesserae without the job tray
    jobs = None  # type: ignore[assignment]


_HERE = Path(__file__).resolve().parent
PLUGIN_ID = _HERE.name


def _load_sibling(name: str) -> ModuleType:
    """Load a helper module beside this file (docs/widgets.md, "Helper modules
    beside server.py"). Registered in ``sys.modules`` under a name unique to
    this widget so dataclasses inside it resolve their own module."""
    module_name = f"_tesserae_plugins.{PLUGIN_ID}.{name}"
    spec = importlib.util.spec_from_file_location(module_name, _HERE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


lifecycle = _load_sibling("media_lifecycle")
images = _load_sibling("images")
screen = _load_sibling("screen")
config_file = _load_sibling("config_file")
xkcd = _load_sibling("xkcd")

logger = logging.getLogger(__name__)

# -- fetching ------------------------------------------------------------------

MAX_STRIPS = config_file.MAX_STRIPS
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Tesserae comics_strips)",
    "Accept-Encoding": "identity",
}
JSON_TIMEOUT_S = 15
IMAGE_TIMEOUT_S = 30
JSON_MAX_BYTES = 256 * 1024
IMAGE_MAX_BYTES = 20 * 1024 * 1024
# How the very first render waits for pictures before painting the
# "fetching" state; the composer gives a widget six seconds in all.
FIRST_RENDER_WAIT_S = 4.0
# Editors poll, and a push is followed up, this often while work is queued.
LIVE_REFRESH_S = 8
TOO_OLD = "This Tesserae is too old for this widget's downloads: update Tesserae."

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class SourceError(Exception):
    """A source could not be fetched. The message is shown to the user."""


def _host(url: str) -> str:
    """The URL's host, "" when it has none or doesn't parse (``https://[``)."""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def get_bytes(url: str, what: str, *, max_bytes: int = IMAGE_MAX_BYTES) -> bytes:
    """``url``'s body through the host's guarded fetch, or SourceError
    naming ``what``."""
    if net_guard is None:
        raise SourceError(TOO_OLD)
    host = _host(url) or "that address"
    try:
        raw, _ = net_guard.fetch_bytes(
            url, headers=HEADERS, timeout=IMAGE_TIMEOUT_S, max_bytes=max_bytes, allow_local=False
        )
    except net_guard.BlockedURLError as err:
        raise SourceError(f"{host} isn't on the public web, so it can't be fetched.") from err
    except urllib.error.HTTPError as err:
        raise SourceError(f"{host} answered HTTP {err.code} for {what}.") from err
    except ValueError as err:
        raise SourceError(f"The picture for {what} is over the size this widget fetches.") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise SourceError(f"Couldn't reach {host}.") from err
    return bytes(raw)


def xkcd_json(url: str) -> Any:
    """One xkcd API answer, parsed, through the host's guarded fetch.
    Raises xkcd.NotFound for a 404, xkcd.XkcdError for anything else."""
    if net_guard is None:
        raise xkcd.XkcdError(TOO_OLD)
    try:
        return net_guard.fetch_json(
            url, headers=HEADERS, timeout=JSON_TIMEOUT_S, max_bytes=JSON_MAX_BYTES
        )
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise xkcd.NotFound("xkcd has no such comic.") from err
        raise xkcd.XkcdError(f"xkcd answered HTTP {err.code}.") from err
    except net_guard.BlockedURLError as err:
        raise xkcd.XkcdError("xkcd.com couldn't be reached from here.") from err
    except ValueError as err:  # not JSON, or over the cap
        raise xkcd.XkcdError("xkcd sent an answer this widget can't read.") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise xkcd.XkcdError("Couldn't reach xkcd.com.") from err


# -- what a cell asks for --------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One thing to show. ``key`` names its record in state.json. A
    ``manual`` source is the picture uploaded for a strip on the admin page:
    it is never fetched."""

    key: str
    kind: str  # "xkcd" | "url" | "manual"
    name: str  # the strip row's id
    url: str = ""
    title: str = ""  # the label set for the strip, if any
    pick: str = "latest"  # xkcd: "latest" | "random"

    @property
    def label(self) -> str:
        return self.title or default_label(self.name, self.kind)

    @property
    def fetched(self) -> bool:
        return self.kind != "manual"


def default_label(name: str, kind: str = "") -> str:
    """A strip's name when it has no label: its id in words ("my-strip" is
    "My Strip"), keeping xkcd lower-case as xkcd writes it ("xkcd-2" is
    "xkcd 2")."""
    words = name.replace("-", " ").strip()
    if words.lower().startswith("xkcd"):
        return words.lower()
    return words.title() or ("xkcd" if kind == "xkcd" else "Strip")


@dataclass(frozen=True)
class Problem:
    """A strip that can't be shown, and why."""

    name: str
    message: str


def url_source(name: str, url: str, title: str = "") -> Source | Problem:
    host = _host(url)
    if not _URL_RE.match(url) or not host or any(c.isspace() for c in url):
        return Problem(title or name, "That image address isn't a web address.")
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    return Source(key=f"url:{digest}", kind="url", name=name, url=url, title=title)


# -- the settings ------------------------------------------------------------------
#
# The comics are chosen once, on the widget's admin page (Widgets > Admin
# pages > Comics, Strips), and every cell shows them. They live in the
# widget's own data_dir as config.json (config_file.py), not as manifest
# settings; the file is started from the defaults the first time it is needed.


@dataclass(frozen=True)
class StripRow:
    """One row of the ``strips`` setting."""

    slug: str
    enabled: bool = True
    label: str = ""
    mode: str = "xkcd"
    pick: str = "latest"
    image_url: str = ""


@dataclass(frozen=True)
class Config:
    """What every comics cell shows."""

    strips: tuple[StripRow, ...]
    captions: bool = True
    status_note: bool = True
    gap: int = 32
    gap_min: int = 6
    gap_max: int = 48
    small_screens: str = "fit"  # "fit" (as many as fit) | "one" (one at a time)

    def layout(self) -> dict[str, Any]:
        """The layout client.js reads."""
        return {
            "captions": self.captions,
            "status_note": self.status_note,
            "gap": self.gap,
            "gap_min": self.gap_min,
            "gap_max": self.gap_max,
        }


def config_from_settings(values: dict[str, Any]) -> Config:
    """A Config from config.json's (normalised) values."""
    clean = config_file.normalise(values)
    rows = tuple(
        StripRow(
            slug=row["slug"],
            enabled=row["enabled"],
            label=row["label"],
            mode=row["mode"],
            pick=row["pick"],
            image_url=row["image_url"],
        )
        for row in clean["strips"]
    )
    return Config(
        strips=rows,
        captions=clean["captions"],
        status_note=clean["status_note"],
        gap=clean["gap"],
        gap_min=clean["gap_min"],
        gap_max=clean["gap_max"],
        small_screens=clean["small_screens"],
    )


def row_source(row: StripRow) -> Source | Problem:
    """Where one strip row's picture comes from, as its Source says."""
    if row.mode == "upload":
        return Source(key=f"manual:{row.slug}", kind="manual", name=row.slug, title=row.label)
    if row.mode == "url":
        if not row.image_url:
            return Problem(row.label or default_label(row.slug), "Give it an image URL first.")
        return url_source(row.slug, row.image_url, row.label)
    return Source(
        key=f"xkcd:{row.slug}", kind="xkcd", name=row.slug, title=row.label, pick=row.pick
    )


def sources_for(config: Config) -> tuple[list[Source], list[Problem]]:
    """What the settings show, top to bottom: each strip switched on, from
    where its Source says. A row that can't be shown is a Problem the page
    notes."""
    wanted: list[Source] = []
    problems: list[Problem] = []
    for row in config.strips:
        if not row.enabled:
            continue
        if len(wanted) >= MAX_STRIPS:
            break
        found = row_source(row)
        if isinstance(found, Problem):
            problems.append(found)
        else:
            wanted.append(found)
    return wanted, problems


def wanted_keys(config: Config) -> set[str]:
    """Every record the settings still name, shown or not: for each strip in
    the list (on or off) its xkcd record, its Direct image URL and its manual
    picture, so switching a strip off or between sources never loses its
    last good picture."""
    keys: set[str] = set()
    for row in config.strips:
        keys.add(f"manual:{row.slug}")
        keys.add(f"xkcd:{row.slug}")
        if row.image_url:
            found = url_source(row.slug, row.image_url)
            if isinstance(found, Source):
                keys.add(found.key)
    return keys


def config_store(data_dir: Path | str) -> Any:
    """The widget's config.json in ``data_dir``."""
    return config_file.ConfigFile(data_dir)


def _plugin_data_dir() -> Path:
    plugin = current_app.config["PLUGIN_REGISTRY"].get(PLUGIN_ID)
    if plugin is None:
        abort(404)
    return Path(plugin.data_dir)


def ensure_settings(data_dir: Path | str) -> dict[str, Any]:
    """The values in config.json, starting the file from the defaults first
    when there is none."""
    values: dict[str, Any] = config_store(data_dir).load()
    return values


# -- fetching each source --------------------------------------------------------


@dataclass
class Fetched:
    """What a source produced: raw pictures with what to keep beside each
    (an xkcd comic's number, title and alt text), and its date. ``keep`` is
    set instead when the source still has the picture already on disk (the
    same xkcd comic as last time): those stored pictures stay as they are."""

    pictures: list[tuple[bytes, dict[str, Any]]]
    edition: str | None = None
    label: str | None = None
    keep: list[dict[str, Any]] | None = None


def _today() -> date:
    """The local day, in the timezone set under Settings when there is one."""
    try:
        from app.tz_resolve import app_timezone

        return datetime.now(app_timezone()).date()
    except Exception:
        return datetime.now().astimezone().date()


def daily_rng(source: Source, today: date) -> random.Random:
    """The random pick for ``source`` today: the same all day (a second
    fetch the same day finds the same comic), a new one tomorrow, and
    different for two random strips in the list."""
    return random.Random(f"{today.isoformat()}:{source.key}")


def fetch_xkcd(source: Source, today: date, record: dict[str, Any], store: Any) -> Fetched:
    """The xkcd comic ``source`` wants. When it is the comic already on disk
    (xkcd publishes three times a week, so "latest" is often the same for a
    couple of days) the picture isn't downloaded again."""
    try:
        if source.pick == "random":
            comic = xkcd.random_comic(xkcd_json, daily_rng(source, today))
        else:
            comic = xkcd.latest(xkcd_json)
    except xkcd.XkcdError as err:
        raise SourceError(str(err)) from err
    stored = [
        p
        for p in record.get("pictures") or []
        if isinstance(p, dict) and store.image_path(str(p.get("file") or "")) is not None
    ]
    if stored and stored[0].get("num") == comic.num:
        return Fetched([], edition=comic.date or None, keep=stored[:1])
    raw = get_bytes(comic.img, f"xkcd {comic.num}")
    meta = {"num": comic.num, "title": comic.title, "alt": comic.alt, "credit": comic.credit}
    return Fetched([(raw, meta)], edition=comic.date or None)


def fetch_image_url(source: Source) -> Fetched:
    return Fetched([(get_bytes(source.url, source.label), {})])


# -- the refresh worker ----------------------------------------------------------


def is_due(source: Source, record: dict[str, Any] | None, today: str, now: float) -> bool:
    """Whether ``source`` should be fetched now, given its record: once a
    day, and after a failure on media_lifecycle.py's backoff."""
    if not record:
        return True
    if record.get("fetched_on") == today:
        return False
    return bool(lifecycle.may_try(record.get("retry"), today, now))


@dataclass
class _Run:
    """One background refresh: a queue a render can add to while it runs.

    Queue entries carry a ``force`` flag (``?fresh=1``, Fetch now), which
    skips the due checks. ``completed`` remembers what this run already
    fetched, so a render working from a snapshot taken before that can't
    queue it again.
    """

    data_dir: Path
    today: date
    queue: list[tuple[Source, bool]] = field(default_factory=list)
    active: Source | None = None
    completed: set[str] = field(default_factory=set)
    done: bool = False
    finished: int = 0
    failed: int = 0
    thread: threading.Thread | None = None
    # Every source the settings name, for the cleanup at the run's end;
    # None when the caller didn't say (a direct call).
    wanted: set[str] | None = None

    def keys(self) -> set[str]:
        keys = {s.key for s, _ in self.queue}
        if self.active is not None:
            keys.add(self.active.key)
        return keys

    def alive(self) -> bool:
        return not self.done and self.thread is not None and self.thread.is_alive()


# Kept outside this module (see images.shared) so a reload of the plugin
# finds the worker already running instead of starting a second one.
_RUNS: dict[str, _Run] = images.shared("runs", dict)
_RUNS_LOCK: threading.Lock = images.shared("runs_lock", threading.Lock)


def _enqueue(
    data_dir: Path,
    sources: list[Source],
    today: date,
    *,
    force: bool = False,
    wanted: set[str] | None = None,
) -> _Run:
    """Queue ``sources`` on the running refresh, starting one if none runs.

    A run whose thread has died without finishing is replaced, not reused.
    The worker runs in a copy of the caller's context, so the widget's
    capability scope (its ``requires:`` hosts) still applies off-thread.
    """
    with _RUNS_LOCK:
        run = _RUNS.get(str(data_dir))
        start = run is None or not run.alive()
        if start:
            run = _Run(data_dir=data_dir, today=today)
            _RUNS[str(data_dir)] = run
        assert run is not None
        if wanted is not None:
            run.wanted = wanted
        known = run.keys() | (set() if force else run.completed)
        run.queue.extend((s, force) for s in sources if s.key not in known)
        if start:
            context = contextvars.copy_context()
            run.thread = threading.Thread(
                target=context.run, args=(_work, run), name="comics-strips-refresh", daemon=True
            )
            run.thread.start()
    return run


def _running_keys(data_dir: Path) -> set[str]:
    with _RUNS_LOCK:
        run = _RUNS.get(str(data_dir))
        return run.keys() if run is not None and run.alive() else set()


def _report(run: _Run, source: Source) -> None:
    if jobs is None:
        return
    total = run.finished + len(run.queue) + 1
    with contextlib.suppress(Exception):
        jobs.report(
            PLUGIN_ID,
            "refresh",
            "Fetching today's comics",
            progress=run.finished / total,
            detail=source.label,
            label="Comics",
        )


def _finish_job(run: _Run, crashed: bool) -> None:
    if jobs is None:
        return
    with contextlib.suppress(Exception):
        summary = f"{run.finished - run.failed} of {run.finished} updated"
        if crashed or (run.failed and run.failed == run.finished):
            jobs.fail(PLUGIN_ID, "refresh", summary)
        else:
            jobs.finish(PLUGIN_ID, "refresh", summary)


def _work(run: _Run) -> None:
    """Drain the run's queue, one source at a time.

    Whatever happens, the run ends marked done with nothing active, so the
    next render starts a fresh worker rather than waiting on a dead one.
    """
    store = images.Store(run.data_dir)
    crashed = False
    try:
        while True:
            with _RUNS_LOCK:
                if not run.queue:
                    run.active = None
                    run.done = True
                    break
                source, force = run.queue.pop(0)
                run.active = source
            # The render that queued this read the state before earlier work
            # in this run landed; ask the state again, now.
            record = store.sources().get(source.key)
            if not force and not is_due(source, record, run.today.isoformat(), time.time()):
                continue
            _report(run, source)
            if not refresh_source(store, source, run.today):
                run.failed += 1
            run.finished += 1
            run.completed.add(source.key)
    except Exception:
        crashed = True
        logger.exception("comics_strips: the refresh worker stopped")
    finally:
        with _RUNS_LOCK:
            run.active = None
            run.done = True
        with contextlib.suppress(Exception):
            gone = store.prune(wanted=run.wanted)
            if gone:
                logger.info("comics_strips: removed %d unused pictures", len(gone))
        _finish_job(run, crashed)


def _fetch_source(source: Source, today: date, record: dict[str, Any], store: Any) -> Fetched:
    if source.kind == "xkcd":
        return fetch_xkcd(source, today, record, store)
    if source.kind == "url":
        return fetch_image_url(source)
    raise SourceError(f"{source.label} is a manual picture: upload one on the admin page.")


def refresh_source(store: Any, source: Source, today: date) -> bool:
    """Fetch one source and record the outcome; True when it updated.

    The previous pictures stay unless a new one was stored, so a failure
    never blanks the page. Storage failures (a full disk) are logged and
    reported as a failed source, never raised into the worker.
    """
    today_iso = today.isoformat()
    started = time.time()

    def attempt(entry: dict[str, Any]) -> None:
        if entry.get("attempted_on") != today_iso:
            entry["attempted_on"] = today_iso
            entry["attempts"] = 0
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["last_attempt"] = started

    try:
        record = store.update(source.key, attempt)
    except Exception:
        logger.exception("comics_strips: could not record an attempt at %s", source.key)
        return False
    try:
        fetched: Fetched | SourceError = _fetch_source(source, today, record, store)
    except SourceError as err:
        fetched = err
    except Exception:
        logger.exception("comics_strips: fetching %s failed", source.key)
        fetched = SourceError(f"Couldn't update {source.label}.")
    return record_fetch(store, source, fetched, today, started)


def record_fetch(
    store: Any, source: Source, fetched: Fetched | SourceError, today: date, started: float
) -> bool:
    """Keep what a fetch of ``source`` brought (or the error it ended in),
    note it in the log, and say whether the source updated."""
    today_iso = today.isoformat()
    stored: list[dict[str, Any]] = []
    if isinstance(fetched, Fetched):
        try:
            stored = fetched.keep if fetched.keep else _store_pictures(store, source, fetched)
        except SourceError as err:
            fetched = err
        except Exception:
            logger.exception("comics_strips: storing %s failed", source.key)
            fetched = SourceError(f"Couldn't keep the picture for {source.label}.")
    if isinstance(fetched, SourceError):
        outcome: Callable[[dict[str, Any]], None] = _failed(str(fetched), today_iso)
    else:
        outcome = _succeeded(fetched, stored, today_iso)
    try:
        entry = store.update(source.key, outcome)
    except Exception:
        logger.exception("comics_strips: could not record the outcome for %s", source.key)
        return False
    _log_fetch(store, source, entry, fetched, stored, today_iso, time.time() - started)
    if isinstance(fetched, Fetched):
        logger.info("comics_strips: updated %s (%s)", source.key, fetched.edition or today_iso)
        return True
    logger.warning("comics_strips: %s not updated: %s", source.key, entry.get("error"))
    return False


def _size(stored: list[dict[str, Any]]) -> str:
    total = sum(int(p.get("bytes") or 0) for p in stored)
    return f"{total / 1048576:.1f} MB" if total >= 1048576 else f"{max(1, round(total / 1024))} KB"


def log_entry(store: Any, label: str, outcome: str, text: str) -> None:
    """One line of the admin page's log. Never raises: the log is a courtesy."""
    with contextlib.suppress(Exception):
        store.append_log({"at": time.time(), "source": label, "outcome": outcome, "text": text})


def _log_fetch(
    store: Any,
    source: Source,
    entry: dict[str, Any],
    fetched: Fetched | SourceError,
    stored: list[dict[str, Any]],
    today: str,
    took: float,
) -> None:
    """Log one fetch: OK (with the comic, size and time taken), unchanged
    (the same xkcd comic as before), or error with the next try."""
    label = source.label
    seconds = f"{took:.1f} s"
    if isinstance(fetched, SourceError):
        retry = lifecycle.describe(entry.get("retry"), today, time.time())
        kept = "; the last good picture stays up" if entry.get("pictures") else ""
        text = f"{fetched} ({seconds}){kept}{'; ' + retry if retry else ''}"
        log_entry(store, label, "error", text)
        return
    num = stored[0].get("num") if stored else None
    what = f"xkcd {num}" if num else (fetched.edition or today)
    if fetched.keep:
        log_entry(store, label, "unchanged", f"still {what}, nothing new to download ({seconds})")
        return
    count = len(stored)
    log_entry(
        store,
        label,
        "ok",
        f"{what}, {count} picture{'' if count == 1 else 's'}, {_size(stored)} in {seconds}",
    )


def _failed(message: str, today: str) -> Callable[[dict[str, Any]], None]:
    """A failed try: the error, and the backoff before the next one. What
    was on disk stays up meanwhile."""

    def record(entry: dict[str, Any]) -> None:
        entry["error"] = message
        entry["retry"] = lifecycle.failed(entry.get("retry"), today, time.time())

    return record


def _succeeded(
    fetched: Fetched, stored: list[dict[str, Any]], today: str
) -> Callable[[dict[str, Any]], None]:
    def record(entry: dict[str, Any]) -> None:
        entry.pop("retry", None)
        entry.update(
            pictures=stored,
            edition=fetched.edition,
            fetched_on=today,
            last_success=time.time(),
            error=None,
        )
        if fetched.label:
            entry["label"] = fetched.label

    return record


def _store_pictures(store: Any, source: Source, fetched: Fetched) -> list[dict[str, Any]]:
    """Convert and keep each fetched picture; skip any that isn't one.
    Raises SourceError when none survive, which leaves the old pictures up."""
    stored = []
    for raw, meta in fetched.pictures:
        try:
            picture = images.normalise(raw)
        except ValueError:
            continue
        name = store.add(source.key, picture)
        stored.append(
            {
                "file": name,
                "w": picture.width,
                "h": picture.height,
                "bytes": len(picture.data),
                **meta,
            }
        )
    if not stored:
        raise SourceError(f"The download for {source.label} wasn't a picture.")
    return stored


# -- widget entry point ----------------------------------------------------------


def _image_url(name: str) -> str:
    return f"/plugins/{PLUGIN_ID}/img/{name}"


def _pictures(record: dict[str, Any], store: Any) -> list[dict[str, Any]]:
    """The record's pictures whose files are still on disk."""
    out = []
    for pic in record.get("pictures") or []:
        if not isinstance(pic, dict):
            continue
        name = str(pic.get("file") or "")
        w, h = int(pic.get("w") or 0), int(pic.get("h") or 0)
        if w > 0 and h > 0 and store.image_path(name) is not None:
            item = {"src": _image_url(name), "w": w, "h": h}
            for key in ("num", "title", "alt", "credit"):
                if pic.get(key):
                    item[key] = pic[key]
            out.append(item)
    return out


def caption_lines(strip: dict[str, Any], captions: bool) -> tuple[str, ...]:
    """The text drawn under a strip, line by line, as client.js draws it: an
    xkcd comic's title beside its credit, then its alt text; only the credit
    with captions off. Nothing for other strips."""
    credit = str(strip.get("credit") or "")
    if not credit:
        return ()
    if not captions:
        return (credit,)
    head = " ".join(t for t in (str(strip.get("title") or ""), credit) if t)
    return tuple(t for t in (head, str(strip.get("alt") or "")) if t)


def resolve_config(data_dir: Path | str) -> Config:
    """What a render shows: config.json (started on first use)."""
    return config_from_settings(ensure_settings(data_dir))


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    store = images.Store(Path(ctx["data_dir"]))
    config = resolve_config(ctx["data_dir"])
    wanted, problems = sources_for(config)
    if not wanted:
        with contextlib.suppress(Exception):
            store.prune_if_due(wanted_keys(config))
        if problems:
            return {"error": f"{problems[0].name}: {problems[0].message}"}
        return {"error": "Turn on a strip under Widgets → Admin pages → Comics, Strips."}

    today = _today()
    today_iso = today.isoformat()
    now = time.time()
    records = store.sources()
    force = bool(ctx.get("fresh"))
    due = [
        s for s in wanted if s.fetched and (force or is_due(s, records.get(s.key), today_iso, now))
    ]
    run = _enqueue(store.root, due, today, force=force, wanted=wanted_keys(config)) if due else None
    if run is None:
        # No worker to clean up after itself (all manual, all up to date):
        # clean up here, at most hourly.
        with contextlib.suppress(Exception):
            store.prune_if_due(wanted_keys(config), now)
    if run is not None and not any(_pictures(records.get(s.key) or {}, store) for s in wanted):
        # Nothing to show yet: give a quick source the chance to land in this
        # render rather than painting "fetching" and waiting for the repaint.
        if run.thread is not None:
            run.thread.join(FIRST_RENDER_WAIT_S)
        records = store.sources()
    busy = _running_keys(store.root)
    payload = _payload(wanted, problems, records, busy, store, today_iso)
    payload["layout"] = config.layout()
    return fit_screen(payload, config, store, ctx, now)


def placement_of(ctx: dict[str, Any]) -> str:
    """Whose turn a render takes. The host's placement id (page, cell,
    device) when it gives one; else the cell's id with its size (and the
    device, when the host names it), so identical cells on a big panel and
    a small one never share a turn; with none of those, one shared turn."""
    given = str(ctx.get("placement_id") or "")
    if given:
        return given
    cell = str(ctx.get("cell_id") or "default")
    placement = f"{cell}:{ctx.get('cell_w') or 0}x{ctx.get('cell_h') or 0}"
    device = str(ctx.get("target_device_id") or "")
    return f"{placement}@{device}" if device else placement


def fit_screen(
    payload: dict[str, Any], config: Config, store: Any, ctx: dict[str, Any], now: float
) -> dict[str, Any]:
    """Keep only what this cell shows (screen.py): everything when it fits,
    else the comics whose turn it is. A settled render (a push, a warm-ahead)
    moves the placement's turn on, at most once per screen.HOLD_S; a preview
    shows the turn without moving it."""
    strips = payload.get("strips") or []
    if len(strips) < 2:
        return payload
    items = [
        screen.Item((int(s["w"]), int(s["h"])), caption_lines(s, config.captions)) for s in strips
    ]
    settled = bool(ctx.get("settled", not ctx.get("preview")))

    def decide(start: int) -> Any:
        return screen.plan(
            ctx.get("cell_w"),
            ctx.get("cell_h"),
            items,
            note=config.status_note and bool(payload.get("notes")),
            gap_min=config.gap_min,
            one_at_a_time=config.small_screens == "one",
            start=start,
        )

    chosen = store.take_turn(placement_of(ctx), len(items), decide, advance=settled, now=now)
    if not chosen.rotating:
        return payload
    # The order they take their turn in, the first shown on top.
    payload["strips"] = [strips[i] for i in chosen.show]
    payload["turns"] = {"showing": len(chosen.show), "of": chosen.total}
    return payload


def freshness(
    record: dict[str, Any], has_pictures: bool, busy: bool, today: str
) -> tuple[str, str | None]:
    """How current a source's pictures are, and the date a note should show:
    ``ok``, ``stale`` (old pictures up, the last try failed), ``missing``
    (none, and the last try failed) or ``pending``. An xkcd comic that is
    still the newest is current, however old it is."""
    error = record.get("error")
    if not has_pictures:
        return ("missing" if error and not busy else "pending"), None
    if error and record.get("fetched_on") != today:
        return "stale", record.get("fetched_on")
    return "ok", None


def _payload(
    wanted: list[Source],
    problems: list[Problem],
    records: dict[str, dict[str, Any]],
    busy: set[str],
    store: Any,
    today: str,
) -> dict[str, Any]:
    strips: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for source in wanted:
        record = records.get(source.key) or {}
        pictures = _pictures(record, store)
        label = source.label
        error = record.get("error")
        busy_now = source.key in busy
        state, since = freshness(record, bool(pictures), busy_now, today)
        if source.kind == "manual" and not pictures:
            state, error = "missing", "No picture uploaded yet."
        sources.append(
            {
                "id": source.name,
                "label": label,
                "state": state,
                "updating": busy_now,
                "edition": record.get("edition"),
                "updated_on": record.get("fetched_on"),
                "message": error or "",
            }
        )
        if state in ("stale", "missing"):
            notes.append({"label": label, "state": state, "since": since, "message": error or ""})
        if pictures:
            first = pictures[0]
            strip = {
                "id": source.name,
                "label": label,
                "src": first["src"],
                "w": first["w"],
                "h": first["h"],
                "date": record.get("edition"),
            }
            for key in ("num", "title", "alt", "credit"):
                if first.get(key):
                    strip[key] = first[key]
            strips.append(strip)
    for problem in problems:
        notes.append({"label": problem.name, "state": "invalid", "message": problem.message})

    pending = [s for s in sources if s["updating"]]
    payload: dict[str, Any] = {
        "strips": strips,
        "sources": sources,
        "notes": notes,
        "updating": bool(pending),
        "today": today,
    }
    if pending:
        payload["live_refresh_s"] = LIVE_REFRESH_S
        payload["live_pending"] = len(pending)
    elif not strips:
        failed = [s["message"] for s in sources if s["state"] == "missing" and s["message"]]
        if failed:
            payload["error"] = "Couldn't fetch any comics yet. " + failed[0]
    return payload


# -- serving the pictures --------------------------------------------------------

# Read-only, so the loopback renderer may load the pictures with no session
# (docs/widgets.md, "Serving your own media").
RENDER_SAFE_ENDPOINTS: tuple[str, ...] = (f"{PLUGIN_ID}_media.image",)


def _store() -> Any:
    return images.Store(_plugin_data_dir())


# -- the admin page ---------------------------------------------------------------
#
# /plugins/comics_strips/: today's comics with a Fetch now button and a
# preview per screen; the strips in order, each with its status, edit and
# upload, and adding a strip; Layout; then the log of every fetch and upload.
# Every change is a POST that saves config.json and redirects back to the
# card it came from, so the page works without JavaScript (static/admin.js
# only adds niceties).

# Room for the multipart framing around an uploaded picture.
UPLOAD_OVERHEAD_BYTES = 64 * 1024


def _admin_plugin() -> Any:
    plugin = current_app.config["PLUGIN_REGISTRY"].get(PLUGIN_ID)
    if plugin is None:
        abort(404)
    return plugin


def _scope(plugin: Any) -> Any:
    """The widget's own network scope (its ``requires:`` hosts), for work an
    admin request starts, as a render's would run in."""
    try:
        from app.capabilities import capability_scope
    except ImportError:  # pragma: no cover - every supported host has it
        return contextlib.nullcontext()
    return capability_scope(plugin.capabilities)


def _admin_tz() -> Any:
    try:
        from app.tz_resolve import app_timezone

        return app_timezone()
    except Exception:
        return None


def _when(ts: Any, tz: Any, today: str) -> str:
    try:
        moment = datetime.fromtimestamp(float(ts), tz)
    except (TypeError, ValueError, OverflowError, OSError):
        return "at an unknown time"
    if moment.date().isoformat() == today:
        return f"today at {moment:%H:%M}"
    return f"{moment:%b} {moment.day} at {moment:%H:%M}"


def _day(iso: Any) -> str:
    try:
        day = date.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    return f"{day:%b} {day.day}"


KIND_NAMES = {"xkcd": "xkcd", "url": "Image URL", "manual": "Upload"}


def source_status(
    source: Source, record: dict[str, Any], store: Any, busy: set[str], today: str, tz: Any
) -> dict[str, Any]:
    """How one source stands, for the admin page: a short chip (and its
    tone), a status line, a warning when it couldn't refresh, and its
    pictures."""
    pictures = _pictures(record, store)
    error = str(record.get("error") or "")
    state, since = freshness(record, bool(pictures), source.key in busy, today)
    fetched = record.get("last_success")
    warning = ""
    if source.key in busy:
        chip, tone, line = "Fetching", "slate", "Fetching now…"
    elif source.kind == "manual":
        if not pictures:
            chip, tone, line = "No picture", "bad", "No picture yet: upload one below."
        else:
            chip, tone = "Manual", "slate"
            line = f"Uploaded picture, {_when(fetched, tz, today)}."
    elif state == "ok":
        chip, tone = "OK", "ok"
        first = pictures[0]
        if first.get("num"):
            what = f"xkcd {first['num']}"
            if first.get("title"):
                what += f", “{first['title']}”"
            if record.get("edition"):
                what += f" ({_day(record['edition'])})"
        else:
            what = "Today's picture"
        line = f"{what}, checked {_when(fetched, tz, today)}."
    elif state == "pending":
        chip, tone, line = "Not fetched", "", "Not fetched yet: Fetch now gets it."
    else:
        if state == "stale":
            chip, tone = "Stale", "warn"
            line = (
                f"Stale since {_day(since) if since else 'earlier'}: showing the last good picture"
            )
            line += f", fetched {_when(fetched, tz, today)}." if fetched else "."
        else:
            chip, tone, line = "Missing", "bad", "Nothing to show yet."
        if error:
            warning = f"Couldn't refresh: {error}"
    return {
        "state": state,
        "chip": chip,
        "tone": tone,
        "line": line,
        "warning": warning,
        "pictures": pictures,
    }


MINE = "Your screens"
REFERENCE = "Reference screens"
MAX_OWN_SCREENS = 8


def _dims(value: Any) -> tuple[int, int] | None:
    """An object's (w, h) when both are positive ints, else None."""
    try:
        width, height = int(value.w), int(value.h)
    except (AttributeError, TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def _comics_cells(page: Any) -> tuple[tuple[int, int] | None, list[tuple[int, int]]]:
    """A page's layout size (None when it takes its device's) and the size
    of each comics cell on it, grid or canvas."""
    if getattr(page, "layout_kind", "grid") == "canvas":
        canvas = getattr(page, "canvas", None)
        cells = [
            el
            for el in getattr(canvas, "els", None) or []
            if el.kind == "widget" and el.widget == PLUGIN_ID
        ]
        base = _dims(canvas)
    else:
        cells = [c for c in getattr(page, "cells", None) or [] if c.plugin == PLUGIN_ID]
        base = _dims(getattr(page, "panel", None))
    return base, [size for size in map(_dims, cells) if size]


def preview_screens(pages: list[Any], devices: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """The screens the admin page previews and which one it opens on.

    First the user's own: each device a page with a comics cell is sent
    to, at the cell's real size (the panel's when the cell fills it),
    then their other devices at full panel size. Then the reference
    screens (screen.SCREENS). It opens on the first device that shows
    comics, else the first reference screen."""
    panels = {}
    for device in devices:
        panel = device.panel if getattr(device, "kind_of", None) else None
        if panel:
            panels[device.id] = (device.display_name, int(panel["w"]), int(panel["h"]))
    showing: list[dict[str, Any]] = []
    for page in pages:
        base, cells = _comics_cells(page)
        for device_id in getattr(page, "device_ids", None) or []:
            if device_id not in panels:
                continue
            name, pw, ph = panels[device_id]
            bw, bh = base or (pw, ph)
            for cw, ch in cells:
                width, height = round(cw * pw / bw), round(ch * ph / bh)
                if width >= 0.95 * pw and height >= 0.95 * ph:
                    width, height = pw, ph
                label = name if (width, height) == (pw, ph) else f"{name}, comics cell"
                showing.append({"name": label, "w": width, "h": height, "device": device_id})
        if not getattr(page, "device_ids", None):
            for cw, ch in cells:
                showing.append({"name": f"Comics cell on {page.name}", "w": cw, "h": ch})
    seen: set[tuple[str, int, int]] = set()
    own: list[dict[str, Any]] = []
    shown_on = {s.get("device") for s in showing}
    others = [
        {"name": name, "w": w, "h": h}
        for device_id, (name, w, h) in panels.items()
        if device_id not in shown_on
    ]
    for entry in showing + others:
        key = (entry["name"], entry["w"], entry["h"])
        if key not in seen and len(own) < MAX_OWN_SCREENS:
            seen.add(key)
            own.append({"name": entry["name"], "w": entry["w"], "h": entry["h"], "group": MINE})
    reference = [{"name": n, "w": w, "h": h, "group": REFERENCE} for n, w, h in screen.SCREENS]
    return own + reference, 0 if showing else len(own)


def _your_screens() -> tuple[list[dict[str, Any]], int]:
    """preview_screens() from the app's pages and devices (just the
    reference screens without an app to ask)."""
    try:
        page_store = current_app.config.get("PAGE_STORE")
        registry = current_app.config.get("DEVICE_REGISTRY")
        pages = page_store.list_active() if page_store is not None else []
        devices = registry.all() if registry is not None else []
    except Exception:
        pages, devices = [], []
    try:
        return preview_screens(pages, devices)
    except Exception:
        logger.warning("comics_strips: couldn't list the user's screens", exc_info=True)
        return preview_screens([], [])


def screens_view(
    config: Config,
    preview: list[dict[str, Any]],
    screens: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """What each screen shows on its first turn, and in words how the rest
    take turns, for the admin page's preview. ``screens`` defaults to the
    reference screens (screen.SCREENS)."""
    if screens is None:
        screens = [{"name": n, "w": w, "h": h, "group": REFERENCE} for n, w, h in screen.SCREENS]
    shown_items = [item for item in preview if item.get("pictures")]
    items = [
        screen.Item(
            (int(item["pictures"][0]["w"]), int(item["pictures"][0]["h"])),
            caption_lines(item["pictures"][0], config.captions),
        )
        for item in shown_items
    ]
    out = []
    for n, sc in enumerate(screens):
        name, width, height = sc["name"], sc["w"], sc["h"]
        turns = screen.turns(
            width,
            height,
            items,
            note=False,
            gap_min=config.gap_min,
            one_at_a_time=config.small_screens == "one",
        )
        chosen = turns[0] if turns else screen.Plan((), False, 0)
        shown = [shown_items[i] for i in chosen.show]
        names = ", ".join(item["label"] for item in shown)
        if not items:
            text = "Nothing to show yet."
        elif not chosen.rotating:
            text = f"Everything at once: {names}."
        else:
            steps = [" and ".join(shown_items[i]["label"] for i in turn.show) for turn in turns]
            text = f"Takes turns, one refresh each: {'; then '.join(steps)}."
        out.append(
            {
                "id": n,
                "name": name,
                "group": sc.get("group", REFERENCE),
                "w": width,
                "h": height,
                "text": text,
                "shown": [
                    {
                        "label": item["label"],
                        "picture": item["pictures"][0],
                        "caption": caption_lines(item["pictures"][0], config.captions),
                    }
                    for item in shown
                ],
            }
        )
    return out


# How the admin page's log marks an outcome: a problem.
LOG_TONES = {"error": "bad"}


def admin_view(store: Any, config: Config, tz: Any) -> dict[str, Any]:
    """Everything the admin page shows besides the forms."""
    today = _today().isoformat()
    records = store.sources()
    busy = _running_keys(store.root)

    strips: dict[str, dict[str, Any]] = {}
    for row in config.strips:
        found = row_source(row)
        if isinstance(found, Problem):
            entry: dict[str, Any] = {"chip": "Can't show", "tone": "bad", "line": found.message}
            entry.update(warning=found.message, pictures=[], state="invalid")
        else:
            entry = source_status(found, records.get(found.key) or {}, store, busy, today, tz)
        if not row.enabled:
            entry.update(chip="Off", tone="", warning="")
        entry["label"] = row.label or default_label(row.slug, row.mode)
        entry["kind"] = KIND_NAMES.get("manual" if row.mode == "upload" else row.mode, "")
        strips[row.slug] = entry

    wanted, problems = sources_for(config)
    preview = [{**strips[s.name], "label": s.label} for s in wanted]
    attention = [
        f"{item['label']} ({item['chip'].lower()})"
        for item in preview
        if item["chip"] in ("Stale", "Missing", "No picture")
    ]
    attention += [f"{p.name} ({p.message.rstrip('.').lower()})" for p in problems]
    lede: list[str] = []
    fetching = [item["label"] for item in preview if item["chip"] == "Fetching"]
    if fetching:
        lede.append(f"Fetching now: {', '.join(fetching)}.")
    elif not preview and not problems:
        lede.append("Nothing to show: add a strip or switch one on below.")
    elif attention:
        total = len(preview) + len(problems)
        verb = "needs" if len(attention) == 1 else "need"
        lede.append(f"{len(attention)} of {total} {verb} a look: {', '.join(attention)}.")
    else:
        lede.append(f"All {len(preview)} are up to date.")
    attempts = [float(r.get("last_attempt") or 0) for r in records.values()]
    if attempts and max(attempts) > 0:
        lede.append(f"Last fetch {_when(max(attempts), tz, today)}.")

    screens, default_screen = _your_screens()

    # The log, oldest first, as columns: when, source, outcome, what.
    log = []
    for entry in store.log():
        when = "?"
        with contextlib.suppress(TypeError, ValueError, OverflowError, OSError):
            when = datetime.fromtimestamp(float(entry["at"]), tz).strftime("%b %d %H:%M:%S")
        outcome = str(entry.get("outcome") or "").lower()
        log.append(
            {
                "when": when,
                "source": str(entry.get("source") or "-")[:40],
                "outcome": outcome,
                "tone": LOG_TONES.get(outcome, ""),
                "text": str(entry["text"]),
            }
        )
    return {
        "lede": " ".join(lede),
        "screens": screens_view(config, preview, screens),
        "screen_default": default_screen,
        "running": bool(busy),
        "preview": preview,
        "strips": strips,
        "problems": problems,
        "log": log,
    }


# The choices the admin page's selects offer, as (value, label).
MODE_CHOICES = (
    ("xkcd", "xkcd"),
    ("url", "Direct image URL"),
    ("upload", "Manual upload"),
)
PICK_CHOICES = (
    ("latest", "The latest comic"),
    ("random", "A random one from the archive, new each day"),
)
SMALL_SCREEN_CHOICES = (
    ("fit", "As many as fit, taking turns with the rest"),
    ("one", "One at a time, taking turns"),
)


def _stored_rows(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in values.get("strips") or [] if isinstance(row, dict)]


def _update(
    data_dir: Path, change: Callable[[dict[str, Any]], dict[str, Any] | None]
) -> dict[str, Any] | None:
    """Change config.json in one step under its lock (starting it first if
    there is none): ``change`` gets the values as stored now, not as they
    were when the request began (another tab may have changed them), and
    returns the new values, or None to leave them. They are checked as every
    save is (config_file.check, ConfigError when they don't fit). Returns
    what was written, or None."""
    ensure_settings(data_dir)
    written: dict[str, Any] | None = config_store(data_dir).update(change)
    return written


def _patch_rows(
    data_dir: Path, change: Callable[[list[dict[str, Any]]], list[dict[str, Any]] | None]
) -> bool:
    """_update for the strip rows alone: ``change`` gets the rows as stored
    now and returns the new rows, or None. True when they were written."""

    def patch(values: dict[str, Any]) -> dict[str, Any] | None:
        rows = change(_stored_rows(values))
        return None if rows is None else {**values, "strips": rows}

    return _update(data_dir, patch) is not None


def _set_row(
    slug: str, **values: Any
) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]] | None]:
    """A _patch_rows change setting ``values`` on the row for ``slug``, if
    that row is still there."""

    def change(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        row = next((r for r in rows if r.get("slug") == slug), None)
        if row is None:
            return None
        row.update(values)
        return rows

    return change


def _back(anchor: str = "strips") -> Response:
    return redirect(url_for(f"{PLUGIN_ID}_media.index", _anchor=anchor))


def _row_label(row: dict[str, Any]) -> str:
    slug = str(row.get("slug") or "")
    return str(row.get("label") or default_label(slug, str(row.get("mode") or "")))


def _keep_manual(data_dir: Path, row: dict[str, Any], picture: Any) -> None:
    """Keep ``picture`` as the strip's manual picture and switch the strip
    to it (Manual upload)."""
    store = images.Store(data_dir)
    slug = str(row["slug"])
    key = f"manual:{slug}"
    name = store.add(key, picture)
    now = time.time()
    today = _today().isoformat()
    stored = {"file": name, "w": picture.width, "h": picture.height, "bytes": len(picture.data)}

    def record(entry: dict[str, Any]) -> None:
        entry.update(
            pictures=[stored],
            fetched_on=today,
            last_success=now,
            last_attempt=now,
            error=None,
        )

    store.update(key, record)
    if not _patch_rows(data_dir, _set_row(slug, mode="upload")):
        logger.info("comics_strips: %s left the list while its picture was kept", slug)
    detail = f"{picture.width}×{picture.height} px, {_size([stored])}"
    log_entry(store, _row_label(row), "uploaded", f"uploaded: {detail}")


def _checked(name: str) -> bool:
    """A checkbox of the posted form: present means on."""
    return request.form.get(name) not in (None, "", "off", "false", "0")


def _queue_row(plugin: Any, row: dict[str, Any]) -> None:
    """Start fetching a strip that was just added or changed, so the page
    has its picture soon (a manual strip has nothing to fetch)."""
    found = row_source(
        StripRow(
            slug=str(row["slug"]),
            label=str(row.get("label") or ""),
            mode=str(row.get("mode") or "xkcd"),
            pick=str(row.get("pick") or "latest"),
            image_url=str(row.get("image_url") or ""),
        )
    )
    if not isinstance(found, Source) or not found.fetched or not row.get("enabled", True):
        return
    data_dir = Path(plugin.data_dir)
    config = config_from_settings(ensure_settings(data_dir))
    with _scope(plugin):
        _enqueue(data_dir, [found], _today(), force=True, wanted=wanted_keys(config))


def blueprint() -> Blueprint:
    bp = Blueprint(f"{PLUGIN_ID}_media", __name__, template_folder="templates")

    @bp.get("/")
    def index() -> str:
        data_dir = _plugin_data_dir()
        values = ensure_settings(data_dir)
        config = config_from_settings(values)
        store = images.Store(data_dir)
        with contextlib.suppress(Exception):
            store.prune_if_due(wanted_keys(config))
        view = admin_view(store, config, _admin_tz())
        # ?screen=N: the preview picked without JavaScript (the picker's
        # Show button); with it, the picker swaps the preview in place.
        picked = request.args.get("screen", type=int)
        if picked is not None and 0 <= picked < len(view["screens"]):
            view["screen_default"] = picked
        return render_template(
            f"{PLUGIN_ID}/index.html",
            plugin_id=PLUGIN_ID,
            endpoint=f"{PLUGIN_ID}_media",
            values=values,
            rows=_stored_rows(values),
            view=view,
            numbers=config_file.NUMBERS,
            mode_choices=MODE_CHOICES,
            pick_choices=PICK_CHOICES,
            small_screen_choices=SMALL_SCREEN_CHOICES,
            max_strips=MAX_STRIPS,
            upload_mb=images.MAX_UPLOAD_BYTES // (1024 * 1024),
        )

    def _posted(*names: str) -> dict[str, Any]:
        """The posted values of these fields; one not posted keeps its value."""
        return {name: request.form[name] for name in names if name in request.form}

    @bp.post("/layout")
    def save_layout() -> Response:
        change = {
            "captions": _checked("captions"),
            "status_note": _checked("status_note"),
            **_posted("small_screens", "gap_min", "gap", "gap_max"),
        }
        try:
            _update(_plugin_data_dir(), lambda values: {**values, **change})
        except config_file.ConfigError as err:
            flash(f"Not saved: {err}", "error")
        else:
            flash("Saved the layout.", "ok")
        return _back("layout")

    @bp.post("/fetch")
    def fetch_now() -> Response:
        plugin = _admin_plugin()
        data_dir = Path(plugin.data_dir)
        config = config_from_settings(ensure_settings(data_dir))
        wanted, _ = sources_for(config)
        sources = [s for s in wanted if s.fetched]
        store = images.Store(data_dir)
        if not sources:
            flash("Nothing to fetch: every strip shown is a manual picture, or none is on.", "warn")
        else:
            with _scope(plugin):
                _enqueue(store.root, sources, _today(), force=True, wanted=wanted_keys(config))
            names = ", ".join(s.label for s in sources)
            log_entry(store, "Fetch now", "started", f"asked for {names}")
            flash("Fetching today's comics now; the job tray shows how far it got.", "ok")
        return redirect(url_for(f"{PLUGIN_ID}_media.index", _anchor="today"))

    @bp.post("/strips/add")
    def add_strip() -> Response:
        plugin = _admin_plugin()
        data_dir = Path(plugin.data_dir)
        form = request.form
        mode = form.get("mode", "xkcd")
        label = form.get("label", "").strip()
        added: list[dict[str, Any]] = []

        def append(current: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            if len(current) >= MAX_STRIPS:
                return None
            taken = {str(r.get("slug")) for r in current}
            slug = config_file.new_slug(label or default_label("", mode) or mode, taken)
            row = {
                "enabled": True,
                "label": label,
                "slug": slug,
                "mode": mode,
                "pick": form.get("pick", "latest"),
                "image_url": form.get("image_url", "").strip(),
            }
            added.append(row)
            return [*current, row]

        try:
            written = _patch_rows(data_dir, append)
        except config_file.ConfigError as err:
            flash(f"Not added: {err}", "error")
            return _back()
        if not written:
            flash(f"The list holds at most {MAX_STRIPS} strips; remove one first.", "error")
            return _back()
        row = added[0]
        store = images.Store(data_dir)
        log_entry(store, _row_label(row), "added", f"added to the list ({row['mode']})")
        if row["mode"] == "upload":
            flash(f"Added {_row_label(row)}: upload its picture below.", "ok")
        else:
            _queue_row(plugin, row)
            flash(f"Added {_row_label(row)}; fetching it now.", "ok")
        return _back(f"strip-{row['slug']}")

    def _row_for(slug: str) -> tuple[Path, dict[str, Any]]:
        """The plugin's data_dir and the row for ``slug``; 404 when the
        list has no such strip."""
        data_dir = _plugin_data_dir()
        rows = _stored_rows(ensure_settings(data_dir))
        row = next((r for r in rows if r.get("slug") == slug), None)
        if row is None:
            abort(404)
        return data_dir, row

    def _change_row(
        slug: str,
        op: Callable[[list[dict[str, Any]], int], tuple[str, str] | None],
    ) -> Response:
        """Apply ``op(rows, index)`` to the row for ``slug`` in one step
        under config.json's lock. ``op`` changes ``rows`` in place and
        returns the message to flash and the anchor to go back to, or None
        when there is nothing to change. A change that doesn't fit is
        refused with the check's message, and nothing is saved."""
        data_dir, _ = _row_for(slug)
        said: list[tuple[str, str]] = []
        gone: list[bool] = []

        def change(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            index = next((i for i, r in enumerate(rows) if r.get("slug") == slug), None)
            if index is None:
                gone.append(True)
                return None
            outcome = op(rows, index)
            if outcome is None:
                return None
            said.append(outcome)
            return rows

        try:
            _patch_rows(data_dir, change)
        except config_file.ConfigError as err:
            flash(f"Not saved: {err}", "error")
            return _back(f"strip-{slug}")
        if gone:
            flash("That strip isn't in the list any more.", "warn")
            return _back()
        if not said:
            return _back(f"strip-{slug}")
        message, anchor = said[0]
        flash(message, "ok")
        return _back(anchor)

    @bp.post("/strips/<slug>/edit")
    def edit_strip(slug: str) -> Response:
        form = request.form
        fields = {
            "label": form.get("label", ""),
            "mode": form.get("mode", "xkcd"),
            "pick": form.get("pick", "latest"),
            "image_url": form.get("image_url", ""),
        }
        before: list[dict[str, Any]] = []

        def op(rows: list[dict[str, Any]], index: int) -> tuple[str, str]:
            before.append(dict(rows[index]))
            rows[index].update(fields)
            return f"Saved {_row_label(rows[index])}.", f"strip-{slug}"

        response = _change_row(slug, op)
        if before:
            with contextlib.suppress(Exception):
                _, row = _row_for(slug)
                source_fields = ("mode", "pick", "image_url")
                if any(before[0].get(k) != row.get(k) for k in source_fields):
                    _queue_row(_admin_plugin(), row)
        return response

    @bp.post("/strips/<slug>/move")
    def move_strip(slug: str) -> Response:
        step = -1 if request.form.get("direction") == "up" else 1

        def op(rows: list[dict[str, Any]], index: int) -> tuple[str, str] | None:
            other = index + step
            if not 0 <= other < len(rows):
                return None
            rows[index], rows[other] = rows[other], rows[index]
            where = "up" if step < 0 else "down"
            return f"Moved {_row_label(rows[other])} {where}.", f"strip-{slug}"

        return _change_row(slug, op)

    @bp.post("/strips/<slug>/switch")
    def switch_strip(slug: str) -> Response:
        enabled = request.form.get("enabled") == "on"

        def op(rows: list[dict[str, Any]], index: int) -> tuple[str, str]:
            rows[index]["enabled"] = enabled
            state = "on" if enabled else "off"
            return f"{_row_label(rows[index])} is switched {state}.", f"strip-{slug}"

        return _change_row(slug, op)

    @bp.post("/strips/<slug>/remove")
    def remove_strip(slug: str) -> Response:
        def op(rows: list[dict[str, Any]], index: int) -> tuple[str, str]:
            row = rows.pop(index)
            return f"Removed {_row_label(row)}.", "strips"

        return _change_row(slug, op)

    @bp.post("/strips/<slug>/upload")
    def upload_strip(slug: str) -> Response:
        # Refuse an oversized body before the form is parsed: parsing spools
        # every byte of it to disk first. The header decides when it is
        # sent; the parser's own limit catches a body without one.
        limit = images.MAX_UPLOAD_BYTES + UPLOAD_OVERHEAD_BYTES
        too_big = f"That picture is over {images.MAX_UPLOAD_BYTES // 1048576} MB."
        if request.content_length is not None and request.content_length > limit:
            flash(too_big, "error")
            return _back(f"strip-{slug}")
        request.max_content_length = limit
        data_dir, row = _row_for(slug)
        try:
            upload = request.files.get("file")
        except RequestEntityTooLarge:
            flash(too_big, "error")
            return _back(f"strip-{slug}")
        if upload is None or not upload.filename:
            flash("Choose a picture to upload first.", "error")
            return _back(f"strip-{slug}")
        try:
            raw = upload.stream.read(images.MAX_UPLOAD_BYTES + 1)
        finally:
            upload.close()  # a large upload is spooled to a temp file
        try:
            picture = images.validate_upload(raw)
        except ValueError as err:
            flash(str(err), "error")
            return _back(f"strip-{slug}")
        _keep_manual(data_dir, row, picture)
        flash(f"{_row_label(row)} shows the picture you uploaded.", "ok")
        return _back(f"strip-{slug}")

    @bp.get("/img/<name>")
    def image(name: str) -> Response:
        path = _store().image_path(name)
        if path is None:
            abort(404)
        response = send_file(path, conditional=True, max_age=365 * 86400)
        # Names are content hashes: a new strip is a new address.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    return bp
