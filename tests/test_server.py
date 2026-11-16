import json
import queue

import pytest
from fastapi.testclient import TestClient

import server


class NotFound(Exception): ...
class NeedsConfirm(Exception): ...
class BadState(Exception): ...


REPORT = {"url": "https://x.org/a.zip", "safety": {"level": "ok", "reasons": [], "skipped": []}}


class StubJobs:
    def __init__(self):
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
    assert r.json() == {"ok": True, "groq_key": True, "firecrawl": False, "mdm_cli": False}


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
