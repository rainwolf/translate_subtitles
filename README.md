# Requirements
- uv, ffmpeg, mkvtoolnix, pandoc

This will extract subtitles, convert ass to srt if necessary, translate, and create a new mkv with only the translated subs as default.

Run with `uv run main.py [--dual]`; `--dual` shows the translated and original subtitles together on screen.

`uv run main.py --openrouter [MODEL]` translates with an OpenRouter model instead of Google Translate (no browser; default model `qwen/qwen3.8-flash`); the subtitle is named `th-or-<vendor>` (e.g. `th-or-qwen`: `<video>.th-or-qwen.srt` next to an mp4, or the track name in the mkv), and the API key comes from `OPENROUTER_API_KEY` or the macOS Keychain item `openrouter-transforms`.
