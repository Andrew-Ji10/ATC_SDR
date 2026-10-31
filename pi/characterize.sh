#!/usr/bin/env bash
# Measure this Pi + Pluto + antenna (CPU, link, gain sweep; about two minutes)
# and save the recommended --max-rate and --gain as pi/record.sh's defaults.
#
#     pi/characterize.sh                 # all Bay channels
#     pi/characterize.sh --area sfo      # any radio/characterize.py option
#     pi/characterize.sh --cpu-only      # no Pluto needed
#
# Run it with the antenna where it will stay, during the day (more traffic to
# tell apart from interference). The report goes to recordings/characterize_*/.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need_venv

FLAGS="$(mktemp)"
trap 'rm -f "$FLAGS"' EXIT
cd "$ROOT"
"$PY" -u radio/characterize.py "${DEVICE[@]}" --flags-file "$FLAGS" "$@"

RECORD_ARGS="$(cat "$FLAGS")"
save_conf
echo "Saved in pi/pluto.conf: pi/record.sh now uses $RECORD_ARGS by default."
