import json
import subprocess
import urllib.error

import pytest

import openrouter_translate as ot

THAI_LINES = [
    "สวัสดีครับคุณผู้ชม",
    "วันนี้อากาศดีมาก",
    "เราจะไปเที่ยวทะเล",
    "แล้วก็กินข้าวกัน",
]


def reply(content, finish="stop", cost=0.001):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"cost": cost},
    }


def lines_reply(lines, **kwargs):
    return reply(json.dumps({"lines": lines}), **kwargs)


def is_lines(payload):
    return '"lines"' in payload["messages"][0]["content"]


class Fake:
    """Scripted _post: responder(payload) returns a response or raises."""

    def __init__(self, responder):
        self.responder = responder
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        return self.responder(payload)


def make(responder, from_lang="th", to_lang="en", model=ot.DEFAULT_MODEL):
    translator = ot.Translator(model, from_lang, to_lang, "k", "http://stub/v1/")
    fake = Fake(responder)
    translator._post = fake
    return translator, fake


def cues_of(texts, step=1000, length=900):
    return [(i * step, i * step + length, text) for i, text in enumerate(texts)]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(ot.time, "sleep", slept.append)
    return slept


# grouping


def test_group_splits_on_gap_chars_and_count():
    cues = cues_of(["ก" * 10] * 3)
    cues.append((cues[-1][1] + 1001, cues[-1][1] + 2000, "ข"))
    assert ot.group_cues(cues) == [[0, 1, 2], [3]]

    cues = cues_of(["ก" * 150] * 3)
    assert ot.group_cues(cues) == [[0, 1], [2]]

    cues = cues_of(["ก"] * 30)
    assert [len(g) for g in ot.group_cues(cues)] == [12, 12, 6]


def test_group_speaker_marker_needs_context():
    # a >> after 2 short cues stays in the group
    cues = cues_of(["ก" * 10, "ข" * 10, ">> ค" * 3, "-ง"])
    assert ot.group_cues(cues) == [[0, 1, 2, 3]]
    # after 4 cues it starts a new group
    cues = cues_of(["ก"] * 4 + [">> ข", "ค"])
    assert ot.group_cues(cues) == [[0, 1, 2, 3], [4, 5]]
    # after 150 characters too
    cues = cues_of(["ก" * 150, "-ข"])
    assert ot.group_cues(cues) == [[0], [1]]


# parsing and cleaning


def test_parse_lines_is_lenient():
    assert ot.parse_lines('["a", "b"]') == ["a", "b"]
    assert ot.parse_lines('{"lines": ["a"]}') == ["a"]
    assert ot.parse_lines('```json\n["a", "[Music]"]\n```') == ["a", "[Music]"]
    assert ot.parse_lines('Sure, here it is: ["a", "b"] hope it helps') == ["a", "b"]
    assert ot.parse_lines('<think>hmm</think>["a"]') == ["a"]
    assert ot.parse_lines("no json") is None
    assert ot.parse_lines("[1, 2]") is None


def test_clean_output_quotes_markers_whitespace():
    assert ot.clean_output('"Hello there"', "สวัสดี", True) == "Hello there"
    assert ot.clean_output("“Hi”", "สวัสดี", True) == "Hi"
    assert ot.clean_output('He said "hi"', 'เขาพูดว่า "hi"', True) == 'He said "hi"'
    assert ot.clean_output(">> Hi", "สวัสดี", True) == "Hi"
    assert ot.clean_output("Hi", ">> สวัสดี", True) == ">> Hi"
    assert ot.clean_output(">> Hi", ">> สวัสดี", True) == ">> Hi"
    assert ot.clean_output(">> Hi >> there", ">> สวัสดี", False) == "Hi there"
    assert ot.clean_output("one\n\ntwo   three", "x", True) == "one two three"


# guards


