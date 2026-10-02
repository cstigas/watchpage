#!/usr/bin/env bash
# Create a local virtualenv and install anything missing from requirements.txt.
# Then check headless Chromium. Install it when it is missing and you say yes.
# If the browser is present but cannot start, print the library command.
# and whether to install a cron job for a watch config.
# Uses .venv in this directory, so it does not need root.
# Exits 1 when Python is missing or too old, or when the venv module is missing.
set -euo pipefail
cd "$(dirname "$0")"

if [[ $# -ne 0 ]]; then
  echo "usage: ./setup.sh" >&2
  exit 1
fi

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

install_requirements() {
  local requirements="$1"
  local missing
  missing="$(REQUIREMENTS="${requirements}" .venv/bin/python - <<'PY'
import importlib.metadata
import os
from pathlib import Path

missing = []
for raw in Path(os.environ["REQUIREMENTS"]).read_text(encoding="utf-8").splitlines():
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
    echo "Dependencies from ${requirements} already installed."
  else
    echo "Installing missing packages: ${missing}"
    .venv/bin/python -m pip install --disable-pip-version-check -r "${requirements}"
  fi
}

install_requirements requirements.txt

chromium_state() {
  .venv/bin/python - <<'PY'
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("missing-package")
    raise SystemExit(0)

with sync_playwright() as playwright:
    try:
        executable = Path(playwright.chromium.executable_path)
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" in text or "playwright install" in text:
            print("missing-browser")
        else:
            print("launch-failed")
            print(f"{type(exc).__name__}: {exc}")
        raise SystemExit(0)
    if not executable.is_file():
        print("missing-browser")
        raise SystemExit(0)
    try:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--disable-gpu"],
        )
        browser.close()
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" in text or "playwright install" in text:
            print("missing-browser")
        else:
            print("launch-failed")
            print(f"{type(exc).__name__}: {exc}")
        raise SystemExit(0)
print("installed")
PY
}

install_chromium() {
  install_requirements requirements-browser.txt
  echo "Installing headless Chromium. This download is large."
  .venv/bin/python -m playwright install chromium
  if [[ "$(uname -s)" == "Linux" && "$(id -u)" -eq 0 ]]; then
    .venv/bin/python -m playwright install-deps chromium
  fi
}

report_launch_failure() {
  local detail="$1"
  echo "Headless Chromium is installed but did not start."
  if [[ -n "${detail}" ]]; then
    echo "${detail}"
  fi
  if [[ "$(uname -s)" == "Linux" ]]; then
    echo "Install its system libraries once with:"
    echo "  sudo .venv/bin/python -m playwright install-deps chromium"
  fi
}

browser_state=""
browser_detail=""
while IFS= read -r line; do
  if [[ -z "${browser_state}" ]]; then
    browser_state="${line}"
  elif [[ -z "${browser_detail}" ]]; then
    browser_detail="${line}"
  fi
done < <(chromium_state)

case "${browser_state}" in
  installed)
    echo "Headless Chromium is installed."
    ;;
  launch-failed)
    report_launch_failure "${browser_detail}"
    ;;
  missing-package|missing-browser)
    install_browser=0
    if ( : <>/dev/tty ) 2>/dev/null; then
      exec 3<>/dev/tty
      echo "Headless Chromium is not installed." >&3
      echo "A headless browser renders pages whose text appears only after JavaScript runs." >&3
      echo "That check is slower, uses much more memory, and is no longer a simple download." >&3
      echo "Skip this unless a watch sets render_javascript to true." >&3
      printf "Install headless Chromium? [y/N] " >&3
      read -r answer <&3
      exec 3<&-
      case "${answer}" in
        [yY]|[yY][eE][sS]) install_browser=1 ;;
        *) echo "Skipping headless Chromium." ;;
      esac
    else
      echo "Headless Chromium is not installed."
      echo "No terminal attached, so headless Chromium was not installed."
    fi
    if [[ "${install_browser}" -eq 1 ]]; then
      install_chromium
      browser_state=""
      browser_detail=""
      while IFS= read -r line; do
        if [[ -z "${browser_state}" ]]; then
          browser_state="${line}"
        elif [[ -z "${browser_detail}" ]]; then
          browser_detail="${line}"
        fi
      done < <(chromium_state)
      if [[ "${browser_state}" == "installed" ]]; then
        echo "Headless Chromium is installed."
      elif [[ "${browser_state}" == "launch-failed" ]]; then
        report_launch_failure "${browser_detail}"
      else
        echo "Headless Chromium is still not installed." >&2
        exit 1
      fi
    fi
    ;;
  *)
    echo "Could not check headless Chromium." >&2
    exit 1
    ;;
