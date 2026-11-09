#!/bin/bash
# Double-click to open Invoice Triage in your browser (macOS; also works on Linux).
# Keep the window open while you use it.
cd "$(dirname "$0")" || exit 1
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Invoice Triage needs Python 3.10 or newer (macOS includes an older one)."
  echo "Install it from https://www.python.org/downloads/ and double-click this file again."
  read -n 1 -r -p "Press any key to close."
  exit 1
fi
exec "$PY" -m invoice_triage