def test_untranslated_guard():
    source = "สวัสดีครับคุณผู้ชมวันนี้อากาศดีมาก"
    assert not ot.untranslated(
        source, "Hello viewers, the weather is nice.", "th", "en"
    )
    assert ot.untranslated(source, "", "th", "en")
    assert ot.untranslated(source, "Hello viewers", "th", "en", "length")
    assert ot.untranslated(source, "สวัสดีครับ hello", "th", "en")
    assert ot.untranslated(source, " ".join(["again"] * 60), "th", "en")
    assert ot.untranslated("Hello there", "Hello there", "en", "th")
    assert not ot.untranslated("Hello there", "สวัสดีครับ", "en", "th")


def test_max_tokens():
    assert ot._max_tokens("x" * 10) == 168
    assert ot._max_tokens("x" * 5000) == 4096


# primary path


def test_primary_accepts_line_aligned_reply():
    english = ["Hello viewers", "The weather is nice", "We go to the sea", "And eat"]
    translator, fake = make(lambda p: lines_reply(english))
    cues = cues_of(THAI_LINES)
    out, ok = translator.group(cues)
    assert ok
    assert out == [(s, e, t) for (s, e, _), t in zip(cues, english)]
    payload = fake.payloads[0]
    assert payload["reasoning"] == {"enabled": False}
    assert payload["usage"] == {"include": True}
    assert payload["temperature"] == 0
    assert payload["response_format"]["type"] == "json_schema"
    assert json.loads(payload["messages"][1]["content"]) == {
        "paragraph": "".join(THAI_LINES),
        "lines": THAI_LINES,
    }
    assert "exactly 4" in payload["messages"][0]["content"]
    assert translator.stats["lines_tried"] == translator.stats["lines_ok"] == 1
    assert translator.stats["cost"] == pytest.approx(0.001)


@pytest.mark.parametrize(
    "bad",
    [
        ["Hello viewers", "The weather is nice", "We go to the sea"],
        ["Hello viewers", "", "We go to the sea", "And eat"],
        ["สวัสดีครับคุณผู้ชม", "วันนี้อากาศดีมาก", "เราจะไปเที่ยวทะเล", "And eat"],
    ],
)
def test_primary_rejected_falls_back_to_spread(bad):
    paragraph = (
        "Hello viewers, the weather is very nice today, so we go to the sea and eat."
    )

    def responder(payload):
        return lines_reply(bad) if is_lines(payload) else reply(paragraph)

    translator, fake = make(responder)
    cues = cues_of(THAI_LINES)
    out, ok = translator.group(cues)
    assert ok
    assert len(fake.payloads) == 2
    # the paragraph is the Thai joined without spaces, and has no schema
    assert fake.payloads[1]["messages"][1]["content"] == "".join(THAI_LINES)
    assert "response_format" not in fake.payloads[1]
    assert " ".join(text for _, _, text in out) == paragraph
    assert out[0][0] == cues[0][0] and out[-1][1] == cues[-1][1]
    assert translator.stats["lines_ok"] == 0 and translator.stats["lines_tried"] == 1


def test_primary_shifted_by_a_line_is_rejected():
    # real drift: the right count, but the Bible line moved onto "อ่ะ"
    texts = ["ว่าเห็นอ่านหนังสือตลอดเลยถือพระคัมภีร์", "อ่ะ"]
    shifted = ["called for a chat.", "You always read books and hold the Bible."]

    def responder(payload):
        if is_lines(payload):
            return lines_reply(shifted)
        return reply("You always read books and hold the Bible. Ah.")

    translator, fake = make(responder)
    out, ok = translator.group(cues_of(texts))
    assert ok and len(fake.payloads) == 2
    assert translator.stats["lines_ok"] == 0
    assert ot.line_mismatch("อ่ะ", shifted[1], "th", "en")
    assert not ot.line_mismatch(texts[0], shifted[1], "th", "en")
    assert not ot.line_mismatch("อ่ะ", "x" * 99, "en", "th")


