"""comics_strips: storage, refresh, render, the admin page, fitting and turns.

The host's guarded fetch is faked (``net``), so nothing reaches the network.
xkcd's own tests are in test_xkcd.py.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient
from PIL import Image


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
images = srv.images
screen = srv.screen
PLUGIN = "comics_strips"
BASE = f"/plugins/{PLUGIN}"
TODAY = date(2026, 10, 25)
LATEST = "https://xkcd.com/info.0.json"
IMAGE = "https://pictures.example.org/strip.png"


def _row(slug: str, **values: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "label": "",
        "slug": slug,
        "mode": "url",
        "pick": "latest",
        "image_url": "",
        **values,
    }


def _configure(data_dir: Any, strips: list[dict[str, Any]], **values: Any) -> None:
    srv.config_store(data_dir).update(lambda v: {**v, **values, "strips": strips})


def _keep_picture(
    store: Any, key: str, size: tuple[int, int] = (60, 20), age: float = 0, **meta: Any
) -> str:
    picture = images.normalise(h.png(size))
    name = store.add(key, picture)
    when = time.time() - age
    store.update(
        key,
        lambda e: e.update(
            pictures=[{"file": name, "w": size[0], "h": size[1], **meta}],
            last_attempt=when,
            last_success=when,
            fetched_on=TODAY.isoformat(),
            error=None,
        ),
    )
    os.utime(store.image_dir / name, (when, when))
    return name


def _wait_done(data_dir: Path) -> Any:
    run = srv._RUNS[str(data_dir)]
    assert run.thread is not None
    run.thread.join(5)
    assert run.done and run.active is None
    return run


def _url_source(url: str = IMAGE, name: str = "strip") -> Any:
    found = srv.url_source(name, url)
    assert isinstance(found, srv.Source)
    return found


# -- pictures ---------------------------------------------------------------------


def test_normalise_flattens_transparency_and_keeps_jpeg() -> None:
    gif = io.BytesIO()
    Image.new("RGBA", (40, 20), (0, 0, 0, 0)).save(gif, format="GIF")
    picture = images.normalise(gif.getvalue())
    assert picture.ext == "png"
    with Image.open(io.BytesIO(picture.data)) as img:
        assert img.convert("RGB").getpixel((5, 5)) == (255, 255, 255)
    assert images.normalise(h.jpeg()).ext == "jpg"
    big = images.normalise(h.png((3000, 600)))
    assert (big.width, big.height) == (2400, 480)
    with pytest.raises(ValueError):
        images.normalise(b"<html>an error page</html>")


def test_store_names_are_validated(tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    name = store.add("xkcd:xkcd", images.normalise(h.png()))
    assert store.image_path(name) == tmp_path / "images" / name
    assert store.image_path("../state.json") is None
    assert store.image_path("x.png") is None


@pytest.mark.parametrize("fmt", ["PNG", "GIF", "WEBP", "JPEG"])
def test_picture_sizes_are_read_from_the_header(fmt: str) -> None:
    buf = io.BytesIO()
    Image.new("RGB", (123, 45), "white").save(buf, format=fmt)
    assert images.header_size(buf.getvalue()) == (123, 45)


def test_a_huge_picture_is_refused_before_pillow_opens_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(images, "MAX_PIXELS", 1000)
    opened: list[bool] = []
    monkeypatch.setattr(images.Image, "open", lambda *a, **k: opened.append(True))
    with pytest.raises(ValueError, match="too many pixels"):
        images.normalise(h.png((100, 100)))
    assert opened == []


# -- refreshing a source ------------------------------------------------------------


def test_is_due() -> None:
    source = _url_source()
    now = 1_000_000.0
    today = "2026-10-25"
    assert srv.is_due(source, None, today, now)
    done = {"fetched_on": today, "last_attempt": now - 10, "pictures": [1]}
    assert not srv.is_due(source, done, today, now)
    yesterday = dict(done, fetched_on="2026-10-24")
    assert srv.is_due(source, yesterday, today, now)
    backing_off = dict(yesterday, retry=srv.lifecycle.failed(None, today, now - 60))
    assert not srv.is_due(source, backing_off, today, now)  # failed a minute ago
    assert srv.is_due(source, backing_off, today, now + srv.lifecycle.RETRY_FIRST_S)


def test_a_direct_image_url_goes_through_the_guarded_fetch(net: Any, tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    source = _url_source()
    net.bytes[IMAGE] = h.png((300, 100))
    assert srv.refresh_source(store, source, TODAY)
    [(kind, url, kw)] = net.calls
    assert (kind, url) == ("bytes", IMAGE)
    assert kw["allow_local"] is False and kw["max_bytes"] == srv.IMAGE_MAX_BYTES
    record = store.sources()[source.key]
    assert record["fetched_on"] == TODAY.isoformat() and record["pictures"][0]["w"] == 300


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ("blocked", "isn't on the public web"),
        ("http", "answered HTTP 403"),
        ("big", "over the size"),
        ("down", "Couldn't reach pictures.example.org."),
    ],
)
def test_a_failed_download_says_why(net: Any, tmp_path: Path, error: str, message: str) -> None:
    from app import net_guard

    raised = {
        "blocked": net_guard.BlockedURLError("host is loopback"),
        "http": h.http_error(IMAGE, 403),
        "big": ValueError("response exceeds cap"),
        "down": TimeoutError("timed out"),
    }[error]
    net.bytes[IMAGE] = raised
    store = images.Store(tmp_path)
    assert not srv.refresh_source(store, _url_source(), TODAY)
    assert message in store.sources()[_url_source().key]["error"]


def test_failed_refresh_keeps_last_good_and_reports_stale(net: Any, tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    source = _url_source()
    net.bytes[IMAGE] = h.png()
    assert srv.refresh_source(store, source, date(2026, 10, 24))
    first = store.sources()[source.key]["pictures"]

    net.bytes[IMAGE] = b"<html>sorry</html>"
    assert not srv.refresh_source(store, source, TODAY)
    record = store.sources()[source.key]
    assert record["pictures"] == first
    assert "wasn't a picture" in record["error"]

    payload = srv._payload([source], [], store.sources(), set(), store, TODAY.isoformat())
    assert payload["strips"][0]["src"].endswith(first[0]["file"])
    assert payload["notes"] == [
        {"label": "Strip", "state": "stale", "since": "2026-10-24", "message": record["error"]}
    ]
    assert "error" not in payload


def test_storage_failure_is_contained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A full disk while recording fails the source, never the worker."""
    store = images.Store(tmp_path)

    def broken(*_a: Any, **_k: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "update", broken)
    assert not srv.refresh_source(store, _url_source(), TODAY)


