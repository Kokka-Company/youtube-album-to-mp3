#!/usr/bin/env python3
"""
Normalise an existing MP3 for a hardware player (e.g. MECHEN).

    python3 mechenformat.py "some file.mp3"

Renames the file after its ID3 title and rewrites the embedded cover art as a
centre-cropped square JPEG of at most 640x640 — the two things cheap players
are fussy about. Tags other than the artwork are left alone.

Pass several files, or -n to see what would happen without touching anything.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from ytmp3 import (
        MAX_ART_PX,
        clean_title,
        find_ffmpeg,
        prepare_cover,
        sanitize_filename,
        split_artist_title,
    )
except ImportError:
    sys.exit("Could not find ytmp3.py — run mechenformat.py from the same folder.")

try:
    from mutagen.id3 import ID3, ID3NoHeaderError, APIC, TALB, TPE1, TPE2, TRCK, TDRC
except ImportError:
    sys.exit("mutagen is not installed. Run: pip3 install --user -r requirements.txt")


def image_size(path):
    """(width, height) of an image file, or None if ffprobe can't read it."""
    ffprobe = (find_ffmpeg() or "ffmpeg").replace("ffmpeg", "ffprobe")
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30, text=True).stdout
        width, height = (int(n) for n in out.strip().split(",")[:2])
        return width, height
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None


def read_title(id3, path):
    """The track title: the ID3 tag if there is one, else the filename."""
    frames = id3.getall("TIT2")
    if frames and str(frames[0].text[0]).strip():
        return str(frames[0].text[0]).strip()
    # No title tag — fall back to the filename, which usually carries it in the
    # same "Artist - Song" shape a download would have produced.
    return clean_title(split_artist_title(path.stem)[1]) or path.stem


def read_artist(id3, path):
    frames = id3.getall("TPE1")
    if frames and str(frames[0].text[0]).strip():
        return str(frames[0].text[0]).strip()
    return split_artist_title(path.stem)[0] or ""


def rewrite_cover(id3, args, report):
    """Re-encode the embedded art as a square JPEG. True if the file changed."""
    covers = id3.getall("APIC")
    if not covers:
        report.append("no embedded cover art — nothing to crop")
        return False

    # Prefer the front cover if the file carries several images.
    art = next((c for c in covers if c.type == 3), covers[0])

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "in"
        src.write_bytes(art.data)

        before = image_size(src)
        if before == (args.size, args.size) and art.mime == "image/jpeg":
            report.append(f"cover already {args.size}x{args.size} JPEG — left as is")
            return False

        out = prepare_cover(src, tmp / "cover.jpg", square=True, max_size=args.size)
        if out is None:
            report.append("cover could not be re-encoded — left as is")
            return False

        if args.exact:
            # prepare_cover never upscales, so art smaller than the target comes
            # back undersized. Only stretch it when asked to.
            after = image_size(out)
            if after and after != (args.size, args.size):
                stretched = tmp / "exact.jpg"
                cmd = [
                    find_ffmpeg() or "ffmpeg", "-y", "-loglevel", "error",
                    "-i", str(out),
                    "-vf", f"scale={args.size}:{args.size}:flags=lanczos",
                    "-frames:v", "1", "-q:v", "2", str(stretched),
                ]
                try:
                    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
                    out = stretched
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                    pass

        after = image_size(out)
        shown_before = f"{before[0]}x{before[1]}" if before else "unreadable"
        shown_after = f"{after[0]}x{after[1]}" if after else "?"
        if after and after != (args.size, args.size):
            # Say so rather than implying the target was reached.
            report.append(
                f"cover {shown_before} -> {shown_after} "
                f"(source too small for {args.size}x{args.size}; -e forces it)"
            )
        else:
            report.append(f"cover {shown_before} -> {shown_after}")

        if not args.dry_run:
            id3.delall("APIC")
            id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover",
                         data=out.read_bytes()))
        return True


ALBUM_FRAMES = {"TALB": TALB, "TPE1": TPE1, "TPE2": TPE2, "TRCK": TRCK, "TDRC": TDRC}


