#!/usr/bin/env node
/**
 * YouTube playlist -> tagged MP3 scraper (Node.js).
 *
 * Downloads a YouTube playlist (or single video) as MP3s at a chosen bitrate,
 * writes full ID3v2.3 tags (title, artist, album, track number, year) and embeds
 * the video thumbnail as square front-cover album art.
 *
 *   node ytmp3.js <playlist-or-video-url> [options]
 *
 * Orchestration is pure Node. Two external binaries do the heavy lifting:
 * yt-dlp (extraction) and ffmpeg (audio + artwork). See README.md.
 */

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import NodeID3 from "node-id3";

const ARCHIVE_NAME = ".downloaded.txt";
const IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".webp"];

// Hardware MP3 players (e.g. MECHEN) reject oversized embedded art. Cover art
// is written as JPEG, square, and never larger than this on either side.
const MAX_ART_PX = 640;

// YouTube intermittently 403s individual audio itags, and hitting it again
// straight away usually 403s too. Each [format, seconds-to-wait-after-failure]
// pair is tried in turn before a track is written off as failed; the third
// attempt re-tries the best format after a longer pause, which is what most
// often clears transient throttling.
const FORMAT_FALLBACKS = [
  ["bestaudio/best", 3],
  ["bestaudio[ext=m4a]/bestaudio", 8],
  ["bestaudio/best", 15],
  ["best", 0],
];

// Where each browser keeps its profile on Linux, including snap/flatpak layouts
// (Ubuntu's Firefox is a snap, so it is NOT under ~/.mozilla).
const BROWSER_PROFILE_DIRS = {
  firefox: [
    "~/.mozilla/firefox",
    "~/snap/firefox/common/.mozilla/firefox",
    "~/.var/app/org.mozilla.firefox/.mozilla/firefox",
  ],
  chrome: ["~/.config/google-chrome"],
  brave: ["~/.config/BraveSoftware/Brave-Browser"],
  chromium: ["~/.config/chromium", "~/snap/chromium/common/chromium"],
  edge: ["~/.config/microsoft-edge"],
  vivaldi: ["~/.config/vivaldi"],
  opera: ["~/.config/opera"],
};

// Bracketed noise that YouTube uploaders bolt onto titles. Only bracket groups
// containing one of these words are stripped, so "(feat. X)", "(Remix)" and
// "(Live)" survive.
const JUNK_WORDS =
  "official|lyrics?|lyric\\s*video|audio|video|visuali[sz]er|" +
  "hd|hq|4k|8k|mv|m/v|full\\s*album|full\\s*song|remaster(?:ed)?|" +
  "explicit|clean\\s*version|free\\s*download|download|out\\s*now|" +
  "with\\s*lyrics|color\\s*coded";

const JUNK_RE = [
  new RegExp(`\\((?:[^()]*\\b(?:${JUNK_WORDS})\\b[^()]*)\\)`, "gi"),
  new RegExp(`\\[(?:[^\\[\\]]*\\b(?:${JUNK_WORDS})\\b[^\\[\\]]*)\\]`, "gi"),
  new RegExp(`\\{(?:[^{}]*\\b(?:${JUNK_WORDS})\\b[^{}]*)\\}`, "gi"),
];
// "Artist - Song | Official Video" — strip a trailing pipe segment that is noise.
const PIPE_JUNK_RE = new RegExp(
  `\\s*[|｜]\\s*[^|｜]*\\b(?:${JUNK_WORDS})\\b[^|｜]*$`,
  "i",
);
// Channel plugs tacked on the end: "Song - incompetech.com", "Song | artist.net".
const PROMO_URL_RE =
  /\s*[-–—|]\s*(?:https?:\/\/)?(?:www\.)?[\w-]+\.(?:com|net|org|io|co|tv|fm|me|info|biz|uk|de)\/?\s*$/i;
const LEADING_TRACK_RE = /^\s*\d{1,3}\s*[-.)\]]\s+/;
const SPLIT_RE = /\s+[-–—]\s+/;
const VIDEO_ID_RE = /[?&]v=([A-Za-z0-9_-]{11})/;

// ------------------------------------------------------------- module wiring
// gui.js drives these; the CLI leaves them at their defaults.

let logger = (msg) => console.log(msg);
let progressHook = null;
let cancelled = false;
let lastOutputDir = null;
const activeChildren = new Set();