esac

run_cron_setup() {
  local action="$1"
  local config_path="$2"
  local schedule="${3:-}"
  local jitter="${4:-0}"
  .venv/bin/python - "$action" "$config_path" "$schedule" "$jitter" <<'PY'
import shutil
import sys
from pathlib import Path

import watchpage

action = sys.argv[1]
config_path = Path(sys.argv[2]).resolve()
schedule = sys.argv[3]
if not sys.argv[4].isdigit():
    raise SystemExit("The random delay is a whole number of seconds, such as 30.")
jitter = int(sys.argv[4])
config = watchpage.load_watch_config(config_path, require_cookies_file=False)
name = str(config["name"])
marker = str(config["cron_marker"])

if action == "status":
    state, lines = watchpage.cron_job_state(marker)
    print(name)
    print(marker)
    print(state)
    print("---")
    for line in lines:
        print(line)
elif action == "preview":
    flock_bin = shutil.which("flock")
    line = watchpage.build_cron_line(
        schedule,
        watchpage.ROOT,
        config_path,
        name,
        marker,
        flock_bin,
        jitter,
    )
    print(line)
elif action == "install":
    flock_bin = shutil.which("flock")
    line = watchpage.build_cron_line(
        schedule,
        watchpage.ROOT,
        config_path,
        name,
        marker,
        flock_bin,
        jitter,
    )
    print(watchpage.install_cron_job(marker, line))
elif action == "uncomment":
    print(watchpage.uncomment_cron_job(marker))
else:
    raise SystemExit(f"unknown cron action: {action}")
PY
}

