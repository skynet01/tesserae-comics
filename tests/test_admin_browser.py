"""The Comics, Strips admin page and widget in a live Chromium: the page
fits light, dark and phone widths, every change works with JavaScript
switched off, the plugin's script only adds niceties (the preview picker, a
question before a remove), and a cell draws an xkcd comic with its caption
and credit, or says it is too small rather than clip one."""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from PIL import Image

from app.main import create_app

PLUGIN = "comics_strips"
BASE = f"/plugins/{PLUGIN}"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="Playwright Chromium not installed"
)


@pytest.fixture
def live(tmp_path: Path) -> Iterator[tuple[str, Flask]]:
    from werkzeug.serving import make_server

    plugins_dir = Path(__file__).resolve().parents[2]
    app = create_app(testing=False, data_root=tmp_path / "data", plugins_dir=plugins_dir)
    app.config["TESTING"] = True
    store = app.config["SETTINGS_STORE"]
    store.patch_section("app", {"onboarded": True})
    store.patch_section("auth", {"disabled": True})
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", app
    finally:
        server.shutdown()
        thread.join(timeout=10)


def _module(app: Flask) -> Any:
    return app.config["PLUGIN_REGISTRY"].get(PLUGIN).server_module


def _config(app: Flask) -> dict[str, Any]:
    plugin = app.config["PLUGIN_REGISTRY"].get(PLUGIN)
    values = plugin.server_module.config_store(plugin.data_dir).read()
    assert values is not None
    return dict(values)


def _seed(app: Flask, slugs: tuple[str, ...] = ("xkcd",)) -> None:
    """xkcd strips already fetched today (nothing is due, so nothing is
    downloaded), with their captions."""
    plugin = app.config["PLUGIN_REGISTRY"].get(PLUGIN)
    module = plugin.server_module
    store = module.images.Store(Path(plugin.data_dir))
    today = module._today().isoformat()
    rows = []
    for n, slug in enumerate(slugs):
        buf = io.BytesIO()
        Image.new("L", (1200, 380 + n), 255).save(buf, format="PNG")
        picture = module.images.normalise(buf.getvalue())
        name = store.add(f"xkcd:{slug}", picture)
        meta = {
            "num": 3000 + n,
            "title": f"Comic {n}",
            "alt": "The alt text, as xkcd's hover text.",
            "credit": f"xkcd.com/{3000 + n}",
        }
        store.update(
            f"xkcd:{slug}",
            lambda e, name=name, picture=picture, meta=meta: e.update(
                pictures=[{"file": name, "w": picture.width, "h": picture.height, **meta}],
                edition=today,
                fetched_on=today,
                last_success=time.time(),
                last_attempt=time.time(),
                error=None,
            ),
        )
        rows.append(
            {
                "enabled": True,
                "label": "",
                "slug": slug,
                "mode": "xkcd",
                "pick": "latest",
                "image_url": "",
            }
        )
    module.config_store(plugin.data_dir).update(lambda values: {**values, "strips": rows})


def test_the_admin_page_fits_light_dark_and_phone_widths(live: tuple[str, Flask]) -> None:
    from playwright.sync_api import sync_playwright

    base, app = live
    _seed(app, ("xkcd", "xkcd-2", "xkcd-3"))
    plugin = app.config["PLUGIN_REGISTRY"].get(PLUGIN)
    store = plugin.server_module.images.Store(Path(plugin.data_dir))
    for n in range(40):
        store.append_log({"at": 1.0e9 + n, "source": "xkcd", "outcome": "ok", "text": f"n{n}"})
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        for width, theme in ((1280, "light"), (390, "light"), (390, "dark")):
            page.emulate_media(color_scheme=theme)
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{base}{BASE}/", wait_until="networkidle")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator("#strips .cx-row").count() == 3
            row = page.locator("#strip-xkcd-2")
            row.locator("summary").click()
            assert row.locator('input[name="image_url"]').is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # The log opens on its newest line.
        log = page.locator(".cx-log")
        assert log.evaluate("el => el.scrollHeight - el.scrollTop - el.clientHeight < 2")
        # Each comic's caption and credit show under it on the page.
        assert (
            page.locator(".cx-preview .cx-cap")
            .first.inner_text()
            .startswith("Comic 0 xkcd.com/3000")
        )
        browser.close()
    assert errors == []