export function setLogger(fn) {
  logger = fn || ((msg) => console.log(msg));
}
export function setProgressHook(fn) {
  progressHook = fn || null;
}
export function requestCancel() {
  cancelled = true;
  for (const child of activeChildren) {
    try {
      child.kill("SIGTERM");
    } catch {
      /* already gone */
    }
  }
}
export function clearCancel() {
  cancelled = false;
}
export function isCancelled() {
  return cancelled;
}
export function getLastOutputDir() {
  return lastOutputDir;
}

const log = (msg) => logger(msg);
const sleep = (seconds) => new Promise((r) => setTimeout(r, seconds * 1000));

// ---------------------------------------------------------------- text helpers

function trimChars(text, chars) {
  let start = 0;
  let end = text.length;
  while (start < end && chars.includes(text[start])) start++;
  while (end > start && chars.includes(text[end - 1])) end--;
  return text.slice(start, end);
}

/** Strip promo noise and leading track numbers from a video title. */
export function cleanTitle(text) {
  if (!text) return "";
  let out = text;
  for (const re of JUNK_RE) out = out.replace(re, " ");
  out = out.replace(PIPE_JUNK_RE, "");
  out = out.replace(PROMO_URL_RE, "");
  out = out.replace(LEADING_TRACK_RE, "");
  out = out.replace(/\s*[|｜]\s*$/, "");
  out = out.replace(/\s+/g, " ");
  return trimChars(out.trim(), " -–—_|.");
}

/** Split "Artist - Song" into a pair. Returns [null, text] if there is no split. */
export function splitArtistTitle(text) {
  const match = SPLIT_RE.exec(text);
  if (match) {
    const artist = text.slice(0, match.index).trim();
    const title = text.slice(match.index + match[0].length).trim();
    // A very long left side is usually part of the song name, not an artist.
    if (artist && title && artist.length <= 60) return [artist, title];
  }
  return [null, text.trim()];
}

export function cleanAlbumName(text) {
  if (!text) return "";
  let out = text.replace(/\((?:full\s*)?album\)|\[(?:full\s*)?album\]/gi, " ");
  out = out.replace(/\b(?:full\s*album|complete\s*album|playlist)\b/gi, " ");
  out = out.replace(/\s+/g, " ");
  return trimChars(out.trim(), " -–—_|.") || text.trim();
}