def test_crashed_worker_ends_its_run_and_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(srv, "refresh_source", explode)
    source = _url_source()
    first = srv._enqueue(tmp_path, [source], TODAY)
    _wait_done(tmp_path)
    assert srv._running_keys(tmp_path) == set()

    # A run left "not done" by a thread that died anyway is not reused.
    first.done = False
    first.active = source
    monkeypatch.setattr(srv, "refresh_source", lambda *a, **k: True)
    second = srv._enqueue(tmp_path, [source], TODAY)
    assert second is not first
    _wait_done(tmp_path)


def test_reloaded_module_shares_the_running_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    again = h.load("_comics_strips_test_reload.server")
    assert again._RUNS is srv._RUNS
    assert again._RUNS_LOCK is srv._RUNS_LOCK
    assert again.images._LOCK is images._LOCK

    gate = threading.Event()
    calls: list[str] = []

    def slow(_store: Any, source: Any, *_a: Any, **_k: Any) -> bool:
        gate.wait(5)
        calls.append(source.key)
        return True

    monkeypatch.setattr(srv, "refresh_source", slow)
    a = srv.Source("url:a", "url", "a", "https://pictures.example.org/a.png")
    b = again.Source("url:b", "url", "b", "https://pictures.example.org/b.png")
    run = srv._enqueue(tmp_path, [a], TODAY)
    # The reloaded code queues onto the old worker instead of starting another.
    assert again._enqueue(tmp_path, [b], TODAY) is run
    assert run.keys() == {"url:a", "url:b"}
    gate.set()
    _wait_done(tmp_path)
    assert calls == ["url:a", "url:b"]
    assert again._running_keys(tmp_path) == set()


def test_finished_source_is_not_fetched_again_from_a_stale_snapshot(
    net: Any, tmp_path: Path
) -> None:
    store = images.Store(tmp_path)
    source = _url_source()
    net.bytes[IMAGE] = h.png()
    assert srv.refresh_source(store, source, TODAY)
    net.calls.clear()
    before = store.sources()[source.key]

    # A render that read the state before that landed queues it again.
    srv._enqueue(tmp_path, [source], TODAY)
    _wait_done(tmp_path)
    assert net.calls == []
    assert store.sources()[source.key]["attempts"] == before["attempts"]

    # And a live run that already did it won't take it back.
    run = srv._Run(data_dir=tmp_path, today=TODAY, completed={source.key})
    gate = threading.Event()
    run.thread = threading.Thread(target=gate.wait, args=(5,), daemon=True)
    run.thread.start()
    srv._RUNS[str(tmp_path)] = run
    try:
        assert srv._enqueue(tmp_path, [source], TODAY) is run
        assert run.queue == []
        srv._enqueue(tmp_path, [source], TODAY, force=True)
        assert [s.key for s, _ in run.queue] == [source.key]
    finally:
        run.queue.clear()
        gate.set()
        run.thread.join(5)
        run.done = True


