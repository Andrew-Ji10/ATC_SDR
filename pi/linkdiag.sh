#!/usr/bin/env bash
# Why can't the Pluto stream the whole band (21.6 MSPS) to the Pi? Isolates the
# radio's capture path, its CPU / network stack, and iiod, while reading the
# radio's CPU load over SSH, then says where the bottleneck is (~2 minutes).
#
#     pi/linkdiag.sh
#     pi/linkdiag.sh --rates 10.8 16.8 21.6 --seconds 8
#     pi/linkdiag.sh --password <radio root password>     # default: analog
#
# The report goes to recordings/linkdiag_<date>-<time>/report.txt.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need_venv
cd "$ROOT"
exec "$PY" -u radio/link_diag.py "${DEVICE[@]}" "$@"