/** Make a string safe for ext4/NTFS/exFAT filenames. */
export function sanitizeFilename(name, maxLen = 180) {
  // eslint-disable-next-line no-control-regex
  let out = String(name).replace(/[/\\:*?"<>|\x00-\x1f]/g, "-");
  out = out.replace(/\s+/g, " ").trim().replace(/\.+$/, "");
  if (out.length > maxLen) out = out.slice(0, maxLen).trim();
  return out || "track";
}

/** Work out the final artist/title/album/year for one video. */
export function deriveTags(info, args, index, total, playlistTitle, playlistUploader) {
  const rawTitle = info.title || "Unknown";
  const uploader = info.uploader || info.channel || "";

  // YouTube Music / "- Topic" uploads carry real music metadata already.
  const metaArtist = info.artist || info.creator;
  const metaTrack = info.track;
  const metaAlbum = info.album;

  let topicArtist = null;
  if (uploader.endsWith(" - Topic")) {
    topicArtist = uploader.slice(0, -" - Topic".length).trim();
  }

  const cleaned = cleanTitle(rawTitle);
  const [splitArtist, splitTitle] = splitArtistTitle(cleaned);

  const title = metaTrack || splitTitle || cleaned || rawTitle;
  let artist =
    args.artist || metaArtist || topicArtist || splitArtist || uploader || "Unknown Artist";
  // yt-dlp joins multiple artists with ", " already; normalise separators.
  artist = artist.replace(/\s*[;/]\s*/g, ", ").trim();

  const album =
    args.album || metaAlbum || cleanAlbumName(playlistTitle) || cleanAlbumName(title);
  const albumArtist =
    args.albumArtist ||
    (args.artist || metaArtist || topicArtist ? artist : playlistUploader || artist);

  let year = "";
  if (info.release_year) year = String(info.release_year);
  else if (info.release_date) year = String(info.release_date).slice(0, 4);
  else if (info.upload_date) year = String(info.upload_date).slice(0, 4);

  return {
    title: String(title).trim(),
    artist,
    album: String(album).trim(),
    albumArtist: String(albumArtist || artist).trim(),
    year,
    genre: (info.genre || "").trim(),
    track: index,
    total,
    url: info.webpage_url || info.original_url || "",
  };
}

// ------------------------------------------------------------ browsers/cookies

function expandHome(p) {
  return p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p;
}

/** Browsers that actually have a profile on this machine, best first. */
export function detectBrowsers() {
  const found = [];
  for (const [name, paths] of Object.entries(BROWSER_PROFILE_DIRS)) {
    if (paths.some((p) => fs.existsSync(expandHome(p)))) found.push(name);
  }
  return found;
}

/** True when YouTube is demanding a signed-in session rather than failing. */
export function isBotCheck(message) {
  const text = String(message || "").toLowerCase();
  return text.includes("not a bot") || text.includes("sign in to confirm");
}

/** On a bot check, fall back to whatever browser is installed. Returns its name. */
function autoEnableCookies(args) {
  if (args.cookies || args.cookiesFromBrowser) return null; // user already chose
  const browsers = detectBrowsers();
  if (!browsers.length) return null;
  args.cookiesFromBrowser = browsers[0];
  return browsers[0];
}

// --------------------------------------------------------------- external bins

function probeBinary(candidates, versionFlag = "--version") {
  for (const candidate of candidates.filter(Boolean)) {
    try {
      const result = spawnSync(candidate, [versionFlag], { stdio: "ignore" });
      if (result.status === 0) return candidate;
    } catch {
      /* try the next one */
    }
  }
  return null;
}

export function findYtDlp() {
  return probeBinary([
    process.env.YTDLP_PATH,
    "yt-dlp",
    path.join(os.homedir(), ".local/bin/yt-dlp"),
    "/usr/local/bin/yt-dlp",
    "/usr/bin/yt-dlp",
  ]);
}

export function findFfmpeg() {
  // ffmpeg uses single-dash options; "--version" exits non-zero.
  return probeBinary([process.env.FFMPEG_PATH, "ffmpeg", "/usr/bin/ffmpeg"], "-version");
}

/** Pull the most useful line out of yt-dlp's stderr instead of dumping it all. */
function tidyError(stderr, code) {
  const lines = String(stderr || "")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  const errorLine = [...lines].reverse().find((l) => l.startsWith("ERROR:"));
  return errorLine || lines[lines.length - 1] || `exited with code ${code}`;
}

function runProcess(bin, args, { capture = true } = {}) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(bin, args, { stdio: ["ignore", capture ? "pipe" : "ignore", "pipe"] });
    } catch (err) {
      reject(err);
      return;
    }
    activeChildren.add(child);
    let stdout = "";
    let stderr = "";
    if (child.stdout) child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.on("error", (err) => {
      activeChildren.delete(child);
      reject(err);
    });
    child.on("close", (code) => {
      activeChildren.delete(child);
      if (code === 0) resolve(stdout);
      else reject(new Error(tidyError(stderr, code)));
    });
  });
}

// ------------------------------------------------------------------- cover art

/** Find the thumbnail yt-dlp wrote in this track's private work directory. */
async function findThumbnail(trackDir) {
  let entries;
  try {
    entries = await fsp.readdir(trackDir);
  } catch {
    return null;
  }
  const name = entries.find((f) => IMAGE_EXTS.includes(path.extname(f).toLowerCase()));
  return name ? path.join(trackDir, name) : null;
}

/**
 * Convert a thumbnail to JPEG cover art: centre-cropped square, downscaled to
 * at most MAX_ART_PX. Never upscales art that is already smaller.
 */
async function prepareCover(ffmpeg, src, dest, { square = true, maxSize = MAX_ART_PX } = {}) {
  const filters = [];
  if (square) filters.push("crop='min(iw,ih)':'min(iw,ih)'");
  filters.push(`scale='min(${maxSize},iw)':'min(${maxSize},ih)':flags=lanczos`);
  try {
    await runProcess(ffmpeg, [
      "-y", "-loglevel", "error",
      "-i", src,
      "-vf", filters.join(","),
      "-frames:v", "1", "-q:v", "2",
      dest,
    ], { capture: false });
  } catch {
    return null;
  }
  try {
    const stat = await fsp.stat(dest);
    return stat.size > 0 ? dest : null;
  } catch {
    return null;
  }
}

