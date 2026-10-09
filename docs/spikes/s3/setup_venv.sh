#!/usr/bin/env bash
# S3 spike: create a throwaway venv with the pinned Edalize and cocotb, outside the repo.
#
#   docs/spikes/s3/setup_venv.sh VENV_DIR PYTHON [PREFERENCE]
#
# PYTHON is a version ("3.14") or an interpreter path; PREFERENCE is passed to uv as
# --python-preference (only-managed = uv's own CPython build, only-system = e.g. Homebrew).
# VENV_DIR must be under /tmp (or $TMPDIR, $RUNNER_TEMP): it is deleted first.
set -euo pipefail

VENV="${1:?usage: setup_venv.sh VENV_DIR PYTHON [PREFERENCE]}"
PY="${2:?usage: setup_venv.sh VENV_DIR PYTHON [PREFERENCE]}"
PREF="${3:-managed}"

EDALIZE_VERSION=0.6.8
COCOTB_VERSION=2.1.0

case "$VENV" in
  /tmp/*|"${TMPDIR:-/nonexistent}"/*|"${RUNNER_TEMP:-/nonexistent}"/*) ;;
  *) echo "setup_venv.sh: refusing $VENV (not under /tmp, \$TMPDIR or \$RUNNER_TEMP)" >&2; exit 2 ;;
esac

unset VIRTUAL_ENV
rm -rf "$VENV"
uv venv -q --python "$PY" --python-preference "$PREF" "$VENV"
uv pip install -q --python "$VENV/bin/python" \
  "edalize==$EDALIZE_VERSION" "cocotb==$COCOTB_VERSION"
"$VENV/bin/python" - <<'PY'
import sys

import cocotb
import edalize.version
import find_libpython

print(f"python   {sys.version.split()[0]} {sys.executable} (base {sys.base_prefix})")
print(f"cocotb   {cocotb.__version__}")
print(f"edalize  {edalize.version.__version__}")
print(f"libpython {find_libpython.find_libpython()}")
PY
