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

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YouTube to MP3 — Kokka &amp; Company</title>
<link rel="icon" href="/favicon.ico">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,500;1,400&display=swap">
<style>
__BRAND_CSS__

  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--kokka-white); color:var(--kokka-gray-700);
    font:15px/1.6 var(--kokka-font-sans);
  }

  /* Full-bleed brand-darker band against the white page below it — the
     alternating section rhythm the brand uses instead of a dark mode. */
  .masthead { background:var(--kokka-brand-darker); padding:20px 24px;
              border-bottom:3px solid var(--kokka-brand-mid); }
  .masthead-inner { max-width:820px; margin:0 auto; }
  .masthead svg { width:232px; height:auto; display:block; }  /* min 200px per brand */

  .wrap { max-width:820px; margin:0 auto; padding:40px 24px 64px; }

  /* The signature eyebrow: small uppercase label above a Playfair heading.
     Set in brand-dark, not brand-mid — brand-mid is 3.60 on white, which the
     brand's own contrast table restricts to large text and borders. */
  .eyebrow { font-size:12px; font-weight:600; text-transform:uppercase;
             letter-spacing:var(--kokka-tracking-widest);
             color:var(--kokka-brand-dark); margin:0 0 12px; }
  h1 { font-family:var(--kokka-font-serif); font-weight:500; font-size:40px;
       line-height:1.15; color:var(--kokka-brand-darker); margin:0 0 12px;
       letter-spacing:var(--kokka-tracking-tight); }
  .sub { color:var(--kokka-gray-500); font-size:15px; margin:0 0 32px;
         max-width:var(--kokka-prose-narrow); }

  .card { background:var(--kokka-white); border:1px solid var(--kokka-gray-200);
          border-radius:var(--kokka-radius-lg); padding:24px; margin-bottom:20px;
          box-shadow:var(--kokka-shadow-md); }

  label { display:block; font-size:11px; font-weight:600; text-transform:uppercase;
          letter-spacing:var(--kokka-tracking-wide); color:var(--kokka-gray-500);
          margin-bottom:7px; }
  input[type=text], input[type=number], select {
    width:100%; padding:11px 12px; background:var(--kokka-white);
    color:var(--kokka-brand-darker); border:1px solid var(--kokka-gray-300);
    border-radius:var(--kokka-radius-md); font-size:14px; font-family:inherit;
  }
  input:focus, select:focus {
    outline:none; border-color:var(--kokka-brand-mid);
    box-shadow:0 0 0 3px rgb(var(--kokka-brand-mid-rgb) / 0.2);
  }
  ::placeholder { color:var(--kokka-gray-400); }
  #url { font-size:16px; padding:14px; }

  .row { display:flex; gap:14px; flex-wrap:wrap; }
  .row > div { flex:1; min-width:130px; }
  .mt { margin-top:16px; }

  button {
    font:inherit; font-weight:600; border:0; border-radius:var(--kokka-radius-md);
    padding:13px 26px; cursor:pointer; color:var(--kokka-white);
    background:var(--kokka-brand-dark); letter-spacing:var(--kokka-tracking-wide);
  }
  button:hover:not(:disabled) { background:var(--kokka-brand-darker); }
  button:focus-visible { outline:2px solid var(--kokka-brand-mid); outline-offset:2px; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.ghost { background:var(--kokka-white); border:1px solid var(--kokka-gray-300);
                 color:var(--kokka-brand-darker); }
  button.ghost:hover:not(:disabled) { background:var(--kokka-gray-50);
                 border-color:var(--kokka-brand-mid); }
  .actions { display:flex; gap:10px; align-items:center; margin-top:20px; flex-wrap:wrap; }

  details { margin-top:18px; border-top:1px solid var(--kokka-gray-200); padding-top:16px; }
  summary { cursor:pointer; color:var(--kokka-brand-darker); font-size:13px;
            font-weight:600; user-select:none; }
  summary:hover { color:var(--kokka-brand-dark); }

  .checks { display:flex; gap:18px; flex-wrap:wrap; margin-top:16px; }
  .checks label { display:flex; align-items:center; gap:8px; text-transform:none;
                  letter-spacing:0; font-size:13px; font-weight:400;
                  color:var(--kokka-gray-700); margin:0; cursor:pointer; }
  .checks input { accent-color:var(--kokka-brand-mid); width:16px; height:16px; }

  #bar { height:6px; background:var(--kokka-gray-200);
         border-radius:var(--kokka-radius-pill); overflow:hidden; margin:2px 0 14px; }
  #fill { height:100%; width:0; background:var(--kokka-brand-mid);
          transition:width .3s ease; }
  #fill.done { background:var(--kokka-success); }
  #status { font-size:13px; color:var(--kokka-gray-500); margin-bottom:12px; }
  #log {
    background:var(--kokka-gray-50); border:1px solid var(--kokka-gray-200);
    border-radius:var(--kokka-radius-md); padding:14px; height:260px; overflow-y:auto;
    font:12.5px/1.65 var(--kokka-font-mono); white-space:pre-wrap;
    word-break:break-word; color:var(--kokka-gray-700);
  }
  #panel { display:none; }
  .hint { color:var(--kokka-gray-500); font-size:12px; margin-top:10px; }
  .foot { color:var(--kokka-gray-500); font-size:12px; margin:28px 0 0;
          padding-top:18px; border-top:1px solid var(--kokka-gray-200); }
