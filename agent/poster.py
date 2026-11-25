"""Posters for library items (contracts/library.md): a JPEG at most 600px wide. Never raises: no poster is fine."""
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from agent import inspect as ins  # the SSRF guard: reused as is, never weakened

log = logging.getLogger(__name__)
MAX_BYTES = 5_000_000
FETCH_TIMEOUT = 10.0
FFMPEG_TIMEOUT = 30
MAX_REDIRECTS = 3
SCALE = "scale='min(600,iw)':-2"  # never wider than 600px, never upscaled
COVER_NAMES = {"cover", "folder", "poster", "thumb", "thumbnail", "front"}


def make_poster(item: dict, folder, dest, image_url: str | None = None) -> bool:
    """Write dest (a .jpg) for the item whose files live in folder; True when one was made."""
    try:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # scratch space beside dest: os.replace must stay on one filesystem (/library is a mount in Docker, /tmp is not)
        with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".poster-") as tmp:
            out = Path(tmp, "poster.jpg")
            files = [(Path(folder, f["path"]), f) for f in item.get("files", [])]
            main = Path(folder, item["files"][item["main"]]["path"]) if files else None
            steps = []
            if image_url:
                steps.append(lambda: _from_url(image_url, out, tmp))
            if main is not None and item.get("kind") == "video":
                steps.append(lambda: _from_video(main, out))
            steps.append(lambda: _from_cover(item, folder, main, out))
            for step in steps:
                try:
                    if step() and out.is_file() and out.stat().st_size > 0:
                        os.replace(out, dest)  # atomic: the UI never sees half a file
                        return True
                except Exception:
                    log.exception("poster step failed")
    except Exception:
        log.exception("could not make a poster")
    return False


def _ffmpeg(*args) -> bool:
    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "-y", "-v", "error", *args], capture_output=True, timeout=FFMPEG_TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _to_jpeg(src: Path, out: Path) -> bool:
    """Scale/convert any image to a JPEG at most 600px wide. Without ffmpeg only a JPEG can be kept, as is."""
    if shutil.which("ffmpeg"):
        return _ffmpeg("-i", str(src), "-frames:v", "1", "-vf", SCALE, "-q:v", "4", str(out))
    with open(src, "rb") as f:
        if f.read(3) != b"\xff\xd8\xff":
            return False
    shutil.copyfile(src, out)
    return True


def _from_video(video: Path, out: Path) -> bool:
    """One frame at about 10% of the duration (the start when the duration is unknown)."""
    t = 0.0
    probe = shutil.which("ffprobe")
    if probe:
        try:
            r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                                str(video)], capture_output=True, text=True, timeout=FFMPEG_TIMEOUT,
                               stdin=subprocess.DEVNULL)
            t = max(float(r.stdout.strip()) * 0.1, 0.0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    for at in dict.fromkeys((t, 0.0)):  # a failed seek retries at the start
        if _ffmpeg("-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", "-vf", SCALE, "-q:v", "4", str(out)) \
                and out.is_file():
            return True
    return False


def _from_cover(item: dict, folder, main, out: Path) -> bool:
    """A thumbnail or cover MDM/yt-dlp wrote next to the file (same name, or cover/folder/thumb...), or the
    item itself when it is an image."""
    if main is not None and item.get("kind") == "image":
        return _to_jpeg(main, out)
    stem = main.stem.lower() if main is not None else None
    for f in item.get("files", []):
        p = Path(folder, f["path"])
        if p == main or not f.get("mime", "").startswith("image/"):
            continue
        if p.stem.lower() == stem or p.stem.lower() in COVER_NAMES:
            if _to_jpeg(p, out):
                return True
    return False


def _from_url(url: str, out: Path, tmp: str) -> bool:
    data = fetch_image(url)
    if data is None:
        return False
    raw = Path(tmp, "fetched")
    raw.write_bytes(data)
    return _to_jpeg(raw, out)


def fetch_image(url: str) -> bytes | None:
    """An https image, at most 5 MB in 10 s, through the same guard as inspect_link: every hop is checked, the
    connection goes to the validated IP, image/* only. None on anything else."""
    deadline = time.monotonic() + FETCH_TIMEOUT
    try:
        with httpx.Client(transport=ins._TRANSPORT, timeout=FETCH_TIMEOUT, follow_redirects=False,
                          trust_env=False) as client:
            cur = url
            for _ in range(MAX_REDIRECTS + 1):
                ins._check(cur)
                if urlparse(cur).scheme.lower() != "https":
                    return None
                u = httpx.URL(cur)
                host = u.raw_host.decode("ascii")
                ips = ins._safe_ips(host, u.port or 443)
                pinned = u.copy_with(host=ips[0], userinfo=b"")
                hdrs = {"Host": u.netloc.decode().rsplit("@", 1)[-1], "User-Agent": "mdm-agent/1.0", "Accept": "image/*"}
                req = client.build_request("GET", pinned, headers=hdrs, extensions={"sni_hostname": host})
                resp = client.send(req, stream=True)
                try:
                    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        cur = urljoin(cur, resp.headers["location"])
                        continue
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if resp.status_code != 200 or not ctype.startswith("image/"):
                        return None
                    if int(resp.headers.get("content-length") or 0) > MAX_BYTES:
                        return None
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        buf += chunk
                        if len(buf) > MAX_BYTES or time.monotonic() > deadline:
                            return None
                    return bytes(buf)
                finally:
                    resp.close()
    except (ins.Blocked, httpx.HTTPError, ValueError, OSError):
        return None
    return None
