#!/usr/bin/env bash
# Send "testing sms send functionality" to each number in .env.
# Does not write state.json and does not edit crontab.
set -euo pipefail
cd "$(dirname "$0")"
export DONT_UPDATE_STATE=1
python3 -c 'import watch; watch.warn_config()'
PYTHONPATH=. python3 -m unittest tests/test_sms_send.py -v
