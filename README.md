# YouTube Playlist → MP3 Scraper

Downloads a YouTube playlist (or a single video) as MP3 files with full ID3 tags
and square album art embedded — the same output shape as ytdlp.online's playlist
downloader, but local, scriptable and resumable.

Runs on **Node.js**. Kokka & Company branded.

## What you get

- **MP3 at 320 kbps** (or 96–256 if you prefer a smaller file)
- **Embedded cover art** — the video thumbnail, centre-cropped to a square and
  written as an ID3 front-cover JPEG so it shows up in phones, car stereos and
  music players
- **Tags**: title, artist, album, album artist, track number `n/total`, year,
  genre, plus the source URL in the comment
- **Smart titles** — `Artist - Song (Official Video) [HD]` becomes artist
  `Artist`, title `Song`. YouTube Music / "- Topic" uploads use their real
  artist/track/album metadata instead of guesswork.
- **Resumable, and self-healing** — a track is skipped only when its MP3 is
  genuinely still in the folder. Every file carries its source URL in its tags,
  so the folder itself decides what's done. Delete an MP3 and re-run: it comes
  back.
- **Parallel downloads**, failures skipped rather than aborting the run
- **Automatic retry** — YouTube intermittently returns `403 Forbidden` on an audio
  stream. Each track is retried up to four times across different formats with a
  growing pause between attempts, which clears nearly all of them.

## Setup

Requires **Node.js 18+**, plus two binaries: `yt-dlp` and `ffmpeg`.

```bash
npm install                      # installs node-id3
sudo apt install ffmpeg          # if you don't have it
```

For yt-dlp, either is fine:

```bash
pipx install yt-dlp                                   # or: pip3 install --user yt-dlp
# ...or grab the standalone binary (no Python on your system at all):
#   https://github.com/yt-dlp/yt-dlp/releases  -> yt-dlp_linux
```

The tool looks for `yt-dlp` on your `PATH`, then `~/.local/bin/yt-dlp`,
`/usr/local/bin`, `/usr/bin`. Override with `YTDLP_PATH=/path/to/yt-dlp`
(and `FFMPEG_PATH` likewise).

> **A note on "no Python":** all of *this* project's code is Node. yt-dlp itself
> is a Python program, and it is the reason this tool survives YouTube's
> constant changes — no JavaScript extractor comes close on reliability. If you
> want Python off the machine entirely, use the `yt-dlp_linux` standalone build,
> which bundles its own runtime and needs no system Python. ffmpeg is a native
> binary either way.

## The easy way: the GUI

```bash
npm start          # or: node gui.js
```

A tab opens in your browser. Paste the link, press **Download**, watch the
progress bar. There's a **Stop** button and an **Open folder** button, and every
option below is available under "More options".

The GUI serves a small page from `127.0.0.1` on a random port, protected by a
one-time token, and closes when you press Ctrl+C in the terminal. Nothing is
exposed to your network.

## One song instead of a whole playlist

Tick **"Just this one song"** in the GUI, or pass `--single`:

```bash
node ytmp3.js "https://www.youtube.com/watch?v=..." --single
```

This matters because a song link copied from YouTube usually still carries
`&list=...`, and without it you'd get the entire album. Single mode pins the
download to that one video, saves it straight into your output folder (no album
subfolder) and drops the `01 - ` prefix, so you get `Song Title.mp3`.

Tags are unaffected — artist, album, year and cover art still come from the
video's own metadata. Add `--album "Singles"` if you'd rather file it in a folder.

## The command line

```bash
node ytmp3.js "https://www.youtube.com/playlist?list=PLxxxxxxxx"
```

Files land in `downloads/<Album Name>/01 - Song.mp3`.

### Common options

```bash
# Pick an output folder and bitrate
node ytmp3.js "<url>" -o ~/Music -b 320

# Force the album and artist tags for a full album rip
node ytmp3.js "<url>" --album "Kind of Blue" --artist "Miles Davis"

# Only tracks 1-5, four at a time
node ytmp3.js "<url>" --items 1-5 -j 4

# Age-restricted or private playlist — reuse your browser login
node ytmp3.js "<url>" --cookies-from-browser firefox

# Everything in one folder, no track-number prefix
node ytmp3.js "<url>" --flat --no-track-numbers
```

### All options

