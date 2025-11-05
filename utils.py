import html
import subprocess, os, json

def get_track_and_type(path=None):
    out = subprocess.run(['mkvmerge', '-J', path], encoding='utf-8', stdout=subprocess.PIPE)
    json_output = json.loads(out.stdout)
    for track in json_output['tracks']:
        if track['type'] == 'subtitles' and 'language' in track['properties'] and 'orced' not in track['properties'].get('track_name', ''):
            if track['properties']['language'] == 'eng':
                if track['properties']['codec_id'] == 'S_TEXT/ASS':
                    return track['id'], True
                return track['id'], False


def extract_and_convert_ass_to_srt(path=None, track_id=None, convert=False):
    ext = 'ass' if convert else 'srt'
    out = subprocess.run(['mkvextract', 'tracks', path, f'{track_id}:{path[:-3]}{ext}'], encoding='utf-8', stdout=subprocess.PIPE)
    if out.returncode != 0:
        raise Exception(f"Failed to extract subtitles\n{path=}\n{track_id=}\n{convert=}")
    if convert:
        out = subprocess.run(['ffmpeg', '-i', f'{path[:-3]}ass', '-c:s', 'srt', f'{path[:-3]}srt'], encoding='utf-8', stdout=subprocess.PIPE)
        if out.returncode != 0:
            raise Exception(f"Failed to convert subtitles\n{path=}\n{track_id=}\n{convert=}")
        os.remove(f'{path[:-3]}ass')


def convert_to_docx(path=None):
    out = subprocess.run(['pandoc', f'{path[:-3]}srt', '-o', f'{path[:-3]}docx', '-f', 'textile', '-t', 'docx'], encoding='utf-8', stdout=subprocess.PIPE)
    if out.returncode != 0:
        raise Exception(f"Failed to convert to DOCX\n{path=}")
    os.remove(f'{path[:-3]}srt')


def convert_html_encodings(path=None):
    srt_file = f'{path[:-3]}srt'
    text = None
    with open(srt_file, 'r', encoding='utf-8') as f:
        text = f.read()
    unescaped_text = html.unescape(text)
    unescaped_text = unescaped_text.replace('-- >', '-->')
    with open(srt_file, 'w', encoding='utf-8') as f:
        f.write(unescaped_text)


def convert_to_srt(path=None):
    out = subprocess.run(['pandoc', f'{path[:-3]}docx', '-o', f'{path[:-3]}srt', '-t', 'textile', '-f', 'docx'], encoding='utf-8', stdout=subprocess.PIPE)
    if out.returncode != 0:
        raise Exception(f"Failed to convert to SRT\n{path=}")
    convert_html_encodings(path=path)
    # out = subprocess.run(['sed', '-iR', 's/-- >/-->/g', f'{path[:-3]}srt'], encoding='utf-8', stdout=subprocess.PIPE)
    # if out.returncode != 0:
    #     raise Exception(f"Failed to fix SRT formatting\n{path=}")
    os.remove(f'{path[:-3]}docx')


def strip_and_add_subtitle(path=None, language='th'):
    strip_tag = '_stripped'
    stripped_file = f'{path[:-4]}{strip_tag}.mkv'
    srt_file = f'{path[:-3]}srt'
    out = subprocess.run(['mkvmerge', '-o', stripped_file, '-S', path], encoding='utf-8', stdout=subprocess.PIPE)
    if out.returncode != 0:
        raise Exception(f"Failed to strip subtitles\n{path=}")
    new_file = stripped_file.replace(strip_tag, f'_{language}')
    out = subprocess.run(['mkvmerge', '-o', new_file, stripped_file, '--language', f'0:{language}', '--track-name', f'0:{language}5555', srt_file], encoding='utf-8', stdout=subprocess.PIPE)
    if out.returncode != 0:
        raise Exception(f"Failed to add subtitles\n{stripped_file=}\n{srt_file=}")
    os.remove(stripped_file)
    os.remove(srt_file)