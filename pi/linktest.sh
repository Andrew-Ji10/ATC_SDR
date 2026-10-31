#!/usr/bin/env bash
# Watch the Pi <-> Pluto link for a set time and log every drop: lost pings,
# the cable link going down, stream errors / reconnects / stalls.
#
#     pi/linktest.sh                     # 1 hour
#     pi/linktest.sh 8h                  # overnight
#     pi/linktest.sh 30m --rate 10.8     # any radio/linkwatch.py option after the duration
#     pi/linktest.sh 0                   # until Ctrl-C
#
# It streams at the rate characterize.sh chose for the recorder (from
# pi/pluto.conf), so it tests the link the way recording will use it. Results go
# to recordings/linktest_<date>-<time>/ (events.csv, link.csv, summary.txt).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need_venv

DURATION=1h
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    DURATION="$1"
    shift
fi
RATE=()
if [[ "$RECORD_ARGS" =~ --max-rate[[:space:]]+([0-9.]+) ]]; then
    RATE=(--rate "${BASH_REMATCH[1]}")
fi

cd "$ROOT"
exec "$PY" -u radio/linkwatch.py --duration "$DURATION" "${DEVICE[@]}" "${RATE[@]}" "$@"