| Option | Purpose |
| --- | --- |
| `-o, --output` | Output directory (default `downloads`) |
| `-b, --bitrate` | 96 / 128 / 160 / 192 / 256 / 320 kbps (default 320) |
| `-j, --jobs` | Parallel downloads (default 3) |
| `--single` | Just this one song — ignore any playlist in the link |
| `--album` | Override the album tag and folder name |
| `--artist` | Override the artist tag on every track |
| `--album-artist` | Override the album-artist tag |
| `--items` | Playlist selection, e.g. `1-10` or `2,5,8` |
| `--flat` | No album subfolder |
| `--no-crop-art` | Keep the 16:9 thumbnail instead of a square crop |
| `--keep-cover-file` | Also write `cover.jpg` beside the MP3s |
| `--no-track-numbers` | Drop the `NN - ` filename prefix |
| `--name-with-artist` | Name files `NN - Artist - Title.mp3` |
| `--force` | Re-download tracks already present |
| `--cookies` | Path to a `cookies.txt` file |
| `--cookies-from-browser` | `chrome`, `firefox`, `brave`, … |
| `--rate-limit` | Cap speed, e.g. `2M` |
| `--proxy` | e.g. `socks5://127.0.0.1:1080` |

## The "not a bot" sign-in wall

YouTube demands a signed-in session for a lot of music, especially the
auto-generated album playlists (`list=OLAK5uy_...`). Without cookies those
downloads fail outright with *"Sign in to confirm you're not a bot"*.

This is handled for you: when a download hits that wall, the tool detects an
installed browser, borrows its YouTube login and retries. You'll see a line like
`YouTube asked for sign-in — retrying with firefox cookies ...` and the download
continues. Ubuntu's snap Firefox is found correctly (its profile lives under
`~/snap/firefox/...`, not `~/.mozilla`).

For it to work you must be **signed in to YouTube in that browser**. If the
retry still fails, sign in and try again, or pick the browser explicitly under
"Sign-in cookies" in the GUI / `--cookies-from-browser firefox` on the command
line. A profile path also works: `--cookies-from-browser firefox:/path/to/profile`.

## Project layout

| File | Purpose |
| --- | --- |
| `ytmp3.js` | Downloader core + CLI |
| `gui.js` | Local web server for the GUI |
| `ui.html` | The GUI page (Kokka-branded markup) |
| `assets/` | Brand tokens, logo, favicon |
| `legacy-python/` | The superseded Python implementation, kept for reference |

## Branding

The GUI follows the Kokka & Company brand spec. Assets in `assets/` are copied
from the `@kokka-company/design` package (`~/Documents/kokka-design`) — the
token stylesheet, the horizontal lockup, and the optically-corrected favicon.
They are inlined into the page at serve time, so the tool has no runtime
dependency on the design repo, and falls back to a built-in palette if
`assets/` goes missing.

To pick up a new version of the design system, re-copy the three files:

```bash
SRC=~/Documents/kokka-design
cp $SRC/tokens/tokens.css                     assets/kokka-tokens.css
cp $SRC/logos/svg/lockup-horizontal-white.svg assets/
cp $SRC/logos/favicon/favicon.ico             assets/
```

## Notes

- **About 320 kbps**: YouTube serves Opus/AAC around 128–160 kbps, so a 320 kbps
  MP3 is a transcode of that source — the file is genuinely 320 kbps (that is what
  ytdlp.online and every similar site produce), but it cannot add detail the
  source never had. `-b 192` gives near-identical sound in a smaller file.
- Keep `--jobs` modest. Too many parallel requests gets you throttled.
- Only download content you have the right to — your own uploads, Creative
  Commons material, or where the rights holder permits it.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `yt-dlp not found` | `pipx install yt-dlp`, or set `YTDLP_PATH` |
| `ffmpeg not found` | `sudo apt install ffmpeg` |
| Extractor errors after a YouTube change | Update yt-dlp — this fixes most breakage |
| Private / members-only playlist | `--cookies-from-browser <browser>` |
| `failed to load cookies` | The browser isn't installed, or its profile is locked — close the browser first, or export a `cookies.txt` and use `--cookies` |
| `Sign in to confirm you're not a bot` | Handled automatically; if it persists, sign in to YouTube in your browser first |
| Art not showing in a player | Some players cache art; re-import the file |