</style>
</head>
<body>
<header class="masthead"><div class="masthead-inner">__LOGO__</div></header>
<div class="wrap">
  <p class="eyebrow">Internal Tool</p>
  <h1>YouTube to MP3</h1>
  <p class="sub">Paste a playlist or video link. Each track is saved as a 320 kbps MP3
     with artist, album, track number and cover art written into the file.</p>

  <div class="card">
    <label for="url">YouTube link</label>
    <input id="url" type="text" placeholder="https://www.youtube.com/playlist?list=..." autofocus>

    <div class="checks" style="margin-top:12px">
      <label><input type="checkbox" id="single"> Just this one song — ignore any playlist in the link</label>
    </div>

    <div class="row mt">
      <div style="flex:3">
        <label for="output">Save to</label>
        <input id="output" type="text" value="__OUTPUT__">
      </div>
      <div>
        <label for="bitrate">Quality</label>
        <select id="bitrate">
          <option value="320" selected>320 kbps</option>
          <option value="256">256 kbps</option>
          <option value="192">192 kbps</option>
          <option value="128">128 kbps</option>
        </select>
      </div>
    </div>

    <details>
      <summary>More options</summary>
      <div class="row mt">
        <div><label for="album">Album</label><input id="album" type="text" placeholder="from playlist"></div>
        <div><label for="artist">Artist</label><input id="artist" type="text" placeholder="auto-detect"></div>
      </div>
      <div class="row mt">
        <div><label for="album_artist">Album artist</label><input id="album_artist" type="text" placeholder="auto"></div>
        <div><label for="items">Tracks</label><input id="items" type="text" placeholder="all, or 1-10"></div>
        <div><label for="jobs">At once</label><input id="jobs" type="number" value="3" min="1" max="8"></div>
      </div>
      <div class="mt">
        <label for="cookies_from_browser">Sign-in cookies</label>
        <select id="cookies_from_browser">__BROWSERS__</select>
        <p class="hint">YouTube demands a signed-in session for some albums.
           Auto borrows your browser's YouTube login only if that happens.</p>
      </div>
      <div class="checks">
        <label><input type="checkbox" id="name_with_artist"> Artist in filename</label>
        <label><input type="checkbox" id="flat"> No album folder</label>
        <label><input type="checkbox" id="no_track_numbers"> No track numbers</label>
        <label><input type="checkbox" id="keep_cover_file"> Save cover.jpg</label>
        <label><input type="checkbox" id="no_crop_art"> Keep wide art</label>
        <label><input type="checkbox" id="force"> Re-download</label>
      </div>
    </details>

    <div class="actions">
      <button id="go">Download</button>
      <button id="stop" class="ghost" disabled>Stop</button>
      <button id="open" class="ghost" disabled>Open folder</button>
    </div>
    <p class="hint">Already-downloaded tracks are skipped, so you can safely re-run the same link.</p>
  </div>

  <div class="card" id="panel">
    <div id="bar"><div id="fill"></div></div>
    <div id="status">Starting…</div>
    <div id="log"></div>
  </div>

  <p class="foot">Kokka &amp; Company — internal tool. Runs locally on this machine;
     nothing is sent to a server.</p>
