import os
import sqlite3
import threading
from pathlib import Path

import pytest

from agent import library as L


def make(folder, files):
    folder.mkdir(parents=True, exist_ok=True)
    for name, size in files.items():
        p = folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    return folder


@pytest.mark.parametrize("name,mime,kind,playable", [
    ("a.mp4", "video/mp4", "video", True), ("a.m4v", "video/x-m4v", "video", True), ("a.webm", "video/webm", "video", True),
    ("a.MOV", "video/quicktime", "video", True), ("a.mkv", "video/x-matroska", "video", False),
    ("a.avi", "video/x-msvideo", "video", False), ("a.wmv", "video/x-ms-wmv", "video", False),
    ("a.mp3", "audio/mpeg", "audio", True), ("a.m4a", "audio/mp4", "audio", True), ("a.ogg", "audio/ogg", "audio", True),
    ("a.opus", "audio/opus", "audio", True), ("a.wav", "audio/wav", "audio", True), ("a.flac", "audio/flac", "audio", True),
    ("a.png", "image/png", "image", False), ("a.jpg", "image/jpeg", "image", False),
    ("a.pdf", "application/pdf", "document", False), ("a.txt", "text/plain", "document", False),
    ("a.epub", "application/epub+zip", "document", False),
    ("a.zip", "application/zip", "archive", False), ("a.7z", "application/x-7z-compressed", "archive", False),
    ("a.iso", "application/x-iso9660-image", "archive", False), ("a.tar", "application/x-tar", "archive", False),
    ("a.bin", "application/octet-stream", "other", False), ("noext", "application/octet-stream", "other", False)])
def test_mime_kind_playable(tmp_path, name, mime, kind, playable):
    make(tmp_path / "j", {name: 3})
    f = L.scan_files(tmp_path / "j")[0]
    assert (f["mime"], f["playable"]) == (mime, playable)
    assert L.kind_of(f["mime"]) == kind


def test_build_item(tmp_path):
    make(tmp_path / "j_1", {"Night.mp4": 100, "extras/b.mkv": 10, "Night.mp4.mdm.json": 5, ".DS_Store": 5, ".hid/x.txt": 5})
    (tmp_path / "j_1" / "link").symlink_to(tmp_path / "j_1" / "Night.mp4")
    it = L.build_item("j_1", tmp_path / "j_1", "Night of the Living Dead (1968)", "https://x/y")
    assert it["id"].startswith("i_") and len(it["id"]) == 10
    assert [(f["index"], f["path"], f["size"]) for f in it["files"]] == [(0, "Night.mp4", 100), (1, "extras/b.mkv", 10)]
    assert it["main"] == 0 and it["kind"] == "video" and it["size"] == 110 and it["poster"] is False
    assert (it["job_id"], it["title"], it["source_url"]) == ("j_1", "Night of the Living Dead (1968)", "https://x/y")
    assert it["created"].endswith("+00:00")
    assert set(it) == {"id", "job_id", "title", "kind", "size", "created", "source_url", "poster", "files", "main"}


def test_build_item_main_largest_and_title_fallback(tmp_path):
    make(tmp_path / "j", {"a.txt": 5, "b.mp3": 50, "c.png": 50})
    it = L.build_item("j", tmp_path / "j", None, "u")
    assert it["main"] == 1 and it["kind"] == "audio" and it["title"] == "b.mp3"  # first of equal sizes


def test_build_item_empty_or_missing(tmp_path):
    (tmp_path / "e").mkdir()
    make(tmp_path / "e", {"x.mdm.json": 1, ".h": 1})
    assert L.build_item("j", tmp_path / "e", "t", "u") is None
    assert L.build_item("j", tmp_path / "nope", "t", "u") is None


def test_schema_and_pragmas(tmp_path):
    lib = L.Library(tmp_path / "sub" / "mdm.db")
    q = lambda sql: lib._db.execute(sql).fetchall()
    assert {r[0] for r in q("SELECT name FROM sqlite_master WHERE type='table'")} >= {"proposals", "jobs", "items", "chats", "chat_events"}
    assert [r[1] for r in q("PRAGMA table_info(items)")] == ["id", "job_id", "title", "kind", "size", "created", "source_url", "poster", "body"]
    assert [r[1] for r in q("PRAGMA table_info(proposals)")] == ["id", "status", "created", "chat_id", "body"]
    assert [r[1] for r in q("PRAGMA table_info(jobs)")] == ["id", "proposal_id", "state", "created", "finished", "body"]
    assert [r[1] for r in q("PRAGMA table_info(chat_events)")] == ["chat_id", "seq", "body"]
    assert q("PRAGMA journal_mode")[0][0] == "wal" and q("PRAGMA foreign_keys")[0][0] == 1


