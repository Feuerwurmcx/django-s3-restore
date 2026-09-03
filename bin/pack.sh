#!/usr/bin/env bash
# Packt ein Paketverzeichnis reproduzierbar nach dist/<paket>-<version>.tar.gz
#
#   ci/pack.sh <paket> [version]
#
# Ohne <version> wird sie aus den Paket-Metadaten gelesen (ci/version-of.sh).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="${1:?paket fehlt}"
VERSION="${2:-}"
OUT_DIR="${OUT_DIR:-dist}"

[[ -d "$PKG" ]] || { echo "FEHLER: $PKG ist kein Verzeichnis" >&2; exit 1; }

if [[ -z "$VERSION" ]]; then
  VERSION="$(bash "$HERE/version-of.sh" "$PKG")"
fi
[[ -n "${VERSION_SUFFIX:-}" ]] && VERSION="${VERSION}${VERSION_SUFFIX}"

SOURCE_DATE="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct -- "$PKG" 2>/dev/null || date +%s)}"

mkdir -p "$OUT_DIR"
ARCHIVE="${OUT_DIR}/${PKG}-${VERSION}.tar.gz"

# --sort/--mtime/--owner/--numeric-owner => byte-identisches Archiv bei gleichem Input
tar --sort=name \
    --mtime="@${SOURCE_DATE}" \
    --owner=0 --group=0 --numeric-owner \
    --exclude='__pycache__' \
    --exclude='*.py[co]' \
    --exclude='.pytest_cache' \
    --exclude='.mypy_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.egg-info' \
    --exclude='.venv' \
    -czf "$ARCHIVE" "$PKG"

echo "$ARCHIVE"
