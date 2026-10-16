# Contributing

Thanks for helping. This is a small tool, and these rules keep it that way.

## Before you start

- **Bugs:** open an issue with the command you ran, the full output, your OS,
  and `python3 -m yt_dlp --version`. Remove any cookie or account details first.
- **Features:** open an issue before you write code, so we can agree it fits.
- **"It stopped working after a YouTube change":** run
  `pip3 install --user -U yt-dlp` first. That fixes most breakage, and it is
  outside this project's control.

## Development setup

```bash
git clone https://github.com/Kokka-Company/youtube-album-to-mp3.git
cd youtube-album-to-mp3
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

You also need `ffmpeg` on your `PATH`.

## Ground rules

- **Python 3.9+, standard library first.** The only runtime dependencies are
  yt-dlp and mutagen. A new dependency needs a strong reason.
- **Match the surrounding code:** its naming, comment density and idiom.
- **Keep the GUI local.** It binds to `127.0.0.1` and checks a one-time token on
  every request. Changes that weaken either will not be merged.
- **Brand files are copied, not edited.** Do not hand-edit anything in
  `assets/`. See [`assets/SOURCE.txt`](./assets/SOURCE.txt) and
  [TRADEMARKS.md](./TRADEMARKS.md).
- **Do not add code that bypasses DRM** or downloads content the user has no
  right to.

## Before you open a pull request

```bash
python3 -m py_compile ytmp3.py gui.py mechenformat.py
python3 ytmp3.py --help
python3 mechenformat.py --help
```

CI runs the same checks. If you changed the download path, also do one real
download of a short Creative Commons video and say in the PR that you did.

Make one change per pull request, and explain why in the description. Update
`README.md` when you change a flag or a behaviour.

By contributing you agree that your work is licensed under the
[MIT License](./LICENSE), and that you will follow the
[Code of Conduct](./CODE_OF_CONDUCT.md).