def test_single_cue_keeps_its_marker_and_strips_quotes():
    translator, _ = make(lambda p: reply('>> "Hello viewers, good evening"'))
    out, ok = translator.group(cues_of([">> สวัสดีครับคุณผู้ชม"]))
    assert ok and out[0][2] == ">> Hello viewers, good evening"
    translator, _ = make(lambda p: reply("Uh"))
    assert translator.group(cues_of([">> อื"]))[0][0][2] == ">> Uh"


def test_fallback_keeps_only_the_first_cue_marker():
    def responder(payload):
        if is_lines(payload):
            return lines_reply(["only one line"])
        return reply(">> Hello viewers >> the weather is nice >> we go to the sea")

    translator, _ = make(responder)
    out, ok = translator.group(cues_of([">> " + THAI_LINES[0]] + THAI_LINES[1:]))
    assert ok
    assert out[0][2].startswith(">> Hello")
    assert ">>" not in " ".join(text for _, _, text in out[1:])


def test_schema_rejected_is_dropped_and_remembered():
    def responder(payload):
        if "response_format" in payload:
            raise ot.HTTPStatusError(400, '{"error": "response_format not supported"}')
        return lines_reply(["Hello", "Weather", "Sea", "Eat"])

    translator, fake = make(responder)
    translator.group(cues_of(THAI_LINES))
    translator.group(cues_of(THAI_LINES))
    assert [("response_format" in p) for p in fake.payloads] == [True, False, False]


def test_reasoning_rejected_is_dropped_and_remembered():
    def responder(payload):
        if "reasoning" in payload:
            raise ot.HTTPStatusError(400, "Reasoning is mandatory for this endpoint")
        return reply("Hello viewers")

    translator, fake = make(responder)
    assert translator.group(cues_of(["สวัสดีครับคุณผู้ชม"]))[1]
    assert translator.group(cues_of(["สวัสดีครับคุณผู้ชม"]))[1]
    assert [("reasoning" in p) for p in fake.payloads] == [True, False, False]


# spreading


def test_spread_english_on_word_boundaries():
    parts = ot.spread("one two three four five six", [1, 1, 1], "en")
    assert parts == ["one two", "three four", "five six"]


def test_spread_thai_target_keeps_every_character():
    text = "สวัสดีครับ วันนี้อากาศดีมาก เราจะไปเที่ยวทะเลกัน"
    parts = ot.spread(text, [5, 10, 5, 10], "th")
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")
    assert all("\n" not in part for part in parts)
    assert sum(1 for part in parts if part) >= 3


def test_spread_cues_hands_empty_share_time_on():
    cues = [(0, 1000, "ก"), (1000, 5000, "ข" * 100), (5000, 6000, "ค")]
    assert ot.spread_cues(cues, "Hello", "en") == [(0, 6000, "Hello")]
    cues = [(0, 1000, "ก" * 100), (1000, 2000, "ข")]
    assert ot.spread_cues(cues, "Hello there", "en") == [(0, 2000, "Hello there")]


def test_en_to_th_fallback_preserves_thai_characters():
    thai = "สวัสดีครับ วันนี้อากาศดีมาก"
    translator, _ = make(lambda p: reply(thai), from_lang="en", to_lang="th")
    cues = cues_of(["Hello", "today the weather is very nice"])
    translator.use_schema = False
    out, ok = translator.group(cues)
    assert ok
    assert "".join(t for _, _, t in out).replace(" ", "") == thai.replace(" ", "")


# guards in the loop and the half retry


def test_untranslated_group_retried_in_halves():
    def responder(payload):
        source = payload["messages"][1]["content"]
        if is_lines(payload):
            lines = json.loads(source)["lines"]
            if len(lines) == 4:
                return lines_reply(lines)  # echoed Thai
            return lines_reply([f"half line {i}" for i in range(len(lines))])
        return reply(source)  # echoed Thai

    translator, fake = make(responder)
    out, ok = translator.group(cues_of(THAI_LINES))
    assert ok
    assert [t for _, _, t in out] == ["half line 0", "half line 1"] * 2
    assert len(fake.payloads) == 4  # lines, text, then one lines per half


