#!/usr/bin/env python3
"""
YouTube playlist -> tagged MP3 scraper.

Downloads a YouTube playlist (or single video) as MP3s at a chosen bitrate,
writes full ID3 tags (title, artist, album, track number, year) and embeds
the video thumbnail as square front-cover album art.

Usage:
    python3 ytmp3.py <playlist-or-video-url> [options]

See README.md for the full option list.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp is not installed. Run: pip3 install --user -r requirements.txt")

try:
    from mutagen.id3 import (
        ID3, ID3NoHeaderError, APIC, COMM, TALB, TCON, TDRC,
        TIT2, TPE1, TPE2, TRCK, WOAS,
    )
    from mutagen.mp3 import MP3
except ImportError:
    sys.exit("mutagen is not installed. Run: pip3 install --user -r requirements.txt")


def find_ffmpeg():
    """ffmpeg from $FFMPEG_PATH if set, else the first one on PATH."""
    override = os.environ.get("FFMPEG_PATH")
    if override and Path(override).is_file():
        return override
    return shutil.which("ffmpeg")


ARCHIVE_NAME = ".downloaded.txt"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# Hardware MP3 players (e.g. MECHEN) reject oversized embedded art. Cover art
# is written as JPEG, square, and never larger than this on either side.
MAX_ART_PX = 640

# YouTube intermittently 403s individual audio itags, and hitting it again
# straight away usually 403s too. Each (format, seconds-to-wait-after-failure)
# pair is tried in turn before a track is written off as failed; the third
# attempt re-tries the best format after a longer pause, which is what most
# often clears transient throttling.
FORMAT_FALLBACKS = (
    ("bestaudio/best", 3),
    ("bestaudio[ext=m4a]/bestaudio", 8),
    ("bestaudio/best", 15),
    ("best", 0),
)

# Bracketed noise that YouTube uploaders bolt onto titles. Only bracket groups
# containing one of these words are stripped, so "(feat. X)", "(Remix)" and
# "(Live)" survive.
_JUNK_WORDS = (
    r"official|lyrics?|lyric\s*video|audio|video|visuali[sz]er|"
    r"hd|hq|4k|8k|mv|m/v|full\s*album|full\s*song|remaster(?:ed)?|"
    r"explicit|clean\s*version|free\s*download|download|out\s*now|"
    r"with\s*lyrics|color\s*coded"
)
_JUNK_RE = [
    re.compile(rf"\((?:[^()]*\b(?:{_JUNK_WORDS})\b[^()]*)\)", re.I),
    re.compile(rf"\[(?:[^\[\]]*\b(?:{_JUNK_WORDS})\b[^\[\]]*)\]", re.I),
    re.compile(rf"\{{(?:[^{{}}]*\b(?:{_JUNK_WORDS})\b[^{{}}]*)\}}", re.I),
]
# "Artist - Song | Official Video" — strip a trailing pipe segment that is noise.
_PIPE_JUNK_RE = re.compile(rf"\s*[|｜]\s*[^|｜]*\b(?:{_JUNK_WORDS})\b[^|｜]*$", re.I)
_LEADING_TRACK_RE = re.compile(r"^\s*\d{1,3}\s*[-.)\]]\s+")
# Channel plugs tacked on the end: "Song - incompetech.com", "Song | artist.net".
_PROMO_URL_RE = re.compile(
    r"\s*[-–—|]\s*(?:https?://)?(?:www\.)?[\w-]+"
    r"\.(?:com|net|org|io|co|tv|fm|me|info|biz|uk|de)/?\s*$",
    re.I,
)
_SPLIT_RE = re.compile(r"\s+[-–—]\s+")

print_lock = threading.Lock()
archive_lock = threading.Lock()

# Set by gui.py so it can stop a run and follow progress. The CLI leaves both
# alone: CANCEL is never set, and PROGRESS_HOOK stays None.
CANCEL = threading.Event()
PROGRESS_HOOK = None
LAST_OUTPUT_DIR = None


def log(msg):
    with print_lock:
        print(msg, flush=True)


class QuietLogger:
    """Swallows yt-dlp chatter; real errors are surfaced by the caller."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


# ---------------------------------------------------------------- text helpers


def clean_title(text):
    """Strip promo noise and leading track numbers from a video title."""
    if not text:
        return ""
    out = text
    for pattern in _JUNK_RE:
        out = pattern.sub(" ", out)
    out = _PIPE_JUNK_RE.sub("", out)
    out = _PROMO_URL_RE.sub("", out)
    out = _LEADING_TRACK_RE.sub("", out)
    out = re.sub(r"\s*[|｜]\s*$", "", out)
    out = re.sub(r"\s+", " ", out)
    return out.strip(" -–—_|.")


