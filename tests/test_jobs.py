import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from agent import jobs as J
from agent import mdm


def report(level="ok", url="https://example.com/f.bin"):
    return {"url": url, "final_url": url, "title": "F", "filename": "f.bin", "size": 100,
            "safety": {"level": level, "reasons": [], "skipped": []}}


def wait(pred, t=5):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class Runner:
    """Records calls; behaviour via .mode: done | error | block (until cancel)."""
    def __init__(self, mode="done"):
        self.mode, self.calls, self.kw = mode, [], []

    def __call__(self, source, dest_dir, files=None, on_event=None, cancel=None, **kw):
        self.calls.append((source, dest_dir, files))
        self.kw.append(kw)
        if self.mode == "error":
            raise mdm.MdmError("boom")
        if self.mode == "block":
            on_event({"downloaded": 1, "size": 10, "speed": 1})
            cancel.wait(10)
            raise mdm.MdmError("cancelled")
        for d in (3, 6, 10):
            on_event({"downloaded": d, "size": 10, "speed": 2})
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        (Path(dest_dir) / "f.bin").write_bytes(b"0123456789")
        return {"path": str(Path(dest_dir) / "f.bin"), "size": 10}


@pytest.fixture
def mk(tmp_path):
    notes = []
    def make(runner=None):
        r = runner or Runner()
        j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=r, notifier=lambda t, m: notes.append((t, m)))
        return j, r, notes
    return make


def test_propose_starts_nothing(mk):
    j, r, _ = mk()
    p = j.propose(report(), note="n")
    time.sleep(0.2)
    assert p["status"] == "pending" and p["id"].startswith("p_") and j.list_jobs() == [] and r.calls == []
    with pytest.raises(J.NotFound):
        j.cancel("j_nope")


def test_approve_is_only_start_path(mk, monkeypatch):
    started = []

    class SpyThread(threading.Thread):
        def start(self):
            started.append(self)
            super().start()
    monkeypatch.setattr(J.threading, "Thread", SpyThread)
    j, r, _ = mk()
    a = j.propose(report()); b = j.propose(report())
    j.reject(a["id"])
    time.sleep(0.2)
    work = lambda: [t for t in started if "_work" in t.name]  # (the poster thread is not a download)
    assert r.calls == [] and work() == [] and j.list_jobs() == []
    assert [m for m in dir(j) if not m.startswith("_") and "start" in m.lower()] == []
    job = j.approve(b["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    assert len(r.calls) == 1 and len(work()) == 1


def test_approve_runs_and_notifies(mk):
    j, r, notes = mk()
    q = j.subscribe()
    p = j.propose(report(), files=[1])
    job = j.approve(p["id"])
    assert job["id"].startswith("j_") and job["proposal_id"] == p["id"]
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    d = j.get_job(job["id"])
    assert d["path"].endswith(f"{job['id']}/f.bin") and d["size"] == 10 and d["finished"]
    assert r.calls[0][1].endswith(job["id"]) and r.calls[0][2] == [1]
    assert notes == [("Download finished", "F")]
    evs = []
    while not q.empty():
        evs.append(q.get())
    types = [e["type"] for e in evs]
    assert types[0] == "proposal" and "job" in types and types[-1] == "job_done"
    assert [e["proposal"]["status"] for e in evs if e["type"] == "proposal"] == ["pending", "approved"]
    assert j.list_proposals()[0]["job_id"] == job["id"]


def test_progress_throttled(mk):
    j, _, _ = mk()
    q = j.subscribe()
    j.approve(j.propose(report())["id"])
    assert wait(lambda: j.list_jobs()[0]["state"] == "done")
    # queued + running + (at most one progress) + final: never one event per progress call
    assert sum(e["type"] == "job" for e in list(q.queue)) <= 5


def test_state_errors(mk):
    j, _, _ = mk()
    p = j.propose(report())
    j.approve(p["id"])
    with pytest.raises(J.BadState):
        j.approve(p["id"])
    with pytest.raises(J.BadState):
        j.reject(p["id"])
    p2 = j.propose(report())
    j.reject(p2["id"])
    with pytest.raises(J.BadState):
        j.approve(p2["id"])
    with pytest.raises(J.NotFound):
        j.approve("p_00000000")
    with pytest.raises(J.NotFound):
        j.reject("p_00000000")
    with pytest.raises(J.NotFound):
        j.get_job("j_00000000")


def test_risky_needs_confirm(mk):
    j, r, _ = mk()
    p = j.propose(report("risky"))
    with pytest.raises(J.NeedsConfirm):
        j.approve(p["id"])
    with pytest.raises(J.NeedsConfirm):
        j.approve(p["id"], confirm_risky=False)
    time.sleep(0.1)
    assert r.calls == [] and j.list_proposals()[0]["status"] == "pending"
    job = j.approve(p["id"], confirm_risky=True)
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")


def test_caution_needs_no_confirm(mk):
    j, _, _ = mk()
    assert j.approve(j.propose(report("caution"))["id"])["state"] in ("queued", "running", "done")


def test_error_job(mk):
    j, _, notes = mk(Runner("error"))
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "error")
    assert j.get_job(job["id"])["error"] == "boom"
    assert notes[0][0] == "Download failed" and "boom" in notes[0][1]


def test_cancel(mk):
    j, _, notes = mk(Runner("block"))
    q = j.subscribe()
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "running")
    assert j.cancel(job["id"])["state"] == "cancelled"
    with pytest.raises(J.BadState):
        j.cancel(job["id"])
    assert notes == []
    assert "job_done" in [e["type"] for e in list(q.queue)]


