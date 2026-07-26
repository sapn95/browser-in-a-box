#!/usr/bin/env sh
# Convenience shim so `./run.sh <command>` keeps working. The tool itself is
# cib.py — standard library only, no venv, no install.
exec python3 "$(dirname "$0")/cib.py" "$@"