pick_watch_config() {
  local -a files
  local file
  local nullglob_was=0
  files=()
  if shopt -q nullglob; then
    nullglob_was=1
  fi
  shopt -s nullglob
  for file in *.json; do
    if [[ "$file" == "config.example.json" ]]; then
      continue
    fi
    files+=("$file")
  done
  if [[ "$nullglob_was" -eq 0 ]]; then
    shopt -u nullglob
  fi
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "No watch config found. Copy config.example.json to config.json, edit it, then run ./setup.sh again." >&2
    return 1
  fi

  local -a sorted
  sorted=()
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    sorted+=("$file")
  done < <(printf '%s\n' "${files[@]}" | LC_ALL=C sort)

  if [[ ${#sorted[@]} -eq 1 ]]; then
    printf '%s\n' "${sorted[0]}"
    echo "Using ${sorted[0]}." >&2
    return 0
  fi

  local default_index=1
  local index=1
  echo "Which config should this job run?" >&2
  for file in "${sorted[@]}"; do
    if [[ "$file" == "config.json" ]]; then
      default_index=$index
    fi
    printf '  %s) %s\n' "$index" "$file" >&2
    index=$((index + 1))
  done
  local choice
  printf 'Choice [%s]: ' "$default_index" >&3
  read -r choice <&3
  if [[ -z "$choice" ]]; then
    choice=$default_index
  fi
  if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > ${#sorted[@]} )); then
    echo "Skipping cron." >&2
    return 1
  fi
  printf '%s\n' "${sorted[$((choice - 1))]}"
}

show_cron_lines() {
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    printf '  %s\n' "$line"
  done <<<"$1"
  return 0
}

warn_missing_flock() {
  if command -v flock >/dev/null 2>&1; then
    return 0
  fi
  echo "Warning: flock is not installed, so a run that is still going is not skipped." >&3
}

prompt_schedule() {
  local config_path="$1"
  local attempts=0 schedule jitter preview
  echo "Schedule is five cron fields. * * * * * runs every minute. 0 * * * * runs every hour." >&3
  echo "A random delay before each check keeps several watches from starting at the same second." >&3
  while [[ "$attempts" -lt 3 ]]; do
    attempts=$((attempts + 1))
    printf "Schedule [* * * * *]: " >&3
    read -r schedule <&3
    if [[ -z "$schedule" ]]; then
      schedule="* * * * *"
    fi
    printf "Random delay before each check, in seconds (0 for none) [30]: " >&3
    read -r jitter <&3
    if [[ -z "$jitter" ]]; then
      jitter="30"
    fi
    if preview="$(run_cron_setup preview "$config_path" "$schedule" "$jitter")"; then
      printf '%s\n' "$schedule"
      printf '%s\n' "$jitter"
      printf '%s\n' "$preview"
      return 0
    fi
  done
  return 1
}

offer_cron_on_tty() {
  local answer config_path output cron_name cron_marker cron_state cron_existing
  local chosen cron_line
  echo "Cron runs the check on a schedule. After every number has been texted, watchpage comments that line out." >&3
  printf "Install a cron job? [y/N] " >&3
  read -r answer <&3
  case "${answer}" in
    [yY]|[yY][eE][sS]) ;;
    *)
      echo "Skipping cron."
      return 0
      ;;
  esac

  if ! config_path="$(pick_watch_config)"; then
    return 0
  fi

  if ! command -v crontab >/dev/null 2>&1; then
    if ! chosen="$(prompt_schedule "$config_path")"; then
      echo "Skipping cron."
      return 0
    fi
    cron_line="$(printf '%s\n' "$chosen" | sed -n '3p')"
    echo "crontab is not installed. Add this line by hand:" >&3
    printf '  %s\n' "$cron_line" >&3
    warn_missing_flock
    return 0
  fi

  if ! output="$(run_cron_setup status "$config_path")"; then
    return 0
  fi
  cron_name="$(printf '%s\n' "$output" | sed -n '1p')"
  cron_marker="$(printf '%s\n' "$output" | sed -n '2p')"
  cron_state="$(printf '%s\n' "$output" | sed -n '3p')"
  cron_existing="$(printf '%s\n' "$output" | sed -n '5,$p')"

  if [[ "$cron_state" == "active" ]]; then
    echo "A cron job for ${cron_marker} is already installed:" >&3
    show_cron_lines "$cron_existing" >&3
    warn_missing_flock
    return 0
  fi

  if [[ "$cron_state" == "commented" ]]; then
    echo "The cron job for ${cron_marker} is commented out, so it is not running:" >&3
    show_cron_lines "$cron_existing" >&3
    warn_missing_flock
    printf "Uncomment it? [y/N] " >&3
    read -r answer <&3
    case "${answer}" in
      [yY]|[yY][eE][sS]) ;;
      *)
        echo "Skipping cron."
        return 0
        ;;
    esac
    if ! output="$(run_cron_setup uncomment "$config_path")"; then
      return 0
    fi
    if [[ "$output" == "uncommented" ]]; then
      echo "Cron job is running again."
    fi
    return 0
  fi

  if ! chosen="$(prompt_schedule "$config_path")"; then
    echo "Skipping cron."
    return 0
  fi
  cron_line="$(printf '%s\n' "$chosen" | sed -n '3p')"
  echo "This line will be added to your crontab:" >&3
  printf '  %s\n' "$cron_line" >&3
  warn_missing_flock
  printf "Add this line to your crontab? [y/N] " >&3
  read -r answer <&3
  case "${answer}" in
    [yY]|[yY][eE][sS]) ;;
    *)
      echo "Skipping cron."
      return 0
      ;;
  esac

  if ! output="$(run_cron_setup install "$config_path" "$(printf '%s\n' "$chosen" | sed -n '1p')" "$(printf '%s\n' "$chosen" | sed -n '2p')")"; then
    return 0
  fi
  if [[ "$output" == "installed" ]]; then
    echo "Installed. The job logs to ${cron_name}.log. Confirm with: crontab -l"
  elif [[ "$output" == "active" ]]; then
    echo "A cron job for ${cron_marker} is already installed."
  elif [[ "$output" == "commented" ]]; then
    echo "That job is commented out. Run ./setup.sh again and answer yes to uncomment it."
  fi
}

offer_cron() {
  if ! ( : <>/dev/tty ) 2>/dev/null; then
    echo "No terminal attached, so no cron job was installed."
    return 0
  fi
  exec 3<>/dev/tty
  offer_cron_on_tty
  local status=$?
  exec 3<&-
  return "$status"
}

offer_cron

echo "Ready. Run the watcher with:"
echo "  .venv/bin/python watchpage.py --config config.json"