def test_cancel_with_real_cli(tmp_path, monkeypatch):
    fake = Path(__file__).parent / "fakes" / "fake_mdm.py"
    monkeypatch.setenv("MDM_CLI", str(fake))
    monkeypatch.setenv("FAKE_MDM_MODE", "slow")
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "other"))  # explicit download_dir must win
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", notifier=lambda *a: None)
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["downloaded"] > 0)
    assert j.cancel(job["id"])["state"] == "cancelled"


def test_real_cli_done(tmp_path, monkeypatch):
    monkeypatch.setenv("MDM_CLI", str(Path(__file__).parent / "fakes" / "fake_mdm.py"))
    monkeypatch.delenv("FAKE_MDM_MODE", raising=False)
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", notifier=lambda *a: None)
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    assert Path(j.get_job(job["id"])["path"]).is_file()


def test_restart_recovery(tmp_path):
    notes = []
    r = Runner("block")
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=r, notifier=lambda *a: notes.append(a))
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "running")
    j2 = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    d = j2.get_job(job["id"])
    assert d["state"] == "error" and d["error"] == "interrupted" and d["finished"]
    assert len(j2.list_proposals()) == 1 and j2.list_proposals()[0]["status"] == "approved"
    # persisted in mdm.db, not only in memory: a third start sees the same
    j3 = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    assert j3.get_job(job["id"])["state"] == "error"
    j._cancels[job["id"]].set()


def test_corrupt_database_file(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "mdm.db").write_text("{nope this is not sqlite" * 50)
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    assert j.list_jobs() == [] and (tmp_path / "data" / "mdm.db.corrupt").exists()
    j.propose(report())  # and the fresh db works
    assert len(J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None).list_proposals()) == 1


def test_propose_rejects_error_report(mk):
    j, _, _ = mk()
    with pytest.raises(ValueError):
        j.propose({"error": "blocked: x"})
    with pytest.raises(ValueError):
        j.propose(report(), files=["a"])


def test_unsubscribe_and_order(mk):
    j, _, _ = mk()
    q = j.subscribe(); j.unsubscribe(q)
    a = j.propose(report()); b = j.propose(report())
    assert q.empty() and [p["id"] for p in j.list_proposals()] == [b["id"], a["id"]]


# ---- robustness: odd state files, failing saves, bounded growth ----

def test_job_without_state_loads(tmp_path):
    lib = J.library.Library(tmp_path / "data" / "mdm.db")
    lib.save_state([], [{"id": "j_1"}])
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    assert [x["id"] for x in j.list_jobs()] == ["j_1"]


