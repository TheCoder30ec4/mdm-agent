# Contract: proposal and job objects (v1)

IDs: `p_` + 8 hex for proposals, `j_` + 8 hex for jobs. Timestamps: ISO-8601 UTC strings.

## Proposal
```json
{"id": "p_1a2b3c4d", "status": "pending|approved|rejected", "created": "...",
 "report": { "...Report from contracts/tools.md..." },
 "files": [0, 2], "note": "why the agent picked this", "job_id": "j_...|null"}
```
Transitions: `pending -> approved` (sets `job_id`) or `pending -> rejected`. Nothing else. Approving twice -> `BadState`.

## Job
```json
{"id": "j_5e6f7a8b", "proposal_id": "p_1a2b3c4d", "url": "...", "title": "str|null",
 "state": "queued|running|done|error|cancelled",
 "downloaded": 0, "size": 123456, "speed": 0,
 "connections": 16, "phase": "str|null", "peers": 3, "peers_seen": 12,
 "path": "absolute path|null", "error": "str|null",
 "created": "...", "finished": "...|null"}
```
- `size` is `null` while unknown. `path` is set on `done`. `error` is set on `error`.
- Destination folder: `DOWNLOAD_DIR/<job id>/` (one folder per job, so names never collide); MDM chooses the file name.
- `cancelled` is reached through `Jobs.cancel` (sets the cancel event; runner raises `MdmError("cancelled")`).

- Persistence: on save at most 300 finished proposals and 300 finished jobs are kept (pending, queued and running are never dropped). Save failures are logged, never raised, so a job always starts after approve and always emits `job_done`.
- `connections` (http) and `phase`, `peers`, `peers_seen` (torrents) are copied from MDM's progress lines when present; the UI shows them with a time-left estimate and a plain-words reason when the speed is 0.
- http downloads use `MDM_CONNECTIONS` parallel connections (default 16, 1-128).
