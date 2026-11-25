#!/usr/bin/env bash
# One command: set everything up (idempotent) and start the agent on http://127.0.0.1:8000.
#   ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

# venv + deps, reinstalled only when requirements.txt changed
# (.venv may be a symlink out of a synced folder; recreate at its target if that was wiped)
if [ ! -x .venv/bin/python ]; then
  python3 -m venv "$([ -L .venv ] && readlink .venv || echo .venv)"
fi
HASH="$(shasum -a 256 requirements.txt | cut -d' ' -f1)"
if [ "$(cat .venv/.requirements.sha256 2>/dev/null)" != "$HASH" ]; then
  .venv/bin/pip install -q -r requirements.txt
  echo "$HASH" > .venv/.requirements.sha256
fi

# .env: create from the template, then stop so the key can be added
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env. Add GROQ_API_KEY to .env, then re-run ./run.sh" >&2
  exit 1
fi

# MDM CLI: mirrors cli_path() in agent/mdm.py (MDM_CLI, fork build, vendor/mdm)
FORK="$HOME/Library/Caches/mdm-agent/target/release"
CLI="${MDM_CLI:-$(sed -n 's/^MDM_CLI=//p' .env | tail -1)}"
if [ -z "$CLI" ] && [ -x "$FORK/misterdownloadmanager-cli" ] && [ -x "$FORK/misterdownloadmanager" ]; then CLI="$FORK/misterdownloadmanager-cli"; fi
[ -n "$CLI" ] || CLI=./vendor/mdm/misterdownloadmanager-cli
if [ ! -x "$CLI" ]; then
  echo "MDM CLI not found at $CLI; building/installing it"
  git submodule update --init
  PATH="/opt/homebrew/opt/rustup/bin:$HOME/.cargo/bin:$PATH"  # same as scripts/build_mdm.sh
  if command -v cargo >/dev/null; then bash scripts/build_mdm.sh; else bash scripts/install_mdm.sh; fi
fi

# Firecrawl (web search) needs Docker; the agent still works for pasted links without it
if docker info >/dev/null 2>&1; then
  bash scripts/firecrawl_local.sh status >/dev/null 2>&1 || bash scripts/firecrawl_local.sh up \
    || echo "warning: Firecrawl failed to start; web search is disabled" >&2
else
  echo "warning: Docker is not running; web search is disabled" >&2
fi

L="${LIBRARY_DIR:-$(sed -n 's/^LIBRARY_DIR=//p' .env | tail -1)}"
echo "Library: ${L:-$HOME/MDM Library}  (set LIBRARY_DIR to change)"
# open the browser once the server answers (macOS only)
if command -v open >/dev/null; then
  (for _ in $(seq 1 60); do
     curl -fs -o /dev/null http://127.0.0.1:8000/api/health && { open http://127.0.0.1:8000; break; }
     sleep 1
   done) &
fi
exec .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8000
