#!/usr/bin/env node
/**
 * Point-and-click front end for ytmp3.js.
 *
 * Run it and a browser tab opens: paste a YouTube playlist link, press Download.
 *
 *     node gui.js
 *
 * It serves a small page from 127.0.0.1 on a random free port (nothing is
 * exposed to your network) and drives ytmp3.js in-process. Uses only Node's
 * standard library plus the same dependency ytmp3.js already needs.
 */

import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import {
  clearCancel,
  detectBrowsers,
  getLastOutputDir,
  isCancelled,
  requestCancel,
  run,
  setLogger,
  setProgressHook,
} from "./ytmp3.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TOKEN = crypto.randomBytes(16).toString("base64url");
const DEFAULT_OUTPUT = path.join(os.homedir(), "Music");

// Brand assets copied from the @kokka-company/design package — see assets/SOURCE.txt.
const ASSETS = path.join(HERE, "assets");
const TEMPLATE = path.join(HERE, "ui.html");

// Enough of the palette to keep the page legible if assets/ ever goes missing.
const FALLBACK_TOKENS = `
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
`;

const FALLBACK_LOGO =
  '<span style="color:#FFFFFF;font-family:Georgia,serif;font-size:22px;' +
  'letter-spacing:0.1em">KOKKA &amp; COMPANY</span>';

/** Read a brand asset, degrading gracefully rather than serving a broken page. */
function loadAsset(name, fallback = "") {
  try {
    return fs.readFileSync(path.join(ASSETS, name), "utf8");
  } catch {
    return fallback;
  }
}

// ------------------------------------------------------------------ run state

const state = {
  lines: [],
  running: false,
  done: 0,
  total: 0,
  finished: false,
  outdir: "",
  summary: "",
};

function resetState() {
  state.lines = [];
  state.running = false;
  state.done = 0;
  state.total = 0;
  state.finished = false;
  state.outdir = "";
  state.summary = "";
}

function addLine(text) {
  state.lines.push(String(text));
}

function snapshot(since) {
  return {
    lines: state.lines.slice(since),
    next: state.lines.length,
    running: state.running,
    done: state.done,
    total: state.total,
    finished: state.finished,
    outdir: state.outdir,
    summary: state.summary,
  };
}

/** Turn the form payload into ytmp3.js command-line arguments. */
export function buildArgv(cfg) {
  const url = String(cfg.url || "").trim();
  if (!url) throw new Error("Paste a YouTube link first.");

  const outdir = String(cfg.output || "").trim() || DEFAULT_OUTPUT;
  const bitrate = parseInt(cfg.bitrate, 10) || 320;
  const jobs = Math.max(1, Math.min(8, parseInt(cfg.jobs, 10) || 3));

  const argv = [url, "-o", outdir, "-b", String(bitrate), "-j", String(jobs)];

  for (const [key, flag] of [
    ["album", "--album"],
    ["artist", "--artist"],
    ["album_artist", "--album-artist"],
    ["items", "--items"],
    ["cookies_from_browser", "--cookies-from-browser"],
  ]) {
    const value = String(cfg[key] || "").trim();
    if (value) argv.push(flag, value);
  }

  for (const [key, flag] of [
    ["single", "--single"],
    ["flat", "--flat"],
    ["name_with_artist", "--name-with-artist"],
    ["keep_cover_file", "--keep-cover-file"],
    ["no_crop_art", "--no-crop-art"],
    ["no_track_numbers", "--no-track-numbers"],
    ["force", "--force"],
  ]) {
    if (cfg[key]) argv.push(flag);
  }

  return argv;
}

/** Drive ytmp3.run(), funnelling its output to the page. */
async function runJob(cfg) {
  let argv;
  try {
    argv = buildArgv(cfg);
  } catch (err) {
    addLine(`Error: ${err.message}`);
    state.running = false;
    state.finished = true;
    state.summary = "Nothing started";
    return;
  }

  // ytmp3 writes progress through its logger hook; point it at us.
  setLogger(addLine);
  clearCancel();
  setProgressHook((done, total) => {
    state.done = done;
    state.total = total;
  });

  try {
    await run(argv);
  } catch (err) {
    addLine(`Error: ${err.message}`);
  } finally {
    setProgressHook(null);
    setLogger(null);
    state.running = false;
    state.finished = true;
    state.outdir = getLastOutputDir() || "";
    if (isCancelled()) state.summary = "Stopped";
    else if (state.total && state.done >= state.total) state.summary = "Finished";
    else state.summary = "Finished with errors";
  }
}

// -------------------------------------------------------------------- web app

