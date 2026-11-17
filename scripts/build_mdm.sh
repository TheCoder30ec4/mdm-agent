#!/usr/bin/env bash
# Build Mister Download Manager from the fork (git submodule ./misterdownloadmanager) in release mode.
# The agent then uses ./misterdownloadmanager/target/release/misterdownloadmanager-cli automatically.
# Needs Rust >= 1.98 (brew install rustup && rustup default stable). First build takes a while.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
git submodule update --init --depth 1 misterdownloadmanager
export PATH="/opt/homebrew/opt/rustup/bin:$HOME/.cargo/bin:$PATH"
command -v cargo >/dev/null || { echo "error: cargo not found; install Rust: brew install rustup && rustup default stable" >&2; exit 1; }
# release: a debug build of the download engine is much slower, which defeats the point
# build output outside the project: a synced folder (iCloud Documents) stalls reads while it uploads GBs of build files
export CARGO_TARGET_DIR="$HOME/Library/Caches/mdm-agent/target"
(cd misterdownloadmanager && cargo build --release -p mdm-app -p mdm-cli)
CLI="$CARGO_TARGET_DIR/release/misterdownloadmanager-cli"
[ -x "$CLI" ] && [ -x "$CARGO_TARGET_DIR/release/misterdownloadmanager" ] || { echo "error: build produced no CLI" >&2; exit 1; }
echo "Built $CLI ($(git -C misterdownloadmanager rev-parse --short HEAD) of the fork)"
