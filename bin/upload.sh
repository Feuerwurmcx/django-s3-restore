#!/usr/bin/env bash
# Lädt ein Archiv in ein Nexus RAW-Repository.
#
#   NEXUS_URL=https://nexus.example.com NEXUS_REPO=python-raw \
#   NEXUS_USER=... NEXUS_PASS=... ci/upload-nexus.sh <archiv> <paket> <version>
set -euo pipefail

ARCHIVE="${1:?archiv fehlt}"
PKG="${2:?paket fehlt}"
VERSION="${3:?version fehlt}"

: "${NEXUS_URL:?NEXUS_URL fehlt}"
: "${NEXUS_REPO:?NEXUS_REPO fehlt}"
: "${NEXUS_USER:?NEXUS_USER fehlt}"
: "${NEXUS_PASS:?NEXUS_PASS fehlt}"

TARGET="${NEXUS_URL%/}/repository/${NEXUS_REPO}/${PKG}/${VERSION}/$(basename "$ARCHIVE")"

echo "Upload -> ${TARGET}"
HTTP_CODE=$(curl --fail-with-body --silent --show-error \
  --retry 3 --retry-delay 5 --retry-connrefused \
  --user "${NEXUS_USER}:${NEXUS_PASS}" \
  --upload-file "$ARCHIVE" \
  --write-out '%{http_code}' \
  --output /dev/null \
  "$TARGET")

echo "HTTP ${HTTP_CODE}"
[[ "$HTTP_CODE" =~ ^2 ]] || exit 1

# Prüfen, dass die Datei wirklich liegt
curl --fail --silent --head --user "${NEXUS_USER}:${NEXUS_PASS}" "$TARGET" >/dev/null
echo "OK: ${PKG}-${VERSION}"