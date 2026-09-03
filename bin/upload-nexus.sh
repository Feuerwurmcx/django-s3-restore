#!/usr/bin/env bash
# Lädt ein Archiv in ein Nexus RAW-Repository.
#
#   NEXUS_URL=https://nexus.example.com NEXUS_REPO=python-raw \
#   NEXUS_USER=... NEXUS_PASS=... ci/upload-nexus.sh <archiv> <paket> <version>
#
# Bricht ab, wenn die Version dort schon liegt (ALLOW_REDEPLOY=1 überschreibt).
set -euo pipefail

ARCHIVE="${1:?archiv fehlt}"
PKG="${2:?paket fehlt}"
VERSION="${3:?version fehlt}"

: "${NEXUS_URL:?NEXUS_URL fehlt}"
: "${NEXUS_REPO:?NEXUS_REPO fehlt}"
: "${NEXUS_USER:?NEXUS_USER fehlt}"
: "${NEXUS_PASS:?NEXUS_PASS fehlt}"

TARGET="${NEXUS_URL%/}/repository/${NEXUS_REPO}/${PKG}/${VERSION}/$(basename "$ARCHIVE")"

exists() {
  local code
  code=$(curl --silent --head --output /dev/null --write-out '%{http_code}' \
              --user "${NEXUS_USER}:${NEXUS_PASS}" "$TARGET")
  [[ "$code" == "200" ]]
}

# Version kommt aus setup.py -> gleiche Version zweimal hochladen ist fast immer
# ein vergessener Version-Bump, kein gewollter Redeploy.
if [[ "${ALLOW_REDEPLOY:-0}" != "1" ]] && exists; then
  echo "FEHLER: ${PKG} ${VERSION} liegt bereits in Nexus." >&2
  echo "        Version in ${PKG}/setup.py erhöhen (oder ALLOW_REDEPLOY=1)." >&2
  echo "        ${TARGET}" >&2
  exit 2
fi

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
exists || { echo "FEHLER: nach dem Upload nicht auffindbar: ${TARGET}" >&2; exit 1; }
echo "OK: ${PKG}-${VERSION}"