def test_group_failing_after_halves_keeps_source():
    translator, _ = make(
        lambda p: lines_reply(json.loads(p["messages"][1]["content"])["lines"])
        if is_lines(p)
        else reply(" ".join(["loop"] * 200))
    )
    cues = cues_of(THAI_LINES)
    assert translator.group(cues) == (cues, False)


def test_length_cut_reply_is_rejected():
    translator, _ = make(lambda p: reply("Hello viewers", finish="length"))
    cues = cues_of(["สวัสดีครับคุณผู้ชม"])
    assert translator.group(cues) == (cues, False)


# transport


def test_transient_errors_retried_then_succeed(no_sleep):
    errors = [
        ot.HTTPStatusError(500, "boom"),
        ot.HTTPStatusError(429, "slow down"),
        urllib.error.URLError("reset"),
        TimeoutError(),
    ]

    def responder(payload):
        if errors:
            raise errors.pop(0)
        return reply("Hello viewers")

    translator, fake = make(responder)
    out, ok = translator.group(cues_of(["สวัสดีครับคุณผู้ชม"]))
    assert ok and out[0][2] == "Hello viewers"
    assert no_sleep == [2, 5, 15, 30]


def test_persistent_error_keeps_source_and_counts(no_sleep):
    def responder(payload):
        raise ot.HTTPStatusError(503, "down")

    translator, fake = make(responder)
    cues = cues_of(THAI_LINES + ["ไปกันเถอะ"])
    cues.append((60000, 61000, "อีกกลุ่มหนึ่ง"))
    out, stats = translator.run(cues), translator.stats
    assert out == cues
    assert stats["groups"] == 2 and stats["failed"] == 2
    # 5 attempts per group, no half retry after a transport failure
    assert len(fake.payloads) == 10
    assert "HTTP 503" in stats["errors"][0]


def test_breaker_stops_after_groups_fail_in_a_row(monkeypatch, no_sleep):
    monkeypatch.setattr(ot, "WORKERS", 1)  # deterministic order

    def responder(payload):
        raise urllib.error.URLError("Connection refused")

    translator, fake = make(responder)
    cues = cues_of(["สวัสดีครับคุณผู้ชม"] * 20, step=3000)  # 20 groups
    out = translator.run(cues)
    assert out == cues and translator.stats["failed"] == 20
    # BREAKER groups spend their retries, the rest send nothing
    assert len(fake.payloads) == ot.BREAKER * 5
    assert "giving up after 8 groups failed in a row" in translator.stats["aborted"]
    assert len(translator.stats["errors"]) == 3


def test_breaker_reset_by_any_answer(monkeypatch, no_sleep):
    monkeypatch.setattr(ot, "WORKERS", 1)
    texts = [f"กลุ่มที่ {i} ครับ" for i in range(15)]

    def responder(payload):
        if payload["messages"][1]["content"] == texts[7]:
            return reply("Group seven")
        raise ot.HTTPStatusError(400, "bad request")

    translator, fake = make(responder)
    translator.run(cues_of(texts, step=3000))
    assert translator.stats["aborted"] is None
    assert translator.stats["failed"] == 14 and translator.stats["translated"] == 1
    assert len(fake.payloads) == 15


@pytest.mark.parametrize("code", [400, 401, 503])
def test_translate_srt_raises_when_nothing_translated(monkeypatch, capsys, code):
    def post(self, payload):
        self.stats["requests"] += 1
        raise ot.HTTPStatusError(code, "no")

    monkeypatch.setattr(ot.Translator, "_post", post)
    monkeypatch.setenv("OPENROUTER_URL", "http://stub/v1")
    srt = "1\n00:00:01,000 --> 00:00:02,000\nสวัสดีครับ\n"
    with pytest.raises(ot.OpenRouterError, match="nothing was translated"):
        ot.translate_srt(srt, "th", "en", key="k")
    assert "1 failed" in capsys.readouterr().out


def test_client_error_is_not_retried(no_sleep):
    def responder(payload):
        raise ot.HTTPStatusError(401, "bad key")

    translator, fake = make(responder)
    cues = cues_of(["สวัสดีครับคุณผู้ชม"])
    assert translator.group(cues) == (cues, False)
    assert len(fake.payloads) == 1 and no_sleep == []


