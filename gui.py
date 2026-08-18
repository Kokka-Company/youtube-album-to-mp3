#!/usr/bin/env python3
"""
Point-and-click front end for ytmp3.py.

Run it and a browser tab opens: paste a YouTube playlist link, press Download.

    python3 gui.py

It serves a tiny page from 127.0.0.1 on a random free port (nothing is exposed
to your network) and drives ytmp3.py in a background thread. Uses only the
Python standard library, so there is nothing extra to install beyond what
ytmp3.py already needs.
"""

import json
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import ytmp3
except ImportError:
    sys.exit("Could not find ytmp3.py — run gui.py from the same folder.")

TOKEN = secrets.token_urlsafe(16)
DEFAULT_OUTPUT = str(Path.home() / "Music")

# Brand assets copied from the @kokka-company/design package — see assets/SOURCE.txt.
ASSETS = Path(__file__).resolve().parent / "assets"

# Enough of the palette to keep the page legible if assets/ ever goes missing.
FALLBACK_TOKENS = """
  :root {
    --kokka-brand-darker:#1E2F3A; --kokka-brand-dark:#365669;
    --kokka-brand-mid:#588DAD; --kokka-brand-mid-rgb:88 141 173;
    --kokka-white:#FFFFFF; --kokka-gray-50:#F9FAFB; --kokka-gray-200:#E5E7EB;
    --kokka-gray-300:#D1D5DB; --kokka-gray-400:#9CA3AF; --kokka-gray-500:#6B7280;
    --kokka-gray-700:#374151; --kokka-success:#16A34A;
    --kokka-font-serif:'Playfair Display',Georgia,serif;
    --kokka-font-sans:ui-sans-serif,system-ui,sans-serif;
    --kokka-font-mono:ui-monospace,SFMono-Regular,Menlo,monospace;
    --kokka-tracking-tight:0.025em; --kokka-tracking-wide:0.05em;
    --kokka-tracking-widest:0.1em;
    --kokka-radius-md:0.5rem; --kokka-radius-lg:0.75rem; --kokka-radius-pill:9999px;
    --kokka-shadow-md:0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1);
    --kokka-prose-narrow:42rem;
  }
"""

FALLBACK_LOGO = (
    '<span style="color:#FFFFFF;font-family:Georgia,serif;font-size:22px;'
    'letter-spacing:0.1em">KOKKA &amp; COMPANY</span>'
)


def load_asset(name, fallback=""):
    """Read a brand asset, degrading gracefully rather than serving a broken page."""
    try:
        return (ASSETS / name).read_text(encoding="utf-8")
    except OSError:
        return fallback

# ------------------------------------------------------------------ run state