</div>

<script>
const TOKEN = "__TOKEN__";
const $ = id => document.getElementById(id);
const FIELDS = ["url","output","album","artist","album_artist","items","jobs","bitrate","cookies_from_browser"];
const FLAGS  = ["single","flat","name_with_artist","keep_cover_file","no_crop_art","no_track_numbers","force"];
let since = 0, timer = null, pollFails = 0;

function api(path, body) {
  // path may already carry a query string, so pick the right separator —
  // "?a=1?token=x" would fold the token into the previous value.
  const sep = path.includes("?") ? "&" : "?";
  return fetch(path + sep + "token=" + TOKEN, {
    method: body ? "POST" : "GET",
    headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined,
  }).then(r => {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  });
}

function setRunning(on) {
  $("go").disabled = on;
  $("stop").disabled = !on;
  if (on) $("open").disabled = true;
}

function start() {
  const cfg = {};
  FIELDS.forEach(f => cfg[f] = $(f).value);
  FLAGS.forEach(f => cfg[f] = $(f).checked);
  if (!cfg.url.trim()) { $(  "url").focus(); return; }

  $("log").textContent = "";
  $("fill").style.width = "0%";
  $("fill").classList.remove("done");
  $("status").textContent = "Reading link…";
  $("panel").style.display = "block";
  since = 0;
  setRunning(true);

  api("/api/start", cfg).then(r => {
    if (r.error) { $("status").textContent = r.error; setRunning(false); return; }
    if (timer) clearInterval(timer);
    timer = setInterval(poll, 600);
  }).catch(e => { $("status").textContent = "Could not start: " + e; setRunning(false); });
}

function poll() {
  api("/api/status?since=" + since).then(s => {
    since = s.next;
    if (s.lines.length) {
      const log = $("log");
      log.textContent += s.lines.join("\n") + "\n";
      log.scrollTop = log.scrollHeight;
    }
    if (s.total) {
      $("fill").style.width = Math.round(100 * s.done / s.total) + "%";
      $("status").textContent = s.running
        ? `${s.done} of ${s.total} tracks`
        : `${s.summary} — ${s.done} of ${s.total} tracks`;
    } else if (!s.running && s.finished) {
      $("status").textContent = s.summary || "Finished";
    }
    if (!s.running && s.finished) {
      clearInterval(timer); timer = null;
      setRunning(false);
      // Only go green on a clean finish — a stopped run shouldn't look complete.
      if (s.summary === "Finished") {
        $("fill").classList.add("done");
        if (!s.total) $("fill").style.width = "100%";
      }
      $("open").disabled = !s.outdir;
    }
    pollFails = 0;
  }).catch(e => {
    // Never fail silently: a broken poll used to leave the panel frozen while
    // the download ran on invisibly behind it.
    if (++pollFails >= 3) {
      $("status").textContent = "Lost contact with the downloader — it may still "
        + "be running. Check the terminal window. (" + e + ")";
    }
  });
}

$("go").onclick = start;
$("stop").onclick = () => { $("status").textContent = "Stopping…"; api("/api/cancel", {}); };
$("open").onclick = () => api("/api/open", {});
$("url").addEventListener("keydown", e => { if (e.key === "Enter") start(); });
</script>
</body>
</html>
"""


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
            # Names come from ytmp3's fixed browser table, so they are safe to
            # drop straight into the markup.
            choices = ['<option value="">Auto (recommended)</option>']
            choices += [f'<option value="{b}">{b}</option>' for b in ytmp3.detect_browsers()]
            page = (
                PAGE.replace("__TOKEN__", TOKEN)
                .replace("__OUTPUT__", DEFAULT_OUTPUT)
                .replace("__BROWSERS__", "".join(choices))
                .replace("__BRAND_CSS__", load_asset("kokka-tokens.css", FALLBACK_TOKENS))
                .replace("__LOGO__", load_asset("lockup-horizontal-white.svg", FALLBACK_LOGO))
            )
            return self._send(200, page, "text/html")

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
