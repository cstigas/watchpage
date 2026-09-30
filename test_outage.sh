#!/usr/bin/env bash
# Time out 10 times against a local server that never responds, then send one outage text.
# Does not write state.json and does not edit crontab.
set -euo pipefail
cd "$(dirname "$0")"
export DONT_UPDATE_STATE=1
python3 -c 'import watch; watch.warn_config()'

echo "A local port will accept connections and never answer."
echo "The watcher times out against it 10 times, then sends one outage text."
echo "This test does not write state.json and does not edit crontab."

python3 watch.py --test-outage