class RunState:
    """Everything the page needs to render, guarded by one lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.lines = []
        self.running = False
        self.done = 0
        self.total = 0
        self.finished = False
        self.outdir = ""
        self.summary = ""

    def add_line(self, text):
        with self.lock:
            self.lines.append(str(text))

    def snapshot(self, since):
        with self.lock:
            return {
                "lines": self.lines[since:],
                "next": len(self.lines),
                "running": self.running,
                "done": self.done,
                "total": self.total,
                "finished": self.finished,
                "outdir": self.outdir,
                "summary": self.summary,
            }


state = RunState()


def build_argv(cfg):
    """Turn the form payload into ytmp3.py command-line arguments."""
    url = (cfg.get("url") or "").strip()
    if not url:
        raise ValueError("Paste a YouTube link first.")

    outdir = (cfg.get("output") or "").strip() or DEFAULT_OUTPUT
    argv = [
        url,
        "-o", str(Path(outdir).expanduser()),
        "-b", str(int(cfg.get("bitrate") or 320)),
        "-j", str(max(1, min(8, int(cfg.get("jobs") or 3)))),
    ]

    for key, flag in (
        ("album", "--album"),
        ("artist", "--artist"),
        ("album_artist", "--album-artist"),
        ("items", "--items"),
        ("cookies_from_browser", "--cookies-from-browser"),
    ):
        value = (cfg.get(key) or "").strip()
        if value:
            argv += [flag, value]

    for key, flag in (
        ("single", "--single"),
        ("flat", "--flat"),
        ("name_with_artist", "--name-with-artist"),
        ("keep_cover_file", "--keep-cover-file"),
        ("no_crop_art", "--no-crop-art"),
        ("no_track_numbers", "--no-track-numbers"),
        ("force", "--force"),
    ):
        if cfg.get(key):
            argv.append(flag)

    return argv


def run_job(cfg):
    """Drive ytmp3.main() on a worker thread, funnelling its output to the page."""
    try:
        argv = build_argv(cfg)
    except Exception as exc:  # noqa: BLE001
        state.add_line(f"Error: {exc}")
        with state.lock:
            state.running = False
            state.finished = True
            state.summary = "Nothing started"
        return

    # ytmp3 writes progress through its module-level log(); point it at us.
    ytmp3.log = state.add_line
    ytmp3.CANCEL.clear()
    ytmp3.LAST_OUTPUT_DIR = None

    def progress(done, total, result):
        with state.lock:
            state.done = done
            state.total = total

    ytmp3.PROGRESS_HOOK = progress

    try:
        ytmp3.main(argv)
    except SystemExit as exc:  # ytmp3 exits this way on setup errors
        if exc.code not in (0, None):
            state.add_line(str(exc.code))
    except Exception as exc:  # noqa: BLE001
        state.add_line(f"Unexpected error: {exc}")
    finally:
        ytmp3.PROGRESS_HOOK = None
        with state.lock:
            state.running = False
            state.finished = True
            state.outdir = ytmp3.LAST_OUTPUT_DIR or ""
            if ytmp3.CANCEL.is_set():
                state.summary = "Stopped"
            elif state.total and state.done >= state.total:
                state.summary = "Finished"
            else:
                state.summary = "Finished with errors"


# -------------------------------------------------------------------- web app

# The page markup lives in ui.html beside this file, so the Kokka & Company
# layout can be edited as HTML instead of as a Python string literal.
TEMPLATE = Path(__file__).resolve().parent / "ui.html"


def render_page():
    """Fill the Kokka-branded template with this run's token and brand assets."""
    try:
        page = TEMPLATE.read_text(encoding="utf-8")
    except OSError:
        return "<h1>ui.html is missing</h1><p>Re-copy it next to gui.py.</p>"

    # Names come from ytmp3's fixed browser table, so they are safe to drop
    # straight into the markup.
    choices = ['<option value="">Auto (recommended)</option>']
    choices += [f'<option value="{b}">{b}</option>' for b in ytmp3.detect_browsers()]

    return (
        page.replace("__TOKEN__", TOKEN)
        .replace("__OUTPUT__", DEFAULT_OUTPUT)
        .replace("__BROWSERS__", "".join(choices))
        .replace("__BRAND_CSS__", load_asset("kokka-tokens.css", FALLBACK_TOKENS))
        .replace("__LOGO__", load_asset("lockup-horizontal-white.svg", FALLBACK_LOGO))
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass  # keep the terminal clean

    # -- helpers ---------------------------------------------------------
    def _send(self, code, body, ctype="application/json"):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _authed(self, query):
        # The token keeps other local programs (and stray web pages) from
        # driving this server; the socket itself is bound to 127.0.0.1.
        return query.get("token", [""])[0] == TOKEN

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return {}

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/":
            return self._send(200, render_page(), "text/html")

        if parsed.path == "/favicon.ico":
            try:
                icon = (ASSETS / "favicon.ico").read_bytes()
            except OSError:
                return self._send(404, "no icon", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "image/x-icon")
            self.send_header("Content-Length", str(len(icon)))
            self.end_headers()
            return self.wfile.write(icon)

        if parsed.path == "/api/status":
            if not self._authed(query):
                return self._json({"error": "bad token"}, 403)
            since = int((query.get("since", ["0"])[0]) or 0)
            return self._json(state.snapshot(since))

        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authed(query):
            return self._json({"error": "bad token"}, 403)

        if parsed.path == "/api/start":
            with state.lock:
                if state.running:
                    return self._json({"error": "A download is already running."})
                state.reset()
                state.running = True
            threading.Thread(target=run_job, args=(self._body(),), daemon=True).start()
            return self._json({"ok": True})

        if parsed.path == "/api/cancel":
            ytmp3.CANCEL.set()
            state.add_line("Stopping after the current track…")
            return self._json({"ok": True})

        if parsed.path == "/api/open":
            with state.lock:
                target = state.outdir
            if target and Path(target).is_dir():
                try:
                    subprocess.Popen(
                        ["xdg-open", target],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                except OSError:
                    pass
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"

    print("YouTube to MP3 — GUI running")
    print(f"  {url}")
    print("Leave this window open. Press Ctrl+C to quit.", flush=True)

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        ytmp3.CANCEL.set()
        server.shutdown()


if __name__ == "__main__":
    main()