def album_tags(id3, path, args, index, total, report):
    """Apply the album-wide tags. True if anything changed."""
    wanted = {}
    if args.album:
        wanted["TALB"] = args.album
    if args.artist:
        wanted["TPE1"] = args.artist
    if args.album_artist:
        wanted["TPE2"] = args.album_artist
    if args.year:
        wanted["TDRC"] = args.year
    if args.album:
        # Track number comes from the NN- prefix when the file has one, so a
        # folder that is already ordered keeps its order.
        stem = path.name[:2]
        n = int(stem) if stem.isdigit() else index
        wanted["TRCK"] = f"{n}/{total}"

    changed = False
    for key, value in wanted.items():
        cur = id3.getall(key)
        cur = str(cur[0].text[0]) if cur else None
        if cur != value:
            report.append(f"{key}: {cur!r} -> {value!r}")
            if not args.dry_run:
                id3.setall(key, [ALBUM_FRAMES[key](encoding=3, text=value)])
            changed = True
    return changed


def target_name(id3, path, args):
    """The filename this track should have, based on its title tag."""
    title = read_title(id3, path)
    if args.with_artist:
        artist = read_artist(id3, path)
        if artist:
            title = f"{artist} - {title}"
    return sanitize_filename(title) + path.suffix.lower()


def process(path, args, index=1, total=1):
    path = Path(path)
    if not path.is_file():
        print(f"skip  {path}: not a file")
        return False
    if path.suffix.lower() != ".mp3":
        print(f"skip  {path}: not an .mp3")
        return False

    try:
        id3 = ID3(str(path))
    except ID3NoHeaderError:
        id3 = ID3()
    except Exception as exc:  # noqa: BLE001 - a bad file must not stop the batch
        print(f"skip  {path.name}: {exc}")
        return False

    report = []
    changed = rewrite_cover(id3, args, report)
    changed |= album_tags(id3, path, args, index, total, report)

    if changed and not args.dry_run:
        # ID3v2.3 has the widest player support (Windows, car head units, phones).
        id3.save(str(path), v2_version=3)

    if args.art_only or args.album:
        print(f"{'would fix' if args.dry_run else 'ok'}  {path.name}")
        for line in report:
            print(f"      {line}")
        return True

    wanted = target_name(id3, path, args)
    dest = path.with_name(wanted)
    if dest == path:
        report.append("filename already matches the title")
    elif dest.exists():
        report.append(f"NOT renamed — {wanted!r} already exists")
    else:
        report.append(f"renamed -> {wanted!r}")
        if not args.dry_run:
            path.rename(dest)

    print(f"{'would fix' if args.dry_run else 'ok'}  {path.name}")
    for line in report:
        print(f"      {line}")
    return True


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="mechenformat",
        description="Rename an MP3 after its title tag and square up its cover art.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("files", nargs="+", metavar="FILE", help="MP3 file(s) to format")
    p.add_argument("-s", "--size", type=int, default=MAX_ART_PX,
                   help="Cover art edge length in pixels")
    p.add_argument("-e", "--exact", action="store_true",
                   help="Upscale art that is smaller than the target instead of "
                        "leaving it at its own size")
    p.add_argument("-a", "--with-artist", action="store_true",
                   help='Name the file "Artist - Title.mp3" instead of "Title.mp3"')
    p.add_argument("-A", "--art-only", action="store_true",
                   help="Only fix the cover art — leave the filename alone. Use "
                        "this on album folders, where the NN- prefix matters")
    p.add_argument("--album", metavar="NAME",
                   help="Treat the files as one album: set the album tag, "
                        "renumber tracks as n/total, and leave filenames alone")
    p.add_argument("--album-artist", metavar="NAME", help="Set the album artist tag")
    p.add_argument("--artist", metavar="NAME", help="Set the artist tag on every file")
    p.add_argument("--year", metavar="YYYY", help="Set the year tag on every file")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="Show what would change without writing anything")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if not find_ffmpeg():
        sys.exit("ffmpeg not found on PATH. Install it: sudo apt install ffmpeg\n"
                 "(or point FFMPEG_PATH at the binary)")
    total = len(args.files)
    done = sum(process(f, args, i, total) for i, f in enumerate(args.files, 1))
    print(f"\n{done} of {len(args.files)} file(s) processed"
          f"{' (dry run — nothing written)' if args.dry_run else ''}.")


if __name__ == "__main__":
    main()
