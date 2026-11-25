"""SQLite catalogue (mdm.db): proposals, jobs, library items, chats. contracts/library.md is the source of truth.
Only metadata lives here; file bytes stay on disk under files/<job id>/."""
import json
import logging
import mimetypes
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
DEFAULT_TITLE = "New download"
TITLE_MAX = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, status TEXT, created TEXT, chat_id TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, proposal_id TEXT, state TEXT, created TEXT, finished TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, job_id TEXT UNIQUE, title TEXT, kind TEXT, size INTEGER,
                                 created TEXT, source_url TEXT, poster TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY, title TEXT, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS chat_events(chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE, seq INTEGER,
                                       body TEXT, PRIMARY KEY(chat_id, seq));
"""


def now(precise: bool = False) -> str:
    """ISO-8601 UTC. Chats use microseconds so "newest updated first" is not a tie within one second."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds" if precise else "seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ---- items: what a finished download looks like ------------------------------------------------

# explicit table first: the platform's mime database differs per OS (.mkv, .opus ... are not everywhere)
MIME = {".mp4": "video/mp4", ".m4v": "video/x-m4v", ".webm": "video/webm", ".mov": "video/quicktime",
        ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv", ".ts": "video/mp2t",
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".opus": "audio/opus",
        ".wav": "audio/wav", ".flac": "audio/flac", ".epub": "application/epub+zip",
        ".7z": "application/x-7z-compressed", ".rar": "application/vnd.rar", ".iso": "application/x-iso9660-image",
        ".cbz": "application/vnd.comicbook+zip", ".cbr": "application/vnd.comicbook-rar"}
