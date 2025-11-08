import os
from translate import change_browser_window_size, translate
from utils import convert_to_docx, convert_to_srt, extract_and_convert_ass_to_srt, get_track_and_type, strip_and_add_subtitle
import glob


if __name__ == '__main__':
    path = 'some_unix_path'
    browser, width, height = None, None, None
    to_lang, from_lang = 'th', 'en'
    files_list = glob.glob(f'{path}/*.mkv')
    files_list = [f for f in files_list if len(list(filter(lambda x: f[:-7] in x, files_list))) == 1]
    for file in files_list:
        if not file.endswith('.mkv'):
            continue
        print(file)
        try:
            id, convert, translation_needed = get_track_and_type(path=file, from_lang=from_lang, to_lang=to_lang)
        except Exception as e:
            print(f"Error getting track and type")
            os.rename(file, f"{file}-subtitles_error")
            continue
        extract_and_convert_ass_to_srt(path=file, track_id=id, convert=convert)
        if translation_needed:
            convert_to_docx(path=file)
            if browser is not None:
                change_browser_window_size(browser=browser, max_width=width, max_height=height)
            def translate_file():
                return translate(path=f'{file[:-3]}docx', from_lang=from_lang, to_lang=to_lang, browser=browser)
            if browser is None:
                browser, width, height = translate_file()
            else:
                browser, _, _ = translate_file()
            # from time import sleep
            # sleep(20)
            convert_to_srt(path=file)
        strip_and_add_subtitle(path=file, language=to_lang)
    if browser is not None:
        browser.quit()