async function readBitrateKbps(ffmpeg, file, fallback) {
  const ffprobe = ffmpeg.replace(/ffmpeg$/, "ffprobe");
  try {
    const out = await runProcess(ffprobe, [
      "-v", "error", "-select_streams", "a:0",
      "-show_entries", "stream=bit_rate",
      "-of", "default=nw=1:nk=1", file,
    ]);
    const kbps = Math.round(parseInt(out.trim(), 10) / 1000);
    return Number.isFinite(kbps) && kbps > 0 ? kbps : fallback;
  } catch {
    return fallback;
  }
}

// ---------------------------------------------------------------------- tagging

async function writeTags(mp3Path, tags, coverPath) {
  const payload = {
    title: tags.title,
    artist: tags.artist,
    album: tags.album,
    performerInfo: tags.albumArtist, // TPE2
    trackNumber: `${tags.track}/${tags.total}`,
  };
  if (tags.year) payload.year = tags.year;
  if (tags.genre) payload.genre = tags.genre;
  if (tags.url) {
    payload.audioSourceUrl = tags.url; // WOAS
    payload.comment = { language: "eng", shortText: "", text: tags.url };
  }
  if (coverPath && fs.existsSync(coverPath)) {
    payload.image = {
      mime: "image/jpeg",
      type: { id: 3, name: "front cover" },
      description: "Cover",
      imageBuffer: await fsp.readFile(coverPath),
    };
  }
  // node-id3 writes ID3v2.3, which has the widest player support
  // (Windows, car head units, phones).
  const result = NodeID3.write(payload, mp3Path);
  if (result !== true) throw new Error(String(result));
}

// --------------------------------------------------------------- archive/state

/** Read the archive as {video_id: filename or null} (null = legacy entry). */
function loadArchive(archivePath) {
  const entries = {};
  let text;
  try {
    text = fs.readFileSync(archivePath, "utf8");
  } catch {
    return entries;
  }
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const tab = line.indexOf("\t");
    if (tab === -1) entries[line] = null;
    else entries[line.slice(0, tab)] = line.slice(tab + 1) || null;
  }
  return entries;
}

let archiveChain = Promise.resolve();
function appendArchive(archivePath, videoId, filename) {
  // Serialise appends so parallel workers cannot interleave a line.
  archiveChain = archiveChain.then(() =>
    fsp.appendFile(archivePath, `${videoId}\t${filename}\n`, "utf8").catch(() => {}),
  );
  return archiveChain;
}

/**
 * Video IDs of MP3s actually sitting in the folder, read from their tags.
 *
 * Every file this tool writes carries its source URL, which makes the folder
 * itself the source of truth: delete an MP3 and it gets fetched again, instead
 * of being skipped forever because a hidden archive file still lists it.
 */
export function scanExistingIds(outDir) {
  const found = new Set();
  let names;
  try {
    names = fs.readdirSync(outDir);
  } catch {
    return found;
  }
  for (const name of names) {
    if (path.extname(name).toLowerCase() !== ".mp3") continue;
    let tags;
    try {
      tags = NodeID3.read(path.join(outDir, name));
    } catch {
      continue; // unreadable tags just mean "unknown"
    }
    const candidates = [tags.audioSourceUrl, tags.comment && tags.comment.text];
    for (const value of candidates) {
      const match = VIDEO_ID_RE.exec(value || "");
      if (match) {
        found.add(match[1]);
        break;
      }
    }
  }
  return found;
}

/** Tracks that can be safely skipped: their file is genuinely still there. */
function resolveDoneIds(archivePath, outDir) {
  const done = scanExistingIds(outDir);
  for (const [videoId, filename] of Object.entries(loadArchive(archivePath))) {
    // Legacy entries recorded no filename, so they cannot be trusted.
    if (filename && fs.existsSync(path.join(outDir, filename))) done.add(videoId);
  }
  return done;
}

// ------------------------------------------------------------------- downloads

