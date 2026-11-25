import json
import os
import threading
import time
from pathlib import Path

import pytest

from agent import mdm

FAKE = str(Path(__file__).parent / "fakes" / "fake_mdm.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MDM_CLI", FAKE)
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "dl"))
    monkeypatch.delenv("FAKE_MDM_MODE", raising=False)
    monkeypatch.setenv("FAKE_MDM_ARGS", str(tmp_path / "args.json"))
    return tmp_path


def test_done_and_progress(env):
    events = []
    out = mdm.run("https://example.com/f.bin", str(env / "dl" / "j1"), on_event=events.append)
    assert out["size"] == 100 and Path(out["path"]).read_bytes() == b"x" * 100
    assert events[-1] == {"downloaded": 100, "size": 100, "speed": 10, "connections": 4}
    assert len(events) == 3
    assert json.loads((env / "args.json").read_text())[:3] == ["--json", "get", "https://example.com/f.bin"]


def test_torrent_args(env):
    mdm.run("magnet:?xt=urn:btih:abc", str(env / "dl" / "j"), files=[0, 2])
    a = json.loads((env / "args.json").read_text())
    assert a[:2] == ["--json", "torrent-get"] and a[-2:] == ["--files", "0,2"]
    # the CLI's torrent-get takes only a magnet or a local path: an http(s) .torrent is a plain `get`
    mdm.run("https://x.org/a.torrent", str(env / "dl" / "j"), files=[1])
    assert json.loads((env / "args.json").read_text())[:3] == ["--json", "get", "https://x.org/a.torrent"]


def test_error_line(env, monkeypatch):
    monkeypatch.setenv("FAKE_MDM_MODE", "error")
    with pytest.raises(mdm.MdmError, match="connection reset"):
        mdm.run("https://example.com/f", str(env / "dl" / "j"))


def test_stderr_nonzero(env, monkeypatch):
    monkeypatch.setenv("FAKE_MDM_MODE", "stderr")
    with pytest.raises(mdm.MdmError, match="fatal: boom"):
        mdm.run("https://example.com/f", str(env / "dl" / "j"))


def test_exit_zero_without_done(env, monkeypatch):
    monkeypatch.setenv("FAKE_MDM_MODE", "silent")
    with pytest.raises(mdm.MdmError):
        mdm.run("https://example.com/f", str(env / "dl" / "j"))


def test_cancel(env, monkeypatch):
    monkeypatch.setenv("FAKE_MDM_MODE", "slow")
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    t0 = time.time()
    with pytest.raises(mdm.MdmError, match="cancelled"):
        mdm.run("https://example.com/f", str(env / "dl" / "j"), cancel=ev)
    assert time.time() - t0 < 10


@pytest.mark.parametrize("bad", ["../escape", "/etc", "sub/../../x"])
def test_dest_confined(env, bad):
    dest = bad if bad.startswith("/") else str(env / "dl" / bad)
    with pytest.raises(mdm.MdmError):
        mdm.run("https://example.com/f", dest)


def test_dest_symlink_escape(env):
    (env / "dl").mkdir()
    (env / "dl" / "link").symlink_to(env)
    with pytest.raises(mdm.MdmError):
        mdm.run("https://example.com/f", str(env / "dl" / "link"))


@pytest.mark.parametrize("src", ["file:///etc/passwd", "javascript:alert(1)", "--help", "", "/etc/passwd",
                                 "https://x/a\nb",
                                 # parser differentials: urllib sees evil.invalid, a WHATWG parser sees 127.0.0.1
                                 "http://127.0.0.1:8799\\@evil.invalid/x", "http://127.0.0.1\\.evil.invalid/",
                                 "http://ex ample.com/", "http://example.com/a\tb", "http://example.com/a\x7f",
                                 "http://example.com/a\x01", "http://example.com/\u00a0", " https://example.com/",
                                 "http://%31%32%37.0.0.1/", "http://b\u00fccher.de/", "http://[::1/x"])
def test_bad_source(env, src):
    with pytest.raises(mdm.MdmError):
        mdm.run(src, str(env / "dl" / "j"))
    with pytest.raises(mdm.MdmError):
        mdm.torrent_info(src)
    assert not (env / "args.json").exists()  # the CLI was never spawned


def test_bad_files(env):
    with pytest.raises(mdm.MdmError):
        mdm.run("magnet:?xt=urn:btih:abc", str(env / "dl" / "j"), files=["1; rm"])


def test_torrent_info(env):
    info = mdm.torrent_info("magnet:?xt=urn:btih:abc")
    assert info["name"] == "Demo" and [f["index"] for f in info["files"]] == [0, 1] and info["size"] == 30


def test_torrent_info_error(env, monkeypatch):
    monkeypatch.setenv("FAKE_MDM_MODE", "error")
    with pytest.raises(mdm.MdmError, match="bad torrent"):
        mdm.torrent_info("magnet:?xt=urn:btih:abc")


def test_missing_cli(env, monkeypatch):
    monkeypatch.setenv("MDM_CLI", str(env / "nope"))
    with pytest.raises(mdm.MdmError):
        mdm.run("https://example.com/f", str(env / "dl" / "j"))
    with pytest.raises(mdm.MdmError):
        mdm.torrent_info("magnet:?xt=urn:btih:abc")


def test_get_uses_configured_connections(monkeypatch):
    seen = {}

    class P:
        def __init__(self, args, **kw):
            seen["args"] = args
            raise OSError("stop here")
    monkeypatch.setattr(mdm.subprocess, "Popen", P)
    for env, want in ((None, "16"), ("32", "32"), ("999", "128"), ("x", "16")):
        if env is None:
            monkeypatch.delenv("MDM_CONNECTIONS", raising=False)
        else:
            monkeypatch.setenv("MDM_CONNECTIONS", env)
        with pytest.raises(mdm.MdmError):
            mdm.run("https://example.com/a.iso", "j1")
        assert seen["args"][-2:] == ["-c", want]


