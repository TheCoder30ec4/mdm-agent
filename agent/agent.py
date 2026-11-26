"""Download assistant on Deep Agents (LangGraph). The model comes from Settings (agent/llm.py); Groq gpt-oss-120b by default.

Safety: no tool here can start a download (propose_download only creates a pending proposal), and a gate
middleware hides and blocks every tool the harness adds by default (shell `execute`, filesystem tools).
"""
import inspect as pyinspect
import json
import re
from pathlib import Path

from deepagents import (GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent,  # Deep Agents harness
                        register_harness_profile)
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from pydantic import create_model

from agent import inspect as inspect_mod
from agent import llm as llm_mod
from agent import settings as settings_mod
from agent import web as web_mod
from agent.prompts import RESEARCHER_PROMPT, SYSTEM_PROMPT

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
    def __init__(self, jobs, llm=None, model=None, max_steps=25, settings_path=None):
        self.jobs = jobs
        self._fixed = llm  # any LangChain chat model; otherwise built from Settings
        self.model = model  # overrides the Settings model
        self.max_steps = max_steps
        lib = getattr(jobs, "library_dir", None)
        self.settings_path = settings_path or (lib / "settings.json" if isinstance(lib, Path) else None)
        self._built = (None, None)  # (config it was built from, model)
        self._web = None
        self._inspect = None

    # built on first use, so importing this module needs no key; rebuilt when Settings change.
    # A chat already running keeps the model its graph was built with.
    @property
    def llm(self):
        if self._fixed is not None:
            return self._fixed
        cfg = settings_mod.effective(self.settings_path)
        if self.model:
            cfg["model"] = self.model
        if self._built[0] != cfg:
            self._built = (cfg, llm_mod.make(cfg))
        return self._built[1]

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

        def propose_download(url: str, files: list[int] | None = None, note: str = "", checksum: str | None = None,
                             mode: str | None = None, height: int | None = None, subs: str | None = None):
            """Propose a download to the user for approval. Does NOT start it. Takes a direct file URL or magnet link.
            Safety data comes from the inspection, not from you. files: torrent file indexes (empty means all).
            Optional: checksum ("sha256:<hex>" the file must match); mode "video" for a web page whose inspect_link
            report has `video` (downloaded as a video: height such as 720, subs such as "en,fr")."""
            if state["proposals"] >= MAX_PROPOSALS:
                return {"error": f"at most {MAX_PROPOSALS} proposals per request"}
            report = self._report(url, cache)  # never model-supplied safety data
            if "error" in report:
                return report
            idx = [i for i in (files or []) if isinstance(i, int) and not isinstance(i, bool)]
            extra = {k: v for k, v in (("checksum", checksum), ("mode", mode), ("height", height), ("subs", subs))
                     if v not in (None, "")}  # only what the model set, so plain proposals stay as before
            p = self.jobs.propose(report, idx or None, str(note or "")[:500], **extra)
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
        # no auto-added general-purpose sub-agent: only the researcher below. deepagents keys harness profiles
        # by the model's LangSmith provider (groq, anthropic, openai, google_genai, ...), so register that one.
        model = self.llm
        register_harness_profile(_provider(model), HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))
        main, research = self._tools(events, {} if run is None else run)
        researcher = {
            "name": "researcher",
            "description": "Searches the web and reads pages to find candidate download links for a request. "
                           "Give it what the user wants; it returns up to 10 candidates with direct file URLs.",
            "system_prompt": RESEARCHER_PROMPT,
            "tools": research,
            "middleware": [_gate(RESEARCH_TOOLS)],
        }
        return create_deep_agent(model=model, tools=main, system_prompt=SYSTEM_PROMPT,
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
        links = _links(user_text)
        if links:  # the user's own links: download them directly, the model is not involved
            yield from self._direct(history, links)
            return
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

    def _direct(self, history, links):
        """A link the user pasted is the user's own decision: inspect it and start the MDM download right away.
        Still enforced: the SSRF/URL guard (blocked links are refused) and one confirm click for Risky files.
        ponytail: a message with links skips the model entirely; "this link + find X" needs two messages."""
        lines = []
        for url in links[:MAX_PROPOSALS]:
            yield {"type": "tool_step", "name": "inspect_link", "args": {"url": _trim(url, 200)}}
            r = self.inspector.inspect_link(url)
            if "error" in r:
                lines.append(f"Not downloaded, {_trim(url, 80)}: {r['error']}")
                continue
            status = r.get("http_status")
            if isinstance(status, int) and status >= 400 and status != 416:
                hint = " (the site is busy or rate-limiting; try again in a few minutes)" if status in (429, 503) else ""
                lines.append(f"Not downloaded, {_trim(url, 80)}: the server answered HTTP {status}{hint}.")
                continue
            # a page that holds a video (inspect found `video` info) is downloaded as a video, not saved as HTML
            p = self.jobs.propose(r, None, "Link you pasted", **({"mode": "video"} if r.get("video") else {}))
            name = r.get("filename") or r.get("host") or url
            try:
                job = self.jobs.approve(p["id"])  # the paste is the approval
            except Exception as e:
                if type(e).__name__ != "NeedsConfirm":
                    raise
                yield {"type": "proposal", "proposal": p}
                lines.append(f"{name} looks risky ({'; '.join(r['safety'].get('reasons') or [])}). "
                             "Press Download on its card if you still want it.")
                continue
            yield {"type": "proposal", "proposal": {**p, "status": "approved", "job_id": job["id"]}}
            size = f"{r['size'] / 1e6:.1f} MB" if isinstance(r.get("size"), int) else "size unknown"
            lines.append(f"Downloading {name} ({size}) with Mister Download Manager. You'll be told when it finishes.")
        if len(links) > MAX_PROPOSALS:
            lines.append(f"Only the first {MAX_PROPOSALS} links were used.")
        text = "\n".join(lines)
        yield {"type": "text", "text": text}
        history.append({"role": "assistant", "content": text})

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


def _provider(model):
    """The provider name deepagents looks harness profiles up by (BaseChatModel._get_ls_params)."""
    try:
        return model._get_ls_params().get("ls_provider") or type(model).__name__.lower()
    except Exception:
        return type(model).__name__.lower()


_LINK = re.compile(r"""(?:https?|ftps?)://[^\s<>"']+|magnet:\?[^\s<>"']+""", re.I)


def _links(text):
    """URLs and magnet links in the user's own message (trailing punctuation dropped, duplicates removed)."""
    return list(dict.fromkeys(m.rstrip(".,;:!?)]}") for m in _LINK.findall(text or "")))


def _schema(fn):
    """Pydantic args schema from the function's signature (keeps tool names/args exactly as in the contract)."""
    fields = {}
    for name, p in pyinspect.signature(fn).parameters.items():
        ann = p.annotation if p.annotation is not pyinspect.Parameter.empty else str
        fields[name] = (ann, ... if p.default is pyinspect.Parameter.empty else p.default)
    return create_model(fn.__name__ + "_args", **fields)