function cookieArgs(args) {
  const out = [];
  if (args.cookies) out.push("--cookies", args.cookies);
  // yt-dlp parses the BROWSER[+KEYRING][:PROFILE][::CONTAINER] spec itself.
  if (args.cookiesFromBrowser) out.push("--cookies-from-browser", args.cookiesFromBrowser);
  return out;
}

function commonArgs(args) {
  const out = ["--no-warnings", "--no-progress", ...cookieArgs(args)];
  if (args.proxy) out.push("--proxy", args.proxy);
  if (args.rateLimit) out.push("--limit-rate", String(args.rateLimit));
  return out;
}

/** Fetch the playlist listing (or single video) without downloading. */
export async function probe(ytdlp, url, args) {
  const argv = [
    "--flat-playlist",
    "--dump-single-json",
    "--ignore-errors",
    "--socket-timeout", "30",
    ...commonArgs(args),
  ];
  // A song link copied from a playlist still carries &list=; this pins the
  // extraction to the one video instead of pulling the whole album.
  if (args.single) argv.push("--no-playlist");
  if (args.items) argv.push("--playlist-items", args.items);
  argv.push(url);

  const stdout = await runProcess(ytdlp, argv);
  const text = stdout.trim();
  if (!text) throw new Error("Could not read that URL (private, deleted, or blocked).");
  return JSON.parse(text);
}

async function moveFile(src, dest) {
  try {
    await fsp.rename(src, dest);
  } catch (err) {
    if (err.code !== "EXDEV") throw err;
    await fsp.copyFile(src, dest); // crossing a filesystem boundary
    await fsp.unlink(src);
  }
}

