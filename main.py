import os
import shutil
from translate import change_browser_window_size, translate
from utils import (
    convert_to_docx,
    convert_to_srt,
    extract_and_convert_ass_to_srt,
    get_track_and_type,
    strip_and_add_subtitle,
)
import glob
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    path = os.environ["TRANSLATE_PATH"]
    browser, width, height = None, None, None
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
        translated_srt = f"{file[:-4]}.{to_lang}.srt"
        # recover the source subtitle from an interrupted run
        if os.path.exists(backup_srt) and not os.path.exists(srt_file):
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
        else:
            translation_needed = True
        if translation_needed:
            if srt_only:
                shutil.copy2(srt_file, backup_srt)
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
            if srt_only:
                # keep the source subtitle, publish the translation as a sidecar
                os.replace(srt_file, translated_srt)
                os.replace(backup_srt, srt_file)
        if srt_only:
            print(f"  -> {translated_srt}")
            continue
        strip_and_add_subtitle(path=file, language=to_lang)
    if browser is not None:
        browser.quit()
