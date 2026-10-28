"""xkcd's official JSON API: the latest comic, or one picked from the archive.

``https://xkcd.com/info.0.json`` is the latest comic and
``https://xkcd.com/<num>/info.0.json`` a numbered one. Each answer carries
the comic's number, title, alt text, date and the address of its picture.

This module only reads those answers. It is handed ``get_json(url)`` (server.py
supplies one that goes through the host's SSRF-guarded fetch), so it never
touches the network itself and is easy to test. ``get_json`` raises
:class:`NotFound` for a comic that has no page (HTTP 404) and
:class:`XkcdError` for anything else that went wrong; both carry a message
fit to show the user.

xkcd is by Randall Munroe and is licensed CC BY-NC 2.5; the widget names
xkcd.com beside every comic it draws.
"""

from __future__ import annotations

import random
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

SITE = "xkcd.com"
LATEST_URL = "https://xkcd.com/info.0.json"
COMIC_URL = "https://xkcd.com/{num}/info.0.json"
# Comic 404 is the joke "Not Found": it has no page and no JSON.
MISSING_NUMBER = 404
TITLE_MAX = 200
ALT_MAX = 1200
# A random pick that lands on a missing comic is retried this many times.
RANDOM_TRIES = 3


class XkcdError(Exception):
    """xkcd could not be read. The message is shown to the user."""


class NotFound(XkcdError):
    """xkcd has no comic by that number."""


GetJson = Callable[[str], Any]


@dataclass(frozen=True)
class Comic:
    num: int
    title: str
    alt: str
    img: str  # the picture's address
    date: str  # ISO day it was published, "" when xkcd didn't say

    @property
    def credit(self) -> str:
        """The attribution drawn beside the comic: ``xkcd.com/3234``."""
        return f"{SITE}/{self.num}"


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _number(value: Any) -> int | None:
    """A whole number from an int or a numeric string (xkcd sends the date
    parts as strings), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _picture_address(value: Any) -> str:
    """The picture's address if it is a web address on xkcd's own hosts
    (served over https), else ""."""
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return ""
    try:
        parts = urllib.parse.urlsplit(value)
        host = (parts.hostname or "").lower()
        _ = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not (host == SITE or host.endswith("." + SITE)):
        return ""
    return urllib.parse.urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))


def parse(data: Any) -> Comic:
    """A :class:`Comic` from one JSON answer, or :class:`XkcdError` when it
    isn't the answer xkcd gives (not an object, no number, no picture, a
    picture somewhere other than xkcd)."""
    if not isinstance(data, dict):
        raise XkcdError("xkcd sent an answer this widget can't read.")
    num = _number(data.get("num"))
    if num is None or num < 1:
        raise XkcdError("xkcd sent an answer without a comic number.")
    img = _picture_address(data.get("img"))
    if not img:
        raise XkcdError(f"xkcd comic {num} has no picture this widget can fetch.")
    day = ""
    year, month, dom = (_number(data.get(k)) for k in ("year", "month", "day"))
    if year and month and dom:
        try:
            day = date(year, month, dom).isoformat()
        except ValueError:
            day = ""
    title = _text(data.get("title"), TITLE_MAX) or _text(data.get("safe_title"), TITLE_MAX)
    return Comic(num=num, title=title, alt=_text(data.get("alt"), ALT_MAX), img=img, date=day)


def latest(get_json: GetJson) -> Comic:
    """The newest comic."""
    return parse(get_json(LATEST_URL))


def numbered(get_json: GetJson, num: int) -> Comic:
    """Comic ``num``; :class:`NotFound` when xkcd has none by that number."""
    if num < 1 or num == MISSING_NUMBER:
        raise NotFound(f"xkcd has no comic number {num}.")
    comic = parse(get_json(COMIC_URL.format(num=num)))
    if comic.num != num:
        raise XkcdError(f"xkcd answered with comic {comic.num} when asked for {num}.")
    return comic


def pick_number(newest: int, rng: random.Random) -> int:
    """A random comic number in 1..``newest``, never the missing 404."""
    choices = newest - (1 if 1 <= MISSING_NUMBER <= newest else 0)
    if choices < 1:
        return 1
    n = rng.randint(1, choices)
    return n + 1 if newest >= MISSING_NUMBER and n >= MISSING_NUMBER else n


def random_comic(get_json: GetJson, rng: random.Random) -> Comic:
    """A comic from the whole archive: the newest number is read first,
    then one at random up to it. A number xkcd doesn't have is asked again,
    a few times."""
    newest = latest(get_json).num
    failure: XkcdError = NotFound("xkcd didn't return a comic.")
    for _ in range(RANDOM_TRIES):
        try:
            return numbered(get_json, pick_number(newest, rng))
        except NotFound as err:
            failure = err
    raise failure
