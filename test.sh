#!/usr/bin/env bash
# Run every test in tests/, including the headless browser check.
set -euo pipefail
cd "$(dirname "$0")"

python="python3"
if [[ -x .venv/bin/python ]]; then
  python=".venv/bin/python"
fi

exec "$python" -m unittest discover -s tests -t . -v
