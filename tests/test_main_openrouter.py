import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from utils import _ms, parse_srt

REPO = Path(__file__).resolve().parent.parent
# run main.py as is, only without the retry back-off
RUN_MAIN = (
    "import runpy, sys, openrouter_translate\n"
    "openrouter_translate.RETRY_DELAYS = (0, 0, 0, 0)\n"
    "sys.argv = ['main.py'] + sys.argv[1:]\n"
    "runpy.run_path('main.py', run_name='__main__')\n"
)

THAI_SRT = (
    "1\n00:00:01,000 --> 00:00:02,500\nสวัสดีครับคุณผู้ชม\n\n"
    "2\n00:00:02,500 --> 00:00:04,000\nวันนี้อากาศดีมาก\n\n"
    "3\n00:00:04,000 --> 00:00:05,000\nเราจะไปเที่ยวทะเล\n\n"
    "4\n00:00:08,000 --> 00:00:09,500\nแล้วพบกันใหม่ครับ\n"
)


class Stub:
    def __init__(self, fail=None):
        # fail: (status, body) answered to every request
        self.fail = fail
        self.payloads = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                stub.payloads.append(
                    (self.path, self.headers["Authorization"], payload)
                )
                if stub.fail:
                    status, body = stub.fail
                else:
                    body, status = json.dumps(stub.answer(payload)).encode(), 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    @staticmethod
    def answer(payload):
        source = payload["messages"][1]["content"]
        if '"lines"' in payload["messages"][0]["content"]:
            count = len(json.loads(source)["lines"])
            content = json.dumps({"lines": [f"english line {i}" for i in range(count)]})
        else:
            content = "english paragraph"
        return {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"cost": 0.0001},
        }


@pytest.fixture
def stub():
    server = Stub()
    yield server
    server.server.shutdown()