def test_every_change_works_without_javascript(live: tuple[str, Flask]) -> None:
    """With scripts off: add, move, switch off, edit and remove strips, save
    the Layout card, and pick the preview's screen. (Clicks are forced:
    Playwright's wait for the element to hold still stalls on a page with
    scripts off.)"""
    from playwright.sync_api import sync_playwright

    base, app = live
    _seed(app, ("xkcd", "xkcd-2"))
    queued: list[str] = []
    _module(app)._enqueue = lambda root, sources, *a, **k: queued.extend(s.key for s in sources)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            java_script_enabled=False, viewport={"width": 1280, "height": 900}
        )
        page = context.new_page()
        url = f"{base}{BASE}/"
        page.goto(url)

        def press(locator: Any, anchor: str) -> None:
            with page.expect_navigation():
                locator.click(force=True)
            assert page.url.endswith(f"{BASE}/#{anchor}")
            page.goto(url)

        add = page.locator("form.cx-add")
        add.locator("#cx-add-mode").select_option("url")
        add.locator("#cx-add-label").fill("Morning")
        add.locator("#cx-add-image_url").fill("https://pictures.example.org/morning.png")
        press(add.get_by_role("button", name="Add strip"), "strip-morning")
        press(
            page.locator("#strip-morning").get_by_role("button", name="Move Morning up"),
            "strip-morning",
        )
        press(
            page.locator("#strip-xkcd").get_by_role("button", name="Switch xkcd off"),
            "strip-xkcd",
        )
        second = page.locator("#strip-xkcd-2")
        second.locator("summary").click(force=True)  # a details element: no script needed
        second.locator('input[name="label"]').fill("Random xkcd")
        second.locator('select[name="pick"]').select_option("random")
        press(second.get_by_role("button", name="Save strip"), "strip-xkcd-2")
        press(page.locator("#strip-xkcd").get_by_role("button", name="Remove xkcd"), "strips")
        layout = page.locator("#layout")
        layout.locator("#cx-gap").fill("40")
        layout.locator("#cx-small_screens").select_option("one")
        layout.locator('label[for="cx-captions"].dx-toggle-switch').click(force=True)
        press(layout.get_by_role("button", name="Save"), "layout")
        page.select_option("#cx-screen-pick", label="7.5-inch (800×480)")
        with page.expect_navigation():
            page.get_by_role("button", name="Show").click(force=True)
        assert "screen=2" in page.url
        shown = page.locator(".cx-screen[data-screen]:not([hidden])")
        assert shown.count() == 1 and shown.get_attribute("data-screen") == "2"
        context.close()
        browser.close()
    values = _config(app)
    assert [(r["slug"], r["mode"], r["pick"]) for r in values["strips"]] == [
        ("morning", "url", "latest"),
        ("xkcd-2", "xkcd", "random"),
    ]
    assert values["strips"][1]["label"] == "Random xkcd"
    assert (values["gap"], values["small_screens"], values["captions"]) == (40, "one", False)
    assert queued[0].startswith("url:") and "xkcd:xkcd-2" in queued


