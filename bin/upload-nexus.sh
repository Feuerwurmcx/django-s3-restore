#!/usr/bin/env bash
# Lädt ein Archiv in ein Nexus RAW-Repository.
#
#   NEXUS_URL=https://nexus.example.com NEXUS_REPO=python-raw \
#   NEXUS_USER=... NEXUS_PASS=... ci/upload-nexus.sh <archiv> <paket> <version>
#
# Die Zugangsdaten werden curl über stdin (--config -) übergeben, nicht über
# --user: sonst stünden sie in der Prozessliste und wären für jeden anderen
# Prozess auf dem Agent per `ps aux` lesbar.
#
# Bricht ab, wenn die Version dort schon liegt (ALLOW_REDEPLOY=1 überschreibt).
set -euo pipefail
set +x            # Schutz davor, dass ein aufrufendes Skript xtrace vererbt

ARCHIVE="${1:?archiv fehlt}"
PKG="${2:?paket fehlt}"
VERSION="${3:?version fehlt}"

: "${NEXUS_URL:?NEXUS_URL fehlt}"
: "${NEXUS_REPO:?NEXUS_REPO fehlt}"
: "${NEXUS_USER:?NEXUS_USER fehlt}"
: "${NEXUS_PASS:?NEXUS_PASS fehlt}"

TARGET="${NEXUS_URL%/}/repository/${NEXUS_REPO}/${PKG}/${VERSION}/$(basename "$ARCHIVE")"

# curl-config-Format: Wert in Anführungszeichen, \ und " müssen escaped werden
cfg_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

# Auth ausschliesslich über stdin -> nie in argv, nie im Build-Log
curl_auth() {
  printf 'user = "%s:%s"\n' "$(cfg_escape "$NEXUS_USER")" "$(cfg_escape "$NEXUS_PASS")" \
    | curl --config - "$@"
}

exists() {
  local code
  code=$(curl_auth --silent --head --output /dev/null --write-out '%{http_code}' "$TARGET")
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
HTTP_CODE=$(curl_auth --fail-with-body --silent --show-error \
  --retry 3 --retry-delay 5 --retry-connrefused \
  --upload-file "$ARCHIVE" \
  --write-out '%{http_code}' \
  --output /dev/null \
  "$TARGET")

echo "HTTP ${HTTP_CODE}"
[[ "$HTTP_CODE" =~ ^2 ]] || exit 1

# Prüfen, dass die Datei wirklich liegt
exists || { echo "FEHLER: nach dem Upload nicht auffindbar: ${TARGET}" >&2; exit 1; }
echo "OK: ${PKG}-${VERSION}"
