// comics_strips, today's comics as one page: each strip at the cell's full
// width, in the order the admin page lists them. An xkcd comic carries its
// title, its alt text and the xkcd.com credit underneath. Tuned on a
// 1440 x 2560 portrait panel.
//
// Fitting is measured, not estimated: the page is drawn, each row's height
// read back, and planRows() keeps as many rows as fit. It tries the
// standard gap first, then squeezes the gaps down to the smallest, and only
// then leaves the last row out, so no strip is ever cut off. Leftover height
// widens the gaps (up to the largest) and the rest centres the page.
//
// The page's styles are inline rather than in the shared stylesheet: a
// <link> applies a tick after innerHTML, and measuring before it lands would
// fit a different page. The shared sheet is only linked for the error and
// empty states, which measure nothing.

// Content width (1440 less 16 px padding a side) the gaps are given for.
const REF_WIDTH = 1408;
// Spare pixels so rounding never pushes the last row past the edge.
const SAFETY_PX = 16;
const SETTLE_MS = 1500;

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function isOn(value, fallback) {
  if (value === undefined || value === null || value === "") return fallback;
  return value !== false && value !== "false" && value !== 0 && value !== "0";
}

// Gaps are set for the 1440 px panel; a narrower cell scales them down.
export function scaleFor(width) {
  if (!(width > 0)) return 1;
  return Math.min(1, width / REF_WIDTH);
}

// The gap options, sanitised and scaled: gap_min <= gap <= gap_max.
export function gapsFor(opts, scale = 1) {
  const num = (value, fallback) => {
    const n = Number(value);
    return value !== "" && value !== null && Number.isFinite(n) && n >= 0 ? n : fallback;
  };
  const gap = num(opts?.gap, 32);
  const min = Math.min(num(opts?.gap_min, 6), gap);
  const max = Math.max(num(opts?.gap_max, 48), gap);
  return { gap: gap * scale, min: min * scale, max: max * scale };
}

// Below this a shrunken comic can't be made out: the cell says so instead.
export const MIN_ZOOM = 0.15;

// How much to shrink a row that is taller than the space it has.
export function shrinkFor(height, available) {
  if (!(height > 0) || !(available > 0) || height <= available) return 1;
  return Math.floor((available / height) * 1000) / 1000;
}

// Which rows to keep and the gap between them, given each row's height and
// the height available. A row taller than the whole box can never show, so
// it is passed over. Of the rest, the longest run from the top that fits
// wins: standard gap first, widened toward the largest gap with what is
// left over; else the gap squeezed toward the smallest; else one row fewer.
export function planRows(heights, available, gaps) {
  const candidates = [];
  heights.forEach((h, i) => {
    if (h > 0 && h <= available) candidates.push(i);
  });
  for (let n = candidates.length; n > 0; n--) {
    const keep = candidates.slice(0, n);
    const content = keep.reduce((sum, i) => sum + heights[i], 0);
    const between = n - 1;
    const spare = available - content;
    if (content + between * gaps.gap <= available) {
      const gap = between
        ? Math.min(gaps.max, Math.max(gaps.gap, Math.floor(spare / between)))
        : gaps.gap;
      return { keep, gap };
    }
    if (between > 0 && content + between * gaps.min <= available) {
      return { keep, gap: Math.max(gaps.min, Math.floor(spare / between)) };
    }
  }
  return { keep: [], gap: gaps.gap };
}

function shortDate(iso, locale) {
  if (typeof iso !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) return "";
  try {
    return new Intl.DateTimeFormat(locale || "en", { month: "short", day: "numeric", timeZone: "UTC" })
      .format(new Date(`${iso}T00:00:00Z`));
  } catch {
    return iso;
  }
}

// One quiet line naming what isn't today's: a stale strip with the day it
// is from, a missing one by name, a strip that can't be shown.
export function noteText(notes, t, locale) {
  if (!Array.isArray(notes) || !notes.length) return "";
  const parts = notes.map((note) => {
    const label = String(note?.label ?? "");
    if (note?.state === "stale") {
      const since = shortDate(note.since, locale);
      return since ? `${label} (${since})` : label;
    }
    if (note?.state === "invalid") return `${label} (${t("not_a_strip", "can't show")})`;
    return label;
  });
  return `${t("not_updated", "Not updated today")}: ${parts.join(", ")}`;
}

// The text under a strip: an xkcd comic's title and credit, then its alt
// text; just the credit with captions off; nothing for other strips. As
// server.py's caption_lines(), which estimates the same height.
export function captionHtml(strip, captions) {
  const credit = strip?.credit ? String(strip.credit) : "";
  if (!credit) return "";
  const title = captions && strip.title ? `<strong>${escapeHtml(strip.title)}</strong> ` : "";
  const alt = captions && strip.alt ? `<p class="cap-alt">${escapeHtml(strip.alt)}</p>` : "";
  return `<div class="cap"><p class="cap-head">${title}<span class="cap-credit">${escapeHtml(credit)}</span></p>${alt}</div>`;
}

