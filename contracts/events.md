# Contract: HTTP API and events (v2, owner of server: api)

Library, chats, streaming and access details: contracts/library.md (source of truth for those).

Server: FastAPI, binds `127.0.0.1:8000`. Loads `.env` at startup. Serves `static/index.html` at `/`.
All bodies are JSON. Errors: `{"detail": "message"}` with the status below.

## Endpoints
| Method, path | Body | Response |
|---|---|---|
| `POST /api/chat` | `{"session": "str", "message": "str"}` | `text/event-stream` of **chat events**, ends with `done`. `session` is the chat id (created on first use). Model history is kept per chat in memory (last 40 messages); the transcript is persisted (see below). Empty or >4000-char message -> 422 |
| `POST /api/chats` | | a new Chat `{"id": "c_...", "title": "New download", "created", "updated"}` |
| `GET /api/chats` | | all chats, newest `updated` first |
| `GET /api/chats/{id}` | | `{"chat": Chat, "events": [...]}` (stored chat events, in order). 404 |
| `DELETE /api/chats/{id}` | | 204; the chat, its events and its in-memory history (not its downloads). 404 |
| `GET /api/proposals` | | list of proposals |
| `POST /api/proposals/{id}/approve` | `{"confirm_risky": false}` | the new **job** (200). 404 unknown, 409 `BadState`, 409 with `{"detail": "needs_confirm"}` if risky and not confirmed |
| `POST /api/proposals/{id}/reject` | | the proposal (200). 404 / 409 |
| `GET /api/jobs` | | list of jobs, newest first |
| `POST /api/jobs/{id}/cancel` | | the job (200). 404 |
| `GET /api/events` | | `text/event-stream` of **global events**, with a comment heartbeat `: ping` every 15 s |
| `GET /api/library` | | `[Item]`, newest first |
| `GET /api/library/{id}` | | `Item`. 404 |
| `GET /api/library/{id}/poster` | | `image/jpeg`, `Cache-Control: private, max-age=86400`. 404 when none |
| `GET /api/library/{id}/stream?file=N` | | the file (default `main`), `Content-Disposition: inline; filename*=UTF-8''<name>`, Range (206 + `Content-Range`, `Accept-Ranges: bytes`; unsatisfiable -> 416, malformed -> 400). `file` must be an int >= 0 (else 422); unknown index or a path escaping the job folder -> 404. Also accepts `&exp=<unix>&sig=<hex>` (a signed link, no cookie needed; bad or expired -> 403). HEAD supported |
| `GET /api/library/{id}/download?file=N` | | same bytes, `attachment; filename*=UTF-8''<name>`, Range supported |
| `POST /api/library/{id}/link?file=N` | | `{"url": "/api/library/{id}/stream?file=N&exp=..&sig=..", "expires": <unix>}`, valid 6 h. 404 |
| `DELETE /api/library/{id}` | | 204. 404 |
| `POST /api/login` | `{"token": "str"}` | `{"ok": true}` + `mdm_session` cookie (30 days). Wrong token -> 401; `MDM_TOKEN` unset -> 400 |
| `POST /api/logout` | | `{"ok": true}`, clears the cookie |
| `GET /api/health` | | `{"ok": true, "groq_key": bool, "firecrawl": bool (reachable), "mdm_cli": bool}` (booleans only, never values) |

**Only `approve` starts a download.** No other endpoint and no agent tool may. Exception: links in the user's own chat message (http/https/ftp/magnet) are inspected and approved in code by `Agent.run` (no model call), unless blocked, answered with HTTP >= 400, or rated risky (then a pending card).

## Access
- Host guard: `localhost`, `127.0.0.1` plus `ALLOWED_HOSTS` (comma-separated env); anything else -> 403 `bad host`. `ALLOWED_HOSTS` without `MDM_TOKEN`: startup fails.
- Non-GET/HEAD requests carrying an `Origin` must be same-origin -> else 403 `bad origin`.
- `MDM_TOKEN` unset: no login (local use). Set: every `/api/*` request needs the `mdm_session` cookie (HttpOnly, SameSite=Strict, Secure over https or when a loopback proxy sends `X-Forwarded-Proto: https`), except `POST /api/login`, `GET /api/health` and signed stream links; otherwise 401 `{"detail": "login required"}`. `/` and `/static/*` stay open so the login form loads.
- Cookie = `<exp>.<HMAC-SHA256>` keyed by the server secret and the token (changing `MDM_TOKEN` signs everyone out). Server secret: `MDM_SECRET`, else `$LIBRARY_DIR/.secret`.
- CSP: `img-src 'self' https: data:`, `media-src 'self'` (posters, `<video>`/`<audio>` streaming).

## SSE framing
Each event: `data: <one-line JSON>\n\n`.

## Chat events (from `Agent.run`, relayed by `/api/chat`)
| `type` | fields |
|---|---|
| `tool_step` | `name` (tool name), `args` (object, long strings trimmed to 200 chars) |
| `proposal` | `proposal` (a Proposal object, status `pending`) |
| `text` | `text` (assistant message, may arrive in one piece) |
| `error` | `message` |
| `done` | none (always last) |

Persisted per chat (`jobs.append_chat_event`), in stream order: `{"type": "user", "text"}` before the agent runs, then every `tool_step`, `text`, `error` as streamed, and `proposal` stored as `{"type": "proposal", "id"}`. `done` is not stored. A storage failure is logged and never breaks the stream.

## Global events (from `Jobs.subscribe`, relayed by `/api/events`)
| `type` | fields |
|---|---|
| `proposal` | `proposal` (any status) |
| `job` | `job` (emitted on every state change and at most once per second for progress) |
| `job_done` | `job` (state `done`, `error` or `cancelled`; the UI shows a toast) |
| `item` | `item` (an Item, when it is created or its poster arrives) |

## UI rules (static/index.html)
- Chat thread; user text, assistant text and `tool_step` chips ("Searching the web...", "Checking link...") in order.
- A `proposal` becomes a **preview card**: image, title, host, file name, size (human readable), content type, file list for torrents, the safety badge (`ok` -> "Looks OK", `caution` -> "Caution", `risky` -> "Risky") with every reason and every skipped check listed, and buttons **Download** and **Skip**. The card says "Safety checks are heuristics, not a guarantee."
- `risky`: Download first asks "This looks risky. Download anyway?" and only then sends `confirm_risky: true`.
- After approve, the card turns into a progress row (bar, speed, size) driven by `job` events; on `job_done` show a toast and, on `done`, the saved path.
- All model- and web-derived text is inserted with `textContent` (never `innerHTML`); `image` is shown only if it starts with `https://`.
- Reload restores cards and jobs from `GET /api/proposals` and `GET /api/jobs`.