def test_removing_a_strip_asks_first(live: tuple[str, Flask]) -> None:
    from playwright.sync_api import sync_playwright

    base, app = live
    _seed(app, ("xkcd", "xkcd-2"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{base}{BASE}/", wait_until="networkidle")
        answers = iter([False, True])
        asked: list[str] = []

        def answer(dialog: Any) -> None:
            asked.append(dialog.message)
            if next(answers):
                dialog.accept()
            else:
                dialog.dismiss()

        page.on("dialog", answer)
        remove = page.locator("#strip-xkcd-2").get_by_role("button", name="Remove xkcd 2")
        remove.click()
        page.wait_for_timeout(200)
        assert page.locator("#strip-xkcd-2").count() == 1
        with page.expect_navigation():
            remove.click()
        page.wait_for_load_state("networkidle")
        assert page.locator("#strip-xkcd-2").count() == 0
        browser.close()
    assert asked == ["Remove xkcd 2 from the list?"] * 2
    assert [r["slug"] for r in _config(app)["strips"]] == ["xkcd"]


_PROBE = """() => {
  const host = [...document.querySelectorAll('*')].find(
    (el) => el.shadowRoot && el.shadowRoot.querySelector('.rows'));
  const sr = host && host.shadowRoot;
  if (!sr) return null;
  const row = [...sr.querySelectorAll('.row')].find((r) => !r.hidden);
  const box = sr.querySelector('.rows').getBoundingClientRect();
  const r = row ? row.getBoundingClientRect() : null;
  return {
    small: sr.querySelector('.too-small')?.textContent || '',
    inside: r ? (r.top >= box.top - 1 && r.bottom <= box.bottom + 1) : null,
    caption: row?.querySelector('.cap')?.innerText.replace(/\\s+/g, ' ').trim() || '',
  };
}"""


def test_a_cell_draws_the_comic_with_its_caption_or_says_it_is_too_small(
    live: tuple[str, Flask],
) -> None:
    from playwright.sync_api import sync_playwright

    base, app = live
    _seed(app)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"{base}/_test/render?plugin={PLUGIN}&w=800&h=480", wait_until="networkidle")
        page.wait_for_timeout(1800)
        normal = page.evaluate(_PROBE)
        page.set_viewport_size({"width": 800, "height": 50})
        page.goto(f"{base}/_test/render?plugin={PLUGIN}&w=800&h=50", wait_until="networkidle")
        page.wait_for_timeout(1800)
        sliver = page.evaluate(_PROBE)
        browser.close()
    assert normal["small"] == "" and normal["inside"] is True
    assert normal["caption"] == "Comic 0 xkcd.com/3000 The alt text, as xkcd's hover text."
    assert sliver["small"] == "Too small a space for a comic."


def test_the_screen_dropdown_swaps_the_preview_without_a_reload(
    live: tuple[str, Flask],
) -> None:
    from playwright.sync_api import sync_playwright

    base, app = live
    _seed(app)
    shown = """() => [...document.querySelectorAll('.cx-screen[data-screen]')]
        .filter((f) => !f.hidden && f.getBoundingClientRect().height > 0)
        .map((f) => f.dataset.screen)"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{base}{BASE}/", wait_until="networkidle")
        page.evaluate("window.__stay = 1")
        first = page.evaluate(shown)
        label = page.locator("#cx-screen-pick option:checked").inner_text()
        page.select_option("#cx-screen-pick", label="7.5-inch (800×480)")
        after = page.evaluate(shown)
        stayed = page.evaluate("window.__stay === 1")
        aside = page.evaluate(
            """() => { const a = document.querySelector('.cx-onscreen').getBoundingClientRect();
                const l = document.querySelector('.cx-preview').getBoundingClientRect();
                return {left: a.right <= l.left, sticky: getComputedStyle(
                  document.querySelector('.cx-onscreen')).position === 'sticky'}; }"""
        )
        page.set_viewport_size({"width": 420, "height": 900})
        stacked = page.evaluate(
            """() => document.querySelector('.cx-onscreen').getBoundingClientRect().bottom
                <= document.querySelector('.cx-preview').getBoundingClientRect().top"""
        )
        browser.close()
    assert label == "32-inch portrait (1440×2560)" and first == ["0"]
    assert after == ["2"] and stayed
    assert aside == {"left": True, "sticky": True}
    assert stacked
