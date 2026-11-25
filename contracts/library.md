# Contract: library, chats, streaming, access (v1)

Phase 2 of MDM Agent: a persistent library you can stream from a server to your laptop. Source of truth for the `store`, `api`, `ui` and `deploy` tasks. Report mismatches instead of improvising.

## Storage layout

`LIBRARY_DIR` (env). Default: `~/MDM Library` (`Path.home() / "MDM Library"`), never under `~/Library/Caches` (macOS purges it) and never inside the repo (iCloud-synced). In Docker: `/library`.

```
$LIBRARY_DIR/
  mdm.db            SQLite catalogue (stdlib sqlite3, WAL, foreign keys on)
  files/<job id>/   what MDM wrote (one folder per job, MDM picks the names)
  posters/<item id>.jpg
```

`DOWNLOAD_DIR` and `DATA_DIR` still override `files/` and the folder holding `mdm.db` when set (old configs keep working). The file bytes are never stored in the database: the DB is the catalogue, the filesystem holds the bytes, and HTTP range requests stream them.

## Schema (agent/library.py owns it)

```sql
proposals(id TEXT PK, status TEXT, created TEXT, chat_id TEXT NULL, body TEXT)       -- body: the proposal JSON (contracts/jobs.md)
jobs(id TEXT PK, proposal_id TEXT, state TEXT, created TEXT, finished TEXT NULL, body TEXT)  -- body: the job JSON
items(id TEXT PK, job_id TEXT UNIQUE, title TEXT, kind TEXT, size INTEGER, created TEXT,
      source_url TEXT, poster TEXT NULL, body TEXT)                                   -- body: the Item JSON below
chats(id TEXT PK, title TEXT, created TEXT, updated TEXT)
chat_events(chat_id TEXT, seq INTEGER, body TEXT, PRIMARY KEY(chat_id, seq))
```

`Jobs` keeps its public methods and exceptions (`list_proposals`, `approve`, `reject`, `list_jobs`, `cancel`, `subscribe`, `unsubscribe`, `propose`, `job`, NotFound/NeedsConfirm/BadState) but persists to `mdm.db` instead of `jobs.json`. No migration from `jobs.json` (the library was reset).

## Item (a finished download in the library)

```json
{"id": "i_1a2b3c4d", "job_id": "j_...", "title": "Night of the Living Dead (1968)",
 "kind": "video|audio|image|document|archive|other", "size": 734003200,
 "created": "ISO-8601", "source_url": "https://...", "poster": true,
 "files": [{"index": 0, "path": "Night.mp4", "size": 734003200, "mime": "video/mp4", "playable": true}],
 "main": 0}
```

- Created by `Jobs` when a job reaches `done`. `files` lists every regular file under `files/<job id>/` (paths relative to it, `.mdm.json` state files and hidden files excluded); `main` is the largest. `kind` comes from the main file's mime.
- `playable`: the browser can play it in a `<video>` or `<audio>` (mp4/m4v/webm/mov with h264 or vp9 by extension, mp3/m4a/ogg/opus/wav/flac). mkv, avi and wmv are `false`: the UI offers "Open in VLC" and Download for those.
- Job events gain one type: `{"type": "item", "item": Item}` on the global stream (`/api/events`) when an item is created or its poster arrives.

## Posters (agent/poster.py)

The first that works, saved as `posters/<item id>.jpg` (max 600px wide):
1. The proposal's `report.image` (an `https://` URL): fetched through the same SSRF guard as `inspect_link`, `image/*` only, at most 5 MB, at most 10 s.
2. Video files: one frame with ffmpeg (`ffmpeg -ss <10% of duration> -i <file> -frames:v 1 -vf scale=600:-2`), when `ffmpeg` is on PATH; argv list, never a shell, 30 s timeout.
3. `torrent` or `video-get` downloads: the thumbnail MDM or yt-dlp wrote next to the file, if any.
Otherwise `poster: false` and the UI shows a typographic card. Posters are made on a worker thread after `done`; a failure is logged, never raised.

## MDM, used more fully (agent/mdm.py)

- **Resume:** a job interrupted by a restart (queued or running at load) whose folder holds a `*.mdm.json` is resumed with `resume <state file> -c N`, not marked failed. Without a state file it is still `error: interrupted`.
- **Checksum:** a proposal may carry `checksum` (`sha256:<hex>` etc.); `get` then passes `--checksum`, and a mismatch is a job error.
- **Video pages:** a proposal with `"mode": "video"` runs `video-get <page> -o <dest> -c N --quality <height> --subs <langs>` (MDM has no `--height`); `inspect_link` reports `video` info `{title, heights, subtitles}` for pages `video-info` understands (it reports no duration or thumbnail; the poster comes from the page image). Needs yt-dlp: when `video-info` says it is missing, the report carries `skipped: ["yt-dlp is not installed: run misterdownloadmanager-cli install-ytdlp"]`.
- **DNS:** `MDM_DNS` (env, optional) is passed as `--dns` to `get` (`video-get` and `resume` have no `--dns`). A resumed download is not re-verified against its checksum.
- Connections stay `MDM_CONNECTIONS` (default 16).

