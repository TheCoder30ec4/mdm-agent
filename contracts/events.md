# Contract: HTTP API and events (v1, owner of server: web)

Server: FastAPI, binds `127.0.0.1:8000`. Loads `.env` at startup. Serves `static/index.html` at `/`.
All bodies are JSON. Errors: `{"detail": "message"}` with the status below.

## Endpoints
| Method, path | Body | Response |
|---|---|---|
| `POST /api/chat` | `{"session": "str", "message": "str"}` | `text/event-stream` of **chat events**, ends with `done`. History is kept per `session` in memory (last 40 messages). Empty or >4000-char message -> 422 |
| `GET /api/proposals` | | list of proposals |
| `POST /api/proposals/{id}/approve` | `{"confirm_risky": false}` | the new **job** (200). 404 unknown, 409 `BadState`, 409 with `{"detail": "needs_confirm"}` if risky and not confirmed |
| `POST /api/proposals/{id}/reject` | | the proposal (200). 404 / 409 |
| `GET /api/jobs` | | list of jobs, newest first |
| `POST /api/jobs/{id}/cancel` | | the job (200). 404 |
| `GET /api/events` | | `text/event-stream` of **global events**, with a comment heartbeat `: ping` every 15 s |
| `GET /api/health` | | `{"ok": true, "groq_key": bool, "firecrawl": bool (reachable), "mdm_cli": bool}` (booleans only, never values) |

**Only `approve` starts a download.** No other endpoint and no agent tool may.

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

## Global events (from `Jobs.subscribe`, relayed by `/api/events`)
| `type` | fields |
|---|---|
| `proposal` | `proposal` (any status) |
| `job` | `job` (emitted on every state change and at most once per second for progress) |
| `job_done` | `job` (state `done`, `error` or `cancelled`; the UI shows a toast) |

## UI rules (static/index.html)
- Chat thread; user text, assistant text and `tool_step` chips ("Searching the web...", "Checking link...") in order.
- A `proposal` becomes a **preview card**: image, title, host, file name, size (human readable), content type, file list for torrents, the safety badge (`ok` -> "Looks OK", `caution` -> "Caution", `risky` -> "Risky") with every reason and every skipped check listed, and buttons **Download** and **Skip**. The card says "Safety checks are heuristics, not a guarantee."
- `risky`: Download first asks "This looks risky. Download anyway?" and only then sends `confirm_risky: true`.
- After approve, the card turns into a progress row (bar, speed, size) driven by `job` events; on `job_done` show a toast and, on `done`, the saved path.
- All model- and web-derived text is inserted with `textContent` (never `innerHTML`); `image` is shown only if it starts with `https://`.
- Reload restores cards and jobs from `GET /api/proposals` and `GET /api/jobs`.
