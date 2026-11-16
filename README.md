# Comics, Strips for Tesserae

A [Tesserae](https://github.com/dmellok/tesserae) widget that puts your comic
strips on one page, each at the cell's full width, in the order you choose.

## Sources

The Far Side's daily panels appear in a row above the strips. Each strip can use:

- **Automatic**: a GoComics strip such as Garfield or Calvin and Hobbes,
  an archived Dilbert, or the latest xkcd. GoComics can use its browser
  challenge session or cookies pasted on the admin page.

- **xkcd**, from xkcd's official JSON API (`https://xkcd.com/info.0.json`
  for the latest comic, `https://xkcd.com/<num>/info.0.json` for a numbered
  one). Choose the latest comic or a random one from the archive (a new one
  each day, the same one all day). The comic's title and its alt text are
  shown under it as the caption.
- **Direct image URL**: a picture on one of the comic hosts listed in the
  manifest, downloaded again each day.
- **Manual upload**: a PNG, JPEG, GIF or WebP you upload on the admin page,
  shown until you switch the strip to another source.

xkcd is by Randall Munroe and is licensed under
[CC BY-NC 2.5](https://xkcd.com/license.html). The widget credits
"xkcd.com/<number>" under every xkcd comic it draws, with captions on or off.
Only use direct image URLs for pictures you are allowed to display.

## Setting it up

Add a Comics, Strips cell to a dashboard, then open **Widgets → Admin pages →
Comics, Strips**. The comics are chosen once there and every comics cell shows
them. The page has:

- **Today's comics**: what each strip shows now, a **Fetch now** button, and a
  preview of what fits on each of your screens (and a few reference screens).
- **Strips**: add, remove, reorder, switch on or off and rename strips; pick
  each one's source; upload a picture.
- **Layout**: what small screens do, the space between comics, xkcd captions,
  and the out-of-date note.
- **Fetches**: a log of every fetch and upload.

New installs start with The Far Side, Dilbert, Garfield, Calvin and Hobbes,
and Pearls Before Swine. Existing strip lists and xkcd selections are kept.
Dilbert can be switched off; its built-in row stays in the list.

## How the layout works

Strips are drawn full width, top to bottom. The page is drawn, each strip's
height measured, and as many strips as fit are kept: first at the usual gap,
then with the gaps squeezed down to the smallest, and only then is the last
strip left out. A strip is never cut off or shrunk past reading size. Spare
height widens the gaps up to the largest and centres the rest.

When not everything fits (a small or landscape screen), the strips take turns:
each refresh shows the next ones that fit, either as many as fit or one at a
time (the "On small screens" setting). Each placement keeps its own turn. On a
Tesserae that passes the placement to widgets (`ctx["placement_id"]`) that is
per page, cell and device; on one that doesn't yet, it is per cell size, so
two identical cells of different sizes still keep separate turns.

## Fetching and storage

Renders never wait on a download. A strip is fetched once a day in the
background (and on **Fetch now**); a failure keeps the last good picture up,
retries with a backoff (30 minutes doubling to 6 hours, at most six tries a
day), and the small out-of-date note says which strip is behind. xkcd posts on
Mondays, Wednesdays and Fridays, so "latest" is often the same comic for a
couple of days: that is not an error.

Everything lives in the widget's data folder: `config.json` (the settings,
written atomically), `state.json`, `log.json`, `turns.json` and `images/`.
Pictures nothing refers to any more are deleted after an hour, and the folder
is kept under 200 MB.

## Network

Every download goes through Tesserae's guarded opener (`app.net_guard`):
public web addresses only, each redirect checked, sizes capped (20 MB a
picture). The manifest lists the GoComics, Far Side, Dilbert archive and
xkcd hosts it can reach. GoComics cookies pasted on the admin page are kept
in Tesserae's encrypted settings store. Its browser session stays in a
private cookie file in the widget's data folder.

## Requirements

Tesserae 0.484.1 or newer (for the guarded fetch that pins the checked
address). Uses Pillow, which Tesserae already ships.

## Tests

Drop the folder into a Tesserae checkout as `plugins/comics_strips/` and run:

```sh
.venv/bin/python -m pytest plugins/comics_strips/tests -q
```

The browser tests need Playwright's Chromium and are skipped without it.

## License

AGPL-3.0, as Tesserae.