def run_main(folder, stub, *args, to_lang="en", from_lang="th", returncode=0):
    env = {
        **os.environ,
        "TRANSLATE_PATH": str(folder),
        "TRANSLATE_FROM_LANG": from_lang,
        "TRANSLATE_TO_LANG": to_lang,
        "OPENROUTER_URL": stub.url,
        "OPENROUTER_API_KEY": "dummy",
    }
    result = subprocess.run(
        [sys.executable, "-c", RUN_MAIN, *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == returncode, result.stderr
    return result.stdout + result.stderr


@pytest.fixture
def mp4(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"")
    (tmp_path / "clip.srt").write_text(THAI_SRT, encoding="utf-8")
    return tmp_path


def names(folder):
    return sorted(p.name for p in folder.iterdir())


def test_mp4_sidecar(mp4, stub):
    out = run_main(mp4, stub, "--openrouter")
    assert names(mp4) == ["clip.mp4", "clip.srt", "clip.th-or-qwen.srt"]
    assert (mp4 / "clip.srt").read_bytes() == THAI_SRT.encode()
    cues = parse_srt((mp4 / "clip.th-or-qwen.srt").read_text(encoding="utf-8"))
    assert [body for _, _, body in cues] == [
        ["english line 0"],
        ["english line 1"],
        ["english line 2"],
        ["english paragraph"],
    ]
    assert cues[0][:2] == ("00:00:01,000", "00:00:02,500")
    assert (
        "th-or-qwen: 2 requests, $0.0002, 2 groups (1/1 line-aligned), 0 failed" in out
    )
    path, auth, payload = stub.payloads[0]
    assert path == "/v1/chat/completions" and auth == "Bearer dummy"
    assert payload["model"] == "qwen/qwen3.8-flash"
    assert "into natural, accurate English" in payload["messages"][0]["content"]


def test_mp4_dual(mp4, stub):
    run_main(mp4, stub, "--openrouter", "--dual")
    assert names(mp4) == ["clip.mp4", "clip.srt", "clip.th-or-qwen.srt"]
    assert (mp4 / "clip.srt").read_bytes() == THAI_SRT.encode()
    cues = parse_srt((mp4 / "clip.th-or-qwen.srt").read_text(encoding="utf-8"))
    assert cues[0][2] == ["english line 0", "สวัสดีครับคุณผู้ชม"]
    assert cues[-1][2] == ["english paragraph", "แล้วพบกันใหม่ครับ"]
    for previous, cue in zip(cues, cues[1:]):
        assert _ms(cue[0]) >= _ms(previous[1])


def test_model_argument_names_the_subtitle(mp4, stub):
    run_main(mp4, stub, "--openrouter", "deepseek/deepseek-v4.1-flash")
    assert names(mp4) == ["clip.mp4", "clip.srt", "clip.th-or-deepseek.srt"]
    assert {payload["model"] for _, _, payload in stub.payloads} == {
        "deepseek/deepseek-v4.1-flash"
    }


@pytest.mark.parametrize(
    "fail, requests",
    [
        ((500, b'{"error": "down"}'), 10),  # retried
        ((400, b'{"error": {"message": "qwen/typo is not a valid model ID"}}'), 2),
        ((401, b'{"error": {"message": "No auth credentials found"}}'), 2),
    ],
)
def test_nothing_translated_writes_nothing(mp4, fail, requests):
    stub = Stub(fail=fail)
    try:
        out = run_main(mp4, stub, "--openrouter", "qwen/typo", returncode=1)
        assert "2 failed" in out and f"HTTP {fail[0]}" in out
        assert "not translated: nothing was translated" in out
        assert len(stub.payloads) == requests
        assert names(mp4) == ["clip.mp4", "clip.srt"]
        assert (mp4 / "clip.srt").read_bytes() == THAI_SRT.encode()
        # the corrected rerun is not blocked by a source-only sidecar
        stub.fail = None
        run_main(mp4, stub, "--openrouter")
        assert names(mp4) == ["clip.mp4", "clip.srt", "clip.th-or-qwen.srt"]
    finally:
        stub.server.shutdown()


@pytest.mark.parametrize("arg", ["--openrouter=", "--openrouter= "])
def test_empty_model_is_rejected(mp4, stub, arg):
    out = run_main(mp4, stub, arg, returncode=2)
    assert "--openrouter MODEL must not be empty" in out
    assert names(mp4) == ["clip.mp4", "clip.srt"] and not stub.payloads


def test_rerun_skips(mp4, stub):
    run_main(mp4, stub, "--openrouter")
    sent = len(stub.payloads)
    out = run_main(mp4, stub, "--openrouter")
    assert "Already translated" in out
    assert len(stub.payloads) == sent
    assert names(mp4) == ["clip.mp4", "clip.srt", "clip.th-or-qwen.srt"]


def test_stale_part_and_orig_recovered(mp4, stub):
    # an interrupted run: cleaned text in clip.srt, the source in .orig
    (mp4 / "clip.srt.orig").write_text(THAI_SRT, encoding="utf-8")
    (mp4 / "clip.srt").write_text("garbage", encoding="utf-8")
    (mp4 / "clip.th-or-qwen.srt.part").write_text("half", encoding="utf-8")
    run_main(mp4, stub, "--openrouter")
    assert names(mp4) == ["clip.mp4", "clip.srt", "clip.th-or-qwen.srt"]
    assert (mp4 / "clip.srt").read_bytes() == THAI_SRT.encode()


def make_mkv(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "th.srt").write_text(THAI_SRT, encoding="utf-8")
    folder = tmp_path / "videos"
    folder.mkdir()
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=10",
            "-i", str(source / "th.srt"), "-map", "0", "-map", "1",
            "-c:v", "mpeg4", "-c:s", "srt", "-metadata:s:s:0", "language=tha",
            str(folder / "video.mkv"),
        ],
        check=True,
    )  # fmt: skip
    return folder


needs_mkv = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("mkvmerge")),
    reason="needs ffmpeg and mkvtoolnix",
)


@needs_mkv
def test_mkv_failure_leaves_only_the_video(tmp_path):
    folder = make_mkv(tmp_path)
    stub = Stub(fail=(401, b'{"error": {"message": "No auth credentials found"}}'))
    try:
        run_main(folder, stub, "--openrouter", returncode=1)
    finally:
        stub.server.shutdown()
    # no extracted video.srt left to be mistaken for a sidecar next run
    assert names(folder) == ["video.mkv"]


@needs_mkv
def test_mkv_track_named(tmp_path, stub):
    folder = make_mkv(tmp_path)
    run_main(folder, stub, "--openrouter")
    assert names(folder) == ["video.mkv", "video_en.mkv"]
    info = json.loads(
        subprocess.run(
            ["mkvmerge", "-J", str(folder / "video_en.mkv")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    subtitles = [t for t in info["tracks"] if t["type"] == "subtitles"]
    assert len(subtitles) == 1
    assert subtitles[0]["properties"]["track_name"] == "th-or-qwen"
    assert subtitles[0]["properties"]["language"] == "eng"
