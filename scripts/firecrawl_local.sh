#!/usr/bin/env bash
# Run Firecrawl (self-hosted) + SearXNG (for /search) locally with Docker. API on http://127.0.0.1:3002 only.
#   scripts/firecrawl_local.sh up | down | status
# Uses the official compose file pinned to a release tag and prebuilt ghcr.io images (no source build).
set -euo pipefail

TAG="${FIRECRAWL_TAG:-v2.11.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/vendor/firecrawl-local"
URL="http://127.0.0.1:3002"

die() { echo "error: $*" >&2; exit 1; }
dc() { (cd "$DIR" && docker compose "$@"); }

cmd="${1:-status}"
case "$cmd" in
  up)
    docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop and retry."
    mkdir -p "$DIR/searxng"
    curl -fsSL "https://raw.githubusercontent.com/firecrawl/firecrawl/$TAG/docker-compose.yaml" -o "$DIR/docker-compose.yaml"

    [ -f "$DIR/.env" ] || cat > "$DIR/.env" <<EOF
PORT=3002
USE_DB_AUTHENTICATION=false
SEARXNG_ENDPOINT=http://searxng:8080
# laptop-sized (upstream defaults are 8 workers/queue and 5 browsers, which got OOM-killed on a 16 GB Mac)
NUM_WORKERS_PER_QUEUE=2
MAX_CONCURRENT_JOBS=2
BROWSER_POOL_SIZE=2
CRAWL_CONCURRENT_REQUESTS=4
EOF

    [ -f "$DIR/searxng/settings.yml" ] || cat > "$DIR/searxng/settings.yml" <<EOF
use_default_settings: true
server:
  secret_key: "$(openssl rand -hex 32)"
  limiter: false
  bind_address: "0.0.0.0"
  port: 8080
search:
  formats: [html, json]
EOF

    cat > "$DIR/docker-compose.override.yml" <<'EOF'
services:
  api:
    image: ghcr.io/firecrawl/firecrawl:latest
    build: !reset null
    restart: unless-stopped
    ports: !override
      - "127.0.0.1:3002:3002"
  playwright-service:
    image: ghcr.io/firecrawl/playwright-service:latest
    build: !reset null
  nuq-postgres:
    image: ghcr.io/firecrawl/nuq-postgres:latest
    build: !reset null
  searxng:
    image: searxng/searxng:latest
    networks: [backend]
    volumes:
      - ./searxng/settings.yml:/etc/searxng/settings.yml:ro
EOF

    dc up -d --no-build
    echo -n "Waiting for Firecrawl on $URL "
    for _ in $(seq 1 90); do
      if curl -fs -o /dev/null "$URL/"; then echo " up."; exit 0; fi
      echo -n "."; sleep 2
    done
    echo; die "Firecrawl did not answer in 3 minutes: see 'docker compose logs api' in $DIR"
    ;;
  down)
    [ -d "$DIR" ] || die "nothing to stop ($DIR does not exist)"
    dc down
    ;;
  status)
    if curl -fs -o /dev/null -m 2 "$URL/"; then echo "Firecrawl is up at $URL"; else echo "Firecrawl is not reachable at $URL"; exit 1; fi
    ;;
  *) die "usage: $0 up|down|status" ;;
esac
