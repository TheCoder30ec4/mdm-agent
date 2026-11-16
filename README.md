# MDM Agent

A chatbot that finds things on the web and downloads them with
[Mister Download Manager](https://github.com/anefzaoui/misterdownloadmanager) (MDM, [fork](https://github.com/TheCoder30ec4/misterdownloadmanager)), **only after you approve**.

You paste a link or describe what you want. The agent searches (self-hosted Firecrawl), inspects the best candidates, and
replies with a **preview card**: what it is, the source, size, type, files, and a safety badge (`Looks OK` / `Caution` / `Risky`)
with reasons. Nothing downloads until you press **Download**. When it finishes you get a toast and a macOS notification.

- Agents: built with [Deep Agents](https://github.com/langchain-ai/deepagents) on [LangGraph](https://github.com/langchain-ai/langgraph).
  A main agent (inspect, propose, job status) hands web research to a `researcher` sub-agent (search, scrape).
  The harness's built-in shell and file tools are hidden from the model and refused at dispatch.
- Brain: `openai/gpt-oss-120b` on [Groq](https://groq.com) via `langchain-groq` (too big to run on a laptop). The agents run locally.
- Search and page previews: [Firecrawl](https://github.com/firecrawl/firecrawl), **self-hosted in Docker** on `127.0.0.1:3002`.
- Downloads: MDM's headless CLI (`misterdownloadmanager-cli`), HTTP(S), FTP and magnet links.

The model has no tool that starts a download. It can only create a *proposal*; only the Download button's HTTP call
(`POST /api/proposals/{id}/approve`) starts a job. Safety checks are heuristics, not a guarantee: a clean badge never means virus-free.

## Setup (macOS)
```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # then add GROQ_API_KEY (and optionally VIRUSTOTAL_API_KEY)
bash scripts/install_mdm.sh     # downloads the MDM macOS release, verifies SHA-256, copies the CLI into vendor/mdm
bash scripts/firecrawl_local.sh up    # needs Docker Desktop; first run pulls several GB. `down` stops it
.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8000
```
Open http://127.0.0.1:8000. A banner tells you what is missing (Groq key, Firecrawl, MDM CLI).

Tests: `.venv/bin/python -m pytest` (no network or keys needed).

## Notes
- MDM's macOS build is marked experimental upstream. On Linux or Windows set `MDM_CLI` to your installed `misterdownloadmanager-cli`.
- Firecrawl images are the official prebuilt `ghcr.io/firecrawl/*:latest` with the compose file pinned to release `v2.11.0`; search goes through a SearXNG container.
- The agent prefers legal sources (Internet Archive, public domain, Creative Commons, official free releases, content you own) and is instructed to decline copyrighted material from infringing sources. You are responsible for what you download.
- `inspect_link` and page scraping refuse localhost/private addresses (SSRF guard); inspection never downloads a file body.
- Known limit: Firecrawl and MDM resolve DNS and follow redirects themselves, so a host that redirects to your LAN only after inspection is not caught.
- Torrents: magnet links only (MDM's CLI takes a magnet or a local `.torrent` file).
- Licence: GPL-3.0, as MDM.
