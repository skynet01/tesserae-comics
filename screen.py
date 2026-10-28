"""What the comics page shows on a screen of a given size.

The page draws each strip at the cell's full width, in order, never cutting
one off (client.js measures and fits). On a big portrait panel everything
fits. On a small or landscape screen it can't, and a strip drawn smaller
than the screen's width would be too small to read, so the comics take turns
instead: each render shows the next ones that fit whole, and the following
render moves on (at most once per HOLD_S, so a send prepared ahead of time
and its follow-ups show the same comics).

``plan()`` decides from the cell's size and the pictures' sizes, estimating
the heights the way client.js draws them; client.js still measures, so an
estimate a little off only changes how the gap is spread.

How many share the screen is the operator's choice (the "On small screens"
setting): as many as fit, or one at a time.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# As client.js: the content width the gaps are set for, and the spare pixels
# kept free.
REF_WIDTH = 1408
SAFETY_PX = 16
HOLD_S = 4 * 60

# The screens the admin page previews: name, width, height.
SCREENS: tuple[tuple[str, int, int], ...] = (
    ("32-inch portrait", 1440, 2560),
    ("13-inch", 1600, 1200),
    ("7.5-inch", 800, 480),
)


def _clamp(low: float, value: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class Item:
    """One strip the page can show: its picture's size, and the lines of
    text drawn under it (a title with its credit, an alt text), if any."""

    size: tuple[int, int]  # (w, h) of the picture
    caption: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    """Which items show (indexes into the list given), and whether they take
    turns: ``rotating`` with the window starting at ``start``."""

    show: tuple[int, ...]
    rotating: bool
    total: int

    @property
    def turns(self) -> int:
        """How many renders it takes to show every item once."""
        return math.ceil(self.total / max(1, len(self.show))) if self.rotating else 1


def caption_height(cell_w: float, width: float, lines: Sequence[str]) -> float:
    """The height of a strip's caption at ``cell_w``, as client.js sets it:
    each line wraps at about half a font size per character."""
    texts = [text for text in lines if text]
    if not texts:
        return 0.0
    font = _clamp(10, 0.011 * cell_w, 24)
    pad = _clamp(3, 0.006 * cell_w, 12)
    per_line = max(1, int(width / (font * 0.52)))
    wrapped = sum(math.ceil(len(text) / per_line) for text in texts)
    return pad + wrapped * font * 1.3


def heights(cell_w: float, items: Sequence[Item]) -> list[float]:
    """Each item's height at ``cell_w``, as client.js draws it."""
    pad = _clamp(4, 0.011 * cell_w, 24)
    width = max(1.0, cell_w - 2 * pad)
    out = []
    for item in items:
        w, h = item.size
        picture = width * h / w if w > 0 else 0.0
        out.append(picture + caption_height(cell_w, width, item.caption))
    return out


def available_height(cell_w: float, cell_h: float, note: bool) -> float:
    pad = _clamp(4, 0.011 * cell_w, 24)
    note_h = (_clamp(2, 0.005 * cell_w, 10) + _clamp(10, 0.0095 * cell_w, 20) * 1.2) if note else 0
    return cell_h - 2 * pad - note_h - SAFETY_PX * min(1.0, cell_w / REF_WIDTH)


def plan(
    cell_w: float | None,
    cell_h: float | None,
    items: Sequence[Item],
    *,
    note: bool,
    gap_min: float,
    one_at_a_time: bool,
    start: int = 0,
) -> Plan:
    """What to show on a ``cell_w`` x ``cell_h`` cell, from ``start`` (the
    placement's turn). Everything when it all fits (or the size is unknown);
    else the items from ``start`` on that fit whole (at least one: a single
    item too tall is drawn smaller, never cut), or one, taking turns."""
    total = len(items)
    if not total or not cell_w or not cell_h or cell_w <= 0 or cell_h <= 0:
        return Plan(tuple(range(total)), False, total)
    hs = heights(cell_w, items)
    room = available_height(cell_w, cell_h, note)
    gap = gap_min * min(1.0, (cell_w - 2 * _clamp(4, 0.011 * cell_w, 24)) / REF_WIDTH)
    if sum(hs) + gap * (total - 1) <= room:
        return Plan(tuple(range(total)), False, total)
    start %= total
    show = [start]
    used = hs[start]
    if not one_at_a_time:
        for step in range(1, total):
            index = (start + step) % total
            if used + gap + hs[index] > room:
                break
            show.append(index)
            used += gap + hs[index]
    return Plan(tuple(show), True, total)


def turns(
    cell_w: float | None,
    cell_h: float | None,
    items: Sequence[Item],
    **kwargs: object,
) -> list[Plan]:
    """Every turn a placement goes through from the top, one plan per
    refresh, until each item has shown once (one plan when all fit)."""
    out: list[Plan] = []
    start = 0
    while len(out) < max(1, len(items)):
        chosen = plan(cell_w, cell_h, items, start=start, **kwargs)  # type: ignore[arg-type]
        out.append(chosen)
        if not chosen.rotating:
            break
        start += len(chosen.show)
        if start >= len(items):
            break
    return out
