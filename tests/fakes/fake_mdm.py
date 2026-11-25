#!/usr/bin/env python3
"""Fake misterdownloadmanager-cli. Mode via env FAKE_MDM_MODE: done (default) | error | stderr | slow | silent.
Also video-info (FAKE_MDM_VIDEO: ok | missing | list | novideo), video-get and resume.
Also records its argv to FAKE_MDM_ARGS (file path) if set."""
import json
import os
import sys
import time

mode = os.environ.get("FAKE_MDM_MODE", "done")
argv = sys.argv[1:]
if os.environ.get("FAKE_MDM_ARGS"):
    with open(os.environ["FAKE_MDM_ARGS"], "w") as f:
        json.dump(argv, f)


def emit(o):
    print(json.dumps(o), flush=True)


if "torrent-info" in argv:
    if mode == "error":
        emit({"event": "error", "error": "bad torrent"})
        sys.exit(1)
    emit({"name": "Demo", "info_hash": "ab" * 20, "size": 30,
          "files": [{"index": 0, "size": 10, "path": "Demo/a.txt"}, {"index": 1, "size": 20, "path": "Demo/b.txt"}]})
    sys.exit(0)

if "video-info" in argv:
    v = os.environ.get("FAKE_MDM_VIDEO", "ok")
    if v == "missing":
        emit({"error": "yt-dlp is not installed (install-ytdlp)"})
        sys.exit(1)
    if v == "novideo":
        emit({"error": "yt-dlp failed: Unsupported URL"})
        sys.exit(1)
    if v == "list":
        emit({"list": "A playlist", "total": 3})
        sys.exit(0)
    emit({"title": "Cat video", "choices": [{"label": "720p", "format": "b[height=720]", "height": 720},
                                            {"label": "1080p", "format": "b[height=1080]", "height": 1080},
                                            {"label": "720p60", "format": "b[height=720][fps=60]", "height": 720},
                                            {"label": "sound", "format": "ba", "height": None}],
          "subtitles": [{"code": "en", "name": "English", "automatic": True}], "needs_js_runtime": False,
          "declared": None})
    sys.exit(0)

if "resume" in argv:  # finishes the download the state file belongs to
    state = argv[argv.index("resume") + 1]
    path = state[: -len(".mdm.json")]
    emit({"event": "progress", "downloaded": 50, "size": 100, "speed": 10, "connections": 4})
    with open(path, "wb") as f:
        f.write(b"r" * 100)
    os.remove(state)
    emit({"event": "done", "path": path, "size": 100, "seconds": 0.1})
    sys.exit(0)

out = argv[argv.index("-o") + 1]
if mode == "error":
    emit({"event": "progress", "downloaded": 5, "size": 100, "speed": 5})
    emit({"event": "error", "error": "connection reset"})
    sys.exit(2)
if mode == "stderr":
    print("fatal: boom", file=sys.stderr)
    sys.exit(3)
if mode == "silent":
    sys.exit(0)
if mode == "slow":
    for i in range(1000):
        emit({"event": "progress", "downloaded": i, "size": 1000, "speed": 1})
        time.sleep(0.05)
for d in (25, 50, 100):
    emit({"event": "progress", "downloaded": d, "size": 100, "speed": 10, "connections": 4})
path = os.path.join(out, "video.mp4" if "video-get" in argv else "file.bin")
with open(path, "wb") as f:
    f.write(b"x" * 100)
emit({"event": "done", "path": path, "size": 100, "seconds": 0.1})
