"""Proposals and download jobs. approve() is the ONLY thing that starts a download."""
import copy
import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent import mdm, notify

log = logging.getLogger(__name__)
KEEP_FINISHED = 300  # finished proposals/jobs kept on save; pending/queued/running are never dropped


class NotFound(Exception):
    pass


class NeedsConfirm(Exception):
    pass


class BadState(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class Jobs:
    def __init__(self, data_dir=None, download_dir=None, runner=mdm.run, notifier=notify.notify):
        self.data_dir = Path(data_dir or os.environ.get("DATA_DIR") or "./data").resolve()
        self.download_dir = Path(download_dir or os.environ.get("DOWNLOAD_DIR") or "./downloads").resolve()
        self._runner = runner
        self._notifier = notifier
        self._lock = threading.RLock()
        self._proposals: dict[str, dict] = {}
        self._jobs: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._subs: list[queue.Queue] = []
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.data_dir / "jobs.json"
        self._load()

    # ---- persistence
    def _load(self):
        try:
            data = json.loads(self._file.read_text())
            self._proposals = {p["id"]: p for p in data.get("proposals", [])}
            self._jobs = {j["id"]: j for j in data.get("jobs", [])}
            changed = False
            for j in self._jobs.values():
                if j.get("state") in ("queued", "running"):
                    j.update(state="error", error="interrupted", finished=_now())
                    changed = True
        except FileNotFoundError:
            return
        except (ValueError, KeyError, TypeError, AttributeError):
            self._proposals, self._jobs = {}, {}
            self._file.replace(self._file.with_suffix(".json.corrupt"))  # keep it, start clean
            return
        if changed:
            self._save()

    def _prune(self):
        for d, key, active in ((self._proposals, "status", ("pending",)),
                               (self._jobs, "state", ("queued", "running"))):
            finished = [k for k, v in d.items() if v.get(key) not in active]  # dicts keep creation order
            for k in finished[:-KEEP_FINISHED]:
                del d[k]
                self._cancels.pop(k, None)
                self._threads.pop(k, None)

    def _save(self):
        """Persist; a failed write is logged, never raised, so state changes and workers carry on."""
        try:
            self._prune()
            self._write()
        except Exception:
            log.exception("could not save %s", self._file)

    def _write(self):
        tmp = self._file.with_name(self._file.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump({"proposals": list(self._proposals.values()), "jobs": list(self._jobs.values())}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._file)

    # ---- events
    def subscribe(self) -> "queue.Queue[dict]":
        q = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _emit(self, ev: dict):
        with self._lock:
            for q in self._subs:
                try:
                    q.put_nowait(copy.deepcopy(ev))
                except queue.Full:
                    pass  # slow consumer misses an event rather than blocking downloads

    def _emit_job(self, job, done=False):
        self._emit({"type": "job", "job": copy.deepcopy(job)})
        if done:
            self._emit({"type": "job_done", "job": copy.deepcopy(job)})

    # ---- proposals
    def propose(self, report: dict, files: list[int] | None = None, note: str = "") -> dict:
        if not isinstance(report, dict) or not report.get("url") or report.get("error"):
            raise ValueError("report must be a successful inspect_link report")
        files = list(files or [])
        if not all(type(i) is int and i >= 0 for i in files):
            raise ValueError("files must be non-negative integers")
        prop = {"id": _id("p"), "status": "pending", "created": _now(), "report": copy.deepcopy(report),
                "files": files, "note": str(note or ""), "job_id": None}
        with self._lock:
            self._proposals[prop["id"]] = prop
            self._save()
            self._emit({"type": "proposal", "proposal": copy.deepcopy(prop)})
            return copy.deepcopy(prop)

    def _proposal(self, pid):
        p = self._proposals.get(pid)
        if p is None:
            raise NotFound(pid)
        return p

    def reject(self, proposal_id: str) -> dict:
        with self._lock:
            p = self._proposal(proposal_id)
            if p["status"] != "pending":
                raise BadState(f"proposal is {p['status']}")
            p["status"] = "rejected"
            self._save()
            self._emit({"type": "proposal", "proposal": copy.deepcopy(p)})
            return copy.deepcopy(p)

    def approve(self, proposal_id: str, confirm_risky: bool = False) -> dict:
        with self._lock:
            p = self._proposal(proposal_id)
            if p["status"] != "pending":
                raise BadState(f"proposal is {p['status']}")
            rep = p["report"]
            if rep.get("safety", {}).get("level") == "risky" and not confirm_risky:
                raise NeedsConfirm("this download is risky; confirm_risky is required")
            job = {"id": _id("j"), "proposal_id": p["id"], "url": rep["url"],
                   "title": rep.get("title") or rep.get("filename"), "state": "queued", "downloaded": 0,
                   "size": rep.get("size"), "speed": 0, "path": None, "error": None, "created": _now(),
                   "finished": None}
            p["status"], p["job_id"] = "approved", job["id"]
            self._jobs[job["id"]] = job
            cancel = self._cancels[job["id"]] = threading.Event()
            t = self._threads[job["id"]] = threading.Thread(
                target=self._work, args=(job["id"], list(p["files"]), cancel), daemon=True)
            self._save()
            self._emit({"type": "proposal", "proposal": copy.deepcopy(p)})
            self._emit_job(job)
            t.start()
            return copy.deepcopy(job)

    # ---- running
    def _work(self, jid, files, cancel):
        job = self._jobs[jid]
        last = [0.0]

        def on_event(ev):
            with self._lock:
                for k in ("downloaded", "size", "speed", "connections", "phase", "peers", "peers_seen"):
                    if ev.get(k) is not None:
                        job[k] = ev[k]
                now = time.monotonic()
                if now - last[0] >= 1.0:
                    last[0] = now
                    self._emit_job(job)

        result, err = None, None
        with self._lock:
            if not cancel.is_set():
                job["state"] = "running"
                self._emit_job(job)
        try:
            if cancel.is_set():
                raise mdm.MdmError("cancelled")
            kw = {"download_dir": str(self.download_dir)} if self._runner is mdm.run else {}
            result = self._runner(job["url"], str(self.download_dir / jid), files=files or None,
                                  on_event=on_event, cancel=cancel, **kw)
        except mdm.MdmError as e:
            err = str(e) or "download failed"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        with self._lock:
            if err is None:
                job.update(state="done", path=(result or {}).get("path"), error=None, speed=0)
                if (result or {}).get("size") is not None:
                    job["size"] = job["downloaded"] = result["size"]
            elif cancel.is_set() or err == "cancelled":
                job.update(state="cancelled", error=None, speed=0)
            else:
                job.update(state="error", error=err, speed=0)
            job["finished"] = _now()
            self._save()
            self._emit_job(job, done=True)
            snap = copy.deepcopy(job)
        name = snap["title"] or snap["url"]
        try:
            if snap["state"] == "done":
                self._notifier("Download finished", name)
            elif snap["state"] == "error":
                self._notifier("Download failed", f"{name}: {snap['error']}")
        except Exception:
            pass

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            j = self._jobs.get(job_id)
            if j is None:
                raise NotFound(job_id)
            if j["state"] not in ("queued", "running"):
                raise BadState(f"job is {j['state']}")
            self._cancels[job_id].set()
            t = self._threads.get(job_id)
        if t:
            t.join(5)  # let the worker settle so the caller sees the final state
        return self.get_job(job_id)

    # ---- queries
    def get_job(self, job_id: str) -> dict:
        with self._lock:
            j = self._jobs.get(job_id)
            if j is None:
                raise NotFound(job_id)
            return copy.deepcopy(j)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(j) for j in reversed(list(self._jobs.values()))]

    def list_proposals(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(p) for p in reversed(list(self._proposals.values()))]