## Chats

- `POST /api/chats` creates `{"id": "c_...", "title": "New download", "created", "updated"}`. The title becomes the first user message, cut to 60 characters.
- `GET /api/chats` returns all chats, newest `updated` first. `GET /api/chats/{id}` returns `{"chat": Chat, "events": [...]}` in order.
- `POST /api/chat` keeps its body (`session`, `message`); `session` is the chat id. Unknown ids are created on first use. The server appends to `chat_events`: `{"type":"user","text"}` before running the agent, then each `tool_step`, `text`, `proposal` (stored as `{"type":"proposal","id"}`) and `error` it streams. The agent's model history stays in memory per chat (lost on restart; the displayed transcript is not).
- `DELETE /api/chats/{id}` removes the chat and its events (not its downloads).

## Library API (server.py)

| Method | Path | Answer |
|---|---|---|
| GET | `/api/library` | `[Item]`, newest first |
| GET | `/api/library/{id}` | `Item` or 404 |
| GET | `/api/library/{id}/poster` | the JPEG, `Cache-Control: private, max-age=86400`, or 404 |
| GET | `/api/library/{id}/stream?file=N` | the file (default `main`), `Content-Disposition: inline`, Range supported (206 with `Content-Range`, `Accept-Ranges: bytes`) |
| GET | `/api/library/{id}/download?file=N` | the same bytes as `attachment; filename*=UTF-8''<name>` (resumable through Range) |
| POST | `/api/library/{id}/link` | `{"url": "/api/library/{id}/stream?file=N&exp=<unix>&sig=<hex>", "expires": <unix>}`: a link valid 6 hours that needs no cookie, for VLC |
| DELETE | `/api/library/{id}` | deletes `files/<job id>/`, the poster and the item; 204 |

- Paths are resolved from the item's `files[N].path` under `files/<job id>/` and must stay inside it (no `..`, no symlink escape). Never a path from the query string.
- `sig` = HMAC-SHA256(server secret, `"{id}:{file}:{exp}"`), hex; compared in constant time; expired is 403.

## Access (server.py)

- Loopback with no `MDM_TOKEN` set: open, as now (local use is unchanged).
- `ALLOWED_HOSTS` (env, comma-separated) adds host names to the DNS-rebinding guard, e.g. the Tailscale name `media-server.tailnet-1234.ts.net`. It is an error to set `ALLOWED_HOSTS` without `MDM_TOKEN`: the server refuses to start with a clear message.
- With `MDM_TOKEN` set, every `/api/*` request needs a session cookie, except `POST /api/login`, `GET /api/health` and signed stream links. `POST /api/login {"token"}` compares in constant time and sets `mdm_session` (HttpOnly, SameSite=Strict, Secure when the request came over https, 30 days): an HMAC of an expiry, not the token itself. `POST /api/logout` clears it. 401 answers `{"detail":"login required"}`; the UI then shows its login form.
- Server secret: `MDM_SECRET` env, else 32 random bytes generated once into `$LIBRARY_DIR/.secret` (mode 600).
- Remote access is Tailscale: the app stays bound to 127.0.0.1 and `tailscale serve --bg 8000` publishes it on the tailnet over HTTPS. Never `0.0.0.0` on the host.

## Python interface (what server.py calls; store implements it on `Jobs`, api fakes it in tests)

`Jobs` stays the one object the server holds. New methods, all thread-safe, raising the existing `NotFound`:

```python
jobs.library_dir -> Path                      # LIBRARY_DIR resolved (for .secret)
jobs.list_items() -> list[dict]               # Items, newest first
jobs.get_item(item_id) -> dict                # Item; NotFound
jobs.item_file(item_id, index=None) -> Path   # absolute, confined to files/<job id>/; index None = main; NotFound when missing or escaping
jobs.poster_path(item_id) -> Path | None      # None when there is no poster file; NotFound for an unknown item
jobs.delete_item(item_id) -> None             # files, poster, row; NotFound
jobs.create_chat(title=None) -> dict          # Chat
jobs.list_chats() -> list[dict]               # newest updated first
jobs.get_chat(chat_id) -> dict                # {"chat": Chat, "events": [...]}; NotFound
jobs.delete_chat(chat_id) -> None             # NotFound
jobs.append_chat_event(chat_id, event: dict) -> None
    # creates the chat if missing; the first {"type":"user"} sets the title (60 chars);
    # {"type":"proposal","id"} also sets proposals.chat_id; bumps chats.updated
```

Proposals and jobs keep their JSON shapes; a proposal gains `"chat_id": "c_...|null"` and a job gains `"item_id": "i_...|null"` (set on done).

## Errors

JSON `{"detail": "..."}` with 400 (bad input), 401 (login), 403 (host, origin, signature), 404, 409 (state), 416 (bad range, from Starlette).
