#!/usr/bin/env bash
# Treat the live page as triggered by looking for a phrase that is not there.
# Sends the alert only. Does not write state and does not edit crontab.
set -euo pipefail
cd "$(dirname "$0")"
export DONT_UPDATE_STATE=1
python3 -c 'import watch; watch.warn_config()'

tmp=$(mktemp)
python3 - "$tmp" <<'PY'
import json
import sys
from pathlib import Path

cfg = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
cfg["watch"]["value"] = "this phrase is not on the page"
cfg["state_file"] = "/tmp/watchpage-test-state-should-not-exist.json"
Path(sys.argv[1]).write_text(json.dumps(cfg), encoding="utf-8")
PY

echo "Looking for text that is not on the page: this phrase is not on the page"
echo "The watch is text/absent, so this run sends the alert."
echo "This test does not write state and does not edit crontab."

python3 watch.py --config "$tmp" --no-record
rm -f "$tmp"
