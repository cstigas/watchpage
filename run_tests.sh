#!/usr/bin/env bash
# Run every test in tests/, one test named with -t / --test, or list the names.
set -euo pipefail
cd "$(dirname "$0")"

python="python3"
if [[ -x .venv/bin/python ]]; then
  python=".venv/bin/python"
fi

usage() {
  cat <<'EOF'
usage: ./run_tests.sh [-l|--list] [-t|--test NAME]

With no arguments, run every test in tests/.
-l, --list    print the test names
-t, --test    run one test

NAME is a test module such as test_browser or test_classify.
test_sms_send and test_outage_with_send send a real SMS.
They run only when named with -t or --test.
A path or dotted unittest name also works.
EOF
}

list_tests() {
  local path stem
  {
    for path in tests/test_*.py; do
      [[ -f "$path" ]] || continue
      stem="${path##*/}"
      printf '%s\n' "${stem%.py}"
    done
    printf '%s\n' test_outage_with_send
  } | sort
}

run_outage() {
  echo "A local port will accept connections and never answer."
  echo "The watcher times out against it 10 times, then sends one outage text."
  echo "This test does not write state.json and does not edit crontab."
  export DONT_UPDATE_STATE=1
  exec "$python" -c 'import sys, watchpage; watchpage.warn_config(); sys.exit(watchpage.run_outage_test(watchpage.load_config()))'
}

list_only=0
test_name=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    -l|--list)
      list_only=1
      shift
      ;;
    -t|--test)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "missing test name" >&2
        usage >&2
        exit 2
      fi
      if [[ -n "$test_name" ]]; then
        echo "only one test name can be passed" >&2
        usage >&2
        exit 2
      fi
      test_name="$2"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$list_only" -eq 1 ]]; then
  if [[ -n "$test_name" ]]; then
    echo "cannot combine --list with --test" >&2
    usage >&2
    exit 2
  fi
  list_tests
  exit 0
fi

if [[ -z "$test_name" ]]; then
  exec "$python" -m unittest discover -s tests -t . -v
fi

if [[ "$test_name" == "test_outage_with_send" || "$test_name" == "outage_with_send" ]]; then
  run_outage
fi

target=""
if [[ "$test_name" == */* || "$test_name" == *.py ]]; then
  if [[ ! -f "$test_name" ]]; then
    echo "unknown test: $test_name" >&2
    echo "tests:" >&2
    list_tests >&2
    exit 1
  fi
  target="$test_name"
elif [[ "$test_name" == *.* ]]; then
  target="$test_name"
elif [[ -f "tests/${test_name}.py" ]]; then
  target="tests/${test_name}.py"
elif [[ -f "tests/test_${test_name}.py" ]]; then
  target="tests/test_${test_name}.py"
else
  echo "unknown test: $test_name" >&2
  echo "tests:" >&2
  list_tests >&2
  exit 1
fi

case "$target" in
  tests/test_sms_send.py|*/test_sms_send.py|tests.test_sms_send|tests.test_sms_send.*)
    export SEND_TEST_SMS=1
    ;;
esac

exec "$python" -m unittest "$target" -v