def test_response_without_choices_is_retried(monkeypatch, no_sleep):
    responses = [b'{"error": {"code": 502, "message": "upstream"}}', b"not json"]
    responses.append(json.dumps(reply("Hello viewers")).encode())

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self.body

    sent = []

    def urlopen(request, timeout):
        sent.append(request)
        return Response(responses.pop(0))

    monkeypatch.setattr(ot.urllib.request, "urlopen", urlopen)
    translator = ot.Translator(
        ot.DEFAULT_MODEL, "th", "en", "secret", "http://stub/v1/"
    )
    out, ok = translator.group(cues_of(["สวัสดีครับคุณผู้ชม"]))
    assert ok and out[0][2] == "Hello viewers"
    assert sent[0].full_url == "http://stub/v1/chat/completions"
    assert sent[0].get_header("Authorization") == "Bearer secret"
    assert translator.stats["requests"] == 3


# names and keys


@pytest.mark.parametrize(
    "model, name",
    [
        ("qwen/qwen3.8-flash", "th-or-qwen"),
        ("deepseek/deepseek-v4.1-flash", "th-or-deepseek"),
        ("z-ai/glm-5.3-flash", "th-or-z-ai"),
        ("plainmodel", "th-or-plainmodel"),
    ],
)
def test_subtitle_name(model, name):
    assert ot.subtitle_name(model) == name


def test_language_name():
    assert ot.language_name("th") == "Thai"
    assert ot.language_name("en") == "English"
    assert ot.language_name("fr") == "fr"


def test_key_from_env_first(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    monkeypatch.setattr(
        ot.subprocess, "run", lambda *a, **k: pytest.fail("keychain read")
    )
    assert ot.api_key() == "from-env"


def test_key_from_keychain(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "from-keychain\n", "")

    monkeypatch.setattr(ot.subprocess, "run", run)
    assert ot.api_key() == "from-keychain"
    assert calls == [
        ["security", "find-generic-password", "-s", "openrouter-transforms", "-w"]
    ]


@pytest.mark.parametrize("missing", ["status", "oserror"])
def test_key_missing_is_a_clear_error(monkeypatch, missing):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def run(args, **kwargs):
        if missing == "oserror":
            raise FileNotFoundError("security")
        return subprocess.CompletedProcess(args, 44, "", "not found")

    monkeypatch.setattr(ot.subprocess, "run", run)
    with pytest.raises(ot.OpenRouterError, match="OPENROUTER_API_KEY"):
        ot.api_key()


# whole file


def test_translate_srt(monkeypatch, capsys):
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\nสวัสดีครับ\nคุณผู้ชม\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nวันนี้อากาศดีมาก\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\n\n\n"
        "4\n00:00:09,000 --> 00:00:10,000\nแล้วพบกันใหม่\n"
    )
    seen = []

    def post(self, payload):
        self.stats["requests"] += 1
        source = payload["messages"][1]["content"]
        seen.append(source)
        if is_lines(payload):
            return lines_reply(
                [f"line {i}" for i in range(len(json.loads(source)["lines"]))]
            )
        return reply("See you again")

    monkeypatch.setattr(ot.Translator, "_post", post)
    monkeypatch.setenv("OPENROUTER_URL", "http://stub/v1")
    out = ot.translate_srt(srt, "th", "en", key="k")
    assert out == (
        "1\n00:00:01,000 --> 00:00:02,000\nline 0\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nline 1\n\n"
        "3\n00:00:09,000 --> 00:00:10,000\nSee you again\n"
    )
    assert json.loads(seen[0])["lines"] == ["สวัสดีครับคุณผู้ชม", "วันนี้อากาศดีมาก"]
    printed = capsys.readouterr().out
    assert (
        "th-or-qwen: 2 requests, $0.0020, 2 groups (1/1 line-aligned), 0 failed"
        in printed
    )
