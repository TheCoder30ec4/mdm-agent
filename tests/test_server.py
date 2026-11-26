import json
import queue
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server


class NotFound(Exception): ...
class NeedsConfirm(Exception): ...
class BadState(Exception): ...


REPORT = {"url": "https://x.org/a.zip", "safety": {"level": "ok", "reasons": [], "skipped": []}}


class StubJobs:
    def __init__(self, root=None):
        self.library_dir = Path(root) if root else None
        self.items = {}
        self.chats = {}
        self.chat_events = []  # (chat_id, event) in call order
        self.starts = 0
        self.calls = []
        self.props = {
            "p_00000001": {"id": "p_00000001", "status": "pending", "report": REPORT, "files": [], "note": "", "job_id": None},
            "p_00000002": {"id": "p_00000002", "status": "pending", "report": {**REPORT, "safety": {"level": "risky", "reasons": ["exe"], "skipped": []}}, "files": [], "note": "", "job_id": None},
        }
        self.q = queue.Queue()
        self.unsubscribed = False

    def propose(self, *a, **k):
        self.calls.append("propose")

    def list_proposals(self):
        self.calls.append("list_proposals")
        return list(self.props.values())

    def list_jobs(self):
        self.calls.append("list_jobs")
        return []

    def get_job(self, jid):
        raise NotFound(jid)

    def approve(self, pid, confirm_risky=False):
        p = self.props.get(pid)
        if p is None:
            raise NotFound(pid)
        if p["status"] != "pending":
            raise BadState(pid)
        if p["report"]["safety"]["level"] == "risky" and not confirm_risky:
            raise NeedsConfirm(pid)
        self.starts += 1
        p["status"], p["job_id"] = "approved", "j_00000001"
        return {"id": "j_00000001", "proposal_id": pid, "state": "queued"}

    def reject(self, pid):
        p = self.props.get(pid)
        if p is None:
            raise NotFound(pid)
        if p["status"] != "pending":
            raise BadState(pid)
        p["status"] = "rejected"
        return p

    def cancel(self, jid):
        raise NotFound(jid)

    def subscribe(self):
        return self.q

    def unsubscribe(self, q):
        self.unsubscribed = True

    # ---- library (contracts/library.md "Python interface") ----
    def add_item(self, iid, files):
        """files: {relative path: bytes}; written under files/<job id>/ so streaming is real."""
        jid = "j_" + iid
        base = self.library_dir / "files" / jid
        out = []
        for i, (rel, data) in enumerate(files.items()):
            if ".." not in rel:
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                (base / rel).write_bytes(data)
            out.append({"index": i, "path": rel, "size": len(data), "mime": "video/mp4", "playable": True})
        self.items[iid] = {"id": iid, "job_id": jid, "title": iid, "kind": "video", "files": out,
                           "main": max(range(len(out)), key=lambda i: out[i]["size"]), "poster": False}
        return self.items[iid]

    def list_items(self):
        return list(self.items.values())[::-1]

    def get_item(self, iid):
        if iid not in self.items:
            raise NotFound(iid)
        return self.items[iid]

    def item_file(self, iid, index=None):
        it = self.get_item(iid)
        index = it["main"] if index is None else index
        if not 0 <= index < len(it["files"]):
            raise NotFound(f"{iid} file {index}")
        base = (self.library_dir / "files" / it["job_id"]).resolve()
        p = (base / it["files"][index]["path"]).resolve()
        if not p.is_relative_to(base) or not p.is_file():
            raise NotFound(f"{iid} file {index}")
        return p

    def poster_path(self, iid):
        self.get_item(iid)
        p = self.library_dir / "posters" / f"{iid}.jpg"
        return p if p.is_file() else None

    def delete_item(self, iid):
        self.get_item(iid)
        del self.items[iid]

    # ---- chats ----
    def create_chat(self, title=None):
        cid = f"c_{len(self.chats) + 1:08d}"
        self.chats[cid] = {"id": cid, "title": title or "New download", "created": "t", "updated": "t"}
        return self.chats[cid]

    def list_chats(self):
        return list(self.chats.values())[::-1]

    def get_chat(self, cid):
        if cid not in self.chats:
            raise NotFound(cid)
        return {"chat": self.chats[cid], "events": [e for c, e in self.chat_events if c == cid]}

    def delete_chat(self, cid):
        if self.chats.pop(cid, None) is None:
            raise NotFound(cid)
        self.chat_events = [(c, e) for c, e in self.chat_events if c != cid]

    def append_chat_event(self, cid, event):
        if cid not in self.chats:
            self.chats[cid] = {"id": cid, "title": "New download", "created": "t", "updated": "t"}
        if event["type"] == "user" and self.chats[cid]["title"] == "New download":
            self.chats[cid]["title"] = event["text"][:60]
        self.chat_events.append((cid, event))


