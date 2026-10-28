"""The xkcd source: reading xkcd's JSON API (xkcd.py), and fetching through
the widget (server.py) with the host's guarded fetch faked (``net``)."""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest


def _helpers() -> Any:
    name = "_comics_strips_test_helpers"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("_helpers.py"))
        assert spec is not None and spec.loader is not None
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


h = _helpers()
srv = h.load("_tesserae_plugins.comics_strips_test.server")
xkcd = srv.xkcd
images = srv.images
TODAY = date(2026, 10, 25)
LATEST = "https://xkcd.com/info.0.json"


def _url(num: int) -> str:
    return f"https://xkcd.com/{num}/info.0.json"


def _img(num: int) -> str:
    return f"https://imgs.xkcd.com/comics/comic_{num}.png"


def _source(pick: str = "latest", slug: str = "xkcd") -> Any:
    return srv.Source(key=f"xkcd:{slug}", kind="xkcd", name=slug, pick=pick)


# -- reading the API (xkcd.py) ----------------------------------------------------


def test_parse_reads_an_answer_as_xkcd_gives_it() -> None:
    comic = xkcd.parse(h.comic(3000, title="Fish", alt="  An   alt\ntext. ", day=(2026, 1, 9)))
    assert comic == xkcd.Comic(
        num=3000,
        title="Fish",
        alt="An alt text.",
        img="https://imgs.xkcd.com/comics/comic_3000.png",
        date="2026-01-09",
    )
    assert comic.credit == "xkcd.com/3000"
    # The title falls back to safe_title; a missing or impossible date is "".
    answer = {**h.comic(7), "title": "", "safe_title": "Safe", "month": "13"}
    assert (xkcd.parse(answer).title, xkcd.parse(answer).date) == ("Safe", "")
    # An http picture address is asked for over https.
    plain = xkcd.parse(h.comic(8, img="http://imgs.xkcd.com/comics/x.png"))
    assert plain.img == "https://imgs.xkcd.com/comics/x.png"


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (["not", "an", "object"], "can't read"),
        ({**h.comic(1), "num": "one"}, "without a comic number"),
        ({**h.comic(1), "num": 0}, "without a comic number"),
        ({**h.comic(1), "img": ""}, "no picture"),
        ({**h.comic(1), "img": "https://evil.example.com/x.png"}, "no picture"),
        ({**h.comic(1), "img": "https://xkcd.com.evil.example/x.png"}, "no picture"),
        ({**h.comic(1), "img": "file:///etc/passwd"}, "no picture"),
    ],
)
def test_parse_refuses_what_isnt_an_xkcd_answer(answer: Any, message: str) -> None:
    with pytest.raises(xkcd.XkcdError, match=message):
        xkcd.parse(answer)


def test_a_random_number_is_in_range_and_never_the_missing_404() -> None:
    rng = random.Random(1)
    picks = {xkcd.pick_number(410, rng) for _ in range(5000)}
    assert picks == set(range(1, 411)) - {404}
    assert xkcd.pick_number(1, rng) == 1
    assert xkcd.pick_number(0, rng) == 1


def test_numbered_refuses_404_and_a_mismatched_answer() -> None:
    with pytest.raises(xkcd.NotFound):
        xkcd.numbered(lambda url: pytest.fail("not asked"), 404)
    with pytest.raises(xkcd.XkcdError, match="answered with comic 9 when asked for 8"):
        xkcd.numbered(lambda url: h.comic(9), 8)


def test_random_comic_reads_the_newest_number_then_one_up_to_it() -> None:
    asked: list[str] = []

    def get_json(url: str) -> Any:
        asked.append(url)
        if url == LATEST:
            return h.comic(500)
        num = int(url.split("/")[3])
        if num == 77:
            raise xkcd.NotFound("gone")
        return h.comic(num)

    rng = random.Random(0)
    first = xkcd.random_comic(get_json, rng)
    assert asked[0] == LATEST and asked[1] == _url(first.num) and 1 <= first.num <= 500

    # A number xkcd doesn't have is asked again, a few times.
    class Fixed(random.Random):
        def __init__(self, picks: list[int]) -> None:
            super().__init__(0)
            self.picks = picks

        def randint(self, a: int, b: int) -> int:
            return self.picks.pop(0)

    assert xkcd.random_comic(get_json, Fixed([77, 12])).num == 12
    with pytest.raises(xkcd.NotFound):
        xkcd.random_comic(get_json, Fixed([77, 77, 77]))


# -- fetching through the widget ----------------------------------------------------------


def test_latest_is_fetched_and_kept_with_its_caption(net: Any, tmp_path: Path) -> None:
    net.json[LATEST] = h.comic(3000, title="Latest", alt="The alt text.")
    net.bytes[_img(3000)] = h.png((740, 300))
    store = images.Store(tmp_path)
    assert srv.refresh_source(store, _source(), TODAY)
    assert net.urls() == [LATEST, _img(3000)]
    # Both through the host's guard: JSON capped small, never a private address.
    json_kw = net.calls[0][2]
    assert json_kw["max_bytes"] == srv.JSON_MAX_BYTES
    assert net.calls[1][2]["allow_local"] is False
    record = store.sources()["xkcd:xkcd"]
    assert record["edition"] == "2026-10-23" and record["fetched_on"] == TODAY.isoformat()
    picture = record["pictures"][0]
    assert {k: picture[k] for k in ("num", "title", "alt", "credit", "w", "h")} == {
        "num": 3000,
        "title": "Latest",
        "alt": "The alt text.",
        "credit": "xkcd.com/3000",
        "w": 740,
        "h": 300,
    }
    payload = srv._payload([_source()], [], store.sources(), set(), store, TODAY.isoformat())
    strip = payload["strips"][0]
    assert (strip["label"], strip["title"], strip["alt"], strip["credit"]) == (
        "xkcd",
        "Latest",
        "The alt text.",
        "xkcd.com/3000",
    )
    assert store.log()[-1]["outcome"] == "ok"
    assert store.log()[-1]["text"].startswith("xkcd 3000, 1 picture")


