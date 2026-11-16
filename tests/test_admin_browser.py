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
        store.update(
            f"xkcd:{slug}",
            lambda e, name=name, picture=picture, n=n: e.update(
                pictures=[
                    {
                        "file": name,
                        "w": picture.width,
                        "h": picture.height,
                        "caption": f"Comic {n} | xkcd.com/{3000 + n} | The alt text, as xkcd's hover text.",
                    }
                ],
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
    rows.append({"slug": "dilbert", "enabled": False, "mode": "auto", "label": "Dilbert"})
    module.config_store(plugin.data_dir).update(
        lambda values: {**values, "strips": rows, "far_side": False}
    )


def test_the_admin_page_fits_light_dark_and_phone_widths(live: tuple[str, Flask]) -> None:
    from playwright.sync_api import sync_playwright

    base, app = live
    _seed(app, ("xkcd", "xkcd-2", "xkcd-3"))
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
            row = page.locator("#strip-xkcd-2")
            row.locator("summary").click()
            assert row.locator('input[name="image_url"]').is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
        browser.close()


def test_xkcd_selection_and_layout_save_without_javascript(live: tuple[str, Flask]) -> None:
    from playwright.sync_api import sync_playwright

    base, app = live
    _seed(app)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(java_script_enabled=False)
        page.goto(f"{base}{BASE}/")
        row = page.locator("#strip-xkcd")
        row.locator("summary").click()
        row.locator('select[name="pick"]').select_option("random")
        with page.expect_navigation():
            row.get_by_role("button", name="Save strip", exact=True).click()
        layout = page.locator("#layout")
        layout.locator("#cx-gap").fill("40")
        with page.expect_navigation():
            layout.get_by_role("button", name="Save", exact=True).click()
        browser.close()
    values = _config(app)
    assert values["strips"][0]["pick"] == "random"
    assert values["gap"] == 40


def test_the_preview_picker_switches_without_a_reload(live: tuple[str, Flask]) -> None:
    from playwright.sync_api import sync_playwright

    base, app = live
    _seed(app)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{base}{BASE}/", wait_until="networkidle")
        page.evaluate("window.__stay = 1")
        page.select_option("#cx-screen-pick", index=2)
        shown = page.locator(".cx-screen[data-screen]:not([hidden])")
        assert shown.count() == 1 and shown.get_attribute("data-screen") == "2"
        assert page.evaluate("window.__stay === 1")
        browser.close()


def test_a_cell_draws_the_comic_credit_and_fits_the_caption(live: tuple[str, Flask]) -> None:
    from playwright.sync_api import sync_playwright

    base, app = live
    _seed(app)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 800, "height": 480})
        page.goto(f"{base}/_test/render?plugin={PLUGIN}&w=800&h=480", wait_until="networkidle")
        page.wait_for_function("""() => [...document.querySelectorAll('*')].some(el =>
            el.shadowRoot?.querySelector('.strip .cap'))""")
        result = page.evaluate("""() => {
            const root = [...document.querySelectorAll('*')].find(el =>
                el.shadowRoot?.querySelector('.strip .cap')).shadowRoot;
            const row = root.querySelector('.strip');
            const box = root.querySelector('.rows').getBoundingClientRect();
            const rect = row.getBoundingClientRect();
            return {caption: row.querySelector('.cap').textContent,
                inside: rect.top >= box.top - 1 && rect.bottom <= box.bottom + 1};
        }""")
        assert "xkcd.com/3000" in result["caption"]
        assert result["inside"]
        browser.close()
