# Contract: modules and agent tools (v1, source of truth)

Python 3.10+. All modules read config from environment (loaded from `.env` by `server.py` only):
`GROQ_API_KEY`, `FIRECRAWL_API_KEY`, `VIRUSTOTAL_API_KEY` (optional), `MDM_CLI` (default `./vendor/mdm/misterdownloadmanager-cli`),
`LIBRARY_DIR` (default `~/MDM Library`; holds `mdm.db`, `files/<job id>/`, `posters/`), `DOWNLOAD_DIR` and `DATA_DIR` (optional, override the files folder and the folder holding `mdm.db`), `MDM_DNS` (optional, passed to `get --dns`), `GROQ_MODEL` (default `openai/gpt-oss-120b`).
Never log or echo key values.

## agent/web.py (owner: tools)
```python
def search(query: str, limit: int = 5) -> list[dict]   # [{"title","url","description"}]; raises WebError
def scrape(url: str, max_chars: int = 8000) -> dict     # {"url","title","description","image","text"}; text = markdown truncated; raises WebError
class WebError(Exception)
```
Uses Firecrawl (`firecrawl-py`), **self-hosted by default**: `FIRECRAWL_API_URL` (default `http://localhost:3002`), `FIRECRAWL_API_KEY` optional (a self-hosted API ignores it; set both to use another instance or the cloud). Connection failures raise `WebError`.

## agent/inspect.py (owner: tools)
```python
def inspect_link(url: str) -> dict    # never raises for bad input: returns a Report with safety.level "risky" or {"error": "..."} for blocked targets
```
Report (JSON-serialisable):
```json
{"url": "...", "final_url": "...", "host": "archive.org", "kind": "http|magnet",
 "title": "str|null", "description": "str|null", "image": "url|null",
 "filename": "str|null", "size": 123456, "content_type": "video/mp4",
 "files": [{"index": 0, "path": "a/b.mkv", "size": 1}],
 "safety": {"level": "ok|caution|risky", "reasons": ["plain-language strings"], "skipped": ["checks that could not run, e.g. 'VirusTotal: no API key'"]}}
```
- `size`, `title`, etc. are `null` when unknown. `files` is only filled for magnet links (via `torrent-info`), else `[]`.
- `video` (only present when found): for an HTML page, `mdm.video_info` runs `video-info`; a page it understands adds `"video": {"title", "heights": [1080, 720], "subtitles": [{"code","name","automatic"}]}` (the CLI reports no duration or thumbnail). When yt-dlp is missing, `safety.skipped` gains `"yt-dlp is not installed: run misterdownloadmanager-cli install-ytdlp"` (the level is unchanged). Any other `video-info` failure is silent.
- Rules: HEAD; if HEAD is refused (400/403/405/501), one ranged GET (`Range: bytes=0-0`) whose body is never read. Follow at most 5 redirects, re-checking each hop.
- SSRF guard: refuse (return `{"error": "blocked: ..."}`) for non-http(s)/ftp(s)/magnet schemes, hosts resolving to loopback, link-local, private, or reserved addresses (check every hop).
- Safety: `risky` if executable/installer extension (`.exe .msi .scr .bat .cmd .ps1 .jar .apk .dmg .pkg .com .vbs`), a double extension ending in one of those, or a VirusTotal malicious verdict. `caution` for http/ftp (no TLS), content-type vs extension mismatch, redirect to another host, URL shortener, IP-literal or punycode host, archive/disk-image types (`.zip .iso .rar .7z`), unknown size, or any skipped check worth noting. Otherwise `ok`. A level never improves because of a missing check.