def test_rows_with_wrong_shape_are_skipped(tmp_path):
    lib = J.library.Library(tmp_path / "data" / "mdm.db")
    lib.save_state([{"id": "p_ok", "status": "rejected"}], [{"id": "j_ok", "state": "done"}])
    for sql in ("INSERT INTO proposals(id,body) VALUES('p_bad','[1,2]')", "INSERT INTO jobs(id,body) VALUES('j_bad','nope')",
                "INSERT INTO jobs(id,body) VALUES('j_none',NULL)"):
        lib._db.execute(sql)
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    assert [x["id"] for x in j.list_jobs()] == ["j_ok"] and [x["id"] for x in j.list_proposals()] == ["p_ok"]


def test_save_failure_in_approve_still_starts(mk, monkeypatch):
    j, r, notes = mk()
    p = j.propose(report())
    monkeypatch.setattr(j, "_write", lambda: (_ for _ in ()).throw(OSError("disk full")))
    job = j.approve(p["id"])  # does not raise
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    assert len(r.calls) == 1 and j.list_proposals()[0]["status"] == "approved"
    assert notes == [("Download finished", "F")]


def test_save_failure_in_worker_still_finishes(mk, monkeypatch):
    j, _, notes = mk()
    q = j.subscribe()
    p = j.propose(report())
    real, n = j._write, [0]

    def flaky():
        n[0] += 1
        if n[0] >= 2:  # approve's save works, the worker's final save fails
            raise OSError("disk full")
        real()
    monkeypatch.setattr(j, "_write", flaky)
    job = j.approve(p["id"])
    assert wait(lambda: notes == [("Download finished", "F")])
    assert j.get_job(job["id"])["state"] == "done"
    assert "job_done" in [e["type"] for e in list(q.queue)]


def test_growth_bounded(mk):
    j, _, _ = mk()
    keep = J.KEEP_FINISHED
    for i in range(keep + 20):
        j._proposals[f"p_{i:08x}"] = {"id": f"p_{i:08x}", "status": "rejected"}
        j._jobs[f"j_{i:08x}"] = {"id": f"j_{i:08x}", "state": "done"}
    j._proposals["p_pend0000"] = {"id": "p_pend0000", "status": "pending"}
    j._proposals = {"p_pend0001": {"id": "p_pend0001", "status": "pending"}, **j._proposals}  # oldest of all
    j._jobs["j_run00000"] = {"id": "j_run00000", "state": "running"}
    j._jobs = {"j_que00000": {"id": "j_que00000", "state": "queued"}, **j._jobs}
    j._save()
    props, jobs = j.lib.load_state()  # what is on disk, not the in-memory dicts
    pids = [p["id"] for p in props]
    jids = [x["id"] for x in jobs]
    assert len(pids) == keep + 2 and {"p_pend0000", "p_pend0001"} <= set(pids)
    assert len(jids) == keep + 2 and {"j_run00000", "j_que00000"} <= set(jids)
    assert "p_00000000" not in pids and f"p_{keep + 19:08x}" in pids  # oldest finished dropped, newest kept
    assert "j_00000000" not in jids and f"j_{keep + 19:08x}" in jids


# ---- library items, posters, resume, chats ----

def drain(q):
    out = []
    while not q.empty():
        out.append(q.get())
    return out


def test_done_builds_item_and_emits_it(mk):
    j, _, _ = mk()
    q = j.subscribe()
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    d = j.get_job(job["id"])
    assert d["item_id"].startswith("i_")
    it = j.get_item(d["item_id"])
    assert it["job_id"] == job["id"] and it["title"] == "F" and it["kind"] == "other" and it["size"] == 10
    assert it["source_url"] == "https://example.com/f.bin" and it["poster"] is False and it["main"] == 0
    assert it["files"] == [{"index": 0, "path": "f.bin", "size": 10, "mime": "application/octet-stream", "playable": False}]
    assert j.list_items() == [it]
    evs = drain(q)
    items = [e for e in evs if e["type"] == "item"]
    assert items and items[0]["item"]["id"] == d["item_id"]
    types = [e["type"] for e in evs]
    assert types.index("item") < types.index("job_done")  # a client that sees item_id already has the item
    assert j.item_file(d["item_id"]).read_bytes() == b"0123456789"


