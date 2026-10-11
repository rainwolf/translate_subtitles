"""Translate SRT subtitles with an OpenRouter chat model.

Standard library only (pythainlp, already a dependency, splits Thai output).
Consecutive cues are grouped into paragraphs; each group is sent as a JSON
array of lines and must come back line for line, otherwise the paragraph is
translated as plain text and spread over the group's cues by source length.
A group that still fails keeps its source text, so one failure never stops
the run.
"""

import http.client
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from utils import THAI, _ms, _timestamp, emit_srt, parse_srt

DEFAULT_MODEL = "qwen/qwen3.8-flash"
DEFAULT_URL = "https://openrouter.ai/api/v1"
KEYCHAIN_SERVICE = "openrouter-transforms"
LANGUAGES = {"en": "English", "th": "Thai"}
RETRY_DELAYS = (2, 5, 15, 30)
WORKERS = 8
TIMEOUT = 120

MAX_GAP_MS = 1000
MAX_CHARS = 400
MAX_CUES = 12
# a speaker change (>> or -) only starts a group that already has some context
MARKER_MIN_CHARS = 150
MARKER_MIN_CUES = 4

QUOTES = '"“”'
TRANSIENT = (
    urllib.error.URLError,
    TimeoutError,
    ConnectionError,
    http.client.HTTPException,
    ValueError,
)

SYSTEM = "You translate {source} video subtitles into natural, accurate {target}. "
LINES_PROMPT = (
    "The user sends a JSON array of {count} consecutive subtitle lines. Reply with "
    'only a JSON object {{"lines": [...]}} holding exactly {count} {target} strings, '
    "one translation per input line in the same order. You may move words between "
    "neighbouring lines so each one reads naturally, but never merge, split, drop "
    "or add lines. Keep a leading >> (speaker change) on the lines that have one."
)
TEXT_PROMPT = (
    "Translate the user's text. Output only the {target} translation, nothing else."
)
LINES_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "subtitle_lines",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
            "required": ["lines"],
            "additionalProperties": False,
        },
    },
}


class OpenRouterError(Exception):
    pass


class HTTPStatusError(OpenRouterError):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body[:200]}")
        self.code, self.body = code, body


def subtitle_name(model):
    """th-or-<vendor>: qwen/qwen3.8-flash -> th-or-qwen."""
    vendor = re.sub(r"[^A-Za-z0-9._-]+", "-", model.split("/")[0]).strip("-")
    return f"th-or-{vendor or 'model'}"


def language_name(code):
    return LANGUAGES.get(code, code)


