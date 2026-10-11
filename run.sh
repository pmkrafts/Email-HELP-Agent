#!/usr/bin/env bash
# One-command runner (POSIX shells / Git Bash):  ./run.sh [run.py options]
#
# Picks the first interpreter that is Python 3.11+ (Windows ships an App-Execution-Alias
# stub named python3 that opens the Microsoft Store, so probing beats trusting PATH).
set -euo pipefail
cd "$(dirname "$0")"

for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    exec "$candidate" run.py "$@"
  fi
done

echo "run.sh: no Python 3.11+ interpreter found (tried: python3, python, py)" >&2
echo "        install Python from https://python.org and retry" >&2
exit 2
