#!/usr/bin/env bash
# Compare RF gains on live traffic (radio/gain_sweep.py): about 10 minutes, the
# gain switching every 2 s, then a report of the SNR at each gain and the best
# --gain. The Pluto address comes from pi/pluto.conf (setup.sh --pluto).
#
#     pi/gain_sweep.sh                                 # 10 minutes, gains 30-65
#     pi/gain_sweep.sh --duration 20m --gains 40 45 50 55 60
#     pi/gain_sweep.sh --device packed:192.168.3.2     # through iqstream: the whole band at once
#
# Run it during the day, with the antenna where it will stay. The report goes to
# recordings/gain_sweep_*/report.txt.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need_venv
cd "$ROOT"
exec "$PY" -u radio/gain_sweep.py "${DEVICE[@]}" "$@"