def test_a_failing_source_backs_off_and_keeps_its_last_good_picture(
    net: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = images.Store(tmp_path)
    source = _url_source()
    net.bytes[IMAGE] = h.png()
    assert srv.refresh_source(store, source, date(2026, 10, 24))
    good = store.sources()[source.key]["pictures"]
    net.bytes[IMAGE] = TimeoutError("down")
    now = time.time()
    for n in range(1, srv.lifecycle.TRIES_PER_DAY + 1):
        assert not srv.refresh_source(store, source, TODAY)
        record = store.sources()[source.key]
        assert record["retry"]["tries"] == n and record["pictures"] == good
    assert not srv.is_due(source, record, TODAY.isoformat(), now + 20 * 3600)
    assert srv.is_due(source, record, "2026-10-26", now + 20 * 3600)
    log = store.log()[-1]
    assert log["outcome"] == "error" and "no more tries today" in log["text"]
    assert "the last good picture stays up" in log["text"]


def test_the_log_records_each_fetch(net: Any, tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    net.bytes[IMAGE] = h.png()
    assert srv.refresh_source(store, _url_source(), TODAY)
    missing = _url_source("https://pictures.example.org/nope.png", "other")
    assert not srv.refresh_source(store, missing, TODAY)
    log = store.log()
    assert [(e["source"], e["outcome"]) for e in log] == [("Strip", "ok"), ("Other", "error")]
    assert re.fullmatch(r"2026-10-25, 1 picture, \d+ KB in \d+\.\d s", log[0]["text"])
    assert log[1]["text"].startswith("Couldn't reach pictures.example.org.")
    for i in range(images.LOG_LINES + 3):
        store.append_log({"at": float(i), "source": "x", "outcome": "ok", "text": str(i)})
    assert len(store.log()) == images.LOG_LINES
    assert store.log()[-1]["text"] == str(images.LOG_LINES + 2)


# -- the widget end to end -----------------------------------------------------------


def _plugin(client: FlaskClient) -> Any:
    return client.application.config["PLUGIN_REGISTRY"].get(PLUGIN)


def _module(client: FlaskClient) -> Any:
    return _plugin(client).server_module


def _settings(client: FlaskClient) -> dict[str, Any]:
    """What config.json holds now (normalised), {} when there is none."""
    return _module(client).config_store(_plugin(client).data_dir).read() or {}


def _store(client: FlaskClient) -> Any:
    return _module(client).images.Store(Path(_plugin(client).data_dir))


def _messages(client: FlaskClient) -> list[str]:
    with client.session_transaction() as session:
        return [text for _, text in session.get("_flashes", [])]


def _xkcd_routes(net: Any, num: int = 3000) -> None:
    net.json[LATEST] = h.comic(num, title="Latest", alt="The alt text.")
    net.bytes[f"https://imgs.xkcd.com/comics/comic_{num}.png"] = h.png((740, 300))


@pytest.mark.parametrize("size", ["sm", "md", "lg"])
def test_widget_renders(
    client: FlaskClient, net: Any, monkeypatch: pytest.MonkeyPatch, size: str
) -> None:
    _xkcd_routes(net)
    monkeypatch.setattr(_module(client), "_today", lambda: TODAY)
    resp = client.get(f"/_test/render?plugin={PLUGIN}&size={size}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'data-plugin="{PLUGIN}"' in body
    assert f"{BASE}/img/" in body
    # Pictures are served read-only from data_dir.
    state = json.loads((Path(_plugin(client).data_dir) / "state.json").read_text())
    picture = state["sources"]["xkcd:xkcd"]["pictures"][0]
    assert (picture["num"], picture["title"], picture["credit"]) == (
        3000,
        "Latest",
        "xkcd.com/3000",
    )
    with client.get(f"{BASE}/img/{picture['file']}") as img:
        assert img.status_code == 200 and img.data[:4] == b"\x89PNG"
    with client.get(f"{BASE}/img/..%2Fstate.json") as missing:
        assert missing.status_code == 404


def test_fetch_answers_from_disk_while_refresh_runs(
    net: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "_today", lambda: TODAY)
    monkeypatch.setattr(srv, "FIRST_RENDER_WAIT_S", 0.0)
    release = threading.Event()
    real_work = srv._work
    monkeypatch.setattr(srv, "_work", lambda run: (release.wait(5), real_work(run)))
    _xkcd_routes(net)
    ctx = {"data_dir": str(tmp_path)}

    first = srv.fetch({}, {}, ctx=ctx)
    assert first["strips"] == []
    assert first["updating"] and first["live_refresh_s"] == srv.LIVE_REFRESH_S
    assert first["live_pending"] == 1

    release.set()
    _wait_done(tmp_path)
    second = srv.fetch({}, {}, ctx=ctx)
    assert len(second["strips"]) == 1 and "live_refresh_s" not in second
    assert second["layout"]["captions"] is True
    started = time.time()
    srv.fetch({}, {}, ctx=ctx)
    assert time.time() - started < 1  # nothing due: no new work, no waiting


def test_no_strips_is_an_error(tmp_path: Path) -> None:
    _configure(tmp_path, [])
    assert "Turn on a strip" in srv.fetch({}, {}, ctx={"data_dir": str(tmp_path)})["error"]
    _configure(tmp_path, [_row("broken")])
    out = srv.fetch({}, {}, ctx={"data_dir": str(tmp_path)})
    assert out["error"] == "Broken: Give it an image URL first."


def test_a_fresh_install_starts_with_the_latest_xkcd(tmp_path: Path) -> None:
    values = srv.ensure_settings(tmp_path)
    assert [(r["slug"], r["mode"], r["pick"]) for r in values["strips"]] == [
        ("xkcd", "xkcd", "latest")
    ]
    assert json.loads((tmp_path / "config.json").read_text())["version"] == 1


# -- the admin page -------------------------------------------------------------------


def test_admin_page_lists_strips_with_status_and_tools(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module(client)
    monkeypatch.setattr(module, "_enqueue", lambda *a, **k: None)
    data_dir = _plugin(client).data_dir
    store = _store(client)
    _keep_picture(
        store, "xkcd:xkcd", (740, 300), num=3000, title="Latest", alt="Alt.", credit="xkcd.com/3000"
    )
    store.update("xkcd:xkcd", lambda e: e.update(edition="2026-10-23"))
    module.config_store(data_dir).update(
        lambda v: {**v, "strips": [*v["strips"], _row("broken", label="Broken one")]}
    )
    body = client.get(f"{BASE}/").get_data(as_text=True)
    assert "xkcd 3000, “Latest” (Oct 23)" in body
    assert "xkcd.com/3000" in body and "Alt." in body
    assert "Give it an image URL first." in body
    assert 'id="strip-xkcd"' in body and 'id="strip-broken"' in body
    assert "CC BY-NC 2.5" in body
    assert "1 of 2 needs a look: Broken one (give it an image url first)." in body


def test_adding_strips_of_each_source(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module(client)
    queued: list[str] = []
    monkeypatch.setattr(
        module, "_enqueue", lambda root, sources, *a, **k: queued.extend(s.key for s in sources)
    )
    client.get(f"{BASE}/")
    resp = client.post(f"{BASE}/strips/add", data={"mode": "xkcd", "pick": "random"})
    assert resp.location == f"{BASE}/#strip-xkcd-2"
    client.post(f"{BASE}/strips/add", data={"mode": "url", "label": "My Strip", "image_url": IMAGE})
    client.post(f"{BASE}/strips/add", data={"mode": "upload", "label": "My Strip"})
    rows = _settings(client)["strips"]
    assert [(r["slug"], r["mode"], r["pick"]) for r in rows] == [
        ("xkcd", "xkcd", "latest"),
        ("xkcd-2", "xkcd", "random"),
        ("my-strip", "url", "latest"),
        ("my-strip-2", "upload", "latest"),
    ]
    # The fetched ones start fetching at once; an upload has nothing to fetch.
    assert queued == ["xkcd:xkcd-2", srv.url_source("my-strip", IMAGE).key]
    assert [e["outcome"] for e in _store(client).log()] == ["added"] * 3


def test_adding_refuses_a_bad_address_and_stops_at_the_most_strips(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_module(client), "_enqueue", lambda *a, **k: None)
    client.get(f"{BASE}/")
    client.post(f"{BASE}/strips/add", data={"mode": "url", "image_url": "ftp://x/y.png"})
    assert any("Image URL must be a web address" in m for m in _messages(client))
    assert len(_settings(client)["strips"]) == 1
    for _ in range(srv.MAX_STRIPS):
        client.post(f"{BASE}/strips/add", data={"mode": "xkcd"})
    assert len(_settings(client)["strips"]) == srv.MAX_STRIPS
    assert any("holds at most" in m for m in _messages(client))
    assert "The list holds at most" in client.get(f"{BASE}/").get_data(as_text=True)


def test_upload_makes_a_manual_strip(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module(client)
    monkeypatch.setattr(module, "_today", lambda: TODAY)
    client.get(f"{BASE}/")
    data = {"file": (io.BytesIO(h.png((640, 200), "RGBA")), "mine.png")}
    resp = client.post(f"{BASE}/strips/xkcd/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert _settings(client)["strips"][0]["mode"] == "upload"
    record = _store(client).sources()["manual:xkcd"]
    assert record["pictures"][0]["w"] == 640
    # Every cell now shows the manual picture, and never fetches it.
    monkeypatch.setattr(module, "_enqueue", lambda *a, **k: pytest.fail("nothing to fetch"))
    out = module.fetch({}, {}, ctx={"data_dir": _plugin(client).data_dir})
    assert out["strips"][0]["src"].startswith(f"{BASE}/img/manual-xkcd-")
    assert "credit" not in out["strips"][0]
    assert ("uploaded", "xkcd") in [(e["outcome"], e["source"]) for e in _store(client).log()]
    # Manual pictures are never pruned for age.
    _store(client).prune(now=time.time() + 400 * 86400)
    assert "manual:xkcd" in _store(client).sources()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "Choose a picture"),
        (b"<html>not a picture</html>", "isn't a picture"),
        # Over the cap (lowered here: werkzeug's test client leaves a request
        # body over 500 KB in an unclosed temp file).
        (b"x" * 2000, "That picture is over"),
    ],
)
def test_upload_refuses_what_is_not_a_usable_picture(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, payload: bytes, message: str
) -> None:
    monkeypatch.setattr(_module(client).images, "MAX_UPLOAD_BYTES", 1000)
    client.get(f"{BASE}/")
    data = {"file": (io.BytesIO(payload), "x.png" if payload else "")}
    client.post(f"{BASE}/strips/xkcd/upload", data=data, content_type="multipart/form-data")
    assert any(message in m for m in _messages(client))
    assert "manual:xkcd" not in _store(client).sources()
    assert _settings(client)["strips"][0]["mode"] == "xkcd"


def test_upload_refuses_too_many_pixels(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_module(client).images, "MAX_PIXELS", 1000)
    client.get(f"{BASE}/")
    data = {"file": (io.BytesIO(h.png((100, 100))), "big.png")}
    client.post(f"{BASE}/strips/xkcd/upload", data=data, content_type="multipart/form-data")
    assert any("isn't a picture this widget can use" in m for m in _messages(client))
    assert client.post(f"{BASE}/strips/nosuch/upload").status_code == 404


def test_an_oversized_upload_is_refused_before_the_form_is_read(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module(client)
    monkeypatch.setattr(module.images, "MAX_UPLOAD_BYTES", 100)
    monkeypatch.setattr(module, "UPLOAD_OVERHEAD_BYTES", 100)
    client.get(f"{BASE}/")
    data = {"file": (io.BytesIO(b"x" * 400), "big.png")}
    resp = client.post(f"{BASE}/strips/xkcd/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert any("That picture is over" in m for m in _messages(client))


def test_fetch_now_queues_every_fetched_source_forced(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module(client)
    queued: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(
        module,
        "_enqueue",
        lambda root, sources, today, force=False, **_: queued.append(
            ([s.key for s in sources], force)
        ),
    )
    module.config_store(_plugin(client).data_dir).update(
        lambda v: {
            **v,
            "strips": [
                _row("xkcd", mode="xkcd"),
                _row("mine", image_url=IMAGE),
                _row("upload", mode="upload"),
                _row("off", mode="xkcd", enabled=False),
            ],
        }
    )
    resp = client.post(f"{BASE}/fetch")
    assert resp.location == f"{BASE}/#today"
    assert queued == [(["xkcd:xkcd", srv.url_source("mine", IMAGE).key], True)]
    assert _store(client).log()[-1]["source"] == "Fetch now"


def test_every_change_is_a_plain_form_that_works_without_javascript(
    client: FlaskClient,
) -> None:
    """Each card saves through a form of its own, no form is nested in
    another, every control sits in the form it posts with, and the page's
    script is the plugin's own."""
    client.get(f"{BASE}/")
    body = client.get(f"{BASE}/").get_data(as_text=True)
    main = body[body.index("<main") : body.index("</main>")]
    forms = re.findall(r"<form\b(.*?)>(.*?)</form>", main, re.DOTALL)
    assert all("<form" not in inner for _, inner in forms)  # nothing nested
    assert ' form="' not in main
    posts = sorted(re.search(r'action="([^"]+)"', attrs).group(1) for attrs, _ in forms)
    expected = {f"{BASE}/layout", f"{BASE}/fetch", f"{BASE}/strips/add", f"{BASE}/"}
    for op in ("move", "edit", "upload"):
        expected.add(f"{BASE}/strips/xkcd/{op}")
    assert set(posts) == expected
    for attrs, _ in forms:
        method = re.search(r'method="(\w+)"', attrs).group(1)
        assert method == ("get" if f'action="{BASE}/"' in attrs else "post")
    assert f"{BASE}/static/admin.js" in body
    # The screen picker works without JavaScript too: ?screen= picks one.
    assert "<noscript><button" in main
    picked = client.get(f"{BASE}/?screen=2").get_data(as_text=True)
    assert '<figure class="cx-screen" data-screen="2">' in picked
    assert re.search(r'<option value="2" selected>', picked)
    fallback = client.get(f"{BASE}/?screen=99").get_data(as_text=True)
    assert '<figure class="cx-screen" data-screen="0">' in fallback


def test_rows_move_switch_edit_and_go_with_plain_posts(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(
        _module(client),
        "_enqueue",
        lambda root, sources, *a, **k: queued.extend(s.key for s in sources),
    )
    _module(client).config_store(_plugin(client).data_dir).update(
        lambda v: {
            **v,
            "strips": [_row("a", mode="xkcd"), _row("b", mode="xkcd"), _row("c", mode="xkcd")],
        }
    )

    def slugs() -> list[str]:
        return [r["slug"] for r in _settings(client)["strips"]]

    resp = client.post(f"{BASE}/strips/c/move", data={"direction": "up"})
    assert resp.location == f"{BASE}/#strip-c" and slugs() == ["a", "c", "b"]
    client.post(f"{BASE}/strips/a/move", data={"direction": "up"})  # already first
    assert slugs() == ["a", "c", "b"]
    client.post(f"{BASE}/strips/b/switch", data={"enabled": "off"})
    assert [r["enabled"] for r in _settings(client)["strips"]] == [True, True, False]
    client.post(
        f"{BASE}/strips/a/edit",
        data={"label": "Morning", "mode": "url", "pick": "latest", "image_url": IMAGE},
    )
    row = _settings(client)["strips"][0]
    assert (row["label"], row["mode"], row["image_url"]) == ("Morning", "url", IMAGE)
    # Changing where a strip comes from fetches it at once; a new label alone doesn't.
    assert queued == [srv.url_source("a", IMAGE).key]
    client.post(
        f"{BASE}/strips/a/edit",
        data={"label": "Morning!", "mode": "url", "pick": "latest", "image_url": IMAGE},
    )
    assert len(queued) == 1
    resp = client.post(f"{BASE}/strips/c/remove")
    assert resp.location == f"{BASE}/#strips" and slugs() == ["a", "b"]
    assert client.post(f"{BASE}/strips/c/remove").status_code == 404
    assert "Removed C." in _messages(client)


def test_a_strip_edit_that_does_not_fit_is_refused(client: FlaskClient) -> None:
    client.get(f"{BASE}/")
    before = _settings(client)
    for data, message in (
        ({"mode": "url", "image_url": "not an address"}, "Image URL must be a web address"),
        ({"mode": "comics"}, "isn't one of the choices for Source"),
        ({"mode": "xkcd", "pick": "oldest"}, "isn't one of the choices for Which comic"),
        ({"label": "x" * 61}, "longer than 60"),
    ):
        client.post(f"{BASE}/strips/xkcd/edit", data=data)
        assert any(message in m for m in _messages(client)), message
        assert _settings(client) == before


def test_the_layout_card_saves_and_refuses_what_does_not_fit(client: FlaskClient) -> None:
    client.get(f"{BASE}/")
    body = client.get(f"{BASE}/").get_data(as_text=True)
    layout = body[body.index('id="layout"') : body.index('id="log"')]
    assert "On small screens" in layout
    assert re.search(r"<legend[^>]*>Space between comics", layout)
    labels = re.findall(r'<label for="cx-(gap(?:_min|_max)?)">(\w+)', layout)
    assert labels == [("gap_min", "Smallest"), ("gap", "Usual"), ("gap_max", "Largest")]
    assert "Out-of-date note" in layout and "xkcd captions" in layout
    resp = client.post(
        f"{BASE}/layout", data={"small_screens": "one", "gap": "40", "status_note": "on"}
    )
    assert resp.location == f"{BASE}/#layout"
    values = _settings(client)
    assert (values["small_screens"], values["gap"], values["captions"], values["status_note"]) == (
        "one",
        40,
        False,
        True,
    )
    client.post(f"{BASE}/layout", data={"gap": "999"})
    assert any("Usual space must be between 0 and 200" in m for m in _messages(client))
    assert _settings(client)["gap"] == 40


def test_the_pictures_on_the_admin_page_keep_a_mount_prefix(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_module(client), "_enqueue", lambda *a, **k: None)
    client.get(f"{BASE}/")
    _keep_picture(_store(client), "xkcd:xkcd", (740, 300))
    body = client.get(f"{BASE}/", environ_overrides={"SCRIPT_NAME": "/tess"}).get_data(as_text=True)
    srcs = re.findall(r'<img[^>]*src="([^"]+)"', body)
    assert srcs and all(s.startswith(f"/tess{BASE}/img/") for s in srcs)


def test_the_admin_page_shows_the_log(client: FlaskClient) -> None:
    client.get(f"{BASE}/")
    assert "Nothing fetched yet." in client.get(f"{BASE}/").get_data(as_text=True)
    _store(client).append_log({"at": 1.0e9, "source": "xkcd", "outcome": "error", "text": "down"})
    body = client.get(f"{BASE}/").get_data(as_text=True)
    assert "cx-log-line--bad" in body and "ERROR" in body and "down" in body


# -- config.json ----------------------------------------------------------------------


def test_config_updates_are_one_step_and_atomic(tmp_path: Path) -> None:
    store = srv.config_store(tmp_path)
    store.load()
    threads = [
        threading.Thread(
            target=store.update,
            args=(lambda v, n=n: {**v, "strips": [*v["strips"], _row(f"s{n}", mode="xkcd")]},),
        )
        for n in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(store.read()["strips"]) == 7  # nothing lost to a race
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    with pytest.raises(srv.config_file.ConfigError):
        store.update(lambda v: {**v, "strips": [_row("a"), _row("a")]})
    assert len(store.read()["strips"]) == 7


def test_a_damaged_config_file_reads_as_the_defaults_until_the_next_save(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{not json")
    store = srv.config_store(tmp_path)
    assert store.read() == srv.config_file.defaults()
    assert (tmp_path / "config.json").read_text() == "{not json"
    store.update(lambda v: {**v, "gap": 10})
    assert json.loads((tmp_path / "config.json").read_text())["gap"] == 10


def test_a_hand_edited_file_cannot_break_a_render(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "strips": [
                    {"slug": "Bad Slug!"},
                    {"slug": "ok", "mode": "gocomics", "pick": "oldest", "image_url": 5},
                    {"slug": "ok"},
                    "junk",
                ],
                "gap": "lots",
                "small_screens": "huge",
            }
        )
    )
    values = srv.config_store(tmp_path).read()
    assert values["strips"] == [
        {
            "enabled": True,
            "label": "",
            "slug": "ok",
            "mode": "xkcd",
            "pick": "latest",
            "image_url": "5",
        }
    ]
    assert (values["gap"], values["small_screens"]) == (32, "fit")


def test_new_slugs_are_unique_and_tidy() -> None:
    new = srv.config_file.new_slug
    assert new("My Strip!", set()) == "my-strip"
    assert new("My Strip", {"my-strip", "my-strip-2"}) == "my-strip-3"
    assert new("", set()) == "strip"
    assert srv.config_file.SLUG_RE.match(new("ÜBER  ++ comic", set()))


# -- cleanup ------------------------------------------------------------------------------


def test_cleanup_forgets_what_the_settings_no_longer_name(tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    hour = images.PRUNE_GRACE_S + 60
    shown = _keep_picture(store, "xkcd:xkcd", age=hour)
    removed = _keep_picture(store, "xkcd:old", (61, 20), age=hour)
    fresh = _keep_picture(store, "url:abc", (62, 20))  # removed a minute ago
    orphan_manual = _keep_picture(store, "manual:old", (63, 20), age=hour)
    kept_manual = _keep_picture(store, "manual:xkcd", (64, 20), age=40 * 86400)
    wanted = {"xkcd:xkcd", "manual:xkcd"}
    now = time.time()
    # The records the settings no longer name go (url:abc's only once
    # untouched for the grace period); their pictures are now unused, and
    # the grace counts from now.
    assert store.prune(wanted=wanted, now=now) == []
    assert set(store.sources()) == {"xkcd:xkcd", "manual:xkcd", "url:abc"}
    gone = store.prune(wanted=wanted, now=now + images.PRUNE_GRACE_S + 60)
    assert sorted(gone) == sorted([removed, orphan_manual])
    for name in (shown, kept_manual, fresh):
        assert (store.image_dir / name).exists()


def test_cleanup_keeps_the_folder_under_budget_but_never_what_is_shown(tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    shown = _keep_picture(store, "xkcd:xkcd", (400, 300), age=10 * 60)
    now = time.time()
    spare, ledger = [], {}
    for n in range(3):
        path = store.image_dir / f"xkcd-old-{n:012x}.png"
        path.write_bytes(b"x" * 1000)
        spare.append(path.name)
        ledger[path.name] = now - (30 - n) * 60  # unused 30, 29, 28 min: in the grace
    (tmp_path / images.RETIRED_FILE).write_text(json.dumps(ledger))
    on_screen = (store.image_dir / shown).stat().st_size
    gone = store.prune(wanted={"xkcd:xkcd"}, max_bytes=on_screen + 1500, now=now)
    assert gone == spare[:2]  # the longest unused first, until under budget
    gone = store.prune(wanted={"xkcd:xkcd"}, max_bytes=0, now=now)
    assert gone == spare[2:]
    assert (store.image_dir / shown).exists()  # over budget, still never deleted


def test_a_picture_that_comes_back_is_never_deleted_under_its_new_record(tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    name = _keep_picture(store, "manual:xkcd", age=2 * 3600)
    store.update("manual:xkcd", lambda e: e.update(pictures=[]))  # replaced
    now = time.time()
    store.prune(now=now - 2 * 3600)  # seen unused two hours ago
    assert store.add("manual:xkcd", images.normalise(h.png((60, 20)))) == name
    assert store.prune(now=now) == []  # its grace started over at the write
    assert (store.image_dir / name).exists()
    gone = images.lifecycle.sweep(
        store.image_dir,
        keep=set(),
        max_bytes=0,
        grace_s=0,
        now=now + 3600,
        still_unused=lambda n: n != name,
    )
    assert name not in gone and (store.image_dir / name).exists()


def test_an_all_manual_setup_still_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No fetch worker runs when every strip is a manual picture, so renders
    clean up too (at most hourly): a removed strip's picture still goes."""
    monkeypatch.setattr(srv, "_today", lambda: TODAY)
    monkeypatch.setattr(srv, "_enqueue", lambda *a, **k: pytest.fail("nothing to fetch"))
    store = images.Store(tmp_path)
    kept = _keep_picture(store, "manual:mine", age=3 * 3600)
    orphan = _keep_picture(store, "manual:gone", (61, 20), age=3 * 3600)
    _configure(tmp_path, [_row("mine", mode="upload")])
    ctx = {"data_dir": str(tmp_path)}
    srv.fetch({}, {}, ctx=ctx)  # forgets gone's record; its picture is now unused
    assert "manual:gone" not in store.sources()
    later = time.time() + images.CLEANUP_EVERY_S + images.PRUNE_GRACE_S + 60
    monkeypatch.setattr(srv.time, "time", lambda: later)
    srv.fetch({}, {}, ctx=ctx)
    assert (store.image_dir / kept).exists()
    assert not (store.image_dir / orphan).exists()
    assert store.prune_if_due({"manual:mine"}, later + 60) is None  # not again within the hour


def test_switched_off_strips_still_clean_up_removed_pictures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "_enqueue", lambda *a, **k: pytest.fail("nothing to fetch"))
    store = images.Store(tmp_path)
    kept = _keep_picture(store, "manual:mine", age=3 * 3600)
    orphan = _keep_picture(store, "manual:gone", (61, 20), age=3 * 3600)
    _configure(tmp_path, [_row("mine", mode="upload", enabled=False)])
    ctx = {"data_dir": str(tmp_path)}
    assert "error" in srv.fetch({}, {}, ctx=ctx)
    assert set(store.sources()) == {"manual:mine"}
    later = time.time() + images.CLEANUP_EVERY_S + images.PRUNE_GRACE_S + 60
    monkeypatch.setattr(srv.time, "time", lambda: later)
    assert "error" in srv.fetch({}, {}, ctx=ctx)
    assert (store.image_dir / kept).exists()
    assert not (store.image_dir / orphan).exists()


# -- the screen: what fits, and taking turns (screen.py) ---------------------------

STRIP_ITEMS = [screen.Item((1200, 380)) for _ in range(4)]
XKCD_ITEM = screen.Item(
    (740, 300), ("A long comic title xkcd.com/3000", "An alt text that runs on " * 6)
)


def _plan(
    w: int, h: int, items: list[Any] = STRIP_ITEMS, *, one: bool = False, start: int = 0
) -> Any:
    return screen.plan(w, h, items, note=True, gap_min=6, one_at_a_time=one, start=start)


def test_the_big_portrait_panel_shows_everything() -> None:
    chosen = _plan(1440, 2560)
    assert chosen.show == (0, 1, 2, 3) and not chosen.rotating
    assert _plan(1440, 2560, one=True).show == (0, 1, 2, 3)  # it all fits


def test_a_small_landscape_screen_takes_turns_and_never_cuts_a_strip() -> None:
    turns = screen.turns(800, 480, STRIP_ITEMS, note=True, gap_min=6, one_at_a_time=False)
    assert [t.show for t in turns] == [(0,), (1,), (2,), (3,)]
    assert all(t.rotating for t in turns)
    room = screen.available_height(800, 480, True)
    heights = screen.heights(800, STRIP_ITEMS)
    assert all(sum(heights[i] for i in t.show) <= room for t in turns)


def test_a_13_inch_screen_shows_as_many_as_fit_or_one_at_a_time() -> None:
    turns = screen.turns(1600, 1200, STRIP_ITEMS, note=True, gap_min=6, one_at_a_time=False)
    assert [t.show for t in turns] == [(0, 1), (2, 3)]
    one = screen.turns(1600, 1200, STRIP_ITEMS, note=True, gap_min=6, one_at_a_time=True)
    assert [t.show for t in one] == [(0,), (1,), (2,), (3,)]


def test_a_caption_makes_a_strip_taller_and_wraps_on_narrow_cells() -> None:
    bare = screen.heights(1440, [screen.Item((740, 300))])[0]
    wide = screen.heights(1440, [XKCD_ITEM])[0]
    assert wide > bare
    # Narrower, the alt text wraps onto more lines: the caption takes a
    # bigger share of the strip's height.
    narrow_bare = screen.heights(600, [screen.Item((740, 300))])[0]
    narrow = screen.heights(600, [XKCD_ITEM])[0]
    assert (narrow - narrow_bare) / narrow > (wide - bare) / wide
    assert screen.caption_height(800, 780, ()) == 0


def test_caption_lines_match_what_the_page_draws() -> None:
    strip = {"title": "Title", "alt": "Alt.", "credit": "xkcd.com/1"}
    assert srv.caption_lines(strip, True) == ("Title xkcd.com/1", "Alt.")
    assert srv.caption_lines(strip, False) == ("xkcd.com/1",)
    assert srv.caption_lines({"title": "No credit"}, True) == ()


def test_a_turn_moves_on_once_per_hold_and_previews_never_move_it(tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    items = [screen.Item((1200, 600)) for _ in range(5)]

    def decide(start: int) -> Any:
        return _plan(800, 480, items, start=start)

    now = 1_000_000.0
    assert store.take_turn("cell", 5, decide, advance=True, now=now).show == (0,)
    # A follow-up inside the hold shows the same; so does a preview later.
    assert store.take_turn("cell", 5, decide, advance=True, now=now + 60).show == (0,)
    assert store.take_turn("cell", 5, decide, advance=False, now=now + 999).show == (0,)
    assert store.take_turn("cell", 5, decide, advance=True, now=now + 300).show == (1,)
    # Another placement has its own turn.
    assert store.take_turn("other", 5, decide, advance=True, now=now + 300).show == (0,)
    for step in range(2, 6):
        shown = store.take_turn("cell", 5, decide, advance=True, now=now + 300 * step).show
    assert shown == (0,)  # round again


def _three_strips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "_today", lambda: TODAY)
    monkeypatch.setattr(srv, "_enqueue", lambda *a, **k: None)
    store = images.Store(tmp_path)
    for slug in ("one", "two", "three"):
        _keep_picture(store, f"xkcd:{slug}", (1200, 380))
    _configure(tmp_path, [_row(s, mode="xkcd") for s in ("one", "two", "three")])


def test_fetch_sends_only_this_turns_comics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _three_strips(tmp_path, monkeypatch)
    small = {"data_dir": str(tmp_path), "cell_w": 800, "cell_h": 300, "cell_id": "a"}
    out = srv.fetch({}, {}, ctx=small)
    assert [s["id"] for s in out["strips"]] == ["one"]
    assert out["turns"] == {"showing": 1, "of": 3}
    big = {"data_dir": str(tmp_path), "cell_w": 1440, "cell_h": 2560, "cell_id": "b"}
    out = srv.fetch({}, {}, ctx=big)
    assert [s["id"] for s in out["strips"]] == ["one", "two", "three"] and "turns" not in out


def test_each_placement_keeps_its_own_turn_with_or_without_a_placement_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two cells with no options of their own share one ``cell_id``. With
    the host's ``placement_id`` each keeps its own turn; without it (a host
    from before that), the cell's size and device keep them apart; with no
    identifiers at all, they share one turn."""
    _three_strips(tmp_path, monkeypatch)

    def render(w: int, h: int, **ids: str) -> list[str]:
        ctx = {"data_dir": str(tmp_path), "cell_w": w, "cell_h": h, "settled": True, **ids}
        return [s["id"] for s in srv.fetch({}, {}, ctx=ctx)["strips"]]

    assert render(800, 300, cell_id="same", placement_id="page1/c1@small") == ["one"]
    assert render(1440, 2560, cell_id="same", placement_id="page2/c1@big") == [
        "one",
        "two",
        "three",
    ]
    render(800, 300, cell_id="same")
    render(1440, 2560, cell_id="same")
    render(800, 300, cell_id="same", target_device_id="kitchen")
    render(0, 0)
    turns = json.loads((tmp_path / images.ROTATION_FILE).read_text())
    assert set(turns) == {
        "page1/c1@small",
        "page2/c1@big",
        "same:800x300",
        "same:1440x2560",
        "same:800x300@kitchen",
        "default:0x0",  # nothing to tell placements apart: one shared turn
    }
    assert turns["page1/c1@small"]["shown"] == 1 and turns["page2/c1@big"]["shown"] == 3
    assert srv.placement_of({}) == "default:0x0"


def test_a_settled_render_moves_the_turn_on_and_a_preview_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _three_strips(tmp_path, monkeypatch)
    clock = [time.time()]
    monkeypatch.setattr(srv.time, "time", lambda: clock[0])
    ctx = {"data_dir": str(tmp_path), "cell_w": 800, "cell_h": 300, "placement_id": "p/c"}

    def shown(**extra: Any) -> list[str]:
        return [s["id"] for s in srv.fetch({}, {}, ctx={**ctx, **extra})["strips"]]

    assert shown(settled=True) == ["one"]
    clock[0] += screen.HOLD_S + 1
    assert shown(settled=False, preview=True) == ["one"]
    assert shown(settled=True) == ["two"]


# -- the admin page's preview: which screens, and which it opens on -------------------


def _device(device_id: str, name: str, w: int, h: int, *, kind: bool = False) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        id=device_id,
        display_name=name,
        kind_of=None if kind else "esp32_client",
        panel={"w": w, "h": h},
    )


def _grid_page(page_id: str, device_ids: list[str], *cells: tuple[str, int, int]) -> Any:
    from app.state.page_store import Cell, Page

    return Page(
        id=page_id,
        name=page_id.title(),
        device_ids=device_ids,
        cells=[
            Cell(id=f"c{n}", x=0, y=0, w=w, h=h, plugin=plugin)
            for n, (plugin, w, h) in enumerate(cells)
        ],
    )


REFERENCE_NAMES = [name for name, _, _ in screen.SCREENS]


def test_the_preview_opens_on_the_screen_that_shows_comics() -> None:
    kitchen = _device("kitchen", "Kitchen", 800, 480)
    frame = _device("frame", "Hall frame", 1440, 2560)
    builtin = _device("builtin", "Built-in kind", 1600, 1200, kind=True)
    pages_list = [
        _grid_page("clock", ["kitchen"], ("clock", 800, 480)),
        _grid_page("comics", ["frame"], (PLUGIN, 1440, 2560)),
    ]
    screens, default = srv.preview_screens(pages_list, [kitchen, frame, builtin])
    assert [(s["name"], s["w"], s["h"], s["group"]) for s in screens] == [
        ("Hall frame", 1440, 2560, srv.MINE),
        ("Kitchen", 800, 480, srv.MINE),
        *[(n, w, h, srv.REFERENCE) for n, w, h in screen.SCREENS],
    ]
    assert default == 0


def test_a_comics_cell_smaller_than_its_screen_previews_at_its_own_size() -> None:
    from app.state.page_store import Panel

    frame = _device("frame", "Hall frame", 1440, 2560)
    half = _grid_page("half", ["frame"], ("clock", 1440, 1280), (PLUGIN, 1440, 1280))
    screens, default = srv.preview_screens([half], [frame])
    assert (screens[default]["name"], screens[default]["w"], screens[default]["h"]) == (
        "Hall frame, comics cell",
        1440,
        1280,
    )
    small = _grid_page("small", ["frame"], (PLUGIN, 720, 640))
    small = small.model_copy(update={"panel": Panel(w=720, h=1280)})
    screens, default = srv.preview_screens([small], [frame])
    assert (screens[default]["w"], screens[default]["h"]) == (1440, 1280)


def test_a_canvas_comics_element_counts_too() -> None:
    from types import SimpleNamespace

    frame = _device("frame", "Hall frame", 1440, 2560)
    element = SimpleNamespace(kind="widget", widget=PLUGIN, w=1440, h=2540)
    page = SimpleNamespace(
        name="Comics",
        layout_kind="canvas",
        canvas=SimpleNamespace(w=1440, h=2560, els=[element]),
        device_ids=["frame"],
    )
    screens, default = srv.preview_screens([page], [frame])
    assert (screens[default]["name"], screens[default]["w"]) == ("Hall frame", 1440)
    assert screens[default]["h"] == 2560


def test_without_a_screen_showing_comics_the_preview_opens_on_the_first_reference() -> None:
    kitchen = _device("kitchen", "Kitchen", 800, 480)
    screens, default = srv.preview_screens([], [kitchen])
    assert [s["name"] for s in screens] == ["Kitchen", *REFERENCE_NAMES]
    assert screens[default]["name"] == REFERENCE_NAMES[0]
    screens, default = srv.preview_screens([], [])
    assert [s["name"] for s in screens] == REFERENCE_NAMES and default == 0


def test_the_preview_says_how_strips_take_turns() -> None:
    config = srv.Config(strips=())
    preview = [
        {"label": n, "pictures": [{"src": f"/{n}", "w": 1200, "h": 380}]} for n in ("A", "B", "C")
    ]
    views = srv.screens_view(config, preview)
    assert views[0]["text"] == "Everything at once: A, B, C."
    assert views[2]["text"].startswith("Takes turns, one refresh each: A")
    assert srv.screens_view(config, [])[0]["text"] == "Nothing to show yet."


def test_the_admin_page_picks_the_users_frame_in_the_screen_dropdown(tmp_path: Path) -> None:
    from app.main import REPO_ROOT, create_app

    (tmp_path / "devices").mkdir(parents=True, exist_ok=True)
    (tmp_path / "devices" / "frame.json").write_text(
        json.dumps(
            {
                "id": "frame",
                "kind": "esp32_client",
                "name": "Hall frame",
                "panel": {"w": 1440, "h": 2560, "orientation": "portrait"},
            }
        )
    )
    app = create_app(testing=True, data_root=tmp_path, plugins_dir=REPO_ROOT / "plugins")
    client = app.test_client()
    app.config["PAGE_STORE"].save(_grid_page("comics", ["frame"], (PLUGIN, 1440, 2560)))
    body = client.get(f"{BASE}/").get_data(as_text=True)
    picker = body[body.index('id="cx-screen-pick"') : body.index("</select>")]
    options = re.findall(r'<option value="(\d+)"( selected)?>([^<]*)</option>', picker)
    assert [(label, bool(on)) for _, on, label in options] == [
        ("Hall frame (1440×2560)", True),
        *[(f"{n} ({w}×{h})", False) for n, w, h in screen.SCREENS],
    ]
    figures = re.findall(r'<figure class="cx-screen" data-screen="(\d+)"( hidden)?>', body)
    assert figures == [("0", ""), ("1", " hidden"), ("2", " hidden"), ("3", " hidden")]


# -- the package ---------------------------------------------------------------------------


def test_the_package_has_no_trace_of_sources_it_does_not_have() -> None:
    root = srv._HERE
    words = re.compile(r"gocomics|far ?side|dilbert|bunny|cookie|amuniversal", re.IGNORECASE)
    hits = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".js", ".json", ".html", ".md", ".mjs"}:
            if path.name == Path(__file__).name:
                continue
            hits += [f"{path.name}: {m}" for m in words.findall(path.read_text(encoding="utf-8"))]
    assert hits == []


def test_the_manifest_fits_the_catalog() -> None:
    manifest = json.loads((srv._HERE / "plugin.json").read_text(encoding="utf-8"))
    assert len(manifest["description"]) < 280
    assert manifest["kind"] == "widget" and "settings" not in manifest
    assert manifest["requires"] == ["network:*"]


def test_the_layout_maths_in_client_js() -> None:
    """tests/clamp_check.mjs, the client's fitting maths, under node."""
    import shutil

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).with_name("clamp_check.mjs")
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "clamp_check: ok" in done.stdout
