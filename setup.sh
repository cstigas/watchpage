#!/usr/bin/env bash
# Create a local virtualenv and install anything missing from requirements.txt.
# Uses .venv in this directory, so it does not need root.
# Exits 1 when Python is missing or too old, or when the venv module is missing.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is not installed." >&2
  exit 1
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    raise SystemExit(f"python3 {version} is too old. Need Python 3.10 or newer.")
PY

if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  echo "python3 is installed, but the venv module is missing." >&2
  echo "On Ubuntu, install it once with: sudo apt install python3-venv" >&2
  echo "After that, ./setup.sh does not need root." >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating .venv"
  python3 -m venv .venv
fi

missing="$(.venv/bin/python - <<'PY'
import importlib.metadata
from pathlib import Path

missing = []
for raw in Path("requirements.txt").read_text(encoding="utf-8").splitlines():
    line = raw.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        continue
    name = line
    for separator in ("==", ">=", "<=", "~=", "!=", ">", "<"):
        if separator in name:
            name = name.split(separator, 1)[0].strip()
    name = name.split("[", 1)[0].strip()
    try:
        importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        missing.append(name)
print(" ".join(missing))
PY
)"

if [[ -z "${missing}" ]]; then
  echo "Dependencies already installed."
else
  echo "Installing missing packages: ${missing}"
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
fi

echo "Ready. Run the watcher with:"
echo "  .venv/bin/python watch.py --config config.json"
