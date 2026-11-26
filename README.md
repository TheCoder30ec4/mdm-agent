# MDM Agent

A chatbot that finds things on the web and downloads them with
[Mister Download Manager](https://github.com/anefzaoui/misterdownloadmanager) (MDM). The engine is built from [the fork](https://github.com/TheCoder30ec4/misterdownloadmanager), included here as the `misterdownloadmanager/` git submodule, **only after you approve**.

You paste a link or describe what you want. The agent searches (self-hosted Firecrawl), inspects the best candidates, and
replies with a **preview card**: what it is, the source, size, type, files, and a safety badge (`Looks OK` / `Caution` / `Risky`)
with reasons. Nothing downloads until you press **Download**. When it finishes you get a toast and a macOS notification.

- Agents: built with [Deep Agents](https://github.com/langchain-ai/deepagents) on [LangGraph](https://github.com/langchain-ai/langgraph).
  A main agent (inspect, propose, job status) hands web research to a `researcher` sub-agent (search, scrape).
  The harness's built-in shell and file tools are hidden from the model and refused at dispatch.
- Brain: `openai/gpt-oss-120b` on [Groq](https://groq.com) via `langchain-groq` (too big to run on a laptop). The agents run locally.
- Search and page previews: [Firecrawl](https://github.com/firecrawl/firecrawl), **self-hosted in Docker** on `127.0.0.1:3002`.
- Downloads: MDM's headless CLI (`misterdownloadmanager-cli`), built from the fork: HTTP(S) and FTP with 16 parallel connections (`MDM_CONNECTIONS`), and magnet links.
- Progress shows speed, time left, connections (or torrent peers) and why a download is slow.

**Paste a link** (http, https, ftp or magnet) and it downloads right away through MDM: the model is not involved, the link still goes through the URL/SSRF guard, links whose server answers an error are reported instead of started, and files rated *Risky* wait for one click.
**Describe what you want** and the agents search; the results come back as cards you approve.

The model has no tool that starts a download. It can only create a *proposal*; only the Download button's HTTP call
(`POST /api/proposals/{id}/approve`) starts a job. Safety checks are heuristics, not a guarantee: a clean badge never means virus-free.

## Quick start (macOS)
```sh
./run.sh
```
Creates the venv, installs dependencies, builds MDM (or installs the prebuilt one), starts Firecrawl if Docker is running, and opens http://127.0.0.1:8000. On the first run it creates `.env` and stops: add `GROQ_API_KEY` (and optionally `VIRUSTOTAL_API_KEY`), then run `./run.sh` again. Re-running is safe.

## Desktop app (Mac and Windows)
A double-click app: a window with the chat, and an icon in the menu bar (Mac) or the system tray (Windows) with **Open**, **Open library folder** and **Quit**. Closing the window only hides it (downloads keep going); Quit stops everything. Opening it again brings the running window back instead of starting a second copy. MDM is built in; nothing else to install.

1. Download the zip: from the [Releases](../../releases) page (tagged versions), or from a run of the **Desktop app** workflow under Actions (its Artifacts). `MDM-Agent-macos-arm64.zip` is for Apple Silicon Macs, `MDM-Agent-windows-x64.zip` for 64-bit Windows.
2. Unzip it. Mac: move `MDM Agent.app` to Applications. Windows: keep the `MDM Agent` folder together and start `MDM Agent.exe` inside it.
3. The app is not signed, so the first start needs one extra step:
   - **macOS**: right-click `MDM Agent.app`, choose **Open**, then **Open** again. If macOS only offers to move it to the Bin, open System Settings, Privacy & Security, and click **Open Anyway** next to MDM Agent.
   - **Windows**: on the blue "Windows protected your PC" screen click **More info**, then **Run anyway**. Windows needs the Microsoft Edge WebView2 runtime, which Windows 10 and 11 normally have.
4. Choose your AI provider in **Settings** (bottom of the sidebar): Groq, Anthropic, OpenAI, Google or any OpenAI-compatible server (Ollama, LM Studio), with its API key. Web search needs Firecrawl (a cloud key or your own URL) under Web search in the same dialog; without it, pasted links still download.

The library (catalogue, downloaded files, posters and the settings) lives in `~/MDM Library` (Windows: `C:\Users\<you>\MDM Library`); the tray's **Open library folder** opens it. The app listens only on `127.0.0.1`, on a free port. Posters need `ffmpeg` on the PATH (Mac: `brew install ffmpeg`). If the app does not start, `.desktop.log` in the library folder says why.

Build it yourself: `pip install -r requirements.txt -r requirements-desktop.txt`, build MDM (`bash scripts/build_mdm.sh`), then `MDM_BIN_DIR=<folder with misterdownloadmanager-cli and misterdownloadmanager> pyinstaller --noconfirm desktop/mdm-agent.spec`; the app is in `dist/`. From source without building: `python desktop/app.py`.

## Linux / server (Docker)
```sh
scripts/firecrawl_local.sh up && docker compose up -d --build
```
Needs a `.env` with `GROQ_API_KEY` (`cp .env.example .env`). The first build compiles MDM from the fork, which is slow (several minutes, a few GB of disk). The app joins Firecrawl's Docker network and stays bound to `127.0.0.1:8000` (it refuses other Host headers, so do not publish it on another address). Run `mkdir -p library` first: the library (catalogue, files, posters) lives in `./library` (or `$LIBRARY_DIR`), owned by uid 1000 in the container. `docker compose down` stops it. The image includes ffmpeg (posters, video merges) and yt-dlp (video pages).

## Stream from your server (Tailscale)
1. Install [Tailscale](https://tailscale.com/download) on the server and on your laptop, and sign both in to the same tailnet.
2. In the server's `.env` set `MDM_TOKEN` (the login password; make it long and random: `openssl rand -hex 32`; failed logins are not rate limited) and `ALLOWED_HOSTS` (the server's MagicDNS name, e.g. `box.tailnet-1234.ts.net`). Optional: `MDM_SECRET` (signs cookies and stream links; otherwise one is generated into the library). The server refuses to start with `ALLOWED_HOSTS` and no `MDM_TOKEN`.
3. Start the app (`./run.sh` or `docker compose up -d`), then on the server: `tailscale serve --bg 8000`.
4. On the laptop open `https://box.tailnet-1234.ts.net` and log in with the token. Library items stream in the browser; for mkv/avi use the item's "Open in VLC" link (a signed link valid 6 hours, no login needed).

The app never listens on `0.0.0.0`: it stays on `127.0.0.1:8000` and `tailscale serve` publishes it on the tailnet over HTTPS. Storage defaults to `~/MDM Library` (`LIBRARY_DIR` to change it).

### Manual setup (macOS)
```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # then add GROQ_API_KEY (and optionally VIRUSTOTAL_API_KEY)
git submodule update --init     # the MDM fork, in ./misterdownloadmanager
bash scripts/build_mdm.sh       # builds MDM from the fork (release mode; needs Rust: brew install rustup && rustup default stable)
# or, without Rust: bash scripts/install_mdm.sh  (prebuilt upstream release into vendor/mdm, used as a fallback)
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
