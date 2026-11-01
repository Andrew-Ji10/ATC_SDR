#!/usr/bin/env bash
# The whole check, in order, with one pass/fail report at the end:
#   1. setup      bash pi/setup.sh --check (packages, the Pluto, the RTL-SDR, clock)
#   2. tests      the test suite on synthetic signals (no radios)
#   3. build      iqstream for the radio (and the Pi's copy)
#   4. pi-alone   the station test with synthetic signals, 2 min: can the Pi keep up?
#   5. station    the real station test: ATC (all 43 channels) and ADS-B together
#
#     pi/full_test.sh                               # station test 30 min
#     pi/full_test.sh 8h --ref 37.43 -122.17        # overnight (run it in tmux)
#
# Anything after the duration goes to the station test. A failed step doesn't
# stop the rest. Everything is in recordings/fulltest_<UTC time>/ (report.txt).
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need_venv

DURATION=30m
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    DURATION="$1"
    shift
fi
OUT="$ROOT/recordings/fulltest_$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$OUT"
cd "$ROOT"
RESULTS=()

step() {                        # step <name> <command...>: run it, log it, record pass/fail
    local name="$1" start rc
    shift
    printf '\n\033[1m== %s\033[0m  (%s)\n' "$name" "$*"
    start=$(date +%s)
    "$@" 2>&1 | tee "$OUT/$name.log"
    rc=${PIPESTATUS[0]}
    RESULTS+=("$(printf '%-9s %-4s %5ss  %s' "$name" "$([ "$rc" -eq 0 ] && echo PASS || echo FAIL)" \
        "$(( $(date +%s) - start ))" "$([ "$rc" -eq 0 ] || echo "exit $rc, see $name.log")")")
}

step setup bash pi/setup.sh --check
if "$PY" -c "import pytest" 2>/dev/null; then
    step tests "$PY" -m pytest -q tests
else
    RESULTS+=("tests     SKIP        pytest not installed (bash pi/setup.sh installs it)")
fi
step build bash pi/build_iqstream.sh
step pi-alone "$PY" -u radio/station_test.py --duration 2m --atc-source synthetic --adsb-source synthetic \
    --out "$OUT/pi_alone"
DEV=()
[ -n "$PLUTO" ] && DEV=(--device "packed:${PLUTO#*:}")
step station "$PY" -u radio/station_test.py --duration "$DURATION" "${DEV[@]}" "$@" --out "$OUT/station"

{
    echo "Full test $(date -u +%Y-%m-%dT%H:%M:%SZ), station test $DURATION"
    echo
    printf '%s\n' "${RESULTS[@]}"
    for run in pi_alone station; do
        if [ -f "$OUT/$run/summary.txt" ]; then
            echo
            echo "-- $run"
            sed -n '2,8p' "$OUT/$run/summary.txt"
        fi
    done
    echo
    echo "Details: $OUT/ (each step's .log; pi_alone/ and station/ hold the full reports)"
} | tee "$OUT/report.txt"