def split_artist_title(text):
    """Split "Artist - Song" into a pair. Returns (None, text) if there is no split."""
    parts = _SPLIT_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        artist, title = parts[0].strip(), parts[1].strip()
        # A very long left side is usually part of the song name, not an artist.
        if artist and title and len(artist) <= 60:
            return artist, title
    return None, text.strip()


def clean_album_name(text):
    if not text:
        return ""
    out = re.sub(r"\((?:full\s*)?album\)|\[(?:full\s*)?album\]", " ", text, flags=re.I)
    out = re.sub(r"\b(?:full\s*album|complete\s*album|playlist)\b", " ", out, flags=re.I)
    out = re.sub(r"\s+", " ", out)
    return out.strip(" -–—_|.") or text.strip()


def sanitize_filename(name, max_len=180):
    """Make a string safe for ext4/NTFS/exFAT filenames."""
    out = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "-", name)
    out = re.sub(r"\s+", " ", out).strip().rstrip(".")
    if len(out) > max_len:
        out = out[:max_len].rstrip()
    return out or "track"


def derive_tags(info, args, index, total, playlist_title, playlist_uploader):
    """Work out the final artist/title/album/year for one video."""
    raw_title = info.get("title") or "Unknown"
    uploader = info.get("uploader") or info.get("channel") or ""

    # YouTube Music / "- Topic" uploads carry real music metadata already.
    meta_artist = info.get("artist") or info.get("creator")
    meta_track = info.get("track")
    meta_album = info.get("album")

    topic_artist = None
    if uploader.endswith(" - Topic"):
        topic_artist = uploader[: -len(" - Topic")].strip()

    cleaned = clean_title(raw_title)
    split_artist, split_title = split_artist_title(cleaned)

    title = meta_track or split_title or cleaned or raw_title
    artist = (
        args.artist
        or meta_artist
        or topic_artist
        or split_artist
        or uploader
        or "Unknown Artist"
    )
    # yt-dlp joins multiple artists with ", " already; normalise separators.
    artist = re.sub(r"\s*[;/]\s*", ", ", artist).strip()

    album = (
        args.album
        or meta_album
        or clean_album_name(playlist_title)
        or clean_album_name(title)
    )
    album_artist = args.album_artist or (
        artist if args.artist or meta_artist or topic_artist else
        (playlist_uploader or artist)
    )

    year = ""
    if info.get("release_year"):
        year = str(info["release_year"])
    elif info.get("release_date"):
        year = str(info["release_date"])[:4]
    elif info.get("upload_date"):
        year = str(info["upload_date"])[:4]

    return {
        "title": title.strip(),
        "artist": artist,
        "album": album.strip(),
        "album_artist": (album_artist or artist).strip(),
        "year": year,
        "genre": (info.get("genre") or "").strip(),
        "track": index,
        "total": total,
        "url": info.get("webpage_url") or info.get("original_url") or "",
    }


# ------------------------------------------------------------------- cover art


def find_thumbnail(track_dir):
    """Find the thumbnail yt-dlp wrote in this track's private work directory."""
    try:
        for candidate in sorted(Path(track_dir).iterdir()):
            if candidate.suffix.lower() in IMAGE_EXTS:
                return candidate
    except OSError:
        return None
    return None


