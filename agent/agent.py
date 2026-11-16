"""Download assistant on Deep Agents (LangGraph) with Groq gpt-oss-120b.

Safety: no tool here can start a download (propose_download only creates a pending proposal), and a gate
middleware hides and blocks every tool the harness adds by default (shell `execute`, filesystem tools).
"""
import inspect as pyinspect
import json
import os

from deepagents import (GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent,  # Deep Agents harness
                        register_harness_profile)
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_groq import ChatGroq  # Groq: openai/gpt-oss-120b
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from pydantic import create_model

from agent import inspect as inspect_mod
from agent import web as web_mod
from agent.prompts import RESEARCHER_PROMPT, SYSTEM_PROMPT

DEFAULT_MODEL = "openai/gpt-oss-120b"
MAX_TOOL_CHARS = 6000
MAX_PROPOSALS = 10  # matches the system prompt
MAIN_TOOLS = frozenset({"inspect_link", "propose_download", "job_status", "list_jobs", "task"})
RESEARCH_TOOLS = frozenset({"web_search", "web_scrape"})
STEP_NAMES = {"task": "research"}  # what the UI shows for the sub-agent hand-off
NUDGE = ("If you recommend any of the links you inspected, call propose_download for each now: the user can only "
         "download through those cards. If you recommend none, say why in one sentence.")


def _trim(v, n):
    return v[:n] + "..." if isinstance(v, str) and len(v) > n else v


def _short_report(r):
    r = dict(r)
    r.pop("image", None)
    for k in ("title", "description"):
        r[k] = _trim(r.get(k), 500)
    if isinstance(r.get("files"), list) and len(r["files"]) > 30:
        r["files"] = r["files"][:30] + [{"note": f"{len(r['files']) - 30} more files omitted"}]
    return r


def _dump(obj):
    s = json.dumps(obj, default=str)
    if len(s) > MAX_TOOL_CHARS:  # ponytail: hard cut can make invalid JSON; fine for the model
        s = s[:MAX_TOOL_CHARS] + "...[truncated]"
    return s


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content blocks
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return ""