# ---- fuller MDM: checksum, dns, video mode, resume, video-info ----

def argv(env):
    return json.loads((env / "args.json").read_text())


def test_checksum_and_dns_go_to_get(env, monkeypatch):
    mdm.run("https://example.com/f.bin", str(env / "dl" / "j"), checksum="sha256:" + "ab" * 32)
    a = argv(env)
    assert a[a.index("--checksum") + 1] == "sha256:" + "ab" * 32 and "--dns" not in a
    monkeypatch.setenv("MDM_DNS", "cloudflare-https")
    mdm.run("https://example.com/f.bin", str(env / "dl" / "j"))
    a = argv(env)
    assert a[a.index("--dns") + 1] == "cloudflare-https" and "--checksum" not in a
    monkeypatch.setenv("MDM_DNS", " ")  # blank: not passed
    mdm.run("https://example.com/f.bin", str(env / "dl" / "j"))
    assert "--dns" not in argv(env)


def test_checksum_mismatch_is_a_job_error(env, monkeypatch):
    monkeypatch.setenv("FAKE_MDM_MODE", "error")  # the CLI reports a mismatch as an error line
    with pytest.raises(mdm.MdmError, match="connection reset"):
        mdm.run("https://example.com/f.bin", str(env / "dl" / "j"), checksum="ab" * 16)


@pytest.mark.parametrize("kw", [{"checksum": "not hex"}, {"checksum": "sha256:zz"}, {"checksum": "--dns"}, {"mode": "audio"},
                                {"height": 0}, {"height": "720"}, {"height": 99999}, {"height": True},
                                {"subs": "-h"}, {"subs": "en fr"}, {"subs": "en;ls"}, {"subs": ""}])
def test_bad_options_never_spawn(env, kw):
    with pytest.raises(mdm.MdmError):
        mdm.run("https://example.com/f", str(env / "dl" / "j"), **kw)
    assert not (env / "args.json").exists()


def test_video_mode(env, monkeypatch):
    monkeypatch.setenv("MDM_DNS", "quad9-tls")
    out = mdm.run("https://video.example/watch?v=1", str(env / "dl" / "j"), mode="video", height=720, subs="en,fr")
    a = argv(env)
    assert a[:3] == ["--json", "video-get", "https://video.example/watch?v=1"]
    assert a[a.index("--quality") + 1] == "720" and a[a.index("--subs") + 1] == "en,fr"
    assert a[a.index("-o") + 1] == str(env / "dl" / "j") and "-c" in a and "--dns" not in a  # video-get has no --dns
    assert out["path"].endswith("video.mp4") and Path(out["path"]).is_file()
    mdm.run("https://video.example/watch?v=1", str(env / "dl" / "j"), mode="video")
    a = argv(env)
    assert "--quality" not in a and "--subs" not in a
    with pytest.raises(mdm.MdmError):
        mdm.run("magnet:?xt=urn:btih:abc", str(env / "dl" / "j"), mode="video")


def test_resume(env):
    dest = env / "dl" / "j"
    dest.mkdir(parents=True)
    (dest / "f.bin").write_bytes(b"partial")
    state = dest / "f.bin.mdm.json"
    state.write_text("{}")
    events = []
    out = mdm.run("https://example.com/f.bin", str(dest), on_event=events.append, resume=str(state))
    a = argv(env)
    assert a[:3] == ["--json", "resume", str(state.resolve())] and a[-2:] == ["-c", "16"]
    assert "-o" not in a and "--checksum" not in a
    assert out["size"] == 100 and Path(out["path"]).read_bytes() == b"r" * 100 and events


@pytest.mark.parametrize("where", ["outside", "wrong_name", "missing", "sibling_job"])
def test_resume_state_file_is_confined(env, where):
    dest = env / "dl" / "j"
    dest.mkdir(parents=True)
    (env / "dl" / "other").mkdir()
    state = {"outside": env / "x.mdm.json", "wrong_name": dest / "f.json", "missing": dest / "nope.mdm.json",
             "sibling_job": env / "dl" / "other" / "f.mdm.json"}[where]
    if where != "missing":
        state.write_text("{}")
    with pytest.raises(mdm.MdmError):
        mdm.run("https://example.com/f.bin", str(dest), resume=str(state))
    assert not (env / "args.json").exists()


def test_video_info(env):
    info = mdm.video_info("https://video.example/watch?v=1")
    assert info == {"title": "Cat video", "heights": [1080, 720],
                    "subtitles": [{"code": "en", "name": "English", "automatic": True}]}
    assert argv(env) == ["--json", "video-info", "https://video.example/watch?v=1"]


def test_video_info_cases(env, monkeypatch):
    monkeypatch.setenv("FAKE_MDM_VIDEO", "list")
    assert mdm.video_info("https://video.example/pl") is None
    monkeypatch.setenv("FAKE_MDM_VIDEO", "missing")
    with pytest.raises(mdm.MdmError, match="yt-dlp is not installed"):
        mdm.video_info("https://video.example/v")
    monkeypatch.setenv("FAKE_MDM_VIDEO", "novideo")
    with pytest.raises(mdm.MdmError, match="Unsupported URL"):
        mdm.video_info("https://example.com/")
    monkeypatch.setenv("MDM_CLI", str(env / "nope"))
    with pytest.raises(mdm.MdmError):
        mdm.video_info("https://example.com/")
    with pytest.raises(mdm.MdmError):
        mdm.video_info("file:///etc/passwd")
