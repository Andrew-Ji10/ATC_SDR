#!/usr/bin/env bash
# Build iqstream (the 12-bit streamer) and check it: loopback on this Pi, the radio
# sending a test pattern, then the radio's real samples at 19.2 and 21.6 MSPS
# with 1 and 2 connections. About 2 minutes.
#
#     pi/iqstream_test.sh
#     pi/iqstream_test.sh --loopback-only      # no radio
#
# The report goes to recordings/iqstream_<date>-<time>/report.txt.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need_venv
bash "$ROOT/pi/build_iqstream.sh"
cd "$ROOT"
DEV=()
# The check takes the plain address: it starts iqstream itself.
[ -n "$PLUTO" ] && DEV=(--device "ip:${PLUTO#*:}")
exec "$PY" -u radio/iqstream_check.py "${DEV[@]}" "$@"
