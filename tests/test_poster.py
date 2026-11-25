import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from agent import inspect as I
from agent import library as L
from agent import poster as P

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")
FAKE_JPEG = b"\xff\xd8\xff\xe0 pretend jpeg"


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


def size_of(jpg):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name",
                        "-of", "csv=p=0", str(jpg)], capture_output=True, text=True, check=True)
    codec, w, h = r.stdout.strip().split(",")
    return codec, int(w), int(h)


def item_for(tmp_path, files, sub="j_1"):
    """A folder holding `files` ({name: bytes | callable(path)}) and its library Item."""
    folder = tmp_path / sub
    folder.mkdir()
    for name, data in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        data(folder / name) if callable(data) else (folder / name).write_bytes(data)
    return L.build_item("j_1", folder, "T", "u"), folder


@pytest.fixture
def net(monkeypatch):
    """Routes for poster image fetches: routes[(host, path)] -> httpx.Response factory."""
    routes, seen = {}, []

    def handler(req):
        seen.append((req.method, req.headers["host"], req.url.host))
        return routes.get((req.headers["host"], req.url.path), lambda: httpx.Response(404))()
    monkeypatch.setattr(I, "_TRANSPORT", httpx.MockTransport(handler))
    dns = {"img.example": ["93.184.216.34"], "evil.test": ["10.0.0.5"], "other.example": ["1.1.1.1"]}
    monkeypatch.setattr(I, "_resolve", lambda h, p: dns[h])
    routes["seen"] = seen
    return routes


# ---- from a local image ----

@needs_ffmpeg
def test_poster_from_local_image_is_scaled_jpeg(tmp_path):
    item, folder = item_for(tmp_path, {"pic.png": lambda p: ffmpeg("-f", "lavfi", "-i", "testsrc=size=1200x800", "-frames:v", "1", str(p))})
    assert item["kind"] == "image"
    dest = tmp_path / "posters" / "i_x.jpg"
    assert P.make_poster(item, folder, dest) is True
    assert size_of(dest) == ("mjpeg", 600, 400)