def test_item_main_is_largest_and_skips_state_and_hidden(mk):
    class Multi(Runner):
        def __call__(self, source, dest_dir, files=None, on_event=None, cancel=None, **kw):
            d = Path(dest_dir)
            (d / "sub").mkdir(parents=True, exist_ok=True)
            (d / "a.mp4").write_bytes(b"1" * 5)
            (d / "sub" / "b.mkv").write_bytes(b"2" * 50)
            (d / "a.mp4.mdm.json").write_text("{}")
            (d / ".hidden").write_text("x")
            return {"path": str(d), "size": 55}
    j, _, _ = mk(Multi())
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    it = j.get_item(j.get_job(job["id"])["item_id"])
    assert [f["path"] for f in it["files"]] == ["a.mp4", "sub/b.mkv"] and it["main"] == 1 and it["size"] == 55
    assert it["kind"] == "video" and [f["playable"] for f in it["files"]] == [True, False]  # mkv: not playable


def test_poster_made_on_worker_and_item_emitted_again(mk, monkeypatch):
    def fake(item, folder, dest, image_url=None):
        assert image_url == "https://img.example/p.png" and Path(folder, item["files"][0]["path"]).is_file()
        Path(dest).write_bytes(b"\xff\xd8\xff jpeg")
        return True
    monkeypatch.setattr(J.poster, "make_poster", fake)
    j, _, _ = mk()
    q = j.subscribe()
    rep = dict(report(), image="https://img.example/p.png")
    job = j.approve(j.propose(rep)["id"])
    iid = None
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    iid = j.get_job(job["id"])["item_id"]
    assert wait(lambda: j.get_item(iid)["poster"] is True)
    assert j.poster_path(iid).read_bytes().startswith(b"\xff\xd8")
    assert [e["item"]["poster"] for e in drain(q) if e["type"] == "item"] == [False, True]


def test_no_poster_no_second_item_event(mk, monkeypatch):
    monkeypatch.setattr(J.poster, "make_poster", lambda *a, **k: False)
    j, _, _ = mk()
    q = j.subscribe()
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    time.sleep(0.2)
    iid = j.get_job(job["id"])["item_id"]
    assert j.poster_path(iid) is None and j.get_item(iid)["poster"] is False
    assert [e["type"] for e in drain(q)].count("item") == 1


def test_done_without_files_makes_no_item(mk):
    class Empty(Runner):
        def __call__(self, *a, **k):
            return {"path": None, "size": None}
    j, _, _ = mk(Empty())
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    assert j.get_job(job["id"])["item_id"] is None and j.list_items() == []


def test_item_file_refuses_escapes(mk, tmp_path):
    j, _, _ = mk()
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    iid = j.get_job(job["id"])["item_id"]
    folder = tmp_path / "dl" / job["id"]
    (tmp_path / "secret.txt").write_text("top secret")
    # a stored path that climbs out
    it = j.get_item(iid)
    for bad in ("../../secret.txt", "../" + job["id"] + "/../../secret.txt", str(tmp_path / "secret.txt")):
        it["files"][0]["path"] = bad
        j.lib.put_item(it)
        with pytest.raises(J.NotFound):
            j.item_file(iid)
    # a symlink inside the folder pointing out
    (folder / "link.txt").symlink_to(tmp_path / "secret.txt")
    it["files"][0]["path"] = "link.txt"
    j.lib.put_item(it)
    with pytest.raises(J.NotFound):
        j.item_file(iid, 0)
    # a symlinked sub-folder
    (folder / "d").symlink_to(tmp_path)
    it["files"][0]["path"] = "d/secret.txt"
    j.lib.put_item(it)
    with pytest.raises(J.NotFound):
        j.item_file(iid, 0)
    # a symlinked job folder
    it["files"][0]["path"] = "f.bin"
    j.lib.put_item(it)
    assert j.item_file(iid).name == "f.bin"
    shutil.move(str(folder), str(tmp_path / "moved"))
    folder.symlink_to(tmp_path / "moved")
    with pytest.raises(J.NotFound):
        j.item_file(iid)