def prepare_cover(src, dest, square=True, max_size=MAX_ART_PX):
    """
    Convert a thumbnail to JPEG cover art: centre-cropped square, downscaled to
    at most max_size. Never upscales art that is already smaller.
    """
    vf = []
    if square:
        vf.append("crop='min(iw,ih)':'min(iw,ih)'")
    vf.append(f"scale='min({max_size},iw)':'min({max_size},ih)':flags=lanczos")
    cmd = [
        find_ffmpeg() or "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src),
        "-vf", ",".join(vf),
        "-frames:v", "1", "-q:v", "2",
        str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return dest if dest.exists() and dest.stat().st_size > 0 else None


# ---------------------------------------------------------------------- tagging


def write_tags(mp3_path, tags, cover_path):
    try:
        id3 = ID3(str(mp3_path))
    except ID3NoHeaderError:
        id3 = ID3()

    id3.delall("APIC")
    id3.setall("TIT2", [TIT2(encoding=3, text=tags["title"])])
    id3.setall("TPE1", [TPE1(encoding=3, text=tags["artist"])])
    id3.setall("TPE2", [TPE2(encoding=3, text=tags["album_artist"])])
    id3.setall("TALB", [TALB(encoding=3, text=tags["album"])])
    id3.setall("TRCK", [TRCK(encoding=3, text=f"{tags['track']}/{tags['total']}")])
    if tags["year"]:
        id3.setall("TDRC", [TDRC(encoding=3, text=tags["year"])])
    if tags["genre"]:
        id3.setall("TCON", [TCON(encoding=3, text=tags["genre"])])
    if tags["url"]:
        id3.setall("WOAS", [WOAS(url=tags["url"])])
        id3.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=tags["url"])])

    if cover_path and cover_path.exists():
        id3.add(
            APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # front cover
                desc="Cover",
                data=cover_path.read_bytes(),
            )
        )

    # ID3v2.3 has the widest player support (Windows, car head units, phones).
    id3.save(str(mp3_path), v2_version=3)


# --------------------------------------------------------------- archive/state


VIDEO_ID_RE = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")


def load_archive(path):
    """Read the archive as {video_id: filename or None} (None = legacy entry)."""
    if not path.exists():
        return {}
    entries = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        video_id, _, filename = line.partition("\t")
        entries[video_id] = filename or None
    return entries


def append_archive(path, video_id, filename):
    with archive_lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{video_id}\t{filename}\n")


def scan_existing_ids(out_dir):
    """Video IDs of MP3s actually sitting in the folder, read from their tags.

    Every file this tool writes carries its source URL, which makes the folder
    itself the source of truth: delete an MP3 and it gets fetched again, instead
    of being skipped forever because a hidden archive file still lists it.
    """
    found = set()
    for mp3 in out_dir.glob("*.mp3"):
        try:
            tags = ID3(str(mp3))
        except Exception:  # noqa: BLE001 - unreadable tags just mean "unknown"
            continue
        candidates = [frame.url for frame in tags.getall("WOAS")]
        for comment in tags.getall("COMM"):
            candidates.extend(str(t) for t in comment.text)
        for value in candidates:
            match = VIDEO_ID_RE.search(value or "")
            if match:
                found.add(match.group(1))
                break
    return found


def resolve_done_ids(archive_path, out_dir):
    """Tracks that can be safely skipped: their file is genuinely still there."""
    on_disk = scan_existing_ids(out_dir)
    verified = {
        video_id
        for video_id, filename in load_archive(archive_path).items()
        # Legacy entries recorded no filename, so they cannot be trusted.
        if filename and (out_dir / filename).exists()
    }
    return on_disk | verified


# ------------------------------------------------------------------- downloads


# Where each browser keeps its profile on Linux, including snap/flatpak layouts
# (Ubuntu's Firefox is a snap, so it is NOT under ~/.mozilla).
BROWSER_PROFILE_DIRS = {
    "firefox": (
        "~/.mozilla/firefox",
        "~/snap/firefox/common/.mozilla/firefox",
        "~/.var/app/org.mozilla.firefox/.mozilla/firefox",
    ),
    "chrome": ("~/.config/google-chrome",),
    "brave": ("~/.config/BraveSoftware/Brave-Browser",),
    "chromium": ("~/.config/chromium", "~/snap/chromium/common/chromium"),
    "edge": ("~/.config/microsoft-edge",),
    "vivaldi": ("~/.config/vivaldi",),
    "opera": ("~/.config/opera",),
}

cookie_lock = threading.Lock()


def detect_browsers():
    """Browsers that actually have a profile on this machine, best first."""
    found = []
    for name, paths in BROWSER_PROFILE_DIRS.items():
        if any(Path(p).expanduser().is_dir() for p in paths):
            found.append(name)
    return found


def parse_cookies_from_browser(spec):
    """Parse yt-dlp's BROWSER[+KEYRING][:PROFILE][::CONTAINER] into its 4-tuple."""
    container = None
    if "::" in spec:
        spec, container = spec.split("::", 1)
    profile = None
    if ":" in spec:
        spec, profile = spec.split(":", 1)
    keyring = None
    if "+" in spec:
        spec, keyring = spec.split("+", 1)
    return (
        spec.strip().lower(),
        profile.strip() or None if profile else None,
        keyring.strip().upper() or None if keyring else None,
        container.strip() or None if container else None,
    )


