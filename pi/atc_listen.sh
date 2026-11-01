#!/usr/bin/env bash
# ATC listen test: the full stack, the whole band (all 43 channels) in one window at
# 20.0 MSPS, for a set time. Saves each transmission as a plain and a cleaned MP3
# (UTC-stamped), logs the stream's health every second, and ends with the uptime,
# per-channel traffic and storage per day / week / month.
#
#     pi/atc_listen.sh                   # 10 minutes
#     pi/atc_listen.sh 1h
#     pi/atc_listen.sh 8h --threshold 10 # any radio/atc_listen.py option after the duration
#
# The radio comes from pi/pluto.conf, through iqstream (packed:<address>), since
# iiod can't carry 20 MSPS. Output: recordings/listen_<UTC time>/.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need_venv

DURATION=10m
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    DURATION="$1"
    shift
fi
DEV=()
if [ -n "$PLUTO" ]; then
    DEV=(--device "packed:${PLUTO#*:}")
fi

cd "$ROOT"
exec "$PY" -u radio/atc_listen.py --duration "$DURATION" "${DEV[@]}" "$@"