/** Download, convert, tag and file a single track. Returns a status object. */
async function downloadOne(ctx, entry, index) {
  const { args, ytdlp, ffmpeg, outDir, workDir, playlistTitle, playlistUploader, archivePath, doneIds, total } = ctx;
  const videoId = entry.id || "";
  const label = entry.title || videoId;

  if (cancelled) return { status: "cancelled", title: label };

  if (videoId && doneIds.has(videoId) && !args.force) {
    log(`[${index}/${total}] skip — already in this folder  ${label}`);
    return { status: "skipped", title: label };
  }

  // webpage_url first. On SoundCloud a fully-extracted entry puts the raw HLS
  // stream URL in `url`; handing that to yt-dlp downloads a stream it then
  // names unpredictably, so the expected output file never appears. Flat
  // playlist entries have no webpage_url and correctly fall through to `url`.
  let url = entry.webpage_url || entry.url || videoId;
  if (url && !url.startsWith("http")) url = `https://www.youtube.com/watch?v=${url}`;

  const stemId = videoId || `track${String(index).padStart(3, "0")}`;
  // Give each track its own directory so the finished file can be found by
  // scanning rather than by predicting yt-dlp's naming, which varies per site.
  const trackDir = path.join(workDir, stemId);
  await fsp.rm(trackDir, { recursive: true, force: true });
  await fsp.mkdir(trackDir, { recursive: true });

  let info = null;
  let lastError = "no media returned";

  for (let attempt = 1; attempt <= FORMAT_FALLBACKS.length; attempt++) {
    const [format, backoff] = FORMAT_FALLBACKS[attempt - 1];
    if (cancelled) return { status: "cancelled", title: label };

    const argv = [
      "-f", format,
      "-x", "--audio-format", "mp3",
      "--audio-quality", `${args.bitrate}K`,
      "--write-thumbnail",
      "--no-playlist",
      "-o", path.join(trackDir, "%(id)s.%(ext)s"),
      "--retries", "5",
      "--fragment-retries", "5",
      "--extractor-retries", "3",
      "--socket-timeout", "30",
      "--dump-json", "--no-simulate",
      ...commonArgs(args),
      url,
    ];

    try {
      const stdout = await runProcess(ytdlp, argv);
      const line = stdout.split("\n").find((l) => l.trim().startsWith("{"));
      if (line) {
        info = JSON.parse(line);
        break;
      }
      lastError = "yt-dlp returned no metadata";
    } catch (err) {
      lastError = err.message;
      if (cancelled) return { status: "cancelled", title: label };
      if (isBotCheck(lastError)) {
        // YouTube wants a signed-in session. Remaining attempts reuse the
        // browser's cookies, which usually clears it outright.
        const browser = autoEnableCookies(args);
        if (browser) {
          log(`[${index}/${total}] YouTube asked for sign-in — retrying with ${browser} cookies ...`);
          continue;
        }
        lastError =
          "YouTube requires a signed-in session for this track. " +
          "Set 'Sign-in cookies from browser' (e.g. firefox) and retry.";
      }
      if (attempt < FORMAT_FALLBACKS.length) {
        // 403s here are usually transient throttling; pausing before the next
        // attempt clears them far more often than retrying at once.
        log(`[${index}/${total}] retrying ${label} in ${backoff}s ...`);
        await sleep(backoff);
      }
    }
  }

  if (!info) {
    log(`[${index}/${total}] FAILED  ${label}: ${lastError}`);
    return { status: "failed", title: label, error: lastError };
  }

  const produced = await fsp.readdir(trackDir).catch(() => []);
  const mp3Name = produced.find((f) => f.toLowerCase().endsWith(".mp3"));
  if (!mp3Name) {
    const msg = `converted file not found (yt-dlp produced: ${produced.join(", ") || "nothing"})`;
    log(`[${index}/${total}] FAILED  ${label}: ${msg}`);
    await fsp.rm(trackDir, { recursive: true, force: true });
    return { status: "failed", title: label, error: msg };
  }
  const mp3Path = path.join(trackDir, mp3Name);

  const tags = deriveTags(info, args, index, total, playlistTitle, playlistUploader);

  let cover = null;
  const thumb = await findThumbnail(trackDir);
  if (thumb) {
    cover = await prepareCover(ffmpeg, thumb, path.join(trackDir, "_cover.jpg"), {
      square: !args.noCropArt,
    });
  }

  try {
    await writeTags(mp3Path, tags, cover);
  } catch (err) {
    // Keep the audio even if tagging trips.
    log(`[${index}/${total}] warning: tagging failed for ${label}: ${err.message}`);
  }

  let stem = args.nameWithArtist
    ? `${String(index).padStart(2, "0")} - ${tags.artist} - ${tags.title}`
    : `${String(index).padStart(2, "0")} - ${tags.title}`;
  if (args.noTrackNumbers) {
    const dash = stem.indexOf(" - ");
    if (dash !== -1) stem = stem.slice(dash + 3);
  }

  let finalPath = path.join(outDir, `${sanitizeFilename(stem)}.mp3`);
  let counter = 2;
  while (fs.existsSync(finalPath) && path.resolve(finalPath) !== path.resolve(mp3Path)) {
    finalPath = path.join(outDir, `${sanitizeFilename(stem)} (${counter}).mp3`);
    counter++;
  }

  try {
    await moveFile(mp3Path, finalPath);
  } catch (err) {
    log(`[${index}/${total}] FAILED  ${label}: could not move file: ${err.message}`);
    return { status: "failed", title: label, error: err.message };
  }

  // The whole per-track directory is scratch; the audio has already moved out.
  await fsp.rm(trackDir, { recursive: true, force: true });

  if (args.keepCoverFile) {
    const art = path.join(outDir, "cover.jpg");
    if (!fs.existsSync(art)) {
      try {
        const written = NodeID3.read(finalPath);
        if (written.image && written.image.imageBuffer) {
          await fsp.writeFile(art, written.image.imageBuffer);
        }
      } catch {
        /* the cover is a nicety, never fail the track over it */
      }
    }
  }

  if (videoId) await appendArchive(archivePath, videoId, path.basename(finalPath));

  const kbps = await readBitrateKbps(ffmpeg, finalPath, args.bitrate);
  log(`[${index}/${total}] ok  ${path.basename(finalPath)}  (${kbps} kbps)`);
  return { status: "ok", title: tags.title, path: finalPath };
}

/** Run `worker` over `items`, at most `limit` at a time. */
async function pool(items, limit, worker) {
  const results = new Array(items.length);
  let cursor = 0;
  const runners = Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, async () => {
    for (;;) {
      const index = cursor++;
      if (index >= items.length) return;
      try {
        results[index] = await worker(items[index], index);
      } catch (err) {
        results[index] = { status: "failed", title: "?", error: err.message };
      }
    }
  });
  await Promise.all(runners);
  return results;
}

// ----------------------------------------------------------------- entry point

