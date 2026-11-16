"""Agent tests: Deep Agents / LangGraph run with a scripted fake chat model (no network, no keys)."""
import json
import os

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent.agent import MAIN_TOOLS, MAX_PROPOSALS, RESEARCH_TOOLS, Agent

REPORT = {"url": "https://archive.org/x.iso", "host": "archive.org", "image": "https://i/x.png",
          "safety": {"level": "ok", "reasons": [], "skipped": []}}


def tc(name, args, i=1):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{i}"}])


class Fake(BaseChatModel):
    """Scripted model. `bound` records the tool names each request exposed; `seen` the messages it was sent."""
    script: list = []
    bound: list = []
    seen: list = []

    @property
    def _llm_type(self):
        return "fake"

    def bind_tools(self, tools, **kw):
        names = sorted(getattr(t, "name", None) or (t.get("function") or t).get("name") for t in tools)
        return _Bound(self, names)

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        self.seen.append(list(messages))
        nxt = self.script.pop(0) if self.script else tc("list_jobs", {})
        if isinstance(nxt, Exception):
            raise nxt
        return ChatResult(generations=[ChatGeneration(message=nxt)])


class _Bound:
    """What bind_tools returns: records the exposed tool names, then delegates."""

    def __init__(self, model, names):
        self.model, self.names = model, names

    def invoke(self, *a, **k):
        self.model.bound.append(self.names)
        return self.model.invoke(*a, **k)

    def __getattr__(self, n):
        return getattr(self.model, n)

    # LangChain calls the bound model as a Runnable
    def __or__(self, other):
        return self.model.__or__(other)


class FakeJobs:
    def __init__(self):
        self.proposed, self.started = [], []

    def propose(self, report, files=None, note=""):
        self.proposed.append((report, files, note))
        return {"id": f"p_{len(self.proposed):08d}", "status": "pending", "report": report, "files": files or [], "note": note}

    def list_jobs(self):
        return [{"id": f"j_{i}", "created": f"2026-01-{i + 1:02d}"} for i in range(12)]

    def get_job(self, job_id):
        raise KeyError(job_id)

    def approve(self, *a, **k):  # must never be called by the agent
        self.started.append(a)


class FakeWeb:
    def search(self, q, limit=5):
        return [{"title": "t", "url": "https://archive.org/x.iso", "description": "d" * 1000}]

    def scrape(self, url):
        return {"url": url, "text": "x" * 9000}


class FakeInspect:
    def __init__(self, error=False):
        self.calls, self.error = 0, error

    def inspect_link(self, url):
        self.calls += 1
        return {"error": "blocked: private address"} if self.error else dict(REPORT, url=url)


def make(script, inspect_error=False, **kw):
    m = Fake(script=list(script), bound=[], seen=[])
    a = Agent(FakeJobs(), llm=m, **kw)
    a._web, a._inspect = FakeWeb(), FakeInspect(inspect_error)
    return a, m


def run(a, text="find it"):
    hist = []
    return list(a.run(hist, text)), hist


def types(evs):
    return [e["type"] for e in evs]


def tool_results(m):
    """All tool-message payloads the model was shown, parsed."""
    out = []
    for msgs in m.seen:
        for x in msgs:
            if isinstance(x, ToolMessage):
                try:
                    out.append(json.loads(x.content))
                except ValueError:
                    out.append(x.content)
    return out


def test_full_flow_research_inspect_propose():
    a, m = make([
        tc("task", {"description": "Find Nosferatu 1922", "subagent_type": "researcher"}),   # main -> researcher
        tc("web_search", {"query": "nosferatu 1922 archive.org"}),                            # researcher
        AIMessage(content="Candidate: https://archive.org/x.iso"),                            # researcher done
        tc("inspect_link", {"url": "https://archive.org/x.iso"}, 2),                          # main
        tc("propose_download", {"url": "https://archive.org/x.iso", "note": "public domain"}, 3),
        AIMessage(content="Found it. Safety: ok. Press Download if you want it."),
    ])
    evs, hist = run(a)
    assert types(evs) == ["tool_step", "tool_step", "tool_step", "tool_step", "proposal", "text", "done"]
    assert [set(n) for n in m.bound] and {frozenset(n) for n in m.bound} == {frozenset(MAIN_TOOLS - {"task"} | {"task"}), frozenset(RESEARCH_TOOLS)}
    assert [e["name"] for e in evs if e["type"] == "tool_step"] == ["research", "web_search", "inspect_link", "propose_download"]
    prop = next(e for e in evs if e["type"] == "proposal")["proposal"]
    assert prop["status"] == "pending" and a.jobs.proposed[0][2] == "public domain"
    assert hist == [{"role": "user", "content": "find it"},
                    {"role": "assistant", "content": "Found it. Safety: ok. Press Download if you want it."}]
    assert a.jobs.started == []


def test_no_tool_can_start_a_download_and_builtins_are_hidden():
    a, m = make([tc("list_jobs", {}), AIMessage(content="ok")])
    run(a)
    exposed = {n for names in m.bound for n in names}
    assert exposed <= MAIN_TOOLS | RESEARCH_TOOLS
    assert "execute" not in exposed and "write_file" not in exposed and "read_file" not in exposed
    assert not any("approve" in n or "download" in n and n != "propose_download" for n in exposed)
    assert MAIN_TOOLS == {"inspect_link", "propose_download", "job_status", "list_jobs", "task"}
    assert a.jobs.started == []


