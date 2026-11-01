#!/usr/bin/env bash
# ADS-B listen test: decode and track aircraft from the RTL-SDR for a set time.
#
#     pi/adsb_listen.sh                          # 10 minutes
#     pi/adsb_listen.sh 1h --ref 37.43 -122.17   # your latitude / longitude: positions from the first message
#     pi/adsb_listen.sh 2m --source synthetic    # no dongle
#
# Output: recordings/adsb_<UTC time>/ (messages.txt.gz, positions.csv, aircraft.csv,
# health.csv, summary.txt).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need_venv
DURATION=10m
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    DURATION="$1"
    shift
fi
cd "$ROOT"
exec "$PY" -u radio/adsb_listen.py --duration "$DURATION" "$@"
