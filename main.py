from translate import change_browser_window_size, translate
from utils import convert_to_docx, convert_to_srt, extract_and_convert_ass_to_srt, get_track_and_type, strip_and_add_subtitle
import glob


if __name__ == '__main__':
    path = 'some_unix_path'
    browser, width, height = None, None, None
    for file in glob.glob(f'{path}/*'):
        print(file)
        id, convert = get_track_and_type(path=file)
        extract_and_convert_ass_to_srt(path=file, track_id=id, convert=convert)
        convert_to_docx(path=file)
        if browser is not None:
            change_browser_window_size(browser=browser, max_width=width, max_height=height)
        def translate_file():
            return translate(path=f'{file[:-3]}docx', from_lang='en', to_lang='th', browser=browser)
        if browser is None:
            browser, width, height = translate_file()
        else:
            browser, _, _ = translate_file()
        convert_to_srt(path=file)
        strip_and_add_subtitle(path=file, language='th')
    browser.quit()
