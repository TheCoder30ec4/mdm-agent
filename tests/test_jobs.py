import json
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
        self.mode, self.calls = mode, []

    def __call__(self, source, dest_dir, files=None, on_event=None, cancel=None):
        self.calls.append((source, dest_dir, files))
        if self.mode == "error":
            raise mdm.MdmError("boom")
        if self.mode == "block":
            on_event({"downloaded": 1, "size": 10, "speed": 1})
            cancel.wait(10)
            raise mdm.MdmError("cancelled")
        for d in (3, 6, 10):
            on_event({"downloaded": d, "size": 10, "speed": 2})
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
    assert r.calls == [] and started == [] and j.list_jobs() == []
    assert [m for m in dir(j) if not m.startswith("_") and "start" in m.lower()] == []
    job = j.approve(b["id"])
    assert wait(lambda: j.get_job(job["id"])["state"] == "done")
    assert len(r.calls) == 1 and len(started) == 1


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
    data = json.loads((tmp_path / "data" / "jobs.json").read_text())
    assert data["jobs"][0]["state"] == "error"
    j._cancels[job["id"]].set()


def test_corrupt_state_file(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "jobs.json").write_text("{nope")
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    assert j.list_jobs() == [] and (tmp_path / "data" / "jobs.json.corrupt").exists()


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
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "jobs.json").write_text(json.dumps({"proposals": [], "jobs": [{"id": "j_1"}]}))
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    assert [x["id"] for x in j.list_jobs()] == ["j_1"]


@pytest.mark.parametrize("content", [[], {"jobs": 5}, {"proposals": [{"x": 1}]}, {"jobs": ["j"]}, {"jobs": [{"state": "running"}]}])
def test_wrong_shape_state_file(tmp_path, content):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "jobs.json").write_text(json.dumps(content))
    j = J.Jobs(tmp_path / "data", tmp_path / "dl", runner=Runner(), notifier=lambda *a: None)
    assert j.list_jobs() == [] and j.list_proposals() == []
    assert (tmp_path / "data" / "jobs.json.corrupt").exists()


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
    data = json.loads((j._file).read_text())
    pids = [p["id"] for p in data["proposals"]]
    jids = [x["id"] for x in data["jobs"]]
    assert len(pids) == keep + 2 and {"p_pend0000", "p_pend0001"} <= set(pids)
    assert len(jids) == keep + 2 and {"j_run00000", "j_que00000"} <= set(jids)
    assert "p_00000000" not in pids and f"p_{keep + 19:08x}" in pids  # oldest finished dropped, newest kept
    assert "j_00000000" not in jids and f"j_{keep + 19:08x}" in jids