def test_blocked_builtin_tools_are_refused_at_dispatch(tmp_path):
    marker = tmp_path / "pwned"
    a, m = make([tc("execute", {"command": f"touch {marker}"}),
                 tc("write_file", {"file_path": "/x", "content": "y"}, 2),
                 tc("task", {"description": "x", "subagent_type": "general-purpose"}, 3),
                 AIMessage(content="done")])
    evs, _ = run(a)
    assert not marker.exists() and not os.path.exists(marker)
    results = tool_results(m)
    assert any("not available" in str(r) for r in results)
    assert types(evs)[-1] == "done" and a.jobs.started == []


def test_max_steps_error_then_done():
    a, m = make([], max_steps=3)  # empty script: model keeps calling list_jobs
    evs, _ = run(a)
    assert types(evs)[-2:] == ["error", "done"]
    assert "Stopped after 3 steps" in evs[-2]["message"]


def test_tool_exception_becomes_error_result():
    a, m = make([tc("job_status", {"job_id": "j_missing"}), AIMessage(content="not found")])
    evs, _ = run(a)
    assert any(isinstance(r, dict) and "KeyError" in r.get("error", "") for r in tool_results(m))
    assert types(evs)[-1] == "done" and "error" not in types(evs)


def test_propose_uses_inspector_report_not_model_safety():
    a, m = make([tc("propose_download", {"url": "https://archive.org/x.iso", "safety": {"level": "ok"}, "note": "n"}),
                 AIMessage(content="ok")])
    run(a)
    assert a.jobs.proposed and a.jobs.proposed[0][0]["safety"] == REPORT["safety"]


def test_inspect_error_blocks_proposal():
    a, m = make([tc("propose_download", {"url": "http://127.0.0.1/x"}), AIMessage(content="blocked")], inspect_error=True)
    evs, _ = run(a)
    assert a.jobs.proposed == [] and "proposal" not in types(evs)


def test_proposal_cap():
    calls = [tc("propose_download", {"url": f"https://archive.org/{i}.iso"}, i) for i in range(MAX_PROPOSALS + 2)]
    a, m = make(calls + [AIMessage(content="ok")])
    evs, _ = run(a)
    assert len(a.jobs.proposed) == MAX_PROPOSALS and types(evs).count("proposal") == MAX_PROPOSALS


def test_model_failure_yields_error_then_done():
    a, m = make([RuntimeError("401 invalid key")])
    evs, _ = run(a)
    assert types(evs) == ["error", "done"] and "401" in evs[0]["message"]


def test_retries_unparsable_tool_call_then_succeeds():
    class Bad(Exception):
        status_code = 400

    a, m = make([Bad("Error code: 400 - Parsing failed. The model generated output that could not be parsed."),
                 Bad("Parsing failed"), AIMessage(content="ok")])
    evs, _ = run(a)
    assert types(evs) == ["text", "done"]


def test_missing_groq_key_is_friendly(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    evs, _ = run(Agent(FakeJobs()))
    assert types(evs) == ["error", "done"] and "GROQ_API_KEY is not set" in evs[0]["message"]


def test_real_groq_build_offers_only_researcher_subagent():
    from langchain_groq import ChatGroq  # no network: building the graph never calls the API
    g = Agent(FakeJobs(), llm=ChatGroq(model="openai/gpt-oss-120b", api_key="not-a-key")).build([])
    assert type(g).__name__ == "CompiledStateGraph"  # a LangGraph graph built by deepagents
    desc = g.nodes["tools"].bound.tools_by_name["task"].description
    listed = desc.split("Available agent types")[1].split("Specify subagent_type")[0]
    assert "- researcher:" in listed and "- general-purpose:" not in listed


def test_task_to_other_subagent_is_refused():
    a, m = make([tc("task", {"description": "x", "subagent_type": "general-purpose"}), AIMessage(content="ok")])
    run(a)
    assert any("sub-agent general-purpose is not available" in str(r) for r in tool_results(m))


def test_nudges_once_when_links_inspected_but_none_proposed():
    a, m = make([tc("inspect_link", {"url": "https://archive.org/x.iso"}),
                 AIMessage(content="Here is a table of links."),            # forgot to propose
                 tc("propose_download", {"url": "https://archive.org/x.iso"}, 2),
                 AIMessage(content="Proposed it.")])
    evs, hist = run(a)
    assert types(evs).count("proposal") == 1 and a.jobs.started == []
    assert any("call propose_download" in str(getattr(x, "content", "")) for msgs in m.seen for x in msgs)  # nudge sent
    assert hist[-1]["role"] == "assistant" and "Proposed it." in hist[-1]["content"]


def test_no_nudge_when_nothing_inspected():
    a, m = make([AIMessage(content="Hello")])
    evs, _ = run(a)
    assert types(evs) == ["text", "done"] and len(m.seen) == 1
