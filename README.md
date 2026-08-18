# YouTube Playlist → MP3 Scraper

Downloads a YouTube playlist (or a single video) as MP3 files with full ID3 tags
and square album art embedded — the same output shape as ytdlp.online's playlist
downloader, but local, scriptable and resumable.

Runs on **Python 3**. Kokka & Company branded.

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
- **Automatic retry** — transient failures are retried across four different
  formats with a growing pause between attempts.
- **Handles YouTube's sign-in wall** — YouTube increasingly refuses anonymous
  audio downloads, handing back a media URL it then serves `403 Forbidden` on.
  The tool recognises this and immediately retries with your browser's cookies
  instead of working through the format ladder for nothing. It also solves
  YouTube's obfuscated `n` parameter, without which the returned URLs 403 on
  any sustained read.

## Setup

Requires **Python 3.9+** and `ffmpeg`.

```bash
pip3 install --user -r requirements.txt   # yt-dlp + mutagen
sudo apt install ffmpeg                   # if you don't have it
```

yt-dlp is imported as a library rather than shelled out to, so there is no
separate binary to install or keep on your `PATH` — `pip install -U yt-dlp` is
all an update takes. ffmpeg is a native binary; the tool finds it on your
`PATH`, or set `FFMPEG_PATH=/path/to/ffmpeg` to point at a specific build.

> **Why Python:** yt-dlp is itself a Python program, and it is the reason this
> tool survives YouTube's constant changes — no JavaScript extractor comes
> close on reliability. Sharing its runtime means one language on the machine
> instead of two, and lets the downloader use yt-dlp's API directly (structured
> metadata and progress callbacks) instead of parsing another process's output.

## The easy way: the GUI

```bash
python3 gui.py
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
python3 ytmp3.py "https://www.youtube.com/watch?v=..." --single
```

This matters because a song link copied from YouTube usually still carries
`&list=...`, and without it you'd get the entire album. Single mode pins the
download to that one video, saves it straight into your output folder (no album
subfolder) and drops the `01 - ` prefix, so you get `Song Title.mp3`.

Tags are unaffected — artist, album, year and cover art still come from the
video's own metadata. Add `--album "Singles"` if you'd rather file it in a folder.

## The command line

```bash
python3 ytmp3.py "https://www.youtube.com/playlist?list=PLxxxxxxxx"
```

Files land in `downloads/<Album Name>/01 - Song.mp3`.

### Common options

```bash
# Pick an output folder and bitrate
python3 ytmp3.py "<url>" -o ~/Music -b 320

# Force the album and artist tags for a full album rip
python3 ytmp3.py "<url>" --album "Kind of Blue" --artist "Miles Davis"

# Only tracks 1-5, four at a time
python3 ytmp3.py "<url>" --items 1-5 -j 4

# Age-restricted or private playlist — reuse your browser login
python3 ytmp3.py "<url>" --cookies-from-browser firefox

# Everything in one folder, no track-number prefix
python3 ytmp3.py "<url>" --flat --no-track-numbers
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
| `ytmp3.py` | Downloader core + CLI |
| `gui.py` | Local web server for the GUI |
| `mechenformat.py` | Tidy an existing MP3 for a hardware player |
| `ui.html` | The GUI page (Kokka-branded markup) |
| `assets/` | Brand tokens, logo, favicon |
| `requirements.txt` | yt-dlp + mutagen |

## Fixing up a file you already have

`mechenformat.py` normalises an MP3 that is already on disk — handy for a
single that came from somewhere else, or one whose art is too big for a
hardware player like a MECHEN.

```bash
python3 mechenformat.py "some file.mp3"
```

It renames the file after its ID3 title and rewrites the embedded cover as a
centre-cropped square JPEG of at most 640x640. Nothing else is touched, and a
file that is already correct is left alone rather than re-encoded.

| Flag | Effect |
| --- | --- |
| `-n` | Dry run — show what would change, write nothing |
| `-a` | Name the file `Artist - Title.mp3` rather than `Title.mp3` |
| `-s N` | Use an edge length other than 640 |
| `-e` | Upscale art smaller than the target instead of leaving it be |

Several files at once is fine: `python3 mechenformat.py ~/Music/*.mp3`.

> It names files from the **title tag alone**, so running it across an album
> folder strips the `01 - ` prefixes and loses the running order in the
> filenames. It is meant for singles; leave album folders to `ytmp3.py`.

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
| `yt-dlp is not installed` | `pip3 install --user -r requirements.txt` |
| `ffmpeg not found` | `sudo apt install ffmpeg`, or set `FFMPEG_PATH` |
| Extractor errors after a YouTube change | `pip3 install --user -U yt-dlp` — this fixes most breakage |
| `403 Forbidden` on every format | YouTube is refusing anonymous downloads. The tool now detects this and retries with your browser's cookies automatically; if no browser is found, pass `--cookies-from-browser firefox` |
| Private / members-only playlist | `--cookies-from-browser <browser>` |
| `failed to load cookies` | The browser isn't installed, or its profile is locked — close the browser first, or export a `cookies.txt` and use `--cookies` |
| `Sign in to confirm you're not a bot` | Handled automatically; if it persists, sign in to YouTube in your browser first |
| Art not showing in a player | Some players cache art; re-import the file |
