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
    assert events[-1] == {"downloaded": 100, "size": 100, "speed": 10}
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