## agent/mdm.py (owner: tools)
```python
def run(source: str, dest_dir: str, files: list[int] | None = None, on_event=None, cancel=None, download_dir=None, *,
        checksum=None, mode=None, height=None, subs=None, resume=None) -> dict   # {"path": str, "size": int|None}
def torrent_info(source: str) -> dict   # {"name","info_hash","size","files":[{"index","size","path"}]}
def video_info(source: str) -> dict | None   # {"title","heights":[...],"subtitles":[...]}; None for a list / no video; MdmError("yt-dlp is not installed ...") when missing
def check_options(checksum=None, mode=None, height=None, subs=None) -> None   # MdmError for a bad value
class MdmError(Exception)
```
- Spawns `MDM_CLI --json get SOURCE -o DEST_DIR` for http/https/ftp(s) or `MDM_CLI --json torrent-get SOURCE -o DEST_DIR [--files 0,2]` for magnet links only (the CLI's torrent commands take a magnet or a local path; an http(s) `.torrent` URL is downloaded with `get` as a file). Args as a list, no shell.
- `checksum` adds `--checksum <value>` to `get`; `MDM_DNS` adds `--dns <value>` to `get` only (`video-get` and `resume` have no such flag).
- `mode="video"` runs `video-get SOURCE -o DEST -c N [--quality HEIGHT] [--subs LANGS]` instead (the flag is `--quality`, not `--height`); its `done` line carries the saved file's `path` and no `size`.
- `resume=<path>` (a `*.mdm.json` inside `dest_dir`, else `MdmError`) runs `resume <state> -c N`; the URL comes from the state file.
- Parses JSON lines: `{"event":"progress","downloaded","size","speed",...}` -> `on_event({"downloaded","size","speed"})`; `{"event":"done","path","size"}` ends successfully; non-zero exit or an `error` line -> `MdmError(message)`.
- `cancel`: a `threading.Event`; when set, terminate the subprocess and raise `MdmError("cancelled")`.
- `dest_dir` must resolve inside `DOWNLOAD_DIR`; else `MdmError`.

## agent/notify.py (owner: tools)
```python
def notify(title: str, message: str) -> None   # macOS: osascript display notification (arg-safe, no string interpolation into a shell); other OS: no-op. Never raises.
```

## agent/jobs.py (owner: tools). Shapes in contracts/jobs.md
```python
class NotFound(Exception); class NeedsConfirm(Exception); class BadState(Exception)
class Jobs:
    def __init__(self, data_dir=None, download_dir=None, runner=mdm.run, notifier=notify.notify, library_dir=None): ...
    def propose(self, report: dict, files: list[int] | None = None, note: str = "", *, checksum=None, mode=None, height=None, subs=None) -> dict   # creates pending proposal; NEVER starts anything; ValueError for bad options
    def approve(self, proposal_id: str, confirm_risky: bool = False) -> dict                   # only way to start a job; NeedsConfirm if safety.level=="risky" and not confirm_risky; BadState if not pending
    def reject(self, proposal_id: str) -> dict
    def cancel(self, job_id: str) -> dict
    def get_job(self, job_id: str) -> dict; def list_jobs(self) -> list[dict]; def list_proposals(self) -> list[dict]
    def subscribe(self) -> "queue.Queue[dict]"; def unsubscribe(self, q) -> None   # receives every event in contracts/events.md (proposal + job)
```
State persists to SQLite (`mdm.db`, agent/library.py; schema, items, chats and the rest of the `Jobs` interface in contracts/library.md). On startup, a `queued`/`running` job whose folder has a `*.mdm.json` is resumed; the others become `error` ("interrupted"). One thread per job. On done: build the library Item, emit `item`, then `job`/`job_done`, call `notifier`, and make the poster on a worker thread (agent/poster.py). On error: emit event and call `notifier(title, message)`.
The runner is called as `runner(url, dest, files=, on_event=, cancel=, **kw)`; `kw` holds only what is set: `download_dir` (real `mdm.run` only), `checksum`, `mode`, `height`, `subs`, and `resume` (state file path) for a resumed job.
`library_dir` (optional) defaults to `data_dir` when that is given, else `LIBRARY_DIR`, else `~/MDM Library`; `posters/` sits beside `mdm.db`.

## agent/agent.py (owner: core): Deep Agents on LangGraph
```python
class Agent:
    def __init__(self, jobs: Jobs, llm=None, model: str | None = None, max_steps: int = 25): ...
    def build(self, events: list) -> CompiledStateGraph   # deepagents.create_deep_agent(...) for one run
    def run(self, history: list[dict], user_text: str) -> Iterator[dict]   # yields chat events (contracts/events.md)
```
- Framework: `deepagents.create_deep_agent` (a LangGraph `CompiledStateGraph`), streamed with `stream_mode="updates", subgraphs=True`.
- `llm`: any LangChain chat model; default `langchain_groq.ChatGroq(model=GROQ_MODEL or "openai/gpt-oss-120b")`, built lazily (importing needs no key).
- Agents: the **main** agent sees `inspect_link`, `propose_download`, `job_status`, `list_jobs`, `task`; one **`researcher`** sub-agent (reached through `task`, subagent_type `"researcher"` only) sees `web_search`, `web_scrape`. The harness's default general-purpose sub-agent is disabled (HarnessProfile for provider `groq`).
- A gate middleware on each agent hides every other tool from the model (the harness's `execute`, `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, `grep`) and refuses any such call at dispatch. It also retries Groq's 400 "tool_use_failed"/"Parsing failed" twice, with a reminder of the exact tool names.
- `history` holds only `{"role": "user"|"assistant", "content": str}` turns; tool traffic stays inside a run.
- `max_steps` maps to the LangGraph `recursion_limit` (`2*max_steps+1`); exceeding it yields `error` "Stopped after N steps...".
- Tool steps from the sub-agent are relayed as `tool_step` events; the `task` hand-off is shown as `tool_step` name `research`.
Tool names and args are exact. **There is no tool that starts a download.**

| name | args | returns to model |
|---|---|---|
| `web_search` | `query: str`, `limit: int=5` (1-10) | `web.search` result list |
| `web_scrape` | `url: str` | `web.scrape` result (text truncated to 4000 chars for the model) |
| `inspect_link` | `url: str` | the Report (image and long text trimmed), also cached by url for this run |
| `propose_download` | `url: str`, `files: list[int] = []`, `note: str = ""`, optional `checksum: str`, `mode: "video"`, `height: int`, `subs: str` (passed to `jobs.propose` only when set) | `{"proposal_id", "status": "pending"}` after `jobs.propose(report, files, note)` using the **cached or freshly computed Report** (never model-supplied safety data); emits a `proposal` chat event |
| `job_status` | `job_id: str` | job object |
| `list_jobs` | none | list of job objects (latest 10) |

Tool errors (WebError, MdmError, blocked URL) are returned to the model as `{"error": "..."}`, never raised. Loop ends when the model answers without tool calls or after `max_steps` (then yields an `error` event). System prompt (agent/prompts.py `SYSTEM_PROMPT`) must say:
1. Search broadly, then `inspect_link` the best candidate(s) before proposing; prefer official/legal sources (Internet Archive, public domain, Creative Commons, official free releases, content the user owns); decline to fetch copyrighted material from infringing sources and say why.
2. Call `propose_download` for at most 10 candidates per request, then tell the user what each is and its safety verdict, and that they decide with the Download button.
3. Never say a download has started or finished; only jobs the user approves run, and the UI reports them.
4. When the user pastes a link, inspect it first. Keep replies short.

## Hardening added after review (v1.1)
- URLs containing a backslash, whitespace or control character, a `%` in the host, or a host that urllib and httpx read differently are refused (`blocked: ...` from inspect, `MdmError` from mdm). Unicode (IDN) hosts must be given in punycode.
- `web.scrape`: public http/https URLs only (same SSRF check as inspect), else `WebError("blocked: ...")`.
- Risky also covers `.sh .command .app .scpt .workflow .dll .lnk .js .hta .reg .docm .xlsm .pptm .deb .rpm .appimage`, executable content types (`application/x-msdownload`, `x-dosexec`, `x-msdos-program`, `vnd.android.package-archive`, `x-apple-diskimage`, `java-archive`, `x-sh`, `x-executable`, `x-mach-binary`), file names in common query keys, names with trailing dots/spaces, and archives wrapping a risky extension.
- VirusTotal lookups strip `user:pass@` from URLs.
- Known residual risk: Firecrawl and MDM resolve DNS again and follow redirects themselves, so a host that redirects to the LAN only after inspection is not caught.