def test_the_same_comic_twice_is_not_downloaded_again_and_is_not_an_error(
    net: Any, tmp_path: Path
) -> None:
    """xkcd posts Monday, Wednesday and Friday: on the day between, "latest"
    is the comic already shown."""
    net.json[LATEST] = h.comic(3000)
    net.bytes[_img(3000)] = h.png((740, 300))
    store = images.Store(tmp_path)
    assert srv.refresh_source(store, _source(), date(2026, 10, 24))
    first = store.sources()["xkcd:xkcd"]
    net.calls.clear()

    assert srv.refresh_source(store, _source(), TODAY)
    assert net.urls() == [LATEST]  # the picture is not fetched again
    record = store.sources()["xkcd:xkcd"]
    assert record["pictures"] == first["pictures"]
    assert record["fetched_on"] == TODAY.isoformat() and record["error"] is None
    assert not srv.is_due(_source(), record, TODAY.isoformat(), 0)
    state, _ = srv.freshness(record, True, False, TODAY.isoformat())
    assert state == "ok"
    assert store.log()[-1]["outcome"] == "unchanged"
    assert store.log()[-1]["text"].startswith("still xkcd 3000, nothing new to download")

    # Once the picture file is gone it is downloaded again.
    (store.image_dir / first["pictures"][0]["file"]).unlink()
    net.calls.clear()
    assert srv.refresh_source(store, _source(), TODAY)
    assert net.urls() == [LATEST, _img(3000)]

    # A new comic replaces it.
    net.json[LATEST] = h.comic(3001, title="Newer")
    net.bytes[_img(3001)] = h.png((500, 500))
    assert srv.refresh_source(store, _source(), TODAY)
    assert store.sources()["xkcd:xkcd"]["pictures"][0]["num"] == 3001


def test_random_is_the_same_all_day_and_new_the_next(net: Any, tmp_path: Path) -> None:
    net.json[LATEST] = h.comic(3000)
    for num in range(1, 3001):
        net.json[_url(num)] = h.comic(num)
        net.bytes[_img(num)] = h.png((300, 100))
    store = images.Store(tmp_path)
    source = _source("random")
    assert srv.refresh_source(store, source, TODAY)
    today_num = store.sources()["xkcd:xkcd"]["pictures"][0]["num"]
    assert net.urls()[0] == LATEST and net.urls()[1] == _url(today_num)
    # Asked again the same day (Fetch now): the same comic, nothing downloaded.
    net.calls.clear()
    assert srv.refresh_source(store, source, TODAY)
    assert "bytes" not in [k for k, _, _ in net.calls]
    assert store.sources()["xkcd:xkcd"]["pictures"][0]["num"] == today_num
    # Two random strips in one list don't show the same comic every day.
    other = srv.daily_rng(_source("random", "other"), TODAY).randint(1, 10**9)
    assert other != srv.daily_rng(source, TODAY).randint(1, 10**9)
    picks = {xkcd.pick_number(3000, srv.daily_rng(source, date(2026, 11, d))) for d in range(1, 8)}
    assert len(picks) > 1


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("http500", "xkcd answered HTTP 500."),
        ("http404", "xkcd has no such comic."),
        ("garbage", "xkcd sent an answer this widget can't read."),
        ("down", "Couldn't reach xkcd.com."),
        ("not-a-comic", "xkcd sent an answer without a comic number."),
        ("bad-picture", "xkcd sent an answer"),
    ],
)
def test_a_bad_response_keeps_the_last_good_comic_and_says_why(
    net: Any, tmp_path: Path, answer: str, message: str
) -> None:
    net.json[LATEST] = h.comic(2999)
    net.bytes[_img(2999)] = h.png((740, 300))
    store = images.Store(tmp_path)
    assert srv.refresh_source(store, _source(), date(2026, 10, 24))
    good = store.sources()["xkcd:xkcd"]["pictures"]
    net.json[LATEST] = {
        "http500": h.http_error(LATEST, 500),
        "http404": h.http_error(LATEST, 404),
        "garbage": json.JSONDecodeError("Expecting value", "<html>", 0),
        "down": TimeoutError("timed out"),
        "not-a-comic": {"hello": "world"},
        "bad-picture": h.comic(3000),
    }[answer]
    if answer == "bad-picture":
        net.bytes[_img(3000)] = b"<html>error</html>"
        message = "The download for xkcd wasn't a picture."
    assert not srv.refresh_source(store, _source(), TODAY)
    record = store.sources()["xkcd:xkcd"]
    assert record["error"] == message
    assert record["pictures"] == good
    assert record["retry"]["tries"] == 1
    state, since = srv.freshness(record, True, False, TODAY.isoformat())
    assert (state, since) == ("stale", "2026-10-24")
    assert store.log()[-1]["outcome"] == "error"


def test_an_old_host_without_the_guarded_fetch_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "net_guard", None)
    store = images.Store(tmp_path)
    assert not srv.refresh_source(store, _source(), TODAY)
    assert store.sources()["xkcd:xkcd"]["error"] == srv.TOO_OLD
    url = srv.url_source("mine", "https://pictures.example.org/a.png")
    assert not srv.refresh_source(store, url, TODAY)
    assert store.sources()[url.key]["error"] == srv.TOO_OLD
