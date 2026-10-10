import bisect
import html
import re
import subprocess, os, json

TIMESTAMP_RE = re.compile(
    r"^\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-+\s*>\s*(\d{2}:\d{2}:\d{2},\d{3})\s*(.*)$"
)


def parse_srt(text):
    """Parse SRT text into (start, end, body_lines) cues.

    Anchors on the timestamps and reattaches everything between them as that
    cue's text, so it tolerates the damage done by the pandoc/Google round-trip.
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
    return cues


def emit_srt(cues):
    blocks = [
        f"{n}\n{start} --> {end}\n" + "\n".join(body or [""])
        for n, (start, end, body) in enumerate(cues, 1)
    ]
    return "\n\n".join(blocks) + "\n"


def rebuild_srt(text):
    """Re-emit a well-formed SRT from the pandoc/Google round-trip.

    The round-trip damages cues three ways: a doubled space before `-->`,
    dialogue cues (`- line`) split off from their header by a spurious blank
    line, and short cues whose text is glued onto the timestamp line. Anchor on
    the timestamps -- those survive intact -- and reattach everything between
    them as that cue's text.
    """
    return emit_srt(parse_srt(text))


def _cue_key(body):
    # pandoc drops <i>-style tags from the translated side, so ignore them
    text = re.sub(r"<[^>]*>|\{[^}]*\}", "", " ".join(body))
    return " ".join(text.lower().split())


def merge_srt(original_text, translated_text):
    """Combine both subtitles so each cue shows the translation above the original."""
    translated = {}
    for start, end, body in parse_srt(translated_text):
        translated.setdefault((start, end), []).append(body)
    cues = []
    for start, end, body in parse_srt(original_text.lstrip("\ufeff")):
        matches = translated.get((start, end))
        if matches:
            translated_body = matches.pop(0)
            # untranslated cues (names, music notes) would show twice
            if _cue_key(translated_body) == _cue_key(body):
                cues.append((start, end, body))
            else:
                cues.append((start, end, translated_body + body))
        else:
            cues.append((start, end, body))
    for (start, end), bodies in translated.items():
        cues.extend((start, end, body) for body in bodies)
    cues.sort(key=lambda cue: (cue[0], cue[1]))
    return emit_srt(cues)


def _ms(timestamp):
    h, m, s = timestamp.replace(",", ".").split(":")
    return round((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)


def _timestamp(ms):
    seconds, ms = divmod(ms, 1000)
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02},{ms:03}"


def merge_srt_by_overlap(original_text, translated_text):
    """Combine independently timed subtitles, the translation above the original.

    The translated cues set the timeline: each original cue joins the translated
    cue it overlaps most (ties go to the earlier one), one overlapping none
    stays a cue of its own. A cue spans all its parts, cut back where it runs
    into a later one but never into either one's own translated (or lone
    original) time, so only cues that already overlapped in a track still do.
    Text a cue already shows (frame-by-frame signs, names) isn't added again.
    """

    def timed(text):
        cues = [
            (_ms(start), _ms(end), body)
            for start, end, body in parse_srt(text.lstrip("\ufeff"))
        ]
        return sorted(cues, key=lambda cue: cue[:2])

    # span start, span end, own start, own end, translated lines, original
    # lines, keys of the text shown
    cues = [
        [start, end, start, end, body, [], {_cue_key(body)}]
        for start, end, body in timed(translated_text)
    ]
    starts = [cue[2] for cue in cues]
    reach = []
    for cue in cues:
        reach.append(max(cue[3], reach[-1] if reach else cue[3]))
    lone = []
    for start, end, body in timed(original_text):
        best, most = None, 0
        for i in range(bisect.bisect_left(starts, end) - 1, -1, -1):
            if reach[i] <= start:
                break
            overlap = min(end, cues[i][3]) - max(start, cues[i][2])
            if overlap > 0 and overlap >= most:
                best, most = i, overlap
        if best is None:
            lone.append([start, end, start, end, [], body, set()])
            continue
        cue = cues[best]
        cue[0], cue[1] = min(cue[0], start), max(cue[1], end)
        # untranslated cues (names, music notes) would show twice
        if _cue_key(body) not in cue[6]:
            cue[5] += body
            cue[6].add(_cue_key(body))
    merged = sorted(cues + lone, key=lambda cue: cue[2:4])
    latest = 0
    for i, following in enumerate(merged):
        # nearest earlier cue first, so clean tracks only ever cut neighbours
        for cue in merged[i - 1 :: -1] if i and latest > following[0] else []:
            if cue[1] > following[0]:
                cut = max(following[0], min(cue[3], following[2]))
                cue[1], following[0] = max(cut, cue[3]), min(cut, following[2])
        latest = max(latest, following[1])
    return emit_srt(
        [(_timestamp(cue[0]), _timestamp(cue[1]), cue[4] + cue[5]) for cue in merged]
    )


def _repeated(previous, body):
    # longest run of leading lines that repeats the previous cue's last lines
    keys = [_cue_key([line]) for line in body]
    previous = [_cue_key([line]) for line in previous]
    for n in range(min(len(previous), len(keys)), 0, -1):
        if keys[:n] == previous[-n:]:
            return n
    return 0


THAI = "\u0e01-\u0e5b"


def join_split_thai_words(lines, window=40):
    """Put the shorter half of a Thai word split across two lines back with the rest.

    Thai has no spaces, so auto-captions break lines mid-word and each line is
    translated on its own. Text that isn't mostly Thai is returned as is; only
    dictionary words with Thai on both sides of the break move (so tags, `>>`
    markers and other scripts stay put), and no line is left with only tags or
    markers.
    """
    text = "".join(lines)
    thai = len(re.findall(f"[{THAI}]", text))
    if thai * 2 <= len(re.findall(rf"[^\W\d_]|[{THAI}]", text)):
        return lines
    from pythainlp.corpus import thai_words
    from pythainlp.tokenize import word_tokenize

    def readable(line):
        # still has text once tags and markers are gone
        line = re.sub(r"<[^>]*>|\{[^}]*\}|\[[^\]]*\]", "", line)
        return re.search(rf"[^\W_]|[{THAI}]", line)

    words = thai_words()
    lines = list(lines)
    for i in range(len(lines) - 1):
        # ๆ, tone marks and trailing vowels never start a syllable
        mark = re.match("[\u0e30-\u0e3a\u0e45-\u0e4e]+", lines[i + 1])
        rest = lines[i + 1][mark.end() :] if mark else ""
        if mark and re.search(f"[{THAI}]$", lines[i]) and readable(rest):
            lines[i], lines[i + 1] = lines[i] + mark[0], rest.lstrip()
        tail = re.search(f"[{THAI}]{{1,{window}}}$", lines[i])
        head = re.match(f"[{THAI}]{{1,{window}}}", lines[i + 1])
        if not (tail and head):
            continue
        left, right = tail[0], head[0]
        tokens = word_tokenize(left + right, engine="newmm")
        offset = 0
        for word in tokens:
            if offset < len(left) < offset + len(word):
                break
            offset += len(word)
        else:
            continue
        # a word that only regroups valid halves (บอก|ว่า read as บอ|กว่า)
        # saves no token, so only fix breaks that make the halves tokenize worse
        if word not in words or len(tokens) >= len(
            word_tokenize(left, engine="newmm") + word_tokenize(right, engine="newmm")
        ):
            continue
        back = len(left) - offset
        ahead = len(word) - back
        cuts = [len(lines[i]) - back, len(lines[i]) + ahead]
        pair = lines[i] + lines[i + 1]
        # move the shorter half, unless that empties a line, strands ไม่ (not)
        # or pulls ผม (I) back onto the ครับ ending the sentence before
        for cut in cuts if back < ahead else cuts[::-1]:
            if readable(pair[:cut]) and readable(pair[cut:]):
                if not re.search("ไม่$|ครับผม$", pair[:cut]):
                    lines[i], lines[i + 1] = pair[:cut].rstrip(), pair[cut:].lstrip()
                break
    return lines


def clean_rolling_srt(
    text,
    min_ms=100,
    max_cps=20,
    min_show_ms=833,
    max_delay_ms=1200,
):
    """Flatten YouTube auto-captions into one non-overlapping line sequence.

    Auto-captions roll: each cue repeats the previous cue's line above a new
    one, joined by ~10 ms cues that only repeat text, or (TTML/srv1 exports)
    every line stays up until the line after next appears. Players show all
    active cues, so lines stack and linger. Detected when a quarter of the
    consecutive cues carry the previous cue's last line plus a new one, or
    half end exactly as the cue after next starts; runs of identical cues
    (layered or frame-by-frame ASS) don't count, and anything else is returned
    unchanged. A rolled cue drops the lines it carries over from the cue right
    before it, cues left empty are dropped, ends are clipped to the next start,
    and a line faster than max_cps chars/s stays up with the next one as a
    2-line cue (max_cps=None keeps every cue to one line). Each cue then stays
    up at least min_show_ms, pushing later cues back, but no cue starts more
    than max_delay_ms late: past that the held cue is cut short instead. Thai
    words split across lines are put back together before pairing.
    """
    cues = [
        (_ms(start), _ms(end), body)
        for start, end, body in parse_srt(text.lstrip("\ufeff"))
    ]
    spoken = []
    for cue in cues:
        if cue[2] and not (spoken and _cue_key(cue[2]) == _cue_key(spoken[-1][2])):
            spoken.append(cue)
    pairs = len(spoken) - 1
    rolled = sum(
        b[0] - a[1] <= 50 and 0 < _repeated(a[2], b[2]) < len(b[2])
        for a, b in zip(spoken, spoken[1:])
    )
    held = sum(
        b[0] < a[1] and abs(a[1] - c[0]) <= 50
        for a, b, c in zip(spoken, spoken[1:], spoken[2:])
    )
    if pairs < 4 or (rolled < pairs / 4 and held < pairs / 2):
        return text
    kept = []
    previous, previous_end = [], None
    for start, end, body in cues:
        # only the cue right before can be rolled over; a gap or blank ends it
        if previous_end is not None and start - previous_end > 50:
            previous = []
        new = body[_repeated(previous, body) if rolled >= pairs / 4 else 0 :]
        previous, previous_end = body, end
        if not new:
            # a repeat only stretches the line it repeats
            if body and kept:
                kept[-1][1] = max(kept[-1][1], end)
            continue
        if kept and start - kept[-1][0] < min_ms:
            kept[-1][1] = max(kept[-1][1], end)
            kept[-1][2] += new
            continue
        kept.append([start, max(end, start + min_ms), new])
    lines = iter(join_split_thai_words([line for cue in kept for line in cue[2]]))
    for cue in kept:
        cue[2] = [next(lines) for _ in cue[2]]
    for cue, following in zip(kept, kept[1:]):
        cue[1] = min(cue[1], following[0])
    merged = []
    for cue in kept:
        last = merged[-1] if merged else None
        if (
            last
            and max_cps
            and len(last[2]) == len(cue[2]) == 1
            and cue[0] - last[1] <= 50
            and len(_cue_key(last[2])) * 1000 > max_cps * (last[1] - last[0])
        ):
            last[1], last[2] = cue[1], last[2] + cue[2]
        else:
            merged.append(cue)
    # clipping leaves lines that started together flashing by; hold each one
    # long enough to read and push the next ones back, the slack of a longer
    # cue or a pause soaks up the delay; past max_delay_ms the held cue is cut
    # short instead, so a start is never more than max_delay_ms late and a
    # cut cue still lasts at least the min_ms its start is ahead of the next
    for i, cue in enumerate(merged):
        if i:
            cue[0] = max(cue[0], merged[i - 1][1])
        cue[1] = max(cue[1], cue[0] + min_show_ms)
        if i + 1 < len(merged):
            cue[1] = min(cue[1], merged[i + 1][0] + max_delay_ms)
    return emit_srt(
        [(_timestamp(start), _timestamp(end), body) for start, end, body in merged]
    )


def get_track_and_type(path=None, from_lang="en", to_lang="th"):
    out = subprocess.run(
        ["mkvmerge", "-J", path], encoding="utf-8", stdout=subprocess.PIPE
    )
    json_output = json.loads(out.stdout)
    # print(json.dumps(json_output, indent=3))
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
    else:
        # in case there's only 1 track but badly labeled
        if (
            len(list(filter(lambda t: t["type"] == "subtitles", json_output["tracks"])))
            == 1
        ):
            track = list(
                filter(lambda t: t["type"] == "subtitles", json_output["tracks"])
            )[0]
            if track["properties"]["codec_id"] == "S_TEXT/ASS":
                return track["id"], True, True
            return track["id"], False, True

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
    else:
        # in case there's only 1 track but badly labeled
        if (
            len(list(filter(lambda t: t["type"] == "subtitles", json_output["tracks"])))
            == 1
        ):
            track = list(
                filter(lambda t: t["type"] == "subtitles", json_output["tracks"])
            )[0]
            if track["properties"]["codec_id"] == "S_TEXT/ASS":
                return track["id"], True, True
            return track["id"], False, True


# text subtitle codecs, and whether ffmpeg has to turn them into srt
TEXT_CODECS = {
    "S_TEXT/UTF8": False,
    "S_TEXT/ASS": True,
    "S_TEXT/SSA": True,
    "S_TEXT/WEBVTT": True,
}


def get_source_track(path=None, from_lang="en"):
    """Find the from_lang track to show under an existing to_lang one.

    Returns (track id, needs ffmpeg conversion) for the non-forced text track
    with the most entries (a signs track has few; ties go to the first), or None.
    """
    out = subprocess.run(
        ["mkvmerge", "-J", path], encoding="utf-8", stdout=subprocess.PIPE
    )
    best = None
    for track in json.loads(out.stdout)["tracks"]:
        properties = track["properties"]
        if (
            track["type"] != "subtitles"
            or "orced" in properties.get("track_name", "")
            or properties.get("forced_track")
            or properties["codec_id"] not in TEXT_CODECS
        ):
            continue
        language_key = "language_ietf" if "language_ietf" in properties else "language"
        entries = properties.get("num_index_entries", 0)
        if properties[language_key].startswith(from_lang) and (
            best is None or entries > best[2]
        ):
            best = track["id"], TEXT_CODECS[properties["codec_id"]], entries
    return best[:2] if best else None


def extract_and_convert_ass_to_srt(
    path=None, track_id=None, convert=False, srt_path=None
):
    srt_path = srt_path or f"{path[:-3]}srt"
    ext = "ass" if convert else "srt"
    out = subprocess.run(
        ["mkvextract", "tracks", path, f"{track_id}:{srt_path[:-3]}{ext}"],
        encoding="utf-8",
        stdout=subprocess.PIPE,
    )
    if out.returncode != 0:
        raise Exception(
            f"Failed to extract subtitles\n{path=}\n{track_id=}\n{convert=}"
        )
    if convert:
        out = subprocess.run(
            ["ffmpeg", "-i", f"{srt_path[:-3]}ass", "-c:s", "srt", srt_path],
            encoding="utf-8",
            stdout=subprocess.PIPE,
        )
        if out.returncode != 0:
            raise Exception(
                f"Failed to convert subtitles\n{path=}\n{track_id=}\n{convert=}"
            )
        os.remove(f"{srt_path[:-3]}ass")


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
