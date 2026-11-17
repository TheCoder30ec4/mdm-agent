"""FastAPI server: chat SSE, proposals, jobs, global events. See contracts/events.md.

Run: uvicorn server:app --host 127.0.0.1 --port 8000
"""
import asyncio
import json
import os
import queue
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}
MAX_HISTORY = 40
MAX_SESSIONS = 200
HEARTBEAT_S = 15
SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src https: data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
}


class ChatIn(BaseModel):
    session: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)


class ApproveIn(BaseModel):
    confirm_risky: bool = False


def _reachable(url: str) -> bool:
    """True if something accepts TCP connections at url (the local Firecrawl)."""
    import socket
    p = urlsplit(url)
    try:
        socket.create_connection((p.hostname, p.port or (443 if p.scheme == "https" else 80)), timeout=1).close()
        return True
    except OSError:
        return False


def sse(obj) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _trim(history: list) -> None:
    """Keep the last MAX_HISTORY messages, starting at a user turn so tool messages are never orphaned."""
    if len(history) <= MAX_HISTORY:
        return
    del history[:-MAX_HISTORY]
    while history and history[0].get("role") != "user":
        del history[0]


def create_app(jobs=None, agent=None) -> FastAPI:
    real = jobs is None
    state = {"jobs": jobs, "agent": agent}
    build_lock = threading.Lock()
    sessions: "OrderedDict[str, dict]" = OrderedDict()

    def get_jobs():
        with build_lock:
            if state["jobs"] is None:
                from agent.jobs import Jobs  # lazy: .env is loaded by lifespan first

                state["jobs"] = Jobs()
            return state["jobs"]

    def get_agent():
        j = get_jobs()
        with build_lock:
            if state["agent"] is None:
                from agent.agent import Agent

                state["agent"] = Agent(j)
            return state["agent"]

    async def lifespan(_app):
        if real:
            from dotenv import load_dotenv

            load_dotenv(BASE / ".env")
        yield

    app = FastAPI(title="MyDownloadManager", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # DNS-rebinding guard: only loopback names; POSTs must be same-origin when Origin is sent.
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        if urlsplit("//" + host).hostname not in ALLOWED_HOSTS:
            resp = JSONResponse({"detail": "bad host"}, status_code=403)
        elif request.method == "POST" and origin is not None and urlsplit(origin).netloc != host:
            resp = JSONResponse({"detail": "bad origin"}, status_code=403)
        else:
            resp = await call_next(request)
        resp.headers.update(SECURITY_HEADERS)
        return resp

    def call(fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # map by name so server needs no import of agent.jobs
            name = type(e).__name__
            if name == "NotFound":
                raise HTTPException(404, str(e) or "not found")
            if name == "NeedsConfirm":
                raise HTTPException(409, "needs_confirm")
            if name == "BadState":
                raise HTTPException(409, str(e) or "bad state")
            raise

    # ---- chat ----
    @app.post("/api/chat")
    async def chat(body: ChatIn):
        text = body.message.strip()
        if not text:
            raise HTTPException(422, "message is empty")
        sess = sessions.setdefault(body.session, {"history": [], "lock": threading.Lock()})
        sessions.move_to_end(body.session)
        while len(sessions) > MAX_SESSIONS:
            sessions.popitem(last=False)
        if not sess["lock"].acquire(blocking=False):
            raise HTTPException(409, "session is busy")

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def work():
            try:
                try:
                    ag = get_agent()
                    for ev in ag.run(sess["history"], text):
                        if stop.is_set():
                            break
                        if ev.get("type") != "done":  # one `done`, always last, is sent below
                            loop.call_soon_threadsafe(q.put_nowait, ev)
                except Exception as e:
                    loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                _trim(sess["history"])
                sess["lock"].release()
                loop.call_soon_threadsafe(q.put_nowait, {"type": "done"})

        threading.Thread(target=work, daemon=True).start()

        async def stream():
            try:
                while True:
                    ev = await q.get()
                    yield sse(ev)
                    if ev.get("type") == "done":
                        return
            finally:
                stop.set()

        return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    # ---- proposals ----
    @app.get("/api/proposals")
    def proposals():
        return get_jobs().list_proposals()

    @app.post("/api/proposals/{pid}/approve")
    def approve(pid: str, body: ApproveIn | None = None):
        # The ONLY place a download is started.
        return call(get_jobs().approve, pid, bool(body and body.confirm_risky))

    @app.post("/api/proposals/{pid}/reject")
    def reject(pid: str):
        return call(get_jobs().reject, pid)

    # ---- jobs ----
    @app.get("/api/jobs")
    def list_jobs():
        return get_jobs().list_jobs()

    @app.post("/api/jobs/{jid}/cancel")
    def cancel(jid: str):
        return call(get_jobs().cancel, jid)

    # ---- global events ----
    @app.get("/api/events")
    async def events(request: Request):
        j = await asyncio.to_thread(get_jobs)
        q = j.subscribe()

        async def stream():
            last = time.monotonic()
            try:
                yield ": ok\n\n"
                while True:
                    try:
                        ev = q.get_nowait()
                    except queue.Empty:
                        if await request.is_disconnected():
                            return
                        if time.monotonic() - last >= HEARTBEAT_S:
                            last = time.monotonic()
                            yield ": ping\n\n"
                        await asyncio.sleep(0.25)
                        continue
                    if ev is None:  # shutdown sentinel
                        return
                    last = time.monotonic()
                    yield sse(ev)
            finally:
                j.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    # ---- health ----
    @app.get("/api/health")
    def health():
        from agent.mdm import cli_path
        mdm = cli_path()
        return {
            "ok": True,
            "groq_key": bool(os.environ.get("GROQ_API_KEY")),
            "firecrawl": _reachable(os.environ.get("FIRECRAWL_API_URL") or "http://localhost:3002"),
            "mdm_cli": bool(shutil.which(mdm) or os.path.isfile(mdm)),
        }

    @app.get("/")
    def index():
        return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