@needs_ffmpeg
def test_small_image_is_not_upscaled(tmp_path):
    item, folder = item_for(tmp_path, {"pic.png": lambda p: ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240", "-frames:v", "1", str(p))})
    dest = tmp_path / "p.jpg"
    assert P.make_poster(item, folder, dest)
    assert size_of(dest) == ("mjpeg", 320, 240)


def test_without_ffmpeg_a_jpeg_is_kept_and_others_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(P.shutil, "which", lambda name: None)
    item, folder = item_for(tmp_path, {"pic.jpg": FAKE_JPEG})
    dest = tmp_path / "p.jpg"
    assert P.make_poster(item, folder, dest) is True and dest.read_bytes() == FAKE_JPEG
    item, folder = item_for(tmp_path, {"pic.png": b"\x89PNG not a jpeg"}, sub="b")
    assert P.make_poster(item, folder, tmp_path / "q.jpg") is False and not (tmp_path / "q.jpg").exists()


# ---- from a video ----

@needs_ffmpeg
def test_poster_from_video_frame(tmp_path):
    item, folder = item_for(tmp_path, {"clip.mp4": lambda p: ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10", "-pix_fmt", "yuv420p", str(p))})
    assert item["kind"] == "video"
    dest = tmp_path / "posters" / "i_v.jpg"
    assert P.make_poster(item, folder, dest) is True
    assert size_of(dest) == ("mjpeg", 320, 240) and dest.stat().st_size > 500


@needs_ffmpeg
def test_wide_video_is_scaled_to_600(tmp_path):
    item, folder = item_for(tmp_path, {"clip.mp4": lambda p: ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=1:size=1280x720:rate=5", "-pix_fmt", "yuv420p", str(p))})
    dest = tmp_path / "p.jpg"
    assert P.make_poster(item, folder, dest)
    assert size_of(dest) == ("mjpeg", 600, 338)


@needs_ffmpeg
def test_video_frame_is_taken_at_about_ten_percent(tmp_path, monkeypatch):
    calls = []
    real = P.subprocess.run
    monkeypatch.setattr(P.subprocess, "run", lambda args, **kw: calls.append(list(args)) or real(args, **kw))
    item, folder = item_for(tmp_path, {"clip.mp4": lambda p: ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=20:size=160x120:rate=2", "-pix_fmt", "yuv420p", str(p))})
    assert P.make_poster(item, folder, tmp_path / "p.jpg")
    ss = next(c for c in calls if c[0].endswith("ffmpeg") and "-ss" in c)
    assert abs(float(ss[ss.index("-ss") + 1]) - 2.0) < 0.3
    assert all(isinstance(a, str) for c in calls for a in c)  # argv lists, never a shell


def test_video_without_ffmpeg_has_no_poster(tmp_path, monkeypatch):
    monkeypatch.setattr(P.shutil, "which", lambda name: None)
    item, folder = item_for(tmp_path, {"clip.mp4": b"not really a video"})
    assert P.make_poster(item, folder, tmp_path / "p.jpg") is False


def test_broken_video_gives_false_not_an_exception(tmp_path):
    item, folder = item_for(tmp_path, {"clip.mp4": b"not really a video"})
    assert P.make_poster(item, folder, tmp_path / "p.jpg") is False


# ---- thumbnail written next to the file ----

def test_cover_next_to_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(P.shutil, "which", lambda name: None)  # keep the JPEG as is
    item, folder = item_for(tmp_path, {"song.mp3": b"m" * 500, "song.jpg": FAKE_JPEG, "unrelated.jpg": b"\xff\xd8\xffno"})
    assert item["kind"] == "audio"
    dest = tmp_path / "p.jpg"
    assert P.make_poster(item, folder, dest) and dest.read_bytes() == FAKE_JPEG


def test_cover_names_and_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(P.shutil, "which", lambda name: None)
    item, folder = item_for(tmp_path, {"song.mp3": b"m" * 500, "Cover.jpg": FAKE_JPEG})
    assert P.make_poster(item, folder, tmp_path / "p.jpg")
    item, folder = item_for(tmp_path, {"song.mp3": b"m" * 500, "screenshot1.jpg": FAKE_JPEG}, sub="n")
    assert P.make_poster(item, folder, tmp_path / "q.jpg") is False


# ---- from report.image (SSRF-guarded) ----

def png_response():
    return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG fake")


def test_fetch_image_ok_and_pins_the_validated_ip(net):
    net[("img.example", "/p.png")] = lambda: httpx.Response(200, headers={"content-type": "image/jpeg; charset=x"}, content=FAKE_JPEG)
    assert P.fetch_image("https://img.example/p.png") == FAKE_JPEG
    assert net["seen"] == [("GET", "img.example", "93.184.216.34")]  # connected to the IP that was checked


@pytest.mark.parametrize("url", ["http://img.example/p.png", "https://evil.test/p.png", "https://127.0.0.1/p.png",
                                 "https://localhost/p.png", "ftp://img.example/p.png", "file:///etc/passwd",
                                 "https://img.example\\@evil.test/p.png", "https://[::1]/p.png", "https://10.0.0.5/p.png"])
def test_fetch_image_refuses_unsafe_targets(net, url):
    net[("img.example", "/p.png")] = png_response
    assert P.fetch_image(url) is None
    assert [s for s in net["seen"] if s[2] != "93.184.216.34"] == [] and net["seen"] == []


def test_fetch_image_checks_every_redirect_hop(net):
    net[("img.example", "/r")] = lambda: httpx.Response(302, headers={"location": "https://evil.test/p.png"})
    net[("evil.test", "/p.png")] = png_response
    assert P.fetch_image("https://img.example/r") is None
    assert [s[2] for s in net["seen"]] == ["93.184.216.34"]  # the private host was never contacted
    net[("img.example", "/ok")] = lambda: httpx.Response(302, headers={"location": "https://other.example/p.png"})
    net[("other.example", "/p.png")] = png_response
    assert P.fetch_image("https://img.example/ok") is not None
    net[("img.example", "/http")] = lambda: httpx.Response(302, headers={"location": "http://other.example/p.png"})
    assert P.fetch_image("https://img.example/http") is None  # a downgrade to http is refused too
    net[("img.example", "/loop")] = lambda: httpx.Response(302, headers={"location": "https://img.example/loop"})
    assert P.fetch_image("https://img.example/loop") is None


def test_fetch_image_only_images_and_200(net):
    net[("img.example", "/html")] = lambda: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")
    net[("img.example", "/none")] = lambda: httpx.Response(200, content=b"abc")
    net[("img.example", "/gone")] = lambda: httpx.Response(404, headers={"content-type": "image/png"}, content=b"x")
    for path in ("/html", "/none", "/gone", "/missing"):
        assert P.fetch_image("https://img.example" + path) is None


def test_fetch_image_size_limit(net):
    big = b"x" * (P.MAX_BYTES + 1)
    net[("img.example", "/big")] = lambda: httpx.Response(200, headers={"content-type": "image/png"}, content=big)  # content-length says so
    net[("img.example", "/chunked")] = lambda: httpx.Response(200, headers={"content-type": "image/png"}, stream=httpx.ByteStream(big))
    net[("img.example", "/edge")] = lambda: httpx.Response(200, headers={"content-type": "image/png"}, content=b"x" * P.MAX_BYTES)
    assert P.fetch_image("https://img.example/big") is None
    assert P.fetch_image("https://img.example/chunked") is None
    assert P.fetch_image("https://img.example/edge") is not None


def test_fetch_image_time_limit(net, monkeypatch):
    net[("img.example", "/slow")] = lambda: httpx.Response(200, headers={"content-type": "image/png"}, stream=httpx.ByteStream(b"x" * 100))
    ticks = iter([0.0, 100.0, 200.0, 300.0])
    monkeypatch.setattr(P.time, "monotonic", lambda: next(ticks, 999.0))
    assert P.fetch_image("https://img.example/slow") is None


def test_fetch_image_network_error_is_none(net):
    def boom():
        raise httpx.ConnectError("down")
    net[("img.example", "/x")] = boom
    assert P.fetch_image("https://img.example/x") is None


@needs_ffmpeg
def test_poster_from_report_image_is_converted_and_scaled(tmp_path, net):
    src = tmp_path / "src.png"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=900x300", "-frames:v", "1", str(src))
    net[("img.example", "/p.png")] = lambda: httpx.Response(200, headers={"content-type": "image/png"}, content=src.read_bytes())
    item, folder = item_for(tmp_path, {"a.bin": b"1" * 10}, sub="x")
    dest = tmp_path / "posters" / "i_r.jpg"
    assert P.make_poster(item, folder, dest, "https://img.example/p.png") is True
    assert size_of(dest) == ("mjpeg", 600, 200)


def test_report_image_wins_then_falls_back_to_the_file(tmp_path, net, monkeypatch):
    monkeypatch.setattr(P.shutil, "which", lambda name: None)
    net[("img.example", "/p.jpg")] = lambda: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8\xff REMOTE")
    item, folder = item_for(tmp_path, {"pic.jpg": FAKE_JPEG})
    assert P.make_poster(item, folder, tmp_path / "a.jpg", "https://img.example/p.jpg")
    assert (tmp_path / "a.jpg").read_bytes().endswith(b"REMOTE")
    # an unusable remote image: the item's own image is used instead
    assert P.make_poster(item, folder, tmp_path / "b.jpg", "https://evil.test/p.jpg")
    assert (tmp_path / "b.jpg").read_bytes() == FAKE_JPEG


def test_never_raises(tmp_path, monkeypatch):
    assert P.make_poster({}, tmp_path, tmp_path / "p.jpg") is False
    assert P.make_poster({"files": [{"path": "x"}], "main": 5}, tmp_path, tmp_path / "p.jpg") is False
    assert P.make_poster(None, tmp_path, tmp_path / "p.jpg") is False
    item, folder = item_for(tmp_path, {"a.png": b"png"})
    (tmp_path / "blocker").write_text("a file, not a folder")
    assert P.make_poster(item, folder, tmp_path / "blocker" / "p.jpg") is False  # cannot create the folder
    monkeypatch.setattr(P, "_from_cover", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert P.make_poster(item, folder, tmp_path / "p.jpg") is False


def test_timeouts_are_bounded_and_survived(tmp_path, monkeypatch):
    seen = []

    def slow(args, **kw):
        seen.append(kw.get("timeout"))
        raise subprocess.TimeoutExpired(args, kw.get("timeout"))
    monkeypatch.setattr(P.subprocess, "run", slow)
    monkeypatch.setattr(P.shutil, "which", lambda name: "/usr/bin/" + name)
    item, folder = item_for(tmp_path, {"clip.mp4": b"v"})
    assert P.make_poster(item, folder, tmp_path / "p.jpg") is False
    assert seen and set(seen) == {30}  # ffprobe and ffmpeg both capped at 30 s


def test_replace_stays_on_one_filesystem(tmp_path, monkeypatch):
    """Docker: the posters folder is a mount and /tmp is not; os.replace across them fails with EXDEV."""
    import errno
    import os
    real = os.replace

    def replace(src, dst):
        if Path(src).parent.parent != Path(dst).parent:  # scratch dir must be directly inside the destination folder
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real(src, dst)
    monkeypatch.setattr(P.os, "replace", replace)
    monkeypatch.setattr(P.shutil, "which", lambda name: None)
    item, folder = item_for(tmp_path, {"pic.jpg": FAKE_JPEG})
    dest = tmp_path / "posters" / "i_x.jpg"
    assert P.make_poster(item, folder, dest) is True and dest.read_bytes() == FAKE_JPEG
    assert [p.name for p in dest.parent.iterdir()] == ["i_x.jpg"]  # scratch dir cleaned up
