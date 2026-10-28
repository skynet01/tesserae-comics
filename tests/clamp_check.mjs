// Plain-node self-check of the comics page's layout maths in client.js:
// which rows fit, the gap, the out-of-date line, the caption. Run by
// test_comics_strips.py (test_the_layout_maths_in_client_js), or by hand:
// node tests/clamp_check.mjs
import assert from "node:assert/strict";
import { captionHtml, gapsFor, noteText, planRows, scaleFor, shrinkFor, MIN_ZOOM } from "../client.js";

const gaps = { gap: 32, min: 6, max: 48 };

// Everything fits at the standard gap; spare height widens it to the max.
assert.deepEqual(planRows([500, 300, 300], 2000, gaps), { keep: [0, 1, 2], gap: 48 });
// Spare height between standard and max is shared out evenly.
assert.deepEqual(planRows([500, 300, 300], 1180, gaps), { keep: [0, 1, 2], gap: 40 });
// Too tight for the standard gap: squeeze toward the smallest.
assert.deepEqual(planRows([500, 300, 300], 1120, gaps), { keep: [0, 1, 2], gap: 10 });
// Too tight even at the smallest gap: the last row goes.
assert.deepEqual(planRows([500, 300, 300], 1105, gaps), { keep: [0, 1], gap: 48 });
// A row taller than the whole box is passed over so the others still show.
assert.deepEqual(planRows([900, 200, 200], 600, gaps), { keep: [1, 2], gap: 48 });
// One row: no gaps to spread.
assert.deepEqual(planRows([400], 1000, gaps), { keep: [0], gap: 32 });
// Nothing fits.
assert.deepEqual(planRows([700, 800], 600, gaps), { keep: [], gap: 32 });

// Gaps scale with width and stay ordered whatever the options say.
assert.equal(scaleFor(1408), 1);
assert.equal(scaleFor(2816), 1, "never enlarged past the reference panel");
assert.equal(scaleFor(704), 0.5);
assert.equal(scaleFor(0), 1);
assert.deepEqual(gapsFor({}, 1), { gap: 32, min: 6, max: 48 });
assert.deepEqual(gapsFor({ gap: 20, gap_min: 30, gap_max: 10 }, 0.5), { gap: 10, min: 10, max: 10 });
assert.deepEqual(gapsFor({ gap: "x", gap_min: -3, gap_max: "" }, 1), { gap: 32, min: 6, max: 48 });

// The status line.
const t = (key, fallback) => fallback;
assert.equal(noteText([], t, "en"), "");
assert.equal(
  noteText(
    [
      { label: "Morning strip", state: "stale", since: "2026-10-24" },
      { label: "xkcd", state: "missing" },
      { label: "Broken", state: "invalid" },
    ],
    t,
    "en",
  ),
  "Not updated today: Morning strip (Oct 24), xkcd, Broken (can't show)",
);

// The caption: title and credit, then the alt text; only the credit with
// captions off; nothing for a strip that isn't xkcd; text escaped.
const strip = { title: "Fish <3", alt: "Alt & more.", credit: "xkcd.com/12" };
assert.equal(
  captionHtml(strip, true),
  '<div class="cap"><p class="cap-head"><strong>Fish &lt;3</strong> <span class="cap-credit">xkcd.com/12</span></p><p class="cap-alt">Alt &amp; more.</p></div>',
);
assert.equal(
  captionHtml(strip, false),
  '<div class="cap"><p class="cap-head"><span class="cap-credit">xkcd.com/12</span></p></div>',
);
assert.equal(captionHtml({ title: "Mine" }, true), "");

// A row taller than its space is drawn smaller to fit, never cut.
assert.equal(shrinkFor(900, 600), 0.666);
assert.equal(shrinkFor(500, 600), 1);
assert.equal(shrinkFor(0, 600), 1);

// An 800 x 50 sliver: the strip would need about a twentieth of its size,
// below what can be made out, so the cell says it is too small instead.
assert.ok(shrinkFor(253, 34) < MIN_ZOOM);
assert.ok(shrinkFor(253, 200) > MIN_ZOOM);

console.log("comics_strips clamp_check: ok");
