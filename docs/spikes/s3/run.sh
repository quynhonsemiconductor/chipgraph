#!/usr/bin/env bash
# S3 spike: the whole experiment, as run locally and by .github/workflows/spike-s3.yml.
#
#   docs/spikes/s3/run.sh OUT_DIR [PYTHON] [PREFERENCE]
#
# Creates a throwaway venv at OUT_DIR/venv (setup_venv.sh), checks that Edalize's
# configure step starts no subprocess (probe_configure.py), then runs every flow x case
# (run_all.py) and prints the summary table. OUT_DIR must be under /tmp, $TMPDIR or
# $RUNNER_TEMP. Needs uv, verilator, iverilog/vvp, make and a C++ compiler on PATH.
# Exits non-zero if any step fails or any case's outcome is not the expected one.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:?usage: run.sh OUT_DIR [PYTHON] [PREFERENCE]}"
PY="${2:-3.14}"
PREF="${3:-only-managed}"

mkdir -p "$OUT"
"$HERE/setup_venv.sh" "$OUT/venv" "$PY" "$PREF" | tee "$OUT/venv.txt"
VPY="$OUT/venv/bin/python"

echo "== probe_configure"
(cd "$HERE" && "$VPY" probe_configure.py --out "$OUT/probe") | tee "$OUT/probe.txt"

echo "== run_all"
(cd "$HERE" && "$VPY" run_all.py --out "$OUT/results")