def is_bot_check(message):
    """True when YouTube is demanding a signed-in session rather than failing."""
    text = str(message).lower()
    return "not a bot" in text or "sign in to confirm" in text


def auto_enable_cookies(args):
    """On a bot check, fall back to whatever browser is installed. Returns its name."""
    with cookie_lock:
        if args.cookies or args.cookies_from_browser:
            return None  # the user already chose; don't override it
        browsers = detect_browsers()
        if not browsers:
            return None
        args.cookies_from_browser = browsers[0]
        return browsers[0]


def _abort_if_cancelled(status):
    """yt-dlp progress hook: aborts an in-flight download when the GUI stops."""
    if CANCEL.is_set():
        raise yt_dlp.utils.DownloadError("cancelled by user")


def build_ydl_opts(args, track_dir, video_id, fmt="bestaudio/best"):
    opts = {
        "format": fmt,
        "progress_hooks": [_abort_if_cancelled],
        # Each track downloads into its own directory so the finished file can
        # be found by scanning rather than by predicting yt-dlp's naming,
        # which varies per site.
        "outtmpl": str(Path(track_dir) / "%(id)s.%(ext)s"),
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": QuietLogger(),
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "prefer_ffmpeg": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(args.bitrate),
            }
        ],
    }
    # Keep yt-dlp on the same binary the cover crop uses.
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    if args.cookies:
        opts["cookiefile"] = args.cookies
    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = parse_cookies_from_browser(args.cookies_from_browser)
    if args.rate_limit:
        opts["ratelimit"] = args.rate_limit
    if args.proxy:
        opts["proxy"] = args.proxy
    return opts