class StubAgent:
    def __init__(self):
        self.histories = []

    def run(self, history, user_text):
        self.histories.append(list(history))
        history.append({"role": "user", "content": user_text})
        yield {"type": "tool_step", "name": "web_search", "args": {"query": user_text}}
        yield {"type": "text", "text": "hi"}
        history.append({"role": "assistant", "content": "hi"})
        yield {"type": "done"}


@pytest.fixture
def env():
    jobs, agent = StubJobs(), StubAgent()
    c = TestClient(server.create_app(jobs=jobs, agent=agent), base_url="http://127.0.0.1:8000")
    return c, jobs, agent


def events(resp):
    return [json.loads(l[6:]) for l in resp.text.splitlines() if l.startswith("data: ")]


def test_chat_stream_ends_with_single_done(env):
    c, jobs, agent = env
    r = c.post("/api/chat", json={"session": "s", "message": "hello"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = events(r)
    assert [e["type"] for e in evs] == ["tool_step", "text", "done"]
    assert jobs.starts == 0


def test_chat_history_kept_per_session(env):
    c, _, agent = env
    c.post("/api/chat", json={"session": "a", "message": "one"})
    c.post("/api/chat", json={"session": "a", "message": "two"})
    c.post("/api/chat", json={"session": "b", "message": "x"})
    assert len(agent.histories[1]) == 2 and agent.histories[2] == []


def test_chat_history_trimmed_to_40():
    class Chatty(StubAgent):
        def run(self, history, user_text):
            self.histories.append(list(history))
            for i in range(30):
                history.append({"role": "user" if i % 2 == 0 else "assistant", "content": str(i)})
            yield {"type": "done"}

    agent = Chatty()
    c = TestClient(server.create_app(jobs=StubJobs(), agent=agent), base_url="http://localhost")
    for _ in range(3):
        c.post("/api/chat", json={"session": "s", "message": "m"})
    assert len(agent.histories[2]) <= 40 and agent.histories[2][0]["role"] == "user"


def test_chat_agent_crash_becomes_error_then_done():
    class Boom(StubAgent):
        def run(self, history, user_text):
            raise RuntimeError("nope")
            yield

    c = TestClient(server.create_app(jobs=StubJobs(), agent=Boom()), base_url="http://localhost")
    evs = events(c.post("/api/chat", json={"session": "s", "message": "m"}))
    assert [e["type"] for e in evs] == ["error", "done"]


@pytest.mark.parametrize("body", [
    {"session": "s", "message": ""},
    {"session": "s", "message": "   "},
    {"session": "s", "message": "x" * 4001},
    {"session": "", "message": "hi"},
    {"message": "hi"},
])
def test_chat_422(env, body):
    assert env[0].post("/api/chat", json=body).status_code == 422


def test_chat_4000_ok(env):
    assert env[0].post("/api/chat", json={"session": "s", "message": "x" * 4000}).status_code == 200


def test_only_approve_starts_a_job(env):
    c, jobs, _ = env
    c.post("/api/chat", json={"session": "s", "message": "hi"})
    c.get("/api/proposals"); c.get("/api/jobs"); c.get("/api/health"); c.get("/")
    c.post("/api/proposals/p_00000001/reject")
    c.post("/api/jobs/j_1/cancel")
    assert jobs.starts == 0
    r = c.post("/api/proposals/p_00000001/approve", json={"confirm_risky": False})
    # p_1 was rejected above -> 409, nothing started
    assert r.status_code == 409 and jobs.starts == 0
    r = c.post("/api/proposals/p_00000002/approve", json={"confirm_risky": True})
    assert r.status_code == 200 and jobs.starts == 1
    assert c.post("/api/proposals/p_00000002/approve").status_code == 409
    assert jobs.starts == 1


def test_approve_without_body_ok(env):
    c, jobs, _ = env
    r = c.post("/api/proposals/p_00000001/approve")
    assert r.status_code == 200 and jobs.starts == 1


def test_needs_confirm(env):
    c, jobs, _ = env
    r = c.post("/api/proposals/p_00000002/approve", json={"confirm_risky": False})
    assert r.status_code == 409 and r.json() == {"detail": "needs_confirm"} and jobs.starts == 0


def test_unknown_ids_404(env):
    c, _, _ = env
    assert c.post("/api/proposals/p_nope/approve").status_code == 404
    assert c.post("/api/proposals/p_nope/reject").status_code == 404
    assert c.post("/api/jobs/j_nope/cancel").status_code == 404


def test_reject_twice_409(env):
    c, _, _ = env
    assert c.post("/api/proposals/p_00000001/reject").json()["status"] == "rejected"
    assert c.post("/api/proposals/p_00000001/reject").status_code == 409


def test_bad_host_rejected(env):
    _, jobs, agent = env
    c = TestClient(server.create_app(jobs=jobs, agent=agent), base_url="http://evil.example")
    assert c.get("/api/health").status_code == 403
    assert c.post("/api/proposals/p_00000001/approve").status_code == 403
    assert jobs.starts == 0


def test_cross_origin_post_rejected(env):
    c, jobs, _ = env
    r = c.post("/api/proposals/p_00000001/approve", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and jobs.starts == 0
    r = c.post("/api/proposals/p_00000001/approve", headers={"Origin": "http://127.0.0.1:8000"})
    assert r.status_code == 200


def test_health_booleans_only(monkeypatch, env):
    for k in ("GROQ_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.setenv(k, "SENTINEL-SECRET-123")
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://127.0.0.1:1")  # nothing listens: unreachable
    monkeypatch.setenv("MDM_CLI", "/nonexistent/mdm")
    r = env[0].get("/api/health")
    assert "SENTINEL" not in r.text
    assert r.json() == {"ok": True, "groq_key": True, "provider": "groq", "llm_ready": True, "firecrawl": False,
                        "mdm_cli": False}


def test_events_relays_and_unsubscribes(env):
    c, jobs, _ = env
    jobs.q.put({"type": "job", "job": {"id": "j_1"}})
    jobs.q.put({"type": "job_done", "job": {"id": "j_1", "state": "done"}})
    jobs.q.put(None)  # sentinel ends the stream
    r = c.get("/api/events")
    assert [e["type"] for e in events(r)] == ["job", "job_done"]
    assert jobs.unsubscribed


def test_index_and_static(env):
    c, _, _ = env
    assert c.get("/").status_code == 200
    assert c.get("/static/index.html").status_code == 200


def test_module_app_imports_without_keys(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert server.app is not None


def test_security_headers_on_every_response(env):
    c, _, _ = env
    for r in (c.get("/"), c.get("/api/proposals"), c.get("/api/health"),
              c.post("/api/proposals/nope/approve"), c.get("/", headers={"host": "evil.com"})):
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        csp = r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp and "default-src 'self'" in csp


def test_index_needs_no_external_resources(env):
    # CSP allows only same-origin script/style/connect; the page must not reference other origins.
    import re
    html = env[0].get("/").text
    assert not re.search(r'<(script|link)[^>]+(src|href)=["\']?(https?:)?//', html)
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", html)


# ---- chats ----

class ProposingAgent(StubAgent):
    def run(self, history, user_text):
        yield {"type": "tool_step", "name": "inspect_link", "args": {}}
        yield {"type": "proposal", "proposal": {"id": "p_00000009", "status": "pending"}}
        yield {"type": "text", "text": "here"}
        yield {"type": "done"}


def test_chat_events_persisted_in_order():
    jobs = StubJobs()
    c = TestClient(server.create_app(jobs=jobs, agent=ProposingAgent()), base_url="http://localhost")
    c.post("/api/chat", json={"session": "c_1", "message": "find x"})
    assert jobs.chat_events == [
        ("c_1", {"type": "user", "text": "find x"}),
        ("c_1", {"type": "tool_step", "name": "inspect_link", "args": {}}),
        ("c_1", {"type": "proposal", "id": "p_00000009"}),
        ("c_1", {"type": "text", "text": "here"}),
    ]
    assert c.get("/api/chats/c_1").json()["chat"]["title"] == "find x"


def test_chat_error_persisted():
    class Boom(StubAgent):
        def run(self, history, user_text):
            raise RuntimeError("nope")
            yield

    jobs = StubJobs()
    c = TestClient(server.create_app(jobs=jobs, agent=Boom()), base_url="http://localhost")
    c.post("/api/chat", json={"session": "c_1", "message": "m"})
    assert [e["type"] for _, e in jobs.chat_events] == ["user", "error"]


def test_chat_storage_failure_does_not_break_stream():
    class Broken(StubJobs):
        def append_chat_event(self, cid, event):
            raise OSError("disk full")

    c = TestClient(server.create_app(jobs=Broken(), agent=StubAgent()), base_url="http://localhost")
    assert [e["type"] for e in events(c.post("/api/chat", json={"session": "s", "message": "m"}))] == ["tool_step", "text", "done"]


def test_chats_crud(env):
    c, jobs, agent = env
    chat = c.post("/api/chats").json()
    assert chat["title"] == "New download" and chat["id"].startswith("c_")
    assert [x["id"] for x in c.get("/api/chats").json()] == [chat["id"]]
    c.post("/api/chat", json={"session": chat["id"], "message": "one"})
    got = c.get(f"/api/chats/{chat['id']}").json()
    assert [e["type"] for e in got["events"]] == ["user", "tool_step", "text"]
    assert c.delete(f"/api/chats/{chat['id']}").status_code == 204
    assert c.get(f"/api/chats/{chat['id']}").status_code == 404
    assert c.delete(f"/api/chats/{chat['id']}").status_code == 404
    c.post("/api/chat", json={"session": chat["id"], "message": "two"})
    assert agent.histories[-1] == []  # deleting the chat dropped its model history


def test_cross_origin_delete_rejected(env):
    c, jobs, _ = env
    cid = c.post("/api/chats").json()["id"]
    assert c.delete(f"/api/chats/{cid}", headers={"Origin": "http://evil.example"}).status_code == 403
    assert cid in jobs.chats


# ---- library ----

DATA = bytes(range(256)) * 4  # 1024 bytes


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.delenv("MDM_SECRET", raising=False)
    jobs = StubJobs(tmp_path)
    jobs.add_item("i_1", {"Night of the Living Dead.mp4": DATA, "sub/s.srt": b"1\n"})
    jobs.add_item("i_bad", {"../../outside.txt": b"x"})
    (tmp_path / "outside.txt").write_bytes(b"secret")
    c = TestClient(server.create_app(jobs=jobs, agent=StubAgent()), base_url="http://127.0.0.1:8000")
    return c, jobs


def test_library_list_and_get(lib):
    c, _ = lib
    assert [i["id"] for i in c.get("/api/library").json()] == ["i_bad", "i_1"]
    assert c.get("/api/library/i_1").json()["main"] == 0
    assert c.get("/api/library/nope").status_code == 404


def test_stream_whole_and_inline(lib):
    c, _ = lib
    r = c.get("/api/library/i_1/stream")
    assert r.status_code == 200 and r.content == DATA
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-disposition"] == "inline; filename*=UTF-8''Night%20of%20the%20Living%20Dead.mp4"
    assert r.headers["content-type"] == "video/mp4"
    assert c.get("/api/library/i_1/stream?file=1").content == b"1\n"


@pytest.mark.parametrize("rng,start,end", [("bytes=0-99", 0, 99), ("bytes=1000-", 1000, 1023), ("bytes=-24", 1000, 1023)])
def test_stream_range(lib, rng, start, end):
    r = lib[0].get("/api/library/i_1/stream", headers={"Range": rng})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes {start}-{end}/1024"
    assert r.content == DATA[start:end + 1] and len(r.content) == end - start + 1


def test_stream_bad_range_416(lib):
    r = lib[0].get("/api/library/i_1/stream", headers={"Range": "bytes=5000-6000"})
    assert r.status_code == 416 and r.headers["content-range"] == "bytes */1024"


def test_download_is_attachment_and_resumable(lib):
    c, _ = lib
    r = c.get("/api/library/i_1/download")
    assert r.status_code == 200 and r.content == DATA
    assert r.headers["content-disposition"].startswith("attachment; filename*=UTF-8''Night%20of")
    r = c.get("/api/library/i_1/download", headers={"Range": "bytes=512-"})
    assert r.status_code == 206 and r.content == DATA[512:]


def test_unicode_filename_rfc5987(lib):
    c, jobs = lib
    jobs.add_item("i_u", {"Amélie 100%.mp4": b"abc"})
    assert c.get("/api/library/i_u/download").headers["content-disposition"] == \
        "attachment; filename*=UTF-8''Am%C3%A9lie%20100%25.mp4"


def test_traversal_and_bad_file_param(lib):
    c, _ = lib
    assert c.get("/api/library/i_bad/stream").status_code == 404
    assert c.get("/api/library/i_bad/download").status_code == 404
    assert c.get("/api/library/i_1/stream?file=9").status_code == 404
    for q in ("file=-1", "file=abc", "file=0.5", "file=../../outside.txt"):
        assert c.get(f"/api/library/i_1/stream?{q}").status_code in (400, 422), q
        assert c.get(f"/api/library/i_1/download?{q}").status_code in (400, 422), q
    assert c.get("/api/library/i_1/stream?path=../../outside.txt").content == DATA  # extra params ignored


def test_poster(lib, tmp_path):
    c, _ = lib
    assert c.get("/api/library/i_1/poster").status_code == 404
    assert c.get("/api/library/nope/poster").status_code == 404
    (tmp_path / "posters").mkdir()
    (tmp_path / "posters" / "i_1.jpg").write_bytes(b"\xff\xd8jpeg")
    r = c.get("/api/library/i_1/poster")
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "private, max-age=86400"


def test_delete_item(lib):
    c, jobs = lib
    assert c.delete("/api/library/i_1").status_code == 204
    assert "i_1" not in jobs.items
    assert c.delete("/api/library/i_1").status_code == 404


def test_signed_link_roundtrip(lib, tmp_path):
    c, _ = lib
    r = c.post("/api/library/i_1/link")
    body = r.json()
    assert r.status_code == 200 and body["url"].startswith("/api/library/i_1/stream?file=0&exp=")
    assert abs(body["expires"] - (time.time() + 6 * 3600)) < 5
    assert c.get(body["url"]).content == DATA
    secret = tmp_path / ".secret"
    assert secret.stat().st_mode & 0o777 == 0o600 and len(secret.read_bytes()) == 32
    assert c.post("/api/library/i_1/link?file=1").json()["url"].startswith("/api/library/i_1/stream?file=1&")
    assert c.post("/api/library/i_1/link?file=7").status_code == 404
    assert c.post("/api/library/nope/link").status_code == 404


def test_signed_link_tampered_or_expired_403(lib):
    import hashlib, hmac
    c, _ = lib
    url = c.post("/api/library/i_1/link").json()["url"]
    assert c.get(url.replace("file=0", "file=1")).status_code == 403
    assert c.get(url.replace("/i_1/", "/i_bad/")).status_code == 403
    assert c.get(url[:-1] + ("0" if url[-1] != "0" else "1")).status_code == 403
    assert c.get(url.replace("&exp=", "&exp=1")).status_code == 403
    assert c.get("/api/library/i_1/stream?sig=abc").status_code == 403
    key = (lib[1].library_dir / ".secret").read_bytes()
    exp = int(time.time()) - 1
    sig = hmac.new(key, f"i_1:0:{exp}".encode(), hashlib.sha256).hexdigest()
    assert c.get(f"/api/library/i_1/stream?file=0&exp={exp}&sig={sig}").status_code == 403


def test_secret_from_env_and_kept(lib, monkeypatch, tmp_path):
    monkeypatch.setenv("MDM_SECRET", "s3cret")
    jobs = StubJobs(tmp_path / "other")
    jobs.add_item("i_1", {"a.mp4": b"abc"})
    c = TestClient(server.create_app(jobs=jobs, agent=StubAgent()), base_url="http://localhost")
    assert c.get(c.post("/api/library/i_1/link").json()["url"]).content == b"abc"
    assert not (tmp_path / "other" / ".secret").exists()


def test_secret_file_reused(lib, tmp_path):
    c, jobs = lib
    url = c.post("/api/library/i_1/link").json()["url"]
    c2 = TestClient(server.create_app(jobs=jobs, agent=StubAgent()), base_url="http://localhost")  # a restart
    assert c2.get(url).status_code == 200


def test_csp_allows_media_and_posters(env):
    csp = env[0].get("/").headers["content-security-policy"]
    assert "media-src 'self'" in csp and "img-src 'self'" in csp


# ---- settings (contracts/settings.md) ----

KEY = "gsk_SENTINEL_settings_key_0123456789"


@pytest.fixture
def st(tmp_path, monkeypatch):
    for k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "MDM_PROVIDER", "MDM_MODEL",
              "GROQ_MODEL", "MDM_BASE_URL", "FIRECRAWL_API_URL", "FIRECRAWL_API_KEY", "MDM_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    c = TestClient(server.create_app(jobs=StubJobs(tmp_path), agent=StubAgent()), base_url="http://127.0.0.1:8000")
    return c, tmp_path


def _no_key(text, key=KEY):
    return all(key[i:i + 8] not in text for i in range(len(key) - 7))  # no 8-char run of the key


def test_settings_get_defaults(st):
    c, _ = st
    s = c.get("/api/settings").json()
    assert s["provider"] == "groq" and s["model"] == "openai/gpt-oss-120b" and s["keys"]["groq"] is False
    assert s["firecrawl_key"] is False and {p["id"] for p in s["providers"]} == {"groq", "anthropic", "openai", "google", "compatible"}
    assert c.get("/api/health").json()["llm_ready"] is False


def test_settings_put_key_never_returned(st):
    c, root = st
    r = c.put("/api/settings", json={"provider": "anthropic", "api_key": KEY, "firecrawl_key": KEY + "fc"})
    assert r.status_code == 200 and _no_key(r.text)
    g = c.get("/api/settings")
    assert _no_key(g.text) and g.json()["keys"]["anthropic"] is True and g.json()["firecrawl_key"] is True
    assert g.json()["model"] == "claude-sonnet-5-5"
    assert _no_key(c.get("/api/health").text)
    assert json.loads((root / "settings.json").read_text())["keys"]["anthropic"] == KEY
    assert (root / "settings.json").stat().st_mode & 0o777 == 0o600


def test_settings_put_validation(st):
    c, _ = st
    assert c.put("/api/settings", json={"provider": "nope"}).status_code == 400
    assert c.put("/api/settings", json={"base_url": "file:///etc/passwd"}).status_code == 400
    assert c.put("/api/settings", json={"firecrawl_url": "ftp://x"}).status_code == 400
    assert c.put("/api/settings", json={"unknown": 1}).status_code == 422
    r = c.put("/api/settings", json={"provider": "compatible", "base_url": "http://localhost:11434/v1", "model": "llama3.1"})
    assert r.json()["base_url"] == "http://localhost:11434/v1" and r.json()["model"] == "llama3.1"
    h = c.get("/api/health").json()
    assert h["provider"] == "compatible" and h["llm_ready"] is True


def test_settings_cross_origin_and_login(st, monkeypatch):
    c, root = st
    r = c.put("/api/settings", json={"api_key": KEY}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and not (root / "settings.json").exists()
    monkeypatch.setenv("MDM_TOKEN", "t" * 40)
    monkeypatch.setenv("MDM_SECRET", "s" * 40)
    c2 = TestClient(server.create_app(jobs=StubJobs(root), agent=StubAgent()), base_url="http://127.0.0.1:8000")
    assert c2.get("/api/settings").status_code == 401
    assert c2.put("/api/settings", json={"api_key": KEY}).status_code == 401
    assert c2.post("/api/settings/test").status_code == 401


def test_settings_test_uses_factory_and_hides_key(st, monkeypatch):
    from agent import llm
    c, _ = st
    c.put("/api/settings", json={"provider": "openai", "api_key": KEY})
    seen = {}

    class Ok:
        def invoke(self, prompt):
            seen["prompt"] = prompt

    def fake_make(cfg, timeout=None, max_retries=2):
        seen.update(timeout=timeout, provider=cfg["provider"])
        return Ok()

    monkeypatch.setattr(llm, "make", fake_make)
    assert c.post("/api/settings/test").json() == {"ok": True}
    assert seen == {"timeout": 20, "provider": "openai", "prompt": "Reply with OK"}

    class Leaky:
        def invoke(self, prompt):
            raise RuntimeError(f"bad request for key {KEY}; Incorrect API key provided: gsk_SE***6789")

    monkeypatch.setattr(llm, "make", lambda cfg, **kw: Leaky())
    r = c.post("/api/settings/test").json()
    assert r["ok"] is False and r["error"] and _no_key(r["error"]) and "6789" not in r["error"]

    class Denied(Exception):
        status_code = 401

    monkeypatch.setattr(llm, "make", lambda cfg, **kw: type("D", (), {"invoke": lambda s, p: (_ for _ in ()).throw(Denied(KEY))})())
    assert c.post("/api/settings/test").json() == {"ok": False, "error": "The provider rejected the API key."}


def test_settings_test_not_configured(st):
    c, _ = st
    r = c.post("/api/settings/test").json()
    assert r["ok"] is False and "Settings" in r["error"]


def test_env_only_groq_key_still_works(st, monkeypatch):
    c, _ = st
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    s = c.get("/api/settings")
    assert s.json()["keys"]["groq"] is True and _no_key(s.text)
    h = c.get("/api/health").json()
    assert h["groq_key"] is True and h["llm_ready"] is True and h["provider"] == "groq"