def test_save_state_keeps_order_and_deletes(tmp_path):
    lib = L.Library(tmp_path / "mdm.db")
    ps = [{"id": f"p_{i}", "status": "pending", "created": "t"} for i in range(3)]
    js = [{"id": f"j_{i}", "state": "done", "proposal_id": f"p_{i}"} for i in range(3)]
    lib.save_state(ps, js)
    ps[0]["status"] = "approved"
    lib.save_state(ps, js)  # an update must not move the row
    got_p, got_j = lib.load_state()
    assert [p["id"] for p in got_p] == ["p_0", "p_1", "p_2"] and got_p[0]["status"] == "approved"
    lib.save_state(ps[1:], js[:1])
    got_p, got_j = lib.load_state()
    assert [p["id"] for p in got_p] == ["p_1", "p_2"] and [j["id"] for j in got_j] == ["j_0"]
    assert lib._db.execute("SELECT proposal_id, state FROM jobs").fetchall() == [("p_0", "done")]


def test_save_state_is_atomic(tmp_path):
    lib = L.Library(tmp_path / "mdm.db")
    lib.save_state([{"id": "p_1", "status": "pending"}], [])
    with pytest.raises(Exception):
        lib.save_state([{"id": "p_2", "status": "pending"}, {"no": "id"}], [])  # KeyError half way
    assert [p["id"] for p in lib.load_state()[0]] == ["p_1"]  # rolled back, and the lock is free again
    lib.save_state([], [])


def test_items_store(tmp_path):
    lib = L.Library(tmp_path / "mdm.db")
    a = {"id": "i_a", "job_id": "j_a", "title": "A", "kind": "video", "size": 1, "created": "2026-01-01T00:00:00+00:00",
         "source_url": "u", "poster": False, "files": [], "main": 0}
    b = dict(a, id="i_b", job_id="j_b", title="B", created="2026-01-02T00:00:00+00:00")
    c = dict(a, id="i_c", job_id="j_c", title="C")  # same time as a: the later insert is newer
    for it in (a, b, c):
        lib.put_item(it)
    assert [i["id"] for i in lib.list_items()] == ["i_b", "i_c", "i_a"]
    with pytest.raises(sqlite3.IntegrityError):  # one item per job
        lib.put_item(dict(a, id="i_dup"))
    lib.put_item(dict(a, poster=True))
    assert lib.get_item("i_a")["poster"] is True
    assert lib._db.execute("SELECT poster FROM items WHERE id='i_a'").fetchone()[0] == "i_a.jpg"
    assert lib._db.execute("SELECT poster FROM items WHERE id='i_b'").fetchone()[0] is None
    lib.delete_item("i_a")
    assert lib.get_item("i_a") is None and len(lib.list_items()) == 2


def test_chat_events_cascade_and_title(tmp_path):
    lib = L.Library(tmp_path / "mdm.db")
    lib.append_event("c1", {"type": "user", "text": "hello there"})
    lib.append_event("c1", {"type": "text", "text": "hi"})
    lib.append_event("c2", {"type": "user", "text": ""})  # empty first message: default title stays
    assert lib.get_chat("c1")["chat"]["title"] == "hello there" and lib.get_chat("c2")["chat"]["title"] == "New download"
    assert [r[0] for r in lib._db.execute("SELECT seq FROM chat_events WHERE chat_id='c1' ORDER BY seq")] == [1, 2]
    assert lib.delete_chat("c1") is True and lib.delete_chat("c1") is False
    assert lib._db.execute("SELECT COUNT(*) FROM chat_events WHERE chat_id='c1'").fetchone()[0] == 0
    assert lib.get_chat("c1") is None and lib.get_chat("c2") is not None


def test_concurrent_writers_share_the_connection(tmp_path):
    lib = L.Library(tmp_path / "mdm.db")
    errs = []

    def writer(n):
        try:
            for i in range(25):
                lib.append_event("c", {"type": "text", "text": f"{n}-{i}"})
                lib.save_state([{"id": f"p_{n}_{i}", "status": "pending"}], [])
        except Exception as e:  # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errs == []
    evs = lib.get_chat("c")["events"]
    assert len(evs) == 150 and len({e["text"] for e in evs}) == 150
    assert [r[0] for r in lib._db.execute("SELECT seq FROM chat_events ORDER BY seq")] == list(range(1, 151))


def test_corrupt_db_is_set_aside(tmp_path):
    (tmp_path / "mdm.db").write_bytes(b"garbage" * 1000)
    lib = L.Library(tmp_path / "mdm.db")
    assert lib.list_items() == [] and (tmp_path / "mdm.db.corrupt").read_bytes().startswith(b"garbage")
