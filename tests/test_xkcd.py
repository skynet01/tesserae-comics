"""The xkcd source: reading xkcd's JSON API (xkcd.py), and fetching through
the widget (server.py) with the host's guarded fetch faked (``net``)."""

from __future__ import annotations

import importlib.util
import random
import sys
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
xkcd = h.load("_comics_catalog_xkcd", "xkcd.py")
LATEST = "https://xkcd.com/info.0.json"


def _url(num: int) -> str:
    return f"https://xkcd.com/{num}/info.0.json"


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
