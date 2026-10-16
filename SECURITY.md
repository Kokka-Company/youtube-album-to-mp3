# Security policy

## Reporting a vulnerability

Do **not** open a public issue. Report it privately through GitHub's
**Security → Report a vulnerability** tab on this repository.

Include what you found, how to reproduce it, and what an attacker could do with
it. We aim to acknowledge reports within 5 business days.

## Scope

In scope:

- The local GUI server (`gui.py`): anything that lets another site, user or
  process on the machine drive it, read its responses, or bypass the per-run
  token.
- Path handling: a crafted playlist, title or tag that writes outside the
  chosen output folder.
- Cookie handling: anything that leaks browser cookies beyond the yt-dlp
  requests they are meant for.

Out of scope:

- Bugs in yt-dlp, ffmpeg or mutagen. Report those upstream.
- YouTube refusing a download. That is a compatibility issue, not a security one.

## Handling cookies safely

`--cookies` and `--cookies-from-browser` give the tool your YouTube session.
Never commit a `cookies.txt` (the `.gitignore` excludes it) and never paste one
into an issue.
