# Requirements
- uv, ffmpeg, mkvtoolnix, pandoc

This will extract subtitles, convert ass to srt if necessary, translate, and create a new mkv with only the translated subs as default.

Run with `uv run main.py [--dual]`; `--dual` shows the translated and original subtitles together on screen.