PLAYABLE = {".mp4", ".m4v", ".webm", ".mov", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac"}
ARCHIVE = {"application/zip", "application/x-tar", "application/gzip", "application/x-gzip", "application/x-7z-compressed",
           "application/vnd.rar", "application/x-bzip2", "application/x-xz", "application/x-iso9660-image",
           "application/x-apple-diskimage"}
DOCUMENT = {"application/pdf", "application/epub+zip", "application/msword", "application/rtf", "application/json",
            "application/vnd.comicbook+zip", "application/vnd.comicbook-rar"}


def mime_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def kind_of(mime: str) -> str:
    major = mime.split("/")[0]
    if major in ("video", "audio", "image"):
        return major
    if mime in ARCHIVE:
        return "archive"
    if major == "text" or mime in DOCUMENT or mime.startswith("application/vnd."):
        return "document"
    return "other"


def scan_files(folder: Path) -> list[dict]:
    """Regular files under folder (not symlinks, no hidden files, no *.mdm.json state files), sorted by path."""
    out = []
    for root, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for n in sorted(names):
            p = Path(root, n)
            if n.startswith(".") or n.endswith(".mdm.json") or p.is_symlink() or not p.is_file():
                continue
            rel = p.relative_to(folder).as_posix()
            out.append({"index": len(out), "path": rel, "size": p.stat().st_size, "mime": mime_of(n),
                        "playable": os.path.splitext(n)[1].lower() in PLAYABLE})
    return out


def build_item(job_id: str, folder: Path, title: str | None, source_url: str, created: str | None = None):
    """The Item (contracts/library.md) for a finished job, or None when it left no files."""
    files = scan_files(Path(folder))
    if not files:
        return None
    main = max(files, key=lambda f: f["size"])["index"]  # max keeps the first of equal sizes
    return {"id": new_id("i"), "job_id": job_id, "title": title or Path(files[main]["path"]).name,
            "kind": kind_of(files[main]["mime"]), "size": sum(f["size"] for f in files), "created": created or now(),
            "source_url": source_url, "poster": False, "files": files, "main": main}


# ---- the store ---------------------------------------------------------------------------------

def _loads(s):
    try:
        v = json.loads(s)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


class Library:
    """One SQLite connection shared by all threads (check_same_thread=False) behind a lock; WAL, foreign keys on."""

    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._db = self._open()
        except sqlite3.DatabaseError:  # not a database: keep the file aside, start clean
            self.path.replace(self.path.with_name(self.path.name + ".corrupt"))
            self._db = self._open()

    def _open(self):
        db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)  # autocommit; BEGIN by hand
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
        except sqlite3.DatabaseError:
            db.close()
            raise
        return db

    def _tx(self):
        return _Tx(self)

    # ---- proposals + jobs (Jobs keeps them in memory and writes them through here)
    def load_state(self) -> tuple[list[dict], list[dict]]:
        """(proposals, jobs) in creation order; rows whose body is not a JSON object are skipped."""
        with self._lock:
            ps = self._db.execute("SELECT body FROM proposals ORDER BY rowid").fetchall()
            js = self._db.execute("SELECT body FROM jobs ORDER BY rowid").fetchall()
        return ([p for p in (_loads(r[0]) for r in ps) if p and p.get("id")],
                [j for j in (_loads(r[0]) for r in js) if j and j.get("id")])

    def save_state(self, proposals, jobs) -> None:
        """Make the tables hold exactly these (upsert keeps row order; the rest are deleted)."""
        with self._tx() as db:
            for p in proposals:
                db.execute("INSERT INTO proposals(id,status,created,chat_id,body) VALUES(?,?,?,?,?) ON CONFLICT(id) DO "
                           "UPDATE SET status=excluded.status, chat_id=excluded.chat_id, body=excluded.body",
                           (p["id"], p.get("status"), p.get("created"), p.get("chat_id"), json.dumps(p)))
            for j in jobs:
                db.execute("INSERT INTO jobs(id,proposal_id,state,created,finished,body) VALUES(?,?,?,?,?,?) ON CONFLICT(id) "
                           "DO UPDATE SET state=excluded.state, finished=excluded.finished, body=excluded.body",
                           (j["id"], j.get("proposal_id"), j.get("state"), j.get("created"), j.get("finished"), json.dumps(j)))
            for table, rows in (("proposals", proposals), ("jobs", jobs)):
                keep = {r["id"] for r in rows}
                for (i,) in db.execute(f"SELECT id FROM {table}").fetchall():
                    if i not in keep:
                        db.execute(f"DELETE FROM {table} WHERE id=?", (i,))

    # ---- items
    def put_item(self, item: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO items(id,job_id,title,kind,size,created,source_url,poster,body) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, poster=excluded.poster, body=excluded.body",
                (item["id"], item["job_id"], item["title"], item["kind"], item["size"], item["created"],
                 item["source_url"], f"{item['id']}.jpg" if item.get("poster") else None, json.dumps(item)))

    def get_item(self, item_id):
        with self._lock:
            r = self._db.execute("SELECT body FROM items WHERE id=?", (item_id,)).fetchone()
        return _loads(r[0]) if r else None

    def list_items(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT body FROM items ORDER BY created DESC, rowid DESC").fetchall()
        return [i for i in (_loads(r[0]) for r in rows) if i]

    def delete_item(self, item_id) -> None:
        with self._lock:
            self._db.execute("DELETE FROM items WHERE id=?", (item_id,))

    # ---- chats
    def create_chat(self, title=None, chat_id=None) -> dict:
        t = now(True)
        chat = {"id": chat_id or new_id("c"), "title": title or DEFAULT_TITLE, "created": t, "updated": t}
        with self._lock:
            self._db.execute("INSERT INTO chats(id,title,created,updated) VALUES(?,?,?,?)",
                             (chat["id"], chat["title"], t, t))
        return chat

    def _chat(self, row):
        return {"id": row[0], "title": row[1], "created": row[2], "updated": row[3]} if row else None

    def get_chat(self, chat_id):
        """{"chat", "events"} or None."""
        with self._lock:
            c = self._chat(self._db.execute("SELECT id,title,created,updated FROM chats WHERE id=?", (chat_id,)).fetchone())
            if c is None:
                return None
            evs = self._db.execute("SELECT body FROM chat_events WHERE chat_id=? ORDER BY seq", (chat_id,)).fetchall()
        return {"chat": c, "events": [e for e in (_loads(r[0]) for r in evs) if e is not None]}

    def list_chats(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT id,title,created,updated FROM chats ORDER BY updated DESC, rowid DESC").fetchall()
        return [self._chat(r) for r in rows]

    def delete_chat(self, chat_id) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM chats WHERE id=?", (chat_id,)).rowcount > 0  # events cascade

    def append_event(self, chat_id: str, event: dict) -> None:
        """Appends in order; creates the chat if missing; the first user message becomes the title."""
        with self._tx() as db:
            row = db.execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
            t = now(True)
            if row is None:
                db.execute("INSERT INTO chats(id,title,created,updated) VALUES(?,?,?,?)", (chat_id, DEFAULT_TITLE, t, t))
                row = (DEFAULT_TITLE,)
            seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM chat_events WHERE chat_id=?", (chat_id,)).fetchone()[0]
            db.execute("INSERT INTO chat_events(chat_id,seq,body) VALUES(?,?,?)", (chat_id, seq, json.dumps(event)))
            title = row[0]
            if event.get("type") == "user" and title == DEFAULT_TITLE:
                title = re.sub(r"\s+", " ", str(event.get("text") or "")).strip()[:TITLE_MAX] or DEFAULT_TITLE
            db.execute("UPDATE chats SET title=?, updated=? WHERE id=?", (title, t, chat_id))


class _Tx:
    """BEGIN ... COMMIT (ROLLBACK on error) while holding the store's lock."""

    def __init__(self, lib):
        self.lib = lib

    def __enter__(self):
        self.lib._lock.acquire()
        try:
            self.lib._db.execute("BEGIN IMMEDIATE")
        except Exception:
            self.lib._lock.release()
            raise
        return self.lib._db

    def __exit__(self, exc_type, *a):
        try:
            self.lib._db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.lib._lock.release()
