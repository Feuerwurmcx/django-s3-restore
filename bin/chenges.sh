#!/usr/bin/env bash
# Gibt die Namen der Pakete (Top-Level-Ordner) aus, die sich seit BASE_REF geändert haben.
# Ohne gültige BASE_REF werden alle Pakete ausgegeben (z.B. erster Build, neuer Branch).
#
#   ci/changed-packages.sh [BASE_REF]
#
# Ein Paket = Top-Level-Ordner mit pyproject.toml, setup.py oder __init__.py.
# Alternativ: PACKAGES="a b c" ci/changed-packages.sh  -> feste Liste statt Autodetect.
set -euo pipefail

BASE_REF="${1:-}"

detect_packages() {
  if [[ -n "${PACKAGES:-}" ]]; then
    printf '%s\n' $PACKAGES
    return
  fi
  for d in */; do
    d="${d%/}"
    [[ "$d" == ci || "$d" == .* ]] && continue
    if [[ -f "$d/pyproject.toml" || -f "$d/setup.py" || -f "$d/__init__.py" ]]; then
      echo "$d"
    fi
  done
}

mapfile -t ALL < <(detect_packages | sort)
if [[ ${#ALL[@]} -eq 0 ]]; then
  echo "FEHLER: keine Pakete gefunden" >&2
  exit 1
fi

# Kein/ungültiger Basis-Commit -> alles bauen
if [[ -z "$BASE_REF" ]] || ! git cat-file -e "${BASE_REF}^{commit}" 2>/dev/null; then
  echo "Kein gültiger Basis-Commit ('${BASE_REF}') – baue alle Pakete" >&2
  printf '%s\n' "${ALL[@]}"
  exit 0
fi

mapfile -t TOUCHED < <(git diff --name-only "${BASE_REF}" HEAD | cut -d/ -f1 | sort -u)

# geändert = Schnittmenge, plus alles wenn gemeinsame CI-Dateien angefasst wurden
if printf '%s\n' "${TOUCHED[@]}" | grep -qx -e 'ci' -e 'Jenkinsfile'; then
  echo "CI-Dateien geändert – baue alle Pakete" >&2
  printf '%s\n' "${ALL[@]}"
  exit 0
fi

comm -12 <(printf '%s\n' "${ALL[@]}") <(printf '%s\n' "${TOUCHED[@]}")