#!/bin/sh
set -eu
alice_project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
alice_python=${ALICE_PYTHON:-"$HOME/.venvs/alice-cognition/bin/python"}
exec "$alice_python" "$alice_project_root/alice/surf/live.py" "$@"
