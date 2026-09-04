import html
import re
import subprocess, os, json

TIMESTAMP_RE = re.compile(
    r"^\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-+\s*>\s*(\d{2}:\d{2}:\d{2},\d{3})\s*(.*)$"
)


def rebuild_srt(text):
    """Re-emit a well-formed SRT from the pandoc/Google round-trip.

    The round-trip damages cues three ways: a doubled space before `-->`,
    dialogue cues (`- line`) split off from their header by a spurious blank
    line, and short cues whose text is glued onto the timestamp line. Anchor on
    the timestamps -- those survive intact -- and reattach everything between
    them as that cue's text.
    """
    lines = text.splitlines()
    cues = []
    i = 0
    while i < len(lines):
        match = TIMESTAMP_RE.match(lines[i])
        if match is None:
            i += 1
            continue
        start, end, trailing = match.groups()
        body = [trailing.strip()] if trailing.strip() else []
        i += 1
        while i < len(lines):
            line = lines[i].strip()
            if TIMESTAMP_RE.match(lines[i]):
                break
            # a bare number right before a timestamp is the next cue's index
            if (
                line.isdigit()
                and i + 1 < len(lines)
                and TIMESTAMP_RE.match(lines[i + 1])
            ):
                break
            if line:
                body.append(line)
            i += 1
        cues.append((start, end, body))
    blocks = [
        f"{n}\n{start} --> {end}\n" + "\n".join(body or [""])
        for n, (start, end, body) in enumerate(cues, 1)
    ]
    return "\n\n".join(blocks) + "\n"


def get_track_and_type(path=None, from_lang="en", to_lang="th"):
    out = subprocess.run(
        ["mkvmerge", "-J", path], encoding="utf-8", stdout=subprocess.PIPE
    )
    json_output = json.loads(out.stdout)
    # maybe the target language already exists
    for track in json_output["tracks"]:
        if track["type"] == "subtitles":
            language_key = (
                "language_ietf"
                if "language_ietf" in track["properties"]
                else "language"
            )
            if track["properties"][language_key].startswith(to_lang):
                if track["properties"]["codec_id"] == "S_TEXT/ASS":
                    return track["id"], True, False
                return track["id"], False, False
    # otherwise extract the src language for translation
    for track in json_output["tracks"]:
        if track["type"] == "subtitles" and "orced" not in track["properties"].get(
            "track_name", ""
        ):
            language_key = (
                "language_ietf"
                if "language_ietf" in track["properties"]
                else "language"
            )
            if track["properties"][language_key].startswith(from_lang):
                if track["properties"]["codec_id"] == "S_TEXT/ASS":
                    return track["id"], True, True
                return track["id"], False, True


def extract_and_convert_ass_to_srt(path=None, track_id=None, convert=False):
    ext = "ass" if convert else "srt"
    out = subprocess.run(
        ["mkvextract", "tracks", path, f"{track_id}:{path[:-3]}{ext}"],
        encoding="utf-8",
        stdout=subprocess.PIPE,
    )
    if out.returncode != 0:
        raise Exception(
            f"Failed to extract subtitles\n{path=}\n{track_id=}\n{convert=}"
        )
    if convert:
        out = subprocess.run(
            ["ffmpeg", "-i", f"{path[:-3]}ass", "-c:s", "srt", f"{path[:-3]}srt"],
            encoding="utf-8",
            stdout=subprocess.PIPE,
        )
        if out.returncode != 0:
            raise Exception(
                f"Failed to convert subtitles\n{path=}\n{track_id=}\n{convert=}"
            )
        os.remove(f"{path[:-3]}ass")


def convert_to_docx(path=None):
    out = subprocess.run(
        [
            "pandoc",
            f"{path[:-3]}srt",
            "-o",
            f"{path[:-3]}docx",
            "-f",
            "textile",
            "-t",
            "docx",
        ],
        encoding="utf-8",
        stdout=subprocess.PIPE,
    )
    if out.returncode != 0:
        raise Exception(f"Failed to convert to DOCX\n{path=}")
    os.remove(f"{path[:-3]}srt")


def convert_html_encodings(path=None):
    srt_file = f"{path[:-3]}srt"
    text = None
    with open(srt_file, "r", encoding="utf-8") as f:
        text = f.read()
    unescaped_text = html.unescape(text)
    unescaped_text = unescaped_text.replace("-- >", "-->")
    unescaped_text = rebuild_srt(unescaped_text)
    with open(srt_file, "w", encoding="utf-8") as f:
        f.write(unescaped_text)


def convert_to_srt(path=None):
    out = subprocess.run(
        [
            "pandoc",
            f"{path[:-3]}docx",
            "-o",
            f"{path[:-3]}srt",
            "-t",
            "textile",
            "-f",
            "docx",
        ],
        encoding="utf-8",
        stdout=subprocess.PIPE,
    )
    if out.returncode != 0:
        raise Exception(f"Failed to convert to SRT\n{path=}")
    convert_html_encodings(path=path)
    # out = subprocess.run(['sed', '-iR', 's/-- >/-->/g', f'{path[:-3]}srt'], encoding='utf-8', stdout=subprocess.PIPE)
    # if out.returncode != 0:
    #     raise Exception(f"Failed to fix SRT formatting\n{path=}")
    os.remove(f"{path[:-3]}docx")


def strip_and_add_subtitle(path=None, language="th"):
    strip_tag = "_stripped"
    stripped_file = f"{path[:-4]}{strip_tag}.mkv"
    srt_file = f"{path[:-3]}srt"
    out = subprocess.run(
        ["mkvmerge", "-o", stripped_file, "-S", path],
        encoding="utf-8",
        stdout=subprocess.PIPE,
    )
    if out.returncode != 0:
        raise Exception(f"Failed to strip subtitles\n{path=}")
    new_file = stripped_file.replace(strip_tag, f"_{language}")
    out = subprocess.run(
        [
            "mkvmerge",
            "-o",
            new_file,
            stripped_file,
            "--language",
            f"0:{language}",
            "--track-name",
            f"0:{language}5555",
            srt_file,
        ],
        encoding="utf-8",
        stdout=subprocess.PIPE,
    )
    if out.returncode != 0:
        raise Exception(f"Failed to add subtitles\n{stripped_file=}\n{srt_file=}")
    os.remove(stripped_file)
    os.remove(srt_file)
