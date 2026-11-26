# Contract: AI provider settings and the desktop app (v1)

Phase 3. Source of truth for the `providers` and `desktop` tasks. Report mismatches instead of improvising.

## Providers

| `provider` | LangChain class (package) | needs | default `model` |
|---|---|---|---|
| `groq` | `ChatGroq` (langchain-groq) | key | `openai/gpt-oss-120b` |
| `anthropic` | `ChatAnthropic` (langchain-anthropic) | key | `claude-sonnet-5-5` |
| `openai` | `ChatOpenAI` (langchain-openai, new dependency) | key | `gpt-5.1` |
| `google` | `ChatGoogleGenerativeAI` (langchain-google-genai, already installed) | key | `gemini-2.5-flash` |
| `compatible` | `ChatOpenAI(base_url=...)` | `base_url`, key optional | none: `model` required |

`compatible` covers Ollama (`http://localhost:11434/v1`), LM Studio, OpenRouter, vLLM and similar. The model is free text in every case; the default only fills an empty field.

## Settings file

`$LIBRARY_DIR/settings.json`, mode 600, written atomically (temp file in the same folder, then `os.replace`). Owned by `agent/settings.py`.

```json
{"provider": "groq", "model": "openai/gpt-oss-120b", "base_url": null,
 "keys": {"groq": "gsk_...", "anthropic": null, "openai": null, "google": null, "compatible": null},
 "firecrawl_url": null, "firecrawl_key": null}
```

Precedence for each value: the settings file, then the environment (`GROQ_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `MDM_PROVIDER`, `MDM_MODEL`, `GROQ_MODEL`, `MDM_BASE_URL`, `FIRECRAWL_API_URL`, `FIRECRAWL_API_KEY`), then the defaults above. With no file and only `GROQ_API_KEY` set, behaviour is exactly as today.

## API (server.py)

Both endpoints are behind the existing `/api/*` login gate and the same-origin check.

- `GET /api/settings` returns `{"provider", "model", "base_url", "firecrawl_url", "keys": {"groq": true|false, ...}, "firecrawl_key": true|false, "providers": [{"id", "label", "needs_key", "needs_base_url", "default_model"}]}`. **Key values are never returned**, only whether each one is set.
- `PUT /api/settings` with any subset of `{"provider", "model", "base_url", "api_key", "firecrawl_url", "firecrawl_key"}`:
  - `api_key` is stored under the provider in effect after the update; `""` clears it. A key is never echoed or logged.
  - `base_url` must be `http` or `https`.
  - Returns the same shape as GET; an unknown provider is a 400.
  - The agent's model is rebuilt on the next chat; per-chat model history is kept.
- `GET /api/health` gains `"provider"` and `"llm_ready"` (provider configured with what it needs). `groq_key` stays for compatibility.
- `POST /api/settings/test` makes one tiny model call (`"Reply with OK"`, 20 s timeout). It returns `{"ok": true}` or `{"ok": false, "error": "<short message, never containing the key>"}`.

## UI (static/)

A Settings entry at the bottom of the sidebar opens a `<dialog>` with:
- a provider select
- model (text, placeholder = the default)
- base URL (shown only for `compatible`)
- an API key password field: placeholder "Saved" when set, empty means keep
- Firecrawl URL and key, under a disclosure "Web search"
- Test and Save buttons

The setup banner's "add GROQ_API_KEY" wording becomes "Choose an AI provider in Settings" with a button that opens the dialog. Everything is built with `el()` and textContent, and the CSP is unchanged.

## Desktop app (desktop/)

- `desktop/app.py`:
  - picks a free loopback port and starts uvicorn (`server:app`) on `127.0.0.1` in a thread
  - sets `MDM_CLI` to the bundled CLI
  - `LIBRARY_DIR` defaults to `~/MDM Library`, as the server does
  - opens a native window (pywebview) on the app
- **Tray / menu bar** (pystray): Open, Open library folder, Quit. Closing the window hides it; Quit stops the server.
- **Single instance:** a second launch focuses the running one, through a lock file plus the port written to `~/MDM Library/.desktop-port`.
- **Packaging:** PyInstaller one-folder build that bundles `static/`, `agent/`, `server.py` and the two MDM binaries (`misterdownloadmanager-cli` and `misterdownloadmanager`, `.exe` on Windows). Output:
  - `MDM Agent.app` zipped as `MDM-Agent-macos-arm64.zip`
  - `MDM Agent\` zipped as `MDM-Agent-windows-x64.zip`
- **CI:** `.github/workflows/desktop.yml` runs on `workflow_dispatch` and on tags `v*`:
  - matrix: macos-14 (arm64) and windows-latest (x64)
  - builds MDM from the submodule (`cargo build --locked --release -p mdm-app -p mdm-cli`), then PyInstaller
  - uploads the zips as artifacts, and attaches them to a GitHub Release on a tag
- **Unsigned:** the README says how to open an unsigned app (macOS: right-click, then Open; Windows: More info, then Run anyway).
- **Web search** works when Firecrawl is set in Settings (cloud key or URL). Without it, pasted links still work.
