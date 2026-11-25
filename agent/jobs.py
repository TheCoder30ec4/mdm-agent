"""Proposals and download jobs. approve() is the ONLY thing that starts a download."""
import copy
import logging
import os
import queue
import shutil
import threading
import time
from pathlib import Path

from agent import library, mdm, notify, poster
from agent.library import new_id as _id
from agent.library import now as _now

log = logging.getLogger(__name__)
KEEP_FINISHED = 300  # finished proposals/jobs kept on save; pending/queued/running are never dropped


class NotFound(Exception):
    pass


class NeedsConfirm(Exception):
    pass


class BadState(Exception):
    pass


class Jobs:
    """Proposals, jobs, library items and chats. Proposals and jobs are kept in memory and written through to
    mdm.db on every state change; items and chats live in the db only."""

    def __init__(self, data_dir=None, download_dir=None, runner=mdm.run, notifier=notify.notify, library_dir=None):
        # LIBRARY_DIR holds mdm.db, files/<job id>/ and posters/; DATA_DIR / DOWNLOAD_DIR still override the db
        # folder and files/. An explicit data_dir (tests) stands in for the library too, so nothing leaks to $HOME.
        root = library_dir or data_dir or os.environ.get("LIBRARY_DIR") or Path.home() / "MDM Library"
        self.library_dir = Path(root).resolve()
        self.data_dir = Path(data_dir or os.environ.get("DATA_DIR") or self.library_dir).resolve()
        self.download_dir = Path(download_dir or os.environ.get("DOWNLOAD_DIR") or self.library_dir / "files").resolve()
        self.posters_dir = self.data_dir / "posters"  # beside the catalogue (the library folder unless DATA_DIR moves it)
        self._runner = runner
        self._notifier = notifier
        self._lock = threading.RLock()
        self._proposals: dict[str, dict] = {}
        self._jobs: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._subs: list[queue.Queue] = []
        self.lib = library.Library(self.data_dir / "mdm.db")
        self._load()

    # ---- persistence
    def _load(self):
        props, jobs = self.lib.load_state()
        self._proposals = {p["id"]: p for p in props}
        self._jobs = {j["id"]: j for j in jobs}
        resumes = []
        for j in self._jobs.values():
            if j.get("state") in ("queued", "running"):
                state = next(iter(sorted((self.download_dir / j["id"]).glob("*.mdm.json"))), None)
                if state is None:
                    j.update(state="error", error="interrupted", finished=_now())
                else:  # MDM left a state file: continue the download instead of failing it
                    j.update(state="queued", error=None, speed=0)
                    resumes.append((j, state))
        self._save()
        for j, state in resumes:
            p = self._proposals.get(j.get("proposal_id")) or {}
            self._start(j["id"], list(p.get("files") or []), self._opts(p), resume=state)

    @staticmethod
    def _opts(p: dict) -> dict:
        """What a worker needs from the proposal (it may be pruned while the job runs)."""
        o = {k: p[k] for k in ("checksum", "mode", "height", "subs") if p.get(k) is not None}
        img = (p.get("report") or {}).get("image")
        if isinstance(img, str) and img.startswith("https://"):
            o["image"] = img
        return o

    def _start(self, jid, files, opts, resume=None):
        cancel = self._cancels[jid] = threading.Event()
        t = self._threads[jid] = threading.Thread(target=self._work, args=(jid, files, cancel, opts, resume), daemon=True)
        t.start()

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
            log.exception("could not save %s", self.lib.path)

    def _write(self):
        # ponytail: rewrites every proposal/job row per save (at most ~600 rows); per-row upserts if that ever shows
        self.lib.save_state(list(self._proposals.values()), list(self._jobs.values()))

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
    def propose(self, report: dict, files: list[int] | None = None, note: str = "", *, checksum: str | None = None,
                mode: str | None = None, height: int | None = None, subs: str | None = None) -> dict:
        """checksum, mode ("video"), height and subs are optional and only present on the proposal when given."""
        if not isinstance(report, dict) or not report.get("url") or report.get("error"):
            raise ValueError("report must be a successful inspect_link report")
        files = list(files or [])
        if not all(type(i) is int and i >= 0 for i in files):
            raise ValueError("files must be non-negative integers")
        try:
            mdm.check_options(checksum, mode, height, subs)
        except mdm.MdmError as e:
            raise ValueError(str(e)) from None
        if report.get("kind") == "magnet" and (checksum or mode):
            raise ValueError("checksum and video mode do not apply to magnet links")
        prop = {"id": _id("p"), "status": "pending", "created": _now(), "report": copy.deepcopy(report),
                "files": files, "note": str(note or ""), "job_id": None, "chat_id": None}
        prop.update({k: v for k, v in (("checksum", checksum), ("mode", mode), ("height", height), ("subs", subs))
                     if v is not None})
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
                   "size": rep.get("size"), "speed": 0, "path": None, "error": None, "item_id": None,
                   "created": _now(), "finished": None}
            p["status"], p["job_id"] = "approved", job["id"]
            self._jobs[job["id"]] = job
            self._save()
            self._emit({"type": "proposal", "proposal": copy.deepcopy(p)})
            self._emit_job(job)
            self._start(job["id"], list(p["files"]), self._opts(p))
            return copy.deepcopy(job)

    # ---- running
    def _work(self, jid, files, cancel, opts=None, resume=None):
        opts = opts or {}
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
            kw.update({k: opts[k] for k in ("checksum", "mode", "height", "subs") if k in opts})  # only when asked for
            if resume:
                kw["resume"] = str(resume)
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
            item = self._make_item(job) if job["state"] == "done" else None
            self._save()
            if item:  # before job_done: a client that sees item_id already has the item
                self._emit({"type": "item", "item": copy.deepcopy(item)})
            self._emit_job(job, done=True)
            snap = copy.deepcopy(job)
        if item:
            threading.Thread(target=self._poster, args=(item, opts.get("image")), daemon=True).start()
        name = snap["title"] or snap["url"]
        try:
            if snap["state"] == "done":
                self._notifier("Download finished", name)
            elif snap["state"] == "error":
                self._notifier("Download failed", f"{name}: {snap['error']}")
        except Exception:
            pass

    def _make_item(self, job):
        """The library Item for a finished job (None when it left no files). Never raises. Caller holds the lock."""
        try:
            item = library.build_item(job["id"], self.download_dir / job["id"], job.get("title"), job["url"])
            if item:
                self.lib.put_item(item)
                job["item_id"] = item["id"]
            return item
        except Exception:
            log.exception("could not build the library item for %s", job.get("id"))
            return None

    def _poster(self, item, image_url):
        """Worker thread: make the poster, then announce the item again with poster=true."""
        dest = self.posters_dir / f"{item['id']}.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not poster.make_poster(item, self.download_dir / item["job_id"], dest, image_url):
            return
        with self._lock:
            cur = self.lib.get_item(item["id"])
            if cur is None:  # deleted while the poster was being made
                dest.unlink(missing_ok=True)
                return
            cur["poster"] = True
            self.lib.put_item(cur)
            self._emit({"type": "item", "item": copy.deepcopy(cur)})

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

    # ---- library items (contracts/library.md, "Python interface")
    def list_items(self) -> list[dict]:
        return self.lib.list_items()

    def get_item(self, item_id: str) -> dict:
        it = self.lib.get_item(item_id)
        if it is None:
            raise NotFound(item_id)
        return it

    def _folder(self, item) -> Path:
        """files/<job id>/ resolved, and still inside the files folder (no symlinked folder)."""
        folder = (self.download_dir / item["job_id"]).resolve()
        if self.download_dir not in folder.parents:
            raise NotFound(item["id"])
        return folder

    def item_file(self, item_id: str, index: int | None = None) -> Path:
        """Absolute path of one of the item's files (default: main). The path comes from the stored list, never
        from a caller, and must resolve inside files/<job id>/ (no `..`, no symlink out)."""
        it = self.get_item(item_id)
        i = it["main"] if index is None else index
        if type(i) is not int or not 0 <= i < len(it["files"]):
            raise NotFound(f"{item_id} file {index}")
        folder = self._folder(it)
        p = (folder / it["files"][i]["path"]).resolve()
        if folder not in p.parents or not p.is_file():
            raise NotFound(f"{item_id} file {index}")
        return p

    def poster_path(self, item_id: str) -> Path | None:
        self.get_item(item_id)
        p = self.posters_dir / f"{item_id}.jpg"
        return p if p.is_file() else None

    def delete_item(self, item_id: str) -> None:
        """Removes files/<job id>/, the poster and the row (the job record stays, without its item)."""
        it = self.get_item(item_id)
        raw = self.download_dir / it["job_id"]
        try:
            if raw.is_symlink():
                raw.unlink()
            else:
                shutil.rmtree(self._folder(it), ignore_errors=True)
        except NotFound:
            pass  # a folder outside files/ is never touched
        (self.posters_dir / f"{item_id}.jpg").unlink(missing_ok=True)
        with self._lock:
            self.lib.delete_item(item_id)
            j = self._jobs.get(it["job_id"])
            if j and j.get("item_id") == item_id:
                j["item_id"] = None
                self._save()

    # ---- chats
    def create_chat(self, title: str | None = None) -> dict:
        return self.lib.create_chat(title)

    def list_chats(self) -> list[dict]:
        return self.lib.list_chats()

    def get_chat(self, chat_id: str) -> dict:
        c = self.lib.get_chat(chat_id)
        if c is None:
            raise NotFound(chat_id)
        return c

    def delete_chat(self, chat_id: str) -> None:
        if not self.lib.delete_chat(chat_id):
            raise NotFound(chat_id)

    def append_chat_event(self, chat_id: str, event: dict) -> None:
        """Creates the chat if missing; the first user message titles it; a proposal event links the proposal."""
        if not isinstance(chat_id, str) or not 0 < len(chat_id) <= 64:
            raise ValueError("bad chat id")
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("event must be an object with a type")
        self.lib.append_event(chat_id, event)
        if event["type"] == "proposal":
            with self._lock:
                p = self._proposals.get(event.get("id"))
                if p is not None:
                    p["chat_id"] = chat_id
                    self._save()
