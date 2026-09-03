#!/usr/bin/env bash
# Ermittelt die Version EINES Pakets aus dessen Metadaten.
#
#   ci/version-of.sh <paket>
#
# Reihenfolge:
#   1. statisch aus setup.py            (version="..." bzw. version=__version__)
#   2. statisch aus setup.cfg           ([metadata] version = ...)
#   3. dynamisch: python setup.py --version   (nur wenn ALLOW_SETUP_EXEC=1)
#
# Statisch zuerst, weil `python setup.py` beliebigen Code im Build ausführt und
# an fehlenden Imports scheitert, die zur Laufzeit gar nicht gebraucht werden.
set -euo pipefail

PKG="${1:?paket fehlt}"
[[ -d "$PKG" ]] || { echo "FEHLER: $PKG ist kein Verzeichnis" >&2; exit 1; }

VERSION="$(python3 - "$PKG" <<'PY'
import ast, pathlib, re, sys

pkg = pathlib.Path(sys.argv[1])


def literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def assignments(tree):
    """name -> String-Literal aller Top-Level-Zuweisungen"""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            val = literal(node.value)
            if val is None:
                continue
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = val
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            val = literal(node.value) if node.value else None
            if val is not None:
                out[node.target.id] = val
    return out


def from_setup_py():
    sp = pkg / "setup.py"
    if not sp.is_file():
        return None
    tree = ast.parse(sp.read_text(encoding="utf-8"))
    local = assignments(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "setup":
            continue
        for kw in node.keywords:
            if kw.arg != "version":
                continue
            # version="1.2.3"
            val = literal(kw.value)
            if val:
                return val
            # version=__version__  ->  im setup.py selbst
            if isinstance(kw.value, ast.Name):
                if kw.value.id in local:
                    return local[kw.value.id]
                # ... sonst im Paket suchen
                return from_module(kw.value.id)
    return None


def from_module(varname="__version__"):
    """__version__ = "..." in den üblichen Modul-Dateien des Pakets"""
    candidates = []
    for pattern in ("__init__.py", "_version.py", "version.py",
                    "*/__init__.py", "*/_version.py", "*/version.py"):
        candidates.extend(sorted(pkg.glob(pattern)))
    rx = re.compile(r"^\s*%s\s*[:=]" % re.escape(varname))
    for f in candidates:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        found = assignments(tree).get(varname)
        if found:
            return found
        # nur als Hinweis, falls dynamisch berechnet
        for line in f.read_text(encoding="utf-8").splitlines():
            if rx.match(line):
                print("HINWEIS: %s in %s ist nicht statisch lesbar: %s"
                      % (varname, f, line.strip()), file=sys.stderr)
    return None


def from_setup_cfg():
    cfg = pkg / "setup.cfg"
    if not cfg.is_file():
        return None
    import configparser
    cp = configparser.ConfigParser()
    cp.read(cfg, encoding="utf-8")
    v = cp.get("metadata", "version", fallback="").strip()
    if not v or v.startswith(("file:", "attr:")):
        if v.startswith("attr:"):
            return from_module(v.split(":", 1)[1].strip().split(".")[-1])
        return None
    return v


for fn in (from_setup_py, from_setup_cfg):
    v = fn()
    if v:
        print(v)
        break
PY
)"

# Fallback: setuptools selbst fragen (führt setup.py aus)
if [[ -z "$VERSION" && "${ALLOW_SETUP_EXEC:-0}" == "1" && -f "$PKG/setup.py" ]]; then
  echo "Statisch nicht lesbar – nutze 'python setup.py --version' in $PKG" >&2
  VERSION="$( cd "$PKG" && python3 setup.py --version 2>/dev/null | tail -n1 | tr -d '[:space:]' )"
fi

if [[ -z "$VERSION" ]]; then
  echo "FEHLER: keine Version für '$PKG' ermittelbar (setup.py / setup.cfg)." >&2
  echo "        Bei dynamisch berechneter Version: ALLOW_SETUP_EXEC=1 setzen." >&2
  exit 1
fi

# Sanity: keine Pfadtrenner o.ä., sonst landet der Upload woanders
if [[ ! "$VERSION" =~ ^[A-Za-z0-9._+!-]+$ ]]; then
  echo "FEHLER: unplausible Version '$VERSION' für '$PKG'" >&2
  exit 1
fi

echo "$VERSION"
