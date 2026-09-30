#!/usr/bin/env bash
# Warn when a required setting in .env is missing or not usable.
# Exits 1 when any warning is printed.
set -euo pipefail
cd "$(dirname "$0")"
python3 -c 'import sys, watchpage; sys.exit(1 if watchpage.warn_config() else 0)'
