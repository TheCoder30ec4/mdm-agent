#!/usr/bin/env bash
# Install misterdownloadmanager-cli into vendor/mdm/ from the latest GitHub release (macOS).
# Verifies the dmg's SHA-256 against the release's SHA256SUMS.txt before mounting it read-only.
set -euo pipefail

REPO="anefzaoui/misterdownloadmanager"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/vendor/mdm"
TMP="$(mktemp -d)"
MNT="$TMP/mnt"
MOUNTED=0

cleanup() {
  if [ "$MOUNTED" = 1 ]; then hdiutil detach "$MNT" -quiet || hdiutil detach "$MNT" -force -quiet || true; fi
  rm -rf "$TMP"
}
trap cleanup EXIT

die() { echo "error: $*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "macOS only"
case "$(uname -m)" in
  arm64)  ARCH_RE='arm64|aarch64|apple.?silicon' ;;
  x86_64) ARCH_RE='x86_64|x64|amd64|intel' ;;
  *) die "unsupported architecture $(uname -m)" ;;
esac

use_gh=0
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then use_gh=1; fi

# list_assets prints one asset name per line; fetch NAME downloads it into $TMP
if [ "$use_gh" = 1 ]; then
  list_assets() { gh release view --repo "$REPO" --json assets --jq '.assets[].name'; }
  fetch() { gh release download --repo "$REPO" --pattern "$1" --dir "$TMP"; }
else
  command -v python3 >/dev/null 2>&1 || die "need gh or python3"
  curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" -o "$TMP/release.json"
  list_assets() { python3 -c 'import json,sys; [print(a["name"]) for a in json.load(open(sys.argv[1]))["assets"]]' "$TMP/release.json"; }
  fetch() {
    local url
    url="$(python3 -c 'import json,sys; print(next(a["browser_download_url"] for a in json.load(open(sys.argv[1]))["assets"] if a["name"]==sys.argv[2]))' "$TMP/release.json" "$1")"
    case "$url" in https://github.com/"$REPO"/releases/download/*) ;; *) die "unexpected download url" ;; esac
    curl -fsSL "$url" -o "$TMP/$1"
  }
fi

ASSETS="$(list_assets)"
DMG="$(printf '%s\n' "$ASSETS" | grep -iE '\.dmg$' | grep -iE "$ARCH_RE" | head -1 || true)"
[ -n "$DMG" ] || DMG="$(printf '%s\n' "$ASSETS" | grep -iE 'universal.*\.dmg$' | head -1 || true)"
[ -n "$DMG" ] || { [ "$(printf '%s\n' "$ASSETS" | grep -ciE '\.dmg$')" = 1 ] && DMG="$(printf '%s\n' "$ASSETS" | grep -iE '\.dmg$')"; } || true
[ -n "$DMG" ] || die "no macOS dmg for $(uname -m) among release assets"
printf '%s\n' "$ASSETS" | grep -qx 'SHA256SUMS.txt' || die "release has no SHA256SUMS.txt"

echo "Downloading $DMG"
fetch "$DMG"
fetch "SHA256SUMS.txt"

EXPECTED="$(awk -v f="$DMG" '{n=$2; sub(/^\*/,"",n); if (n==f) {print tolower($1); exit}}' "$TMP/SHA256SUMS.txt")"
[ -n "$EXPECTED" ] || die "$DMG not listed in SHA256SUMS.txt"
ACTUAL="$(shasum -a 256 "$TMP/$DMG" | awk '{print tolower($1)}')"
[ "$EXPECTED" = "$ACTUAL" ] || die "SHA-256 mismatch for $DMG (expected $EXPECTED, got $ACTUAL)"
echo "SHA-256 verified"

mkdir -p "$MNT"
hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$MNT" "$TMP/$DMG" >/dev/null
MOUNTED=1

CLI="$(find "$MNT" -type f -name misterdownloadmanager-cli -perm -u+x 2>/dev/null | head -1)"
[ -n "$CLI" ] || die "misterdownloadmanager-cli not found inside the dmg"

mkdir -p "$DEST"
# The CLI is only a launcher: it runs the app's program (misterdownloadmanager) that must sit next to it.
APP="$(dirname "$CLI")/misterdownloadmanager"
[ -x "$APP" ] || die "misterdownloadmanager (the app's program) not found next to the CLI in the dmg"
cp "$CLI" "$DEST/misterdownloadmanager-cli"
cp "$APP" "$DEST/misterdownloadmanager"
chmod 755 "$DEST/misterdownloadmanager-cli" "$DEST/misterdownloadmanager"
xattr -d com.apple.quarantine "$DEST/misterdownloadmanager-cli" "$DEST/misterdownloadmanager" 2>/dev/null || true
echo "Installed $DEST/misterdownloadmanager-cli"