def test_item_file_index_and_unknown(mk):
    j, _, _ = mk()
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    iid = j.get_job(job["id"])["item_id"]
    assert j.item_file(iid, 0) == j.item_file(iid, None)
    for bad in (1, -1, "0", True, 1.5):
        with pytest.raises(J.NotFound):
            j.item_file(iid, bad)
    for fn in (j.get_item, j.item_file, j.poster_path, j.delete_item):
        with pytest.raises(J.NotFound):
            fn("i_00000000")


def test_delete_item_removes_files_poster_row(mk, tmp_path, monkeypatch):
    monkeypatch.setattr(J.poster, "make_poster", lambda item, folder, dest, image_url=None: Path(dest).write_bytes(b"x") or True)
    j, _, _ = mk()
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    iid = j.get_job(job["id"])["item_id"]
    assert wait(lambda: j.poster_path(iid) is not None)
    j.delete_item(iid)
    assert not (tmp_path / "dl" / job["id"]).exists() and j.list_items() == []
    assert not (j.posters_dir / f"{iid}.jpg").exists() and j.get_job(job["id"])["item_id"] is None
    with pytest.raises(J.NotFound):
        j.get_item(iid)


def test_items_survive_restart_and_no_bytes_in_db(tmp_path):
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    iid = j.get_job(job["id"])["item_id"]
    j2 = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    assert j2.get_item(iid)["title"] == "F" and j2.get_job(job["id"])["item_id"] == iid
    raw = (tmp_path / "data" / "mdm.db").read_bytes()
    for ext in ("-wal",):
        w = tmp_path / "data" / ("mdm.db" + ext)
        raw += w.read_bytes() if w.exists() else b""
    assert b"0123456789" not in raw  # the file's bytes are on disk, never in the catalogue


def test_library_dir_layout(tmp_path, monkeypatch):
    for k in ("DATA_DIR", "DOWNLOAD_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path / "lib"))
    j = J.Jobs(notifier=lambda *a: None)
    assert j.library_dir == (tmp_path / "lib").resolve() and (tmp_path / "lib" / "mdm.db").is_file()
    assert j.download_dir == (tmp_path / "lib" / "files").resolve()
    # the old settings still win
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "f"))
    j = J.Jobs(notifier=lambda *a: None)
    assert (tmp_path / "d" / "mdm.db").is_file() and j.download_dir == (tmp_path / "f").resolve()
    assert j.library_dir == (tmp_path / "lib").resolve()
    monkeypatch.delenv("LIBRARY_DIR")
    assert J.Jobs(tmp_path / "x", tmp_path / "y").library_dir == (tmp_path / "x").resolve()  # never ~/MDM Library in tests


def interrupted(tmp_path, with_state):
    """A database left by a crashed server: a running job (and optionally MDM's state file)."""
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner("block"), notifier=lambda *a: None)
    p = j.propose(report(), files=[2], checksum="sha256:" + "ab" * 32)
    job = j.approve(p["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "running")
    folder = tmp_path / "dl" / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    if with_state:
        (folder / "f.bin.mdm.json").write_text("{}")
    return j, job


def test_interrupted_job_with_state_file_is_resumed(tmp_path):
    j, job = interrupted(tmp_path, with_state=True)
    r = Runner()
    j2 = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=r, notifier=lambda *a: None)
    assert wait(lambda: j2.get_job(job["id"])["state"] == "done")
    d = j2.get_job(job["id"])
    assert d["error"] is None and d["item_id"]
    assert len(r.calls) == 1 and r.calls[0][0] == "https://example.com/f.bin" and r.calls[0][1].endswith(job["id"])
    assert r.kw[0]["resume"].endswith(f"{job['id']}/f.bin.mdm.json")
    assert "checksum" in r.kw[0]
    j._cancels[job["id"]].set()


def test_interrupted_job_without_state_file_errors(tmp_path):
    j, job = interrupted(tmp_path, with_state=False)
    r = Runner()
    j2 = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=r, notifier=lambda *a: None)
    d = j2.get_job(job["id"])
    assert d["state"] == "error" and d["error"] == "interrupted" and r.calls == []
    j._cancels[job["id"]].set()


