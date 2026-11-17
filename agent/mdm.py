"""Wrapper around misterdownloadmanager-cli (--json mode). Args are lists, never a shell."""
import json
import os
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx

SCHEMES = ("http", "https", "ftp", "ftps", "magnet")


class MdmError(Exception):
    pass


# built from the fork (submodule) by scripts/build_mdm.sh; build output lives outside iCloud-synced folders
FORK_CLI = os.path.expanduser("~/Library/Caches/mdm-agent/target/release/misterdownloadmanager-cli")
RELEASE_CLI = "./vendor/mdm/misterdownloadmanager-cli"  # prebuilt upstream release, fallback


def cli_path() -> str:
    """MDM_CLI if set, else the CLI built from the fork, else the prebuilt release."""
    # the CLI is only a launcher: it needs the app's program next to it, so a half-finished build is skipped
    fork_ready = all(os.access(p, os.X_OK) for p in (FORK_CLI, os.path.join(os.path.dirname(FORK_CLI), "misterdownloadmanager")))
    return os.environ.get("MDM_CLI") or (FORK_CLI if fork_ready else RELEASE_CLI)


def _cli() -> str:
    return cli_path()


def _check_source(source: str) -> str:
    """Reject URLs that different parsers could read differently (WHATWG parsers treat '\\' as '/' and drop
    tabs/newlines, so 'http://127.0.0.1\\@evil/' means evil to urllib but 127.0.0.1 to the downloader)."""
    if not isinstance(source, str) or not source:
        raise MdmError("invalid source")
    if any(c == "\\" or c.isspace() or ord(c) < 33 or ord(c) == 127 for c in source):
        raise MdmError("invalid url: contains a backslash, whitespace or control character")
    try:
        p = urlparse(source)
        scheme = p.scheme.lower()
        if scheme not in SCHEMES:
            raise MdmError("unsupported url scheme (allowed: http, https, ftp, ftps, magnet)")
        if scheme != "magnet":
            host = (p.hostname or "").lower()
            if not host or "%" in host or host != httpx.URL(source).raw_host.decode("ascii").lower():
                raise MdmError("invalid url: ambiguous host")
    except (ValueError, httpx.InvalidURL, UnicodeError):
        raise MdmError("invalid url") from None
    return source


def _is_torrent(source: str) -> bool:
    # The CLI's torrent-get/torrent-info take a magnet or a LOCAL .torrent path, not a URL: an http(s)
    # '.torrent' link is fetched as an ordinary file with `get`.
    return urlparse(source).scheme.lower() == "magnet"


def _confine(dest_dir: str, download_dir: str | None = None) -> Path:
    base = Path(download_dir or os.environ.get("DOWNLOAD_DIR") or "./downloads").resolve()
    if ".." in Path(str(dest_dir)).parts:
        raise MdmError("dest_dir must not contain '..'")
    p = Path(dest_dir)
    p = (p if p.is_absolute() else base / p).resolve()
    if p != base and base not in p.parents:
        raise MdmError("dest_dir must be inside DOWNLOAD_DIR")
    return p


# what MDM's --json progress lines carry: http (connections) and torrent (phase, peers, peers_seen)
PROGRESS_KEYS = ("downloaded", "size", "speed", "connections", "phase", "peers", "peers_seen")


def _connections() -> int:
    """Parallel connections per http download. 16 measured ~45% faster than MDM's default 8 on archive.org
    (per-connection throttled); 32 gave little more. MDM allows 1-128."""
    try:
        n = int(os.environ.get("MDM_CONNECTIONS") or 16)
    except ValueError:
        n = 16
    return min(max(n, 1), 128)


def _err_text(obj: dict) -> str:
    return str(obj.get("error") or obj.get("message") or "download failed")


def run(source: str, dest_dir: str, files: list[int] | None = None, on_event=None, cancel=None,
        download_dir: str | None = None) -> dict:
    source = _check_source(source)
    dest = _confine(dest_dir, download_dir)
    if files is not None and not all(type(i) is int and i >= 0 for i in files):
        raise MdmError("files must be non-negative integers")
    dest.mkdir(parents=True, exist_ok=True)
    if _is_torrent(source):
        args = [_cli(), "--json", "torrent-get", source, "-o", str(dest)]
        if files:
            args += ["--files", ",".join(str(i) for i in files)]
    else:
        args = [_cli(), "--json", "get", source, "-o", str(dest), "-c", str(_connections())]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                stdin=subprocess.DEVNULL)
    except OSError as e:
        raise MdmError(f"cannot start downloader: {e.strerror or e}") from None

    stderr_buf: list[str] = []
    err_thread = threading.Thread(target=lambda: stderr_buf.append(proc.stderr.read()), daemon=True)
    err_thread.start()
    finished = threading.Event()
    cancelled = threading.Event()

    def watch():
        while not finished.is_set():
            if cancel is not None and cancel.wait(0.1):
                cancelled.set()
                proc.terminate()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return
            if cancel is None:
                finished.wait(0.1)

    threading.Thread(target=watch, daemon=True).start()

    done = None
    error = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("event")
            if kind == "progress" and on_event:
                try:
                    on_event({k: ev[k] for k in PROGRESS_KEYS if ev.get(k) is not None})
                except Exception:
                    pass  # a broken callback must not kill the download
            elif kind == "done":
                done = ev
            elif kind == "error" or (kind is None and "error" in ev):
                error = _err_text(ev)
        rc = proc.wait()
    finally:
        finished.set()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    err_thread.join(2)  # let the stderr reader finish so a failure keeps its message
    if cancelled.is_set() or (cancel is not None and cancel.is_set() and done is None):
        raise MdmError("cancelled")
    if error:
        raise MdmError(error)
    if rc != 0:
        tail = (stderr_buf[0].strip().splitlines()[-1:] if stderr_buf and stderr_buf[0].strip() else [])
        raise MdmError(tail[0][:300] if tail else f"downloader exited with code {rc}")
    if done is None:
        raise MdmError("downloader finished without a result")
    return {"path": done.get("path"), "size": done.get("size")}


def torrent_info(source: str) -> dict:
    source = _check_source(source)
    try:
        r = subprocess.run([_cli(), "--json", "torrent-info", source], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise MdmError(f"cannot start downloader: {e.strerror or e}") from None
    except subprocess.TimeoutExpired:
        raise MdmError("torrent-info timed out") from None
    info = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if "error" in o and "files" not in o:
                raise MdmError(_err_text(o))
            if "files" in o or "info_hash" in o:
                info = o
    if r.returncode != 0 or info is None:
        raise MdmError((r.stderr.strip().splitlines() or ["torrent-info failed"])[-1][:300])
    return {"name": info.get("name"), "info_hash": info.get("info_hash"), "size": info.get("size"),
            "files": info.get("files") or []}
