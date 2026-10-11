import argparse
import os
import shutil
import tempfile
import openrouter_translate
from translate import change_browser_window_size, translate
from utils import (
    convert_to_docx,
    clean_rolling_srt,
    convert_to_srt,
    extract_and_convert_ass_to_srt,
    get_source_track,
    get_track_and_type,
    merge_srt,
    merge_srt_by_overlap,
    parse_srt,
    strip_and_add_subtitle,
)
import glob
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dual",
        action="store_true",
        help="merge the original and translated subtitles so both show on screen",
    )
    parser.add_argument(
        "--openrouter",
        nargs="?",
        const=openrouter_translate.DEFAULT_MODEL,
        default=None,
        metavar="MODEL",
        help="translate with an OpenRouter model instead of Google Translate"
        " (default %(const)s); the subtitle is named th-or-<vendor>",
    )
    args = parser.parse_args()
    name, key = None, None
    if args.openrouter is not None:
        if not args.openrouter.strip():
            parser.error("--openrouter MODEL must not be empty")
        name = openrouter_translate.subtitle_name(args.openrouter)
        try:
            key = openrouter_translate.api_key()
        except openrouter_translate.OpenRouterError as e:
            parser.exit(1, f"{e}\n")
    path = os.environ["TRANSLATE_PATH"]
    browser, width, height = None, None, None
    failures = 0
    to_lang = os.environ.get("TRANSLATE_TO_LANG", "th")
    from_lang = os.environ.get("TRANSLATE_FROM_LANG", "en")
    # mkv can be remuxed; mp4 is sidecar-srt only
    files_list = glob.glob(f"{path}/*.mkv") + glob.glob(f"{path}/*.mp4")
    files_list = [
        f
        for f in files_list
        if len(list(filter(lambda x: f[:-7] in x, files_list))) == 1
    ]
    for file in files_list:
        srt_file = f"{file[:-4]}.srt"
        backup_srt = f"{srt_file}.orig"
        translated_srt = f"{file[:-4]}.{name or to_lang}.srt"
        # recover the source subtitle from an interrupted run; the .srt left
        # behind may already be cleaned or translated
        if os.path.exists(f"{backup_srt}.tmp"):
            os.remove(f"{backup_srt}.tmp")
        if name and os.path.exists(f"{translated_srt}.part"):
            os.remove(f"{translated_srt}.part")
        if os.path.exists(backup_srt):
            os.replace(backup_srt, srt_file)
        srt_only = False
        if not file.endswith((".mkv", ".mp4")):
            print("Skipping unsupported file")
            continue
        elif os.path.exists(srt_file):
            if os.path.exists(translated_srt):
                print(f"Already translated, skipping:\n  {translated_srt}")
                continue
            srt_only = True
        # else:
        #     continue
        print(file)
        source = None
        if not os.path.exists(srt_file):
            try:
                id, convert, translation_needed = get_track_and_type(
                    path=file, from_lang=from_lang, to_lang=to_lang
                )
            except Exception as e:
                print(f"Error getting track and type:\n  {file}")
                os.rename(file, f"{file}-subtitles_error")
                continue
            extract_and_convert_ass_to_srt(path=file, track_id=id, convert=convert)
            if args.dual and not translation_needed:
                source = get_source_track(path=file, from_lang=from_lang)
        else:
            translation_needed = True
        if srt_only:
            # a .orig that exists is always complete, recovery trusts it
            shutil.copy2(srt_file, f"{backup_srt}.tmp")
            os.replace(f"{backup_srt}.tmp", backup_srt)
        with open(srt_file, "rb") as f:
            raw = f.read()
        # same fallback pandoc uses for non-UTF-8 input
        try:
            original_srt = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            original_srt = raw.decode("latin-1")
        # --dual already shows 2 lines per cue, don't pair up fast lines too
        cleaned_srt = clean_rolling_srt(original_srt, max_cps=None if args.dual else 20)
        if cleaned_srt is not original_srt:
            print(
                f"  cleaned rolling auto-captions: {len(parse_srt(original_srt))}"
                f" -> {len(parse_srt(cleaned_srt))} cues"
            )
            with open(srt_file, "w", encoding="utf-8") as f:
                f.write(cleaned_srt)
            original_srt = cleaned_srt
        if source is not None:
            # both languages are already in the mkv, just show them together
            source_srt = ""
            with tempfile.TemporaryDirectory() as tmp:
                source_file = os.path.join(tmp, "source.srt")
                try:
                    extract_and_convert_ass_to_srt(
                        path=file,
                        track_id=source[0],
                        convert=source[1],
                        srt_path=source_file,
                    )
                    with open(source_file, encoding="utf-8-sig") as f:
                        source_srt = clean_rolling_srt(f.read(), max_cps=None)
                except Exception as e:
                    print(f"  failed to read the {from_lang} track: {type(e).__name__}")
            if parse_srt(source_srt):
                with open(srt_file, "w", encoding="utf-8") as f:
                    f.write(merge_srt_by_overlap(source_srt, original_srt))
                print(f"  merged embedded {from_lang} + {to_lang} tracks")
            else:
                print(f"  no embedded {from_lang} cues, keeping {to_lang} only")
        if translation_needed and args.openrouter:
            try:
                translated = openrouter_translate.translate_srt(
                    original_srt, from_lang, to_lang, args.openrouter, key
                )
                if args.dual:
                    translated = merge_srt_by_overlap(original_srt, translated)
                if srt_only:
                    with open(f"{translated_srt}.part", "w", encoding="utf-8") as f:
                        f.write(translated)
                    os.replace(f"{translated_srt}.part", translated_srt)
                else:
                    with open(srt_file, "w", encoding="utf-8") as f:
                        f.write(translated)
            except openrouter_translate.OpenRouterError as e:
                # nothing is written, so the next run tries this file again
                print(f"  not translated: {e}")
                failures += 1
                if not srt_only:
                    # extracted from the mkv; left behind it would look like a sidecar
                    os.remove(srt_file)
                continue
            finally:
                if srt_only:
                    # keep the source subtitle, the translation is a sidecar
                    os.replace(backup_srt, srt_file)
                    if os.path.exists(f"{translated_srt}.part"):
                        os.remove(f"{translated_srt}.part")
        elif translation_needed:
            convert_to_docx(path=file)
            if browser is not None:
                change_browser_window_size(
                    browser=browser, max_width=width, max_height=height
                )

            def translate_file():
                return translate(
                    path=f"{file[:-3]}docx",
                    from_lang=from_lang,
                    to_lang=to_lang,
                    browser=browser,
                )

            if browser is None:
                browser, width, height = translate_file()
            else:
                browser, _, _ = translate_file()
            # from time import sleep
            # sleep(20)
            convert_to_srt(path=file)
            if args.dual:
                with open(srt_file, encoding="utf-8") as f:
                    merged = merge_srt(original_srt, f.read())
                with open(srt_file, "w", encoding="utf-8") as f:
                    f.write(merged)
            if srt_only:
                # keep the source subtitle, publish the translation as a sidecar
                os.replace(srt_file, translated_srt)
                os.replace(backup_srt, srt_file)
        if srt_only:
            print(f"  -> {translated_srt}")
            continue
        strip_and_add_subtitle(
            path=file,
            language=to_lang,
            track_name=name if translation_needed else None,
        )
    if browser is not None:
        browser.quit()
    if failures:
        parser.exit(1, f"{failures} file(s) not translated\n")
