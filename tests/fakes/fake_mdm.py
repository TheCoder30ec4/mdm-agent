#!/usr/bin/env python3
"""Fake misterdownloadmanager-cli. Mode via env FAKE_MDM_MODE: done (default) | error | stderr | slow | silent.
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
path = os.path.join(out, "file.bin")
with open(path, "wb") as f:
    f.write(b"x" * 100)
emit({"event": "done", "path": path, "size": 100, "seconds": 0.1})
