"""FastAPI server: chat SSE, chats, proposals, jobs, library streaming, global events, token access.
See contracts/events.md and contracts/library.md.

Run: uvicorn server:app --host 127.0.0.1 --port 8000
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import queue
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}  # always allowed; the ALLOWED_HOSTS env adds to these
LOOPBACK_CLIENTS = {"127.0.0.1", "::1"}
COOKIE = "mdm_session"
SESSION_S = 30 * 86400
LINK_S = 6 * 3600
STREAM_PATH = re.compile(r"/api/library/[^/]+/stream")
log = logging.getLogger(__name__)
MAX_HISTORY = 40
MAX_SESSIONS = 200
HEARTBEAT_S = 15
SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' https: data:; media-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
}


class ChatIn(BaseModel):
    session: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)


class ApproveIn(BaseModel):
    confirm_risky: bool = False


class LoginIn(BaseModel):
    token: str = Field(min_length=1, max_length=1000)


def access_config() -> dict:
    """Hosts and token from the environment. ALLOWED_HOSTS without MDM_TOKEN is refused."""
    extra = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
    token = os.environ.get("MDM_TOKEN") or None
    if extra and not token:
        raise RuntimeError(
            "ALLOWED_HOSTS is set but MDM_TOKEN is not: remote access needs a login. "
            "Set MDM_TOKEN to a long random string, or unset ALLOWED_HOSTS for local use only."
        )
    return {"hosts": ALLOWED_HOSTS | extra, "token": token}


def _mac(key: bytes, msg: str) -> str:
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()


def _disposition(kind: str, name: str) -> str:
    return f"{kind}; filename*=UTF-8''{quote(name, safe='')}"  # RFC 5987


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
    cfg = {"hosts": ALLOWED_HOSTS, "token": None}
    secret_box: list[bytes] = []

    def configure():
        cfg.update(access_config())

    if not real:  # the real app reads its config in lifespan, after .env is loaded
        configure()

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

    def get_secret() -> bytes:
        """MDM_SECRET, else 32 random bytes kept in $LIBRARY_DIR/.secret (mode 600, created once)."""
        with build_lock:
            if secret_box:
                return secret_box[0]
            env = os.environ.get("MDM_SECRET")
            if env:
                secret_box.append(env.encode())
                return secret_box[0]
        path = Path(get_jobs().library_dir) / ".secret"
        with build_lock:
            if not secret_box:
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(secrets.token_bytes(32))
                except FileExistsError:
                    pass
                key = path.read_bytes()
                if len(key) < 32:
                    raise RuntimeError(f"{path} is shorter than 32 bytes: delete it to make a new one")
                secret_box.append(key)
            return secret_box[0]

    def session_key() -> bytes:
        # the token is mixed in, so changing MDM_TOKEN signs everyone out
        return get_secret() + b"\0session\0" + (cfg["token"] or "").encode()

    def session_ok(cookie: str | None) -> bool:
        exp, _, sig = (cookie or "").partition(".")
        if not exp.isdigit() or int(exp) < time.time():
            return False
        return hmac.compare_digest(sig, _mac(session_key(), exp))

    def link_sig(item_id: str, index: int, exp: int) -> str:
        return _mac(get_secret(), f"{item_id}:{index}:{exp}")

    async def lifespan(_app):
        if real:
            from dotenv import load_dotenv

            load_dotenv(BASE / ".env")
            configure()  # raises (startup aborts) on ALLOWED_HOSTS without MDM_TOKEN
        yield

    app = FastAPI(title="MyDownloadManager", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # DNS-rebinding guard: loopback names plus ALLOWED_HOSTS; writes must be same-origin when Origin is sent.
        # With MDM_TOKEN set, /api/* needs the session cookie, except login, health and signed stream links
        # (the stream endpoint checks those signatures itself).
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        path = request.url.path
        if urlsplit("//" + host).hostname not in cfg["hosts"]:
            resp = JSONResponse({"detail": "bad host"}, status_code=403)
        elif request.method not in ("GET", "HEAD") and origin is not None and urlsplit(origin).netloc != host:
            resp = JSONResponse({"detail": "bad origin"}, status_code=403)
        elif (
            cfg["token"]
            and path.startswith("/api/")
            and (request.method, path) not in (("POST", "/api/login"), ("GET", "/api/health"))
            and not (STREAM_PATH.fullmatch(path) and "sig" in request.query_params)
            and not await asyncio.to_thread(session_ok, request.cookies.get(COOKIE))
        ):
            resp = JSONResponse({"detail": "login required"}, status_code=401)
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

        def keep(ev):
            """Append a streamed event to the chat transcript; a storage failure never breaks the stream."""
            t = ev.get("type")
            if t == "proposal":
                ev = {"type": "proposal", "id": (ev.get("proposal") or {}).get("id")}
            elif t not in ("user", "tool_step", "text", "error"):
                return
            try:
                get_jobs().append_chat_event(body.session, ev)
            except Exception:
                log.exception("could not save a chat event")

        def work():
            try:
                try:
                    keep({"type": "user", "text": text})
                    ag = get_agent()
                    for ev in ag.run(sess["history"], text):
                        if stop.is_set():
                            break
                        if ev.get("type") != "done":  # one `done`, always last, is sent below
                            keep(ev)
                            loop.call_soon_threadsafe(q.put_nowait, ev)
                except Exception as e:
                    err = {"type": "error", "message": f"{type(e).__name__}: {e}"}
                    keep(err)
                    loop.call_soon_threadsafe(q.put_nowait, err)
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

    # ---- chats ----
    @app.post("/api/chats")
    def create_chat():
        return get_jobs().create_chat()

    @app.get("/api/chats")
    def list_chats():
        return get_jobs().list_chats()

    @app.get("/api/chats/{cid}")
    def get_chat(cid: str):
        return call(get_jobs().get_chat, cid)

    @app.delete("/api/chats/{cid}", status_code=204)
    def delete_chat(cid: str):
        call(get_jobs().delete_chat, cid)
        sessions.pop(cid, None)
        return Response(status_code=204)

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

    # ---- library ----
    @app.get("/api/library")
    def library():
        return get_jobs().list_items()

    @app.get("/api/library/{iid}")
    def item(iid: str):
        return call(get_jobs().get_item, iid)

    @app.delete("/api/library/{iid}", status_code=204)
    def delete_item(iid: str):
        call(get_jobs().delete_item, iid)
        return Response(status_code=204)

    @app.api_route("/api/library/{iid}/poster", methods=["GET", "HEAD"])
    def poster(iid: str):
        p = call(get_jobs().poster_path, iid)
        if p is None:
            raise HTTPException(404, "no poster")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    def send_file(iid: str, index: int | None, kind: str):
        # The path only ever comes from jobs.item_file (confined to files/<job id>/), never from the query.
        p = call(get_jobs().item_file, iid, index)
        return FileResponse(p, headers={"Content-Disposition": _disposition(kind, Path(p).name)})  # Range: Starlette

    @app.api_route("/api/library/{iid}/stream", methods=["GET", "HEAD"])
    def stream_file(iid: str, file: int | None = Query(None, ge=0), exp: int | None = None, sig: str | None = None):
        if sig is not None:  # a signed link (needs no cookie): file and exp are part of what was signed
            if file is None or exp is None or exp < time.time():
                raise HTTPException(403, "link expired or invalid")
            if not hmac.compare_digest(sig.encode(), link_sig(iid, file, exp).encode()):
                raise HTTPException(403, "bad signature")
        return send_file(iid, file, "inline")

    @app.api_route("/api/library/{iid}/download", methods=["GET", "HEAD"])
    def download_file(iid: str, file: int | None = Query(None, ge=0)):
        return send_file(iid, file, "attachment")

    @app.post("/api/library/{iid}/link")
    def link(iid: str, file: int | None = Query(None, ge=0)):
        j = get_jobs()
        index = call(j.get_item, iid)["main"] if file is None else file
        call(j.item_file, iid, index)  # 404 now rather than a dead link later
        exp = int(time.time()) + LINK_S
        return {"url": f"/api/library/{quote(iid, safe='')}/stream?file={index}&exp={exp}&sig={link_sig(iid, index, exp)}",
                "expires": exp}

    # ---- access ----
    @app.post("/api/login")
    def login(body: LoginIn, request: Request):
        token = cfg["token"]
        if not token:
            raise HTTPException(400, "login is off: MDM_TOKEN is not set")
        if not hmac.compare_digest(body.token.encode(), token.encode()):
            raise HTTPException(401, "wrong token")
        exp = str(int(time.time()) + SESSION_S)
        client = request.client.host if request.client else ""
        https = request.url.scheme == "https" or (
            client in LOOPBACK_CLIENTS and request.headers.get("x-forwarded-proto") == "https")  # tailscale serve
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, f"{exp}.{_mac(session_key(), exp)}", max_age=SESSION_S, path="/",
                        httponly=True, samesite="strict", secure=https)
        return resp

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
        return resp

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