def api_key():
    """OPENROUTER_API_KEY, else the macOS Keychain item openrouter-transforms."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
        )
    except OSError:
        out = None
    if out is not None and out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    raise OpenRouterError(
        "no OpenRouter API key: set OPENROUTER_API_KEY or add it to the Keychain "
        f"(security add-generic-password -s {KEYCHAIN_SERVICE} -a openrouter -w)"
    )


def group_cues(cues):
    """Split (start_ms, end_ms, text) cues into lists of consecutive indexes."""
    groups, current, chars = [], [], 0
    for i, (start, _, text) in enumerate(cues):
        if current:
            marker = text.lstrip().startswith((">>", "-"))
            if (
                start - cues[current[-1]][1] > MAX_GAP_MS
                or chars + len(text) > MAX_CHARS
                or len(current) >= MAX_CUES
                or (
                    marker
                    and (chars >= MARKER_MIN_CHARS or len(current) >= MARKER_MIN_CUES)
                )
            ):
                groups.append(current)
                current, chars = [], 0
        current.append(i)
        chars += len(text)
    if current:
        groups.append(current)
    return groups


def _strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def parse_lines(content):
    """The list of strings in a reply, tolerating code fences and chatter."""
    text = _strip_think(content)
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    candidates = [text]
    if "[" in text and "]" in text:
        candidates.append(text[text.find("[") : text.rfind("]") + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            data = data.get("lines")
        if isinstance(data, list) and all(isinstance(line, str) for line in data):
            return data
    return None


def clean_output(text, source, keep_markers):
    """Drop quotes and >> markers the model added, flatten to one line."""
    if not any(q in source for q in QUOTES):
        text = text.translate({ord(q): None for q in QUOTES})
    if not keep_markers or ">>" not in source:
        text = text.replace(">>", " ")
    elif source.lstrip().startswith(">>") and not text.lstrip().startswith(">>"):
        text = f">> {text}"
    return " ".join(text.split())


def thai_share(text):
    letters = re.findall(rf"[{THAI}]|[^\W\d_]", text)
    return len(re.findall(f"[{THAI}]", text)) / len(letters) if letters else 0.0


def _words(text, lang):
    if lang == "th":
        return len(re.sub(r"\s", "", text)) / 4
    return len(text.split())


def _word_limit(source, lang):
    if lang == "th":
        return len(re.sub(r"\s", "", source)) / 2 + 20
    return 3 * len(source.split()) + 20


def untranslated(source, output, from_lang, to_lang, finish_reason=None):
    """True when the output is empty, cut off, still in the source language or loops."""
    if not output.strip() or finish_reason == "length":
        return True
    share = thai_share(output)
    if from_lang == "th" and share > 0.3:
        return True
    if to_lang == "th" and share < 0.3:
        return True
    return _words(output, to_lang) > _word_limit(source, from_lang)


def spread(text, weights, to_lang):
    """Split text over len(weights) parts proportionally, on word boundaries."""
    if to_lang == "th":
        from pythainlp.tokenize import word_tokenize

        tokens, sep = word_tokenize(text, engine="newmm", keep_whitespace=True), ""
    else:
        tokens, sep = text.split(), " "
    total_weight = sum(weights) or 1
    total = sum(len(token) for token in tokens)
    bounds, cumulative = [], 0
    for weight in weights:
        cumulative += weight
        bounds.append(total * cumulative / total_weight)
    parts = [[] for _ in weights]
    position, part = 0, 0
    for token in tokens:
        middle = position + len(token) / 2
        while part < len(weights) - 1 and middle > bounds[part]:
            part += 1
        parts[part].append(token)
        position += len(token)
    return [" ".join(sep.join(tokens).split()) for tokens in parts]


def spread_cues(cues, text, to_lang):
    """Spread a translation over the cues; an empty share hands its time on."""
    shares = spread(text, [len(cue[2]) for cue in cues], to_lang)
    out, pending = [], None
    for (start, end, _), share in zip(cues, shares):
        if not share:
            if out:
                out[-1] = (out[-1][0], max(out[-1][1], end), out[-1][2])
            elif pending is None:
                pending = start
            continue
        if pending is not None:
            start, pending = pending, None
        out.append((start, end, share))
    return out


def _max_tokens(source):
    return min(4096, 4 * len(source) + 128)


class Translator:
    def __init__(self, model, from_lang, to_lang, key, base_url):
        self.model, self.from_lang, self.to_lang = model, from_lang, to_lang
        self.key, self.url = key, f"{base_url.rstrip('/')}/chat/completions"
        self.source_sep = "" if from_lang == "th" else " "
        self.target_sep = "" if to_lang == "th" else " "
        self.system = SYSTEM.format(
            source=language_name(from_lang), target=language_name(to_lang)
        )
        self.lock = threading.Lock()
        self.disable_reasoning = True
        self.use_schema = True
        self.stats = {
            "requests": 0,
            "cost": 0.0,
            "groups": 0,
            "lines_tried": 0,
            "lines_ok": 0,
            "failed": 0,
            "errors": [],
        }

    def _post(self, payload):
        with self.lock:
            self.stats["requests"] += 1
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as e:
            raise HTTPStatusError(e.code, e.read().decode("utf-8", "replace")) from None
        if not data.get("choices"):
            error = data.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            raise HTTPStatusError(
                code if isinstance(code, int) else 502, json.dumps(error)[:200]
            )
        return data

    def _post_with_retry(self, payload):
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                return self._post(payload)
            except HTTPStatusError as e:
                if (e.code != 429 and e.code < 500) or attempt == len(RETRY_DELAYS):
                    raise
            except TRANSIENT:
                if attempt == len(RETRY_DELAYS):
                    raise
            time.sleep(RETRY_DELAYS[attempt])

    def complete(self, instruction, user, max_tokens, schema=False):
        """(content, finish_reason) of one chat completion."""
        while True:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system + instruction},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "max_tokens": max_tokens,
                "usage": {"include": True},
            }
            reasoning, structured = self.disable_reasoning, schema and self.use_schema
            if reasoning:
                payload["reasoning"] = {"enabled": False}
            if structured:
                payload["response_format"] = LINES_SCHEMA
            try:
                data = self._post_with_retry(payload)
                break
            except HTTPStatusError as e:
                body = e.body.lower()
                if e.code == 400 and reasoning and "reasoning" in body:
                    self.disable_reasoning = False
                elif (
                    e.code == 400
                    and structured
                    and re.search("response_format|json_schema|structured", body)
                ):
                    self.use_schema = False
                else:
                    raise
        cost = (data.get("usage") or {}).get("cost") or 0.0
        with self.lock:
            self.stats["cost"] += cost
        choice = data["choices"][0]
        return (choice.get("message") or {}).get("content") or "", choice.get(
            "finish_reason"
        )

    def _lines(self, texts):
        user = json.dumps(texts, ensure_ascii=False)
        instruction = LINES_PROMPT.format(
            count=len(texts), target=language_name(self.to_lang)
        )
        content, finish = self.complete(
            instruction, user, _max_tokens(user), schema=True
        )
        lines = parse_lines(content)
        if finish == "length" or lines is None or len(lines) != len(texts):
            return None
        lines = [
            clean_output(line, text, keep_markers=True)
            for line, text in zip(lines, texts)
        ]
        if not all(lines):
            return None
        joined_source = self.source_sep.join(texts)
        if untranslated(
            joined_source, self.target_sep.join(lines), self.from_lang, self.to_lang
        ):
            return None
        return lines

    def _text(self, texts):
        source = self.source_sep.join(texts)
        instruction = TEXT_PROMPT.format(target=language_name(self.to_lang))
        content, finish = self.complete(instruction, source, _max_tokens(source))
        text = clean_output(_strip_think(content), source, keep_markers=False)
        if untranslated(source, text, self.from_lang, self.to_lang, finish):
            return None
        return text

    def attempt(self, cues, count=False):
        """Translated cues, or None when the output fails the guards."""
        texts = [cue[2] for cue in cues]
        if len(cues) > 1:
            lines = self._lines(texts)
            if count:
                with self.lock:
                    self.stats["lines_tried"] += 1
                    self.stats["lines_ok"] += lines is not None
            if lines is not None:
                return [
                    (start, end, line) for (start, end, _), line in zip(cues, lines)
                ]
        text = self._text(texts)
        return None if text is None else spread_cues(cues, text, self.to_lang)

    def _error(self, e):
        with self.lock:
            if len(self.stats["errors"]) < 3:
                self.stats["errors"].append(f"{type(e).__name__}: {e}"[:200])

    def group(self, cues):
        """(translated cues, ok); a part that fails keeps its source cues."""
        try:
            out = self.attempt(cues, count=True)
        except Exception as e:
            # transport failed after retries; splitting would only repeat that
            self._error(e)
            return cues, False
        if out is not None:
            return out, True
        if len(cues) == 1:
            return cues, False
        mid = len(cues) // 2
        result, ok = [], True
        for half in (cues[:mid], cues[mid:]):
            try:
                part = self.attempt(half)
            except Exception as e:
                self._error(e)
                part = None
            if part is None:
                result += half
                ok = False
            else:
                result += part
        return result, ok

    def run(self, cues):
        groups = [[cues[i] for i in indexes] for indexes in group_cues(cues)]
        self.stats["groups"] = len(groups)
        out = []
        with ThreadPoolExecutor(WORKERS) as pool:
            for translated, ok in pool.map(self.group, groups):
                out += translated
                self.stats["failed"] += not ok
        return out


def translate_cues(cues, from_lang, to_lang, model=DEFAULT_MODEL, key=None):
    """Translate (start_ms, end_ms, text) cues; returns (cues, stats)."""
    translator = Translator(
        model,
        from_lang,
        to_lang,
        key or api_key(),
        os.environ.get("OPENROUTER_URL") or DEFAULT_URL,
    )
    return translator.run(cues), translator.stats


def report(name, stats):
    tried = stats["lines_tried"]
    aligned = (
        f"{stats['lines_ok']}/{tried} line-aligned" if tried else "no multi-line groups"
    )
    print(
        f"  {name}: {stats['requests']} requests, ${stats['cost']:.4f}, "
        f"{stats['groups']} groups ({aligned}), {stats['failed']} failed"
    )
    if stats["failed"]:
        print(
            f"  {stats['failed']} groups kept the source text: "
            + "; ".join(stats["errors"] or ["untranslated output"])
        )


def translate_srt(srt_text, from_lang, to_lang, model=DEFAULT_MODEL, key=None):
    """Translate SRT text, print one usage line, return the translated SRT."""
    sep = "" if from_lang == "th" else " "
    cues = [
        (_ms(start), _ms(end), sep.join(body).strip())
        for start, end, body in parse_srt(srt_text.lstrip("﻿"))
    ]
    cues = [cue for cue in cues if cue[2]]
    translated, stats = translate_cues(cues, from_lang, to_lang, model, key)
    report(subtitle_name(model), stats)
    return emit_srt(
        [
            (_timestamp(start), _timestamp(end), [text])
            for start, end, text in translated
        ]
    )