const BOOLEAN_FLAGS = {
  "--single": "single",
  "--flat": "flat",
  "--no-crop-art": "noCropArt",
  "--keep-cover-file": "keepCoverFile",
  "--no-track-numbers": "noTrackNumbers",
  "--name-with-artist": "nameWithArtist",
  "--force": "force",
};

const VALUE_FLAGS = {
  "-o": "output",
  "--output": "output",
  "-b": "bitrate",
  "--bitrate": "bitrate",
  "-j": "jobs",
  "--jobs": "jobs",
  "--album": "album",
  "--artist": "artist",
  "--album-artist": "albumArtist",
  "--items": "items",
  "--cookies": "cookies",
  "--cookies-from-browser": "cookiesFromBrowser",
  "--rate-limit": "rateLimit",
  "--proxy": "proxy",
};

const HELP = `
Usage: node ytmp3.js <playlist-or-video-url> [options]

Download a YouTube playlist as tagged MP3s with embedded album art.

  -o, --output DIR         Output directory (default: downloads)
  -b, --bitrate N          96|128|160|192|256|320 kbps (default: 320)
  -j, --jobs N             Parallel downloads (default: 3)
      --single             Just this one song — ignore any playlist in the link
      --items SPEC         Playlist selection, e.g. '1-10' or '2,5,8'
      --album NAME         Override the album tag
      --artist NAME        Override the artist tag on every track
      --album-artist NAME  Override the album-artist tag
      --flat               No album subfolder
      --no-crop-art        Keep the 16:9 thumbnail instead of a square crop
      --keep-cover-file    Also write cover.jpg beside the MP3s
      --no-track-numbers   Drop the "NN - " filename prefix
      --name-with-artist   Name files "NN - Artist - Title.mp3"
      --force              Re-download tracks already present
      --cookies FILE       Path to a cookies.txt file
      --cookies-from-browser B   chrome, firefox, brave, ...
      --rate-limit RATE    Cap speed, e.g. 2M
      --proxy URL          e.g. socks5://127.0.0.1:1080
  -h, --help               Show this help
`.trim();

export function parseArgs(argv) {
  const args = {
    url: null,
    output: "downloads",
    bitrate: 320,
    jobs: 3,
    single: false,
    flat: false,
    noCropArt: false,
    keepCoverFile: false,
    noTrackNumbers: false,
    nameWithArtist: false,
    force: false,
    album: null,
    artist: null,
    albumArtist: null,
    items: null,
    cookies: null,
    cookiesFromBrowser: null,
    rateLimit: null,
    proxy: null,
    help: false,
  };

  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token === "-h" || token === "--help") {
      args.help = true;
    } else if (token in BOOLEAN_FLAGS) {
      args[BOOLEAN_FLAGS[token]] = true;
    } else if (token in VALUE_FLAGS) {
      const value = argv[++i];
      if (value === undefined) throw new Error(`${token} needs a value`);
      args[VALUE_FLAGS[token]] = value;
    } else if (token.startsWith("-") && token !== "-") {
      throw new Error(`Unknown option: ${token}`);
    } else if (args.url === null) {
      args.url = token;
    } else {
      throw new Error(`Unexpected argument: ${token}`);
    }
  }

  args.bitrate = parseInt(args.bitrate, 10) || 320;
  if (![96, 128, 160, 192, 256, 320].includes(args.bitrate)) {
    throw new Error(`Bitrate must be one of 96, 128, 160, 192, 256, 320 (got ${args.bitrate})`);
  }
  args.jobs = Math.max(1, Math.min(8, parseInt(args.jobs, 10) || 3));
  return args;
}

/**
 * Run a full download. Returns a summary object; throws on setup failure so the
 * caller (CLI or GUI) decides how to report it.
 */