def test_resumed_job_can_be_cancelled(tmp_path):
    j, job = interrupted(tmp_path, with_state=True)
    j2 = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner("block"), notifier=lambda *a: None)
    assert wait(lambda: j2.get_job(job["id"])["state"] == "running")
    assert j2.cancel(job["id"])["state"] == "cancelled"
    j._cancels[job["id"]].set()


def test_proposal_options(mk):
    j, r, _ = mk()
    plain = j.propose(report())
    assert not {"checksum", "mode", "height", "subs"} & set(plain) and plain["chat_id"] is None
    p = j.propose(report(), checksum="sha256:" + "ab" * 32, mode="video", height=720, subs="en,fr")
    assert (p["checksum"][:7], p["mode"], p["height"], p["subs"]) == ("sha256:", "video", 720, "en,fr")
    job = j.approve(p["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    assert r.kw[-1] == {"checksum": "sha256:" + "ab" * 32, "mode": "video", "height": 720, "subs": "en,fr"}
    j.approve(plain["id"])
    assert wait(lambda: len(r.kw) == 2) and r.kw[1] == {}  # a plain proposal passes nothing extra to the runner
    for bad in ({"checksum": "x; rm"}, {"mode": "audio"}, {"height": 0}, {"height": "720"}, {"height": True},
                {"subs": "--help"}, {"subs": "en;ls"}):
        with pytest.raises(ValueError):
            j.propose(report(), **bad)
    with pytest.raises(ValueError):
        j.propose(dict(report(), kind="magnet"), mode="video")


def test_chats(mk):
    j, _, _ = mk()
    c = j.create_chat()
    assert c["id"].startswith("c_") and c["title"] == "New download" and c["created"] == c["updated"]
    assert j.get_chat(c["id"]) == {"chat": c, "events": []}
    j.append_chat_event(c["id"], {"type": "user", "text": "  find   me\nthe film " + "x" * 100})
    j.append_chat_event(c["id"], {"type": "text", "text": "ok"})
    j.append_chat_event(c["id"], {"type": "user", "text": "second"})
    g = j.get_chat(c["id"])
    assert g["chat"]["title"] == ("find me the film " + "x" * 100)[:60] and len(g["chat"]["title"]) == 60
    assert [e["type"] for e in g["events"]] == ["user", "text", "user"] and g["events"][0]["text"].startswith("  find")
    # unknown ids are created on first use; an explicit title is kept
    j.append_chat_event("web-session-1", {"type": "user", "text": "hello"})
    assert j.get_chat("web-session-1")["chat"]["title"] == "hello"
    named = j.create_chat("Mine")
    j.append_chat_event(named["id"], {"type": "user", "text": "hi"})
    assert j.get_chat(named["id"])["chat"]["title"] == "Mine"
    ids = [x["id"] for x in j.list_chats()]
    assert set(ids) == {c["id"], "web-session-1", named["id"]}
    j.append_chat_event(c["id"], {"type": "text", "text": "bump"})  # newest updated first
    assert j.list_chats()[0]["id"] == c["id"]
    j.delete_chat(c["id"])
    with pytest.raises(J.NotFound):
        j.get_chat(c["id"])
    with pytest.raises(J.NotFound):
        j.delete_chat(c["id"])
    assert j.lib._db.execute("SELECT COUNT(*) FROM chat_events WHERE chat_id=?", (c["id"],)).fetchone()[0] == 0
    for bad in ("", "x" * 65, None, 5):
        with pytest.raises(ValueError):
            j.append_chat_event(bad, {"type": "user"})
    with pytest.raises(ValueError):
        j.append_chat_event("c", {"text": "no type"})


def test_chat_events_persist_across_restart_and_keep_order(tmp_path):
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    for i in range(12):
        j.append_chat_event("c1", {"type": "text", "text": str(i)})
    j2 = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    assert [e["text"] for e in j2.get_chat("c1")["events"]] == [str(i) for i in range(12)]


def test_proposal_event_links_chat(mk):
    j, _, _ = mk()
    p = j.propose(report())
    j.append_chat_event("c_one", {"type": "proposal", "id": p["id"]})
    assert j.list_proposals()[0]["chat_id"] == "c_one"
    j.append_chat_event("c_one", {"type": "proposal", "id": "p_nope"})  # unknown proposal: ignored
    assert j.get_chat("c_one")["events"][0] == {"type": "proposal", "id": p["id"]}
    # the link is persisted
    row = j.lib._db.execute("SELECT chat_id FROM proposals WHERE id=?", (p["id"],)).fetchone()
    assert row[0] == "c_one"
    j.delete_chat("c_one")  # deleting the chat keeps proposals
    assert len(j.list_proposals()) == 1


def test_foreign_keys_and_wal(mk):
    j, _, _ = mk()
    assert j.lib._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert j.lib._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with pytest.raises(sqlite3.IntegrityError):
        j.lib._db.execute("INSERT INTO chat_events(chat_id,seq,body) VALUES('nochat',1,'{}')")


def test_real_cli_video_mode_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("MDM_CLI", str(Path(__file__).parent / "fakes" / "fake_mdm.py"))
    monkeypatch.setenv("FAKE_MDM_ARGS", str(tmp_path / "args.json"))
    monkeypatch.delenv("FAKE_MDM_MODE", raising=False)
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", notifier=lambda *a: None)
    job = j.approve(j.propose(report(url="https://video.example/watch?v=1"), mode="video", height=720, subs="en")["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    a = json.loads((tmp_path / "args.json").read_text())
    assert a[1] == "video-get" and a[a.index("--quality") + 1] == "720" and a[a.index("--subs") + 1] == "en"
    it = j.get_item(j.get_job(job["id"])["item_id"])
    assert [f["path"] for f in it["files"]] == ["video.mp4"] and it["kind"] == "video" and it["files"][0]["playable"]


def test_real_cli_resume_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("MDM_CLI", str(Path(__file__).parent / "fakes" / "fake_mdm.py"))
    monkeypatch.setenv("FAKE_MDM_ARGS", str(tmp_path / "args.json"))
    j, job = interrupted(tmp_path, with_state=True)  # leaves dl/<job>/f.bin.mdm.json behind
    (tmp_path / "dl" / job["id"] / "f.bin").write_bytes(b"part")
    j2 = J.Jobs(tmp_path / "data", tmp_path / "dl", notifier=lambda *a: None)  # real mdm.run
    assert wait(lambda: j2.get_job(job["id"])["state"] == "done")
    a = json.loads((tmp_path / "args.json").read_text())
    assert a[1] == "resume" and a[2].endswith("f.bin.mdm.json")
    assert j2.item_file(j2.get_job(job["id"])["item_id"]).read_bytes() == b"r" * 100
    j._cancels[job["id"]].set()


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")
def test_video_download_gets_a_real_poster(tmp_path):
    import subprocess

    class VideoRunner(Runner):
        def __call__(self, source, dest_dir, files=None, on_event=None, cancel=None, **kw):
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                            "-pix_fmt", "yuv420p", str(Path(dest_dir) / "clip.mp4")], check=True)
            return {"path": str(Path(dest_dir) / "clip.mp4"), "size": 1}
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=VideoRunner(), notifier=lambda *a: None)
    job = j.approve(j.propose(report())["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    iid = j.get_job(job["id"])["item_id"]
    assert wait(lambda: j.get_item(iid)["poster"] is True)
    assert j.poster_path(iid).read_bytes()[:3] == b"\xff\xd8\xff" and j.get_item(iid)["kind"] == "video"
