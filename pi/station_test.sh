#!/usr/bin/env bash
# Station test: ATC (all 43 channels, cores 0-1, clips on core 3) and ADS-B (the
# RTL-SDR, core 2) together for a set time. Reports each system's uptime, every
# core's load, temperature and storage per day / week / month.
#
#     pi/station_test.sh 1h --ref 37.43 -122.17
#     pi/station_test.sh 8h --ref 37.43 -122.17          # overnight (run it in tmux)
#     pi/station_test.sh 10m --atc-source synthetic --adsb-source synthetic   # no radios
#
# The Pluto comes from pi/pluto.conf, through iqstream. Output: recordings/station_<UTC time>/.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need_venv
DURATION=30m
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    DURATION="$1"
    shift
fi
DEV=()
[ -n "$PLUTO" ] && DEV=(--device "packed:${PLUTO#*:}")
cd "$ROOT"
exec "$PY" -u radio/station_test.py --duration "$DURATION" "${DEV[@]}" "$@"