export async function run(argv) {
  lastOutputDir = null; // never let a failed run report the previous folder
  const args = parseArgs(argv);
  if (args.help || !args.url) {
    log(HELP);
    return { ok: 0, skipped: 0, failed: [], cancelled: 0, outDir: null, help: true };
  }

  const ytdlp = findYtDlp();
  if (!ytdlp) {
    throw new Error(
      "yt-dlp not found. Install it with:  pipx install yt-dlp\n" +
        "or download the standalone binary from https://github.com/yt-dlp/yt-dlp/releases\n" +
        "and put it on your PATH (or set YTDLP_PATH).",
    );
  }
  const ffmpeg = findFfmpeg();
  if (!ffmpeg) throw new Error("ffmpeg not found on PATH. Install it: sudo apt install ffmpeg");

  log(`Reading ${args.url} ...`);
  let info;
  try {
    info = await probe(ytdlp, args.url, args);
  } catch (err) {
    // The playlist listing itself can hit the sign-in wall; same fix.
    const browser = isBotCheck(err.message) ? autoEnableCookies(args) : null;
    if (!browser) throw new Error(err.message);
    log(`YouTube asked for sign-in — retrying with ${browser} cookies ...`);
    info = await probe(ytdlp, args.url, args);
  }

  let entries;
  let playlistTitle = "";
  let playlistUploader = info.uploader || info.channel || "";
  if (info._type === "playlist") {
    entries = (info.entries || []).filter(Boolean);
    playlistTitle = info.title || "";
  } else {
    entries = [info];
  }
  if (!entries.length) throw new Error("Nothing to download — the playlist is empty or unavailable.");

  if (args.single) {
    // One song shouldn't get its own album folder or a "01 - " prefix, unless
    // the user deliberately named an album to file it under.
    args.noTrackNumbers = true;
    if (!args.album) args.flat = true;
  }

  const albumFolder = args.album || cleanAlbumName(playlistTitle) || "YouTube Audio";
  const base = path.resolve(expandHome(args.output));
  const outDir = args.flat ? base : path.join(base, sanitizeFilename(albumFolder));
  const workDir = path.join(outDir, ".work");
  await fsp.mkdir(outDir, { recursive: true });
  // A previous run that was killed outright leaves scratch files behind.
  await fsp.rm(workDir, { recursive: true, force: true });
  await fsp.mkdir(workDir, { recursive: true });
  lastOutputDir = outDir;

  const archivePath = path.join(outDir, ARCHIVE_NAME);
  const doneIds = args.force ? new Set() : resolveDoneIds(archivePath, outDir);

  const total = entries.length;
  log(`${total} track(s) -> ${outDir}  @ ${args.bitrate} kbps, ${args.jobs} at a time\n`);

  // Publish the track count before any downloading starts, so a progress bar
  // can show "0 of N" immediately instead of sitting empty until track 1 lands.
  if (progressHook) {
    try {
      progressHook(0, total, null);
    } catch {
      /* a bad UI hook must not stop work */
    }
  }

  const ctx = {
    args, ytdlp, ffmpeg, outDir, workDir,
    playlistTitle, playlistUploader, archivePath, doneIds, total,
  };

  let completed = 0;
  const results = await pool(entries, args.jobs, async (entry, i) => {
    const result = await downloadOne(ctx, entry, i + 1);
    completed++;
    if (progressHook) {
      try {
        progressHook(completed, total, result);
      } catch {
        /* ignore */
      }
    }
    return result;
  });

  await archiveChain; // let any pending archive appends land
  // Clean up scratch space, leaving the archive and MP3s behind.
  await fsp.rm(workDir, { recursive: true, force: true });

  const ok = results.filter((r) => r && r.status === "ok").length;
  const skipped = results.filter((r) => r && r.status === "skipped").length;
  const cancelledCount = results.filter((r) => r && r.status === "cancelled").length;
  const failed = results.filter((r) => r && r.status === "failed");

  const tail = cancelledCount ? `, ${cancelledCount} cancelled` : "";
  log(`\nDone: ${ok} downloaded, ${skipped} skipped, ${failed.length} failed${tail}.`);
  if (failed.length) {
    log("Failed tracks:");
    for (const r of failed) log(`  - ${r.title}: ${r.error || "unknown error"}`);
  }
  log(`Files are in: ${outDir}`);

  return { ok, skipped, failed, cancelled: cancelledCount, outDir };
}

// Run as a CLI only when invoked directly, not when gui.js imports this.
const invokedDirectly =
  process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));

if (invokedDirectly) {
  process.on("SIGINT", () => {
    requestCancel();
    console.log("\nInterrupted. Re-run the same command to resume.");
    process.exit(130);
  });
  run(process.argv.slice(2))
    .then((summary) => {
      process.exit(summary.failed && summary.failed.length && !summary.ok ? 1 : 0);
    })
    .catch((err) => {
      console.error(`Error: ${err.message}`);
      process.exit(1);
    });
}