def _gate(allowed):
    """Middleware: the model only sees `allowed` tools, and any other tool call is refused at dispatch.
    Also retries the unparsable-tool-call 400 that gpt-oss sometimes triggers on Groq."""
    class Gate(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            req = request.override(tools=[t for t in request.tools if getattr(t, "name", None) in allowed])
            for attempt in range(3):
                try:
                    if attempt:  # retry with a nudge: gpt-oss sometimes invents tool names
                        nudge = HumanMessage(content="Call only these tools, with exact names: " + ", ".join(sorted(allowed)) + ".")
                        return handler(req.override(messages=[*req.messages, nudge]))
                    return handler(req)
                except Exception as e:
                    bad_call = getattr(e, "status_code", None) == 400 and (
                        "Parsing failed" in str(e) or "tool_use_failed" in str(e))
                    if attempt < 2 and bad_call:
                        continue
                    raise

        def wrap_tool_call(self, request, handler):
            name = request.tool_call["name"]
            sub = (request.tool_call.get("args") or {}).get("subagent_type")
            if name not in allowed or (name == "task" and sub != "researcher"):
                what = f"sub-agent {sub}" if name == "task" else f"tool {name}"
                return ToolMessage(content=_dump({"error": f"{what} is not available"}),
                                   tool_call_id=request.tool_call["id"], name=name)
            return handler(request)

    return Gate()


class Agent:
    def __init__(self, jobs, llm=None, model=None, max_steps=25):
        self.jobs = jobs
        self._llm = llm  # any LangChain chat model; default is Groq (created lazily)
        self.model = model or os.environ.get("GROQ_MODEL") or DEFAULT_MODEL
        self.max_steps = max_steps
        self._web = None
        self._inspect = None

    # the Groq model is built on first use, so importing this module needs no key
    @property
    def llm(self):
        if self._llm is None:
            key = os.environ.get("GROQ_API_KEY")
            if not key:
                raise RuntimeError("GROQ_API_KEY is not set. Copy .env.example to .env, add your key, and restart the server.")
            self._llm = ChatGroq(model=self.model, api_key=key, temperature=0.2, max_retries=2)
        return self._llm

    @property
    def web(self):  # tests swap in a fake via self._web
        return self._web or web_mod

    @property
    def inspector(self):
        return self._inspect or inspect_mod

    # ---- tools: plain functions, errors become {"error": ...} ----
    def _report(self, url, cache):
        if url not in cache:
            r = self.inspector.inspect_link(url)
            if "error" in r:
                return r
            cache[url] = r
        return cache[url]

    def _tools(self, events, run):
        """Fresh tool set per run: holds that run's inspect cache, proposal count and event list."""
        cache = run.setdefault("cache", {})
        state = run.setdefault("state", {"proposals": 0})

        def safe(fn):
            def run(**kw):
                try:
                    return _dump(fn(**kw))
                except Exception as e:  # WebError, NotFound, bad args ...
                    return _dump({"error": f"{type(e).__name__}: {e}"})
            run.__name__, run.__doc__ = fn.__name__, fn.__doc__
            return StructuredTool.from_function(run, name=fn.__name__, description=fn.__doc__,
                                                args_schema=_schema(fn))

        def web_search(query: str, limit: int = 5):
            """Search the web. Returns a list of {title, url, description}."""
            res = self.web.search(query, min(max(int(limit), 1), 10))
            return [{**r, "description": _trim(r.get("description"), 300)} for r in res]

        def web_scrape(url: str):
            """Fetch a web page as text (markdown, truncated)."""
            r = dict(self.web.scrape(url))
            r["text"] = _trim(r.get("text"), 4000)
            return r

        def inspect_link(url: str):
            """Inspect a download link (HEAD only): size, type, files, and a safety verdict. Never downloads."""
            r = self._report(url, cache)
            return r if "error" in r else _short_report(r)

        def propose_download(url: str, files: list[int] | None = None, note: str = ""):
            """Propose a download to the user for approval. Does NOT start it. Takes a direct file URL or magnet link.
            Safety data comes from the inspection, not from you. files: torrent file indexes (empty means all)."""
            if state["proposals"] >= MAX_PROPOSALS:
                return {"error": f"at most {MAX_PROPOSALS} proposals per request"}
            report = self._report(url, cache)  # never model-supplied safety data
            if "error" in report:
                return report
            idx = [i for i in (files or []) if isinstance(i, int) and not isinstance(i, bool)]
            p = self.jobs.propose(report, idx or None, str(note or "")[:500])
            state["proposals"] += 1
            events.append({"type": "proposal", "proposal": p})
            return {"proposal_id": p["id"], "status": "pending"}

        def job_status(job_id: str):
            """Get the state of a download job by id."""
            return self.jobs.get_job(job_id)

        def list_jobs():
            """List the latest download jobs."""
            return sorted(self.jobs.list_jobs(), key=lambda j: j.get("created") or "", reverse=True)[:10]

        main = [safe(f) for f in (inspect_link, propose_download, job_status, list_jobs)]
        research = [safe(f) for f in (web_search, web_scrape)]
        return main, research

    def build(self, events, run=None, checkpointer=None):
        """The compiled LangGraph agent for one run."""
        # no auto-added general-purpose sub-agent: only the researcher below
        register_harness_profile("groq", HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))
        main, research = self._tools(events, {} if run is None else run)
        researcher = {
            "name": "researcher",
            "description": "Searches the web and reads pages to find candidate download links for a request. "
                           "Give it what the user wants; it returns up to 10 candidates with direct file URLs.",
            "system_prompt": RESEARCHER_PROMPT,
            "tools": research,
            "middleware": [_gate(RESEARCH_TOOLS)],
        }
        return create_deep_agent(model=self.llm, tools=main, system_prompt=SYSTEM_PROMPT,
                                 subagents=[researcher], middleware=[_gate(MAIN_TOOLS)], checkpointer=checkpointer)

    # ---- run: streams chat events (contracts/events.md) ----
    def run(self, history, user_text):
        try:
            yield from self._run(history, user_text)
        except Exception as e:
            if isinstance(e, GraphRecursionError):
                yield {"type": "error", "message": f"Stopped after {self.max_steps} steps without a final answer."}
            elif "tool_use_failed" in str(e) or "Parsing failed" in str(e):
                yield {"type": "error", "message": "The model produced an invalid tool call. Please try again."}
            else:
                yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
        yield {"type": "done"}

    def _run(self, history, user_text):
        history.append({"role": "user", "content": user_text})
        events, run, final = [], {}, []
        graph = self.build(events, run, checkpointer=InMemorySaver())  # checkpointer: lets the nudge continue the run
        config = {"recursion_limit": self.max_steps * 2 + 1, "configurable": {"thread_id": "run"}}
        yield from self._relay(graph.stream({"messages": [dict(m) for m in history]}, config=config,
                                            stream_mode="updates", subgraphs=True), events, final)
        if run["state"]["proposals"] == 0 and run["cache"]:  # inspected links but made no card: ask once
            yield from self._relay(graph.stream({"messages": [{"role": "user", "content": NUDGE}]}, config=config,
                                                stream_mode="updates", subgraphs=True), events, final)
        if final:  # keep plain turns only; tool traffic lives inside the run
            history.append({"role": "assistant", "content": "\n\n".join(final)})

    def _relay(self, stream, events, final):
        """LangGraph updates -> chat events (main-agent text, every tool step, proposals)."""
        for ns, update in stream:
            for node_out in update.values():
                msgs = node_out.get("messages") if isinstance(node_out, dict) else None
                for m in msgs if isinstance(msgs, list) else []:
                    if getattr(m, "type", "") != "ai":
                        continue
                    text = _text(m.content)
                    if text and not ns:  # sub-agent chatter stays inside the sub-agent
                        yield {"type": "text", "text": text}
                        final.append(text)
                    for c in m.tool_calls or []:
                        yield {"type": "tool_step", "name": STEP_NAMES.get(c["name"], c["name"]),
                               "args": {k: _trim(v, 200) for k, v in (c.get("args") or {}).items()}}
            while events:
                yield events.pop(0)


def _schema(fn):
    """Pydantic args schema from the function's signature (keeps tool names/args exactly as in the contract)."""
    fields = {}
    for name, p in pyinspect.signature(fn).parameters.items():
        ann = p.annotation if p.annotation is not pyinspect.Parameter.empty else str
        fields[name] = (ann, ... if p.default is pyinspect.Parameter.empty else p.default)
    return create_model(fn.__name__ + "_args", **fields)