const STYLE = `
  :host { display: block; width: 100%; height: 100%; }
  .comics {
    box-sizing: border-box; width: 100%; height: 100%;
    display: flex; flex-direction: column; overflow: hidden;
    padding: clamp(4px, 1.1cqw, 24px);
    background: var(--surface);
    border-radius: var(--cell-corner-radius, 0);
    color: var(--text-primary);
  }
  .comics * { box-sizing: border-box; }
  .rows {
    flex: 1 1 0; min-height: 0; overflow: hidden;
    display: flex; flex-direction: column; justify-content: safe center;
  }
  .row { flex: none; }
  .row[hidden] { display: none; }
  .strip img { display: block; width: 100%; height: auto; margin: 0 auto; }
  .cap {
    padding-top: clamp(3px, 0.6cqw, 12px);
    font-family: var(--font-family, inherit);
    font-size: clamp(10px, 1.1cqw, 24px); line-height: 1.3;
    overflow-wrap: anywhere;
  }
  .cap p { margin: 0; }
  .cap-credit { color: var(--text-muted); white-space: nowrap; }
  .cap-alt { color: var(--text-secondary, var(--text-primary)); }
  .too-small {
    margin: auto; font-family: var(--font-family, inherit);
    font-size: clamp(10px, 40cqh, 16px); color: var(--text-muted); text-align: center;
  }
  .note {
    flex: none; padding-top: clamp(2px, 0.5cqw, 10px);
    font-family: var(--font-family, inherit); font-size: clamp(10px, 0.95cqw, 20px);
    line-height: 1.2; color: var(--text-muted); text-align: right;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
`;

function stripHtml(strip, captions) {
  const alt = strip.title ? `${strip.label}: ${strip.title}` : strip.label;
  return `<div class="row strip" data-w="${Number(strip.w) || 1}" data-h="${Number(strip.h) || 1}">
      <img src="${escapeHtml(strip.src)}" width="${Number(strip.w) || 1}" height="${Number(strip.h) || 1}" alt="${escapeHtml(alt)}">
      ${captionHtml(strip, captions)}
    </div>`;
}

// Fonts can change a caption's line count; wait for them, but never long.
function settle() {
  if (typeof document === "undefined" || !document.fonts?.ready) return Promise.resolve();
  return Promise.race([document.fonts.ready, new Promise((r) => setTimeout(r, SETTLE_MS))]);
}

function fit(shadow, opts, tooSmall) {
  const box = shadow.querySelector(".rows");
  if (!box) return;
  box.querySelector(".too-small")?.remove();
  const width = box.clientWidth;
  const available = box.clientHeight - SAFETY_PX * scaleFor(width);
  // A detached host has no size; leave the natural flow alone.
  if (!(width > 0) || !(available > 0)) return;

  // A strip taller than the box would never fit; narrow its picture until
  // the picture and its caption do, so a wide, short cell still shows
  // strips rather than nothing. Narrowed past MIN_ZOOM of the width it
  // can't be made out, and doesn't count.
  const unreadable = new Set();
  for (const row of shadow.querySelectorAll(".strip")) {
    const ratio = Number(row.dataset.w) / Number(row.dataset.h);
    const img = row.querySelector("img");
    if (!img || !(ratio > 0)) continue;
    const caption = row.querySelector(".cap")?.getBoundingClientRect().height || 0;
    const room = Math.max(0, available - caption);
    img.style.maxWidth = `${Math.floor(room * ratio)}px`;
    if (room * ratio < MIN_ZOOM * width) unreadable.add(row);
  }

  const rows = [...box.querySelectorAll(".row")];
  rows.forEach((row) => { row.hidden = false; row.style.zoom = ""; });
  const heights = rows.map((row) => (unreadable.has(row) ? Infinity : row.getBoundingClientRect().height));
  const plan = planRows(heights, available, gapsFor(opts, scaleFor(width)));
  let keep = plan.keep;
  if (!keep.length) {
    // Nothing fits whole (one comic a turn on a small screen, still too
    // tall): draw the first readable row smaller so it fits, never cut off;
    // and if even that would be too small to make out (an 800 x 50 sliver),
    // say so plainly rather than clip it or draw a smudge.
    const first = rows.findIndex((row) => !unreadable.has(row));
    const zoom = first >= 0 ? shrinkFor(heights[first], available) : 0;
    if (zoom < MIN_ZOOM) {
      rows.forEach((row) => { row.hidden = true; });
      const note = shadow.ownerDocument?.createElement?.("p");
      if (note) {
        note.className = "too-small";
        note.textContent = tooSmall;
        box.appendChild(note);
      }
      return;
    }
    rows[first].style.zoom = String(zoom);
    keep = [first];
  }
  const kept = new Set(keep);
  rows.forEach((row, i) => { row.hidden = !kept.has(i); });
  box.style.gap = `${plan.gap}px`;
}

export default async function render(shadow, ctx) {
  const t = ctx?.t || ((key, fallback) => fallback ?? key);
  const data = ctx?.data ?? {};
  // The layout comes from the widget's admin page, sent with the data.
  const opts = { ...(data.layout || {}) };
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="comics_strips">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>${t("comics", "Comics")}</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const strips = Array.isArray(data.strips) ? data.strips : [];
  const note = isOn(opts.status_note, true) ? noteText(data.notes, t, ctx?.locale) : "";

  if (!strips.length) {
    const message = data.updating
      ? t("fetching", "Fetching today's comics…")
      : t("no_comics", "No comics to show yet.");
    shadow.innerHTML = `
      ${css}
      <div class="w is-bleed" data-widget="comics_strips">
        <div class="bleed-empty">${escapeHtml(message)}</div>
      </div>`;
    return;
  }

  const captions = isOn(opts.captions, true);
  shadow.innerHTML = `
    <style>${STYLE}</style>
    <div class="comics" data-widget="comics_strips">
      <div class="rows">
        ${strips.map((s) => stripHtml(s, captions)).join("")}
      </div>
      ${note ? `<div class="note">${escapeHtml(note)}</div>` : ""}
    </div>`;
  await settle();
  fit(shadow, opts, t("too_small", "Too small a space for a comic."));
}
