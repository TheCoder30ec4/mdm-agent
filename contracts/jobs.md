# Contract: proposal and job objects (v1)

IDs: `p_` + 8 hex for proposals, `j_` + 8 hex for jobs. Timestamps: ISO-8601 UTC strings.

## Proposal
```json
{"id": "p_1a2b3c4d", "status": "pending|approved|rejected", "created": "...",
 "report": { "...Report from contracts/tools.md..." },
 "files": [0, 2], "note": "why the agent picked this", "job_id": "j_...|null", "chat_id": "c_...|null",
 "checksum": "sha256:<hex>", "mode": "video", "height": 720, "subs": "en,fr"}
```
- `chat_id` is set by `Jobs.append_chat_event` when a `{"type":"proposal","id"}` event is stored (contracts/library.md).
- `checksum`, `mode`, `height` and `subs` are optional and only present when given (`Jobs.propose(..., checksum=, mode=, height=, subs=)`; the agent's `propose_download` passes them when the model sets them). Invalid values raise `ValueError`.
  - `checksum`: hex MD5, SHA-1, SHA-256, SHA-512 or CRC32, optionally `sha256:`-prefixed. `get` gets `--checksum`; a mismatch is a job error. Not for magnet links.
  - `mode: "video"`: the URL is a web page; the job runs `video-get` (needs yt-dlp). `height` (1-8000, e.g. 720) becomes `--quality`, `subs` (`en,fr`, `all`, `none`) becomes `--subs`. Not for magnet links. A link the user pastes is proposed with `mode: "video"` automatically when its report has `video`.
Transitions: `pending -> approved` (sets `job_id`) or `pending -> rejected`. Nothing else. Approving twice -> `BadState`.

## Job
```json
{"id": "j_5e6f7a8b", "proposal_id": "p_1a2b3c4d", "url": "...", "title": "str|null",
 "state": "queued|running|done|error|cancelled",
 "downloaded": 0, "size": 123456, "speed": 0,
 "connections": 16, "phase": "str|null", "peers": 3, "peers_seen": 12,
 "path": "absolute path|null", "error": "str|null",
 "created": "...", "finished": "...|null", "item_id": "i_...|null"}
```
- `size` is `null` while unknown. `path` is set on `done`. `error` is set on `error`.
- Destination folder: `<files dir>/<job id>/` (`$LIBRARY_DIR/files/`, or `DOWNLOAD_DIR` when set); one folder per job, so names never collide; MDM chooses the file name.
- On `done`, `Jobs` builds the library Item (contracts/library.md) from that folder, sets `item_id`, emits `{"type":"item","item":Item}` (before `job`/`job_done`), then makes the poster on a worker thread and emits the item again with `poster: true` when it lands. A job that left no files has `item_id: null`.
- `cancelled` is reached through `Jobs.cancel` (sets the cancel event; runner raises `MdmError("cancelled")`).

- Persistence: SQLite `mdm.db` (contracts/library.md); on every state change at most 300 finished proposals and 300 finished jobs are kept (pending, queued and running are never dropped; library items are never pruned). Save failures are logged, never raised, so a job always starts after approve and always emits `job_done`.
- `connections` (http) and `phase`, `peers`, `peers_seen` (torrents) are copied from MDM's progress lines when present; the UI shows them with a time-left estimate and a plain-words reason when the speed is 0.
- http downloads use `MDM_CONNECTIONS` parallel connections (default 16, 1-128).
- Restart: a job left `queued` or `running` whose folder holds a `*.mdm.json` is resumed (`mdm.run(..., resume=<state file>)`, which runs `resume <state> -c N`); without one it becomes `error` with `error: "interrupted"`.
