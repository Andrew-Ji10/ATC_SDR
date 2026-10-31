#!/usr/bin/env bash
# Record ATC airband with the Pluto for a set time (run pi/setup.sh once first).
#
#     pi/record.sh                       # 1 hour, all Bay Area channels
#     pi/record.sh 30m                   # 30 minutes
#     pi/record.sh 2h --area sfo         # any radio/record.py option after the duration
#     pi/record.sh 1h30m --area south_bay --max-rate 4 --gain 50
#     pi/record.sh 0                     # until Ctrl-C (or `kill`, which still saves the summary)
#
# Durations: 90 (minutes), 90s, 45m, 2h, 1h30m, 1d. The Pluto address comes from
# pi/pluto.conf (setup.sh --pluto), and so do the --max-rate / --gain that
# characterize.sh recommended; options given here override them. Recordings go
# to recordings/<area>_<date>-<time>/. Run from a terminal you get the live
# status line; from cron or systemd the output goes to recordings/logs/ instead.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need_venv

DURATION=1h
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    DURATION="$1"
    shift
fi
# shellcheck disable=SC2206
CMD=("$PY" -u radio/record.py --duration "$DURATION" "${DEVICE[@]}" $RECORD_ARGS "$@")

cd "$ROOT"
if [ -t 1 ]; then
    exec "${CMD[@]}"
fi
mkdir -p recordings/logs
exec "${CMD[@]}" >>"recordings/logs/record_$(date +%Y%m%d-%H%M%S).log" 2>&1