function renderPage() {
  // Names come from ytmp3's fixed browser table, so they are safe to drop
  // straight into the markup.
  const choices = ['<option value="">Auto (recommended)</option>'].concat(
    detectBrowsers().map((b) => `<option value="${b}">${b}</option>`),
  );
  let page;
  try {
    page = fs.readFileSync(TEMPLATE, "utf8");
  } catch {
    return "<h1>ui.html is missing</h1><p>Re-copy it next to gui.js.</p>";
  }
  return page
    .replaceAll("__TOKEN__", TOKEN)
    .replaceAll("__OUTPUT__", DEFAULT_OUTPUT)
    .replaceAll("__BROWSERS__", choices.join(""))
    .replaceAll("__BRAND_CSS__", loadAsset("kokka-tokens.css", FALLBACK_TOKENS))
    .replaceAll("__LOGO__", loadAsset("lockup-horizontal-white.svg", FALLBACK_LOGO));
}

function send(res, code, body, type = "application/json") {
  const payload = Buffer.from(body, "utf8");
  res.writeHead(code, {
    "Content-Type": `${type}; charset=utf-8`,
    "Content-Length": payload.length,
    "Cache-Control": "no-store",
  });
  res.end(payload);
}

const sendJson = (res, obj, code = 200) => send(res, code, JSON.stringify(obj));

function readBody(req) {
  return new Promise((resolve) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 1e6) raw = raw.slice(0, 1e6); // don't buffer unbounded input
    });
    req.on("end", () => {
      try {
        resolve(JSON.parse(raw || "{}"));
      } catch {
        resolve({});
      }
    });
  });
}

function openExternally(target) {
  // Honour $BROWSER so headless/test runs can point this somewhere harmless.
  const cmd = process.env.BROWSER || "xdg-open";
  try {
    spawn(cmd, [target], { detached: true, stdio: "ignore" }).unref();
  } catch {
    /* opening is a convenience, never fatal */
  }
}

async function handle(req, res) {
  const url = new URL(req.url, "http://127.0.0.1");
  const pathname = url.pathname;
  // The token keeps other local programs (and stray web pages) from driving
  // this server; the socket itself is bound to 127.0.0.1.
  const authed = url.searchParams.get("token") === TOKEN;

  if (req.method === "GET") {
    if (pathname === "/") return send(res, 200, renderPage(), "text/html");

    if (pathname === "/favicon.ico") {
      try {
        const icon = fs.readFileSync(path.join(ASSETS, "favicon.ico"));
        res.writeHead(200, {
          "Content-Type": "image/x-icon",
          "Content-Length": icon.length,
        });
        return res.end(icon);
      } catch {
        return send(res, 404, "no icon", "text/plain");
      }
    }

    if (pathname === "/api/status") {
      if (!authed) return sendJson(res, { error: "bad token" }, 403);
      const since = parseInt(url.searchParams.get("since") || "0", 10) || 0;
      return sendJson(res, snapshot(since));
    }

    return send(res, 404, "not found", "text/plain");
  }

  if (req.method === "POST") {
    if (!authed) return sendJson(res, { error: "bad token" }, 403);

    if (pathname === "/api/start") {
      if (state.running) {
        return sendJson(res, { error: "A download is already running." });
      }
      resetState();
      state.running = true;
      const cfg = await readBody(req);
      runJob(cfg); // deliberately not awaited — the page polls /api/status
      return sendJson(res, { ok: true });
    }

    if (pathname === "/api/cancel") {
      requestCancel();
      addLine("Stopping after the current track…");
      return sendJson(res, { ok: true });
    }

    if (pathname === "/api/open") {
      const target = state.outdir;
      if (target && fs.existsSync(target)) openExternally(target);
      return sendJson(res, { ok: true });
    }

    return sendJson(res, { error: "not found" }, 404);
  }

  return send(res, 405, "method not allowed", "text/plain");
}

function main() {
  const server = http.createServer((req, res) => {
    handle(req, res).catch((err) => {
      try {
        sendJson(res, { error: String(err && err.message) }, 500);
      } catch {
        /* response already sent */
      }
    });
  });

  server.listen(0, "127.0.0.1", () => {
    const { port } = server.address();
    const url = `http://127.0.0.1:${port}/?token=${TOKEN}`;
    process.stdout.write(
      "YouTube to MP3 — GUI running\n" +
        `  ${url}\n` +
        "Leave this window open. Press Ctrl+C to quit.\n",
    );
    setTimeout(() => openExternally(url), 500);
  });

  process.on("SIGINT", () => {
    process.stdout.write("\nShutting down.\n");
    requestCancel();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 1500).unref();
  });
}

const invokedDirectly =
  process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (invokedDirectly) main();