def probe(url, args):
    """Fetch the playlist listing (or single video) without downloading."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": QuietLogger(),
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "socket_timeout": 30,
    }
    if args.cookies:
        opts["cookiefile"] = args.cookies
    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = parse_cookies_from_browser(args.cookies_from_browser)
    if args.single:
        # A song link copied from a playlist still carries &list=; this pins the
        # extraction to the one video instead of pulling the whole album.
        opts["noplaylist"] = True
    if args.items:
        opts["playlist_items"] = args.items
    if args.proxy:
        opts["proxy"] = args.proxy

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise RuntimeError("Could not read that URL (private, deleted, or blocked).")
    return info


def download_one(entry, index, total, args, out_dir, work_dir, playlist_title,
                 playlist_uploader, archive_path, done_ids):
    """Download, convert, tag and file a single track. Returns a status dict."""
    video_id = entry.get("id") or ""
    label = entry.get("title") or video_id

    if CANCEL.is_set():
        return {"status": "cancelled", "title": label}

    if video_id and video_id in done_ids and not args.force:
        log(f"[{index}/{total}] skip — already in this folder  {label}")
        return {"status": "skipped", "title": label}

    # webpage_url first. On SoundCloud a fully-extracted entry puts the raw HLS
    # stream URL in "url"; handing that to yt-dlp downloads a stream it then
    # names unpredictably, so the expected output file never appears. Flat
    # playlist entries have no webpage_url and correctly fall through to "url".
    url = entry.get("webpage_url") or entry.get("url") or video_id
    if url and not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={url}"

    stem_id = video_id or f"track{index:03d}"
    track_dir = work_dir / stem_id
    shutil.rmtree(track_dir, ignore_errors=True)
    track_dir.mkdir(parents=True, exist_ok=True)

    info = None
    last_error = "no media returned"
    for attempt, (fmt, backoff) in enumerate(FORMAT_FALLBACKS, start=1):
        if CANCEL.is_set():
            return {"status": "cancelled", "title": label}
        opts = build_ydl_opts(args, track_dir, stem_id, fmt)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            if info:
                break
        except Exception as exc:  # noqa: BLE001 - one bad video must not stop the run
            last_error = str(exc)
            if CANCEL.is_set():
                return {"status": "cancelled", "title": label}
            if is_bot_check(last_error):
                # YouTube wants a signed-in session. Remaining attempts reuse
                # the browser's cookies, which usually clears it outright.
                browser = auto_enable_cookies(args)
                if browser:
                    log(f"[{index}/{total}] YouTube asked for sign-in — "
                        f"retrying with {browser} cookies ...")
                    continue
                last_error = (
                    "YouTube requires a signed-in session for this track. "
                    "Set 'Sign-in cookies from browser' (e.g. firefox) and retry."
                )
            if attempt < len(FORMAT_FALLBACKS):
                # 403s here are usually transient throttling; pausing before the
                # next attempt clears them far more often than retrying at once.
                log(f"[{index}/{total}] retrying {label} in {backoff}s ...")
                time.sleep(backoff)

    if not info:
        log(f"[{index}/{total}] FAILED  {label}: {last_error}")
        return {"status": "failed", "title": label, "error": last_error}

    produced = sorted(p.name for p in track_dir.iterdir()) if track_dir.exists() else []
    mp3_path = next((track_dir / n for n in produced if n.lower().endswith(".mp3")), None)
    if mp3_path is None:
        msg = f"converted file not found (yt-dlp produced: {', '.join(produced) or 'nothing'})"
        log(f"[{index}/{total}] FAILED  {label}: {msg}")
        shutil.rmtree(track_dir, ignore_errors=True)
        return {"status": "failed", "title": label, "error": msg}

    tags = derive_tags(info, args, index, total, playlist_title, playlist_uploader)

    cover = None
    thumb = find_thumbnail(track_dir)
    if thumb:
        cover = prepare_cover(
            thumb,
            track_dir / "_cover.jpg",
            square=not args.no_crop_art,
        )

    try:
        write_tags(mp3_path, tags, cover)
    except Exception as exc:  # noqa: BLE001 - keep the audio even if tagging trips
        log(f"[{index}/{total}] warning: tagging failed for {label}: {exc}")

    if args.name_with_artist:
        stem = f"{index:02d} - {tags['artist']} - {tags['title']}"
    else:
        stem = f"{index:02d} - {tags['title']}"
    if args.no_track_numbers:
        stem = stem.split(" - ", 1)[1] if " - " in stem else stem
    final_path = out_dir / f"{sanitize_filename(stem)}.mp3"

    counter = 2
    while final_path.exists() and final_path.resolve() != mp3_path.resolve():
        final_path = out_dir / f"{sanitize_filename(stem)} ({counter}).mp3"
        counter += 1

    try:
        shutil.move(str(mp3_path), str(final_path))
    except OSError as exc:
        log(f"[{index}/{total}] FAILED  {label}: could not move file: {exc}")
        return {"status": "failed", "title": label, "error": str(exc)}

    # The whole per-track directory is scratch; the audio has already moved out.
    shutil.rmtree(track_dir, ignore_errors=True)

    if args.keep_cover_file:
        art = out_dir / "cover.jpg"
        if not art.exists():
            thumb_again = None  # cover already consumed; re-extract from the tag
            try:
                audio = ID3(str(final_path))
                pics = audio.getall("APIC")
                if pics:
                    art.write_bytes(pics[0].data)
            except Exception:  # noqa: BLE001
                pass
            del thumb_again

    if video_id:
        append_archive(archive_path, video_id, final_path.name)

    try:
        kbps = int(MP3(str(final_path)).info.bitrate / 1000)
    except Exception:  # noqa: BLE001
        kbps = args.bitrate

    log(f"[{index}/{total}] ok  {final_path.name}  ({kbps} kbps)")
    return {"status": "ok", "title": tags["title"], "path": str(final_path)}


# ----------------------------------------------------------------------- entry


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="ytmp3",
        description="Download a YouTube playlist as tagged MP3s with embedded album art.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("url", help="YouTube playlist, album, channel or single video URL")
    p.add_argument("-o", "--output", default="downloads",
                   help="Output directory (an album subfolder is created inside)")
    p.add_argument("-b", "--bitrate", type=int, default=320,
                   choices=[96, 128, 160, 192, 256, 320],
                   help="MP3 bitrate in kbps")
    p.add_argument("-j", "--jobs", type=int, default=3,
                   help="Parallel downloads (keep low to avoid throttling)")
    p.add_argument("--album", help="Override the album tag")
    p.add_argument("--artist", help="Override the artist tag for every track")
    p.add_argument("--album-artist", help="Override the album-artist tag")
    p.add_argument("--single", action="store_true",
                   help="Just this one song — ignore any playlist in the link, "
                        "save straight to the output folder with no track number")
    p.add_argument("--items", help="Playlist selection, e.g. '1-10' or '2,5,8'")
    p.add_argument("--flat", action="store_true",
                   help="Write MP3s straight into --output, no album subfolder")
    p.add_argument("--no-crop-art", action="store_true",
                   help="Keep the 16:9 thumbnail instead of cropping to a square")
    p.add_argument("--keep-cover-file", action="store_true",
                   help="Also save cover.jpg next to the MP3s")
    p.add_argument("--no-track-numbers", action="store_true",
                   help="Do not prefix filenames with the track number")
    p.add_argument("--name-with-artist", action="store_true",
                   help="Name files 'NN - Artist - Title.mp3'")
    p.add_argument("--force", action="store_true",
                   help="Re-download tracks already listed in the archive file")
    p.add_argument("--cookies", help="Path to a cookies.txt file")
    p.add_argument("--cookies-from-browser",
                   help="Load cookies from a browser, e.g. chrome, firefox, brave")
    p.add_argument("--rate-limit", type=int,
                   help="Max download speed in bytes/sec, e.g. 2000000")
    p.add_argument("--proxy", help="Proxy URL, e.g. socks5://127.0.0.1:1080")
    return p.parse_args(argv)


def main(argv=None):
    global LAST_OUTPUT_DIR
    args = parse_args(argv or sys.argv[1:])

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        sys.exit(
            "ffmpeg not found on PATH. Install it: sudo apt install ffmpeg\n"
            "(or point FFMPEG_PATH at the binary)"
        )

    log(f"Reading {args.url} ...")
    try:
        info = probe(args.url, args)
    except Exception as exc:  # noqa: BLE001
        # The playlist listing itself can hit the sign-in wall; same fix.
        browser = auto_enable_cookies(args) if is_bot_check(exc) else None
        if not browser:
            sys.exit(f"Error: {exc}")
        log(f"YouTube asked for sign-in — retrying with {browser} cookies ...")
        try:
            info = probe(args.url, args)
        except Exception as exc2:  # noqa: BLE001
            sys.exit(f"Error: {exc2}")

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        playlist_title = info.get("title") or ""
        playlist_uploader = info.get("uploader") or info.get("channel") or ""
    else:
        entries = [info]
        playlist_title = ""
        playlist_uploader = info.get("uploader") or info.get("channel") or ""

    if not entries:
        sys.exit("Nothing to download — the playlist is empty or unavailable.")

    if args.single:
        # One song shouldn't get its own album folder or a "01 - " prefix,
        # unless the user deliberately named an album to file it under.
        args.no_track_numbers = True
        if not args.album:
            args.flat = True

    album_folder = args.album or clean_album_name(playlist_title) or "YouTube Audio"
    base = Path(args.output).expanduser()
    out_dir = base if args.flat else base / sanitize_filename(album_folder)
    work_dir = out_dir / ".work"
    out_dir.mkdir(parents=True, exist_ok=True)
    # A previous run that was killed outright leaves scratch files behind.
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(exist_ok=True)
    LAST_OUTPUT_DIR = str(out_dir)

    archive_path = out_dir / ARCHIVE_NAME
    done_ids = set() if args.force else resolve_done_ids(archive_path, out_dir)

    total = len(entries)
    log(f"{total} track(s) -> {out_dir}  @ {args.bitrate} kbps, {args.jobs} at a time\n")

    # Publish the track count before any downloading starts, so a progress bar
    # can show "0 of N" immediately instead of sitting empty until track 1 lands.
    if PROGRESS_HOOK:
        try:
            PROGRESS_HOOK(0, total, None)
        except Exception:  # noqa: BLE001
            pass

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {
            pool.submit(
                download_one, entry, idx, total, args, out_dir, work_dir,
                playlist_title, playlist_uploader, archive_path, done_ids,
            ): idx
            for idx, entry in enumerate(entries, start=1)
        }
        for fut in as_completed(futures):
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                result = {"status": "failed", "title": "?", "error": str(exc)}
            results.append(result)
            if PROGRESS_HOOK:
                try:
                    PROGRESS_HOOK(len(results), total, result)
                except Exception:  # noqa: BLE001 - a bad UI hook must not stop work
                    pass

    # Clean up scratch space, leaving the archive and MP3s behind.
    shutil.rmtree(work_dir, ignore_errors=True)

    ok = sum(1 for r in results if r["status"] == "ok")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    cancelled = sum(1 for r in results if r["status"] == "cancelled")
    failed = [r for r in results if r["status"] == "failed"]

    tail = f", {cancelled} cancelled" if cancelled else ""
    log(f"\nDone: {ok} downloaded, {skipped} skipped, {len(failed)} failed{tail}.")
    if failed:
        log("Failed tracks:")
        for r in failed:
            log(f"  - {r['title']}: {r.get('error', 'unknown error')}")
    log(f"Files are in: {out_dir}")
    return 1 if failed and ok == 0 else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Re-run the same command to resume.")
