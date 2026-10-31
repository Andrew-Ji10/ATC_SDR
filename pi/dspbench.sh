#!/usr/bin/env bash
# Can this Pi process the whole airband (all 43 channels, one window at 21.6 MSPS)?
# Benchmarks today's receiver against a compiled, multi-core polyphase channelizer,
# checks the channelizer is correct, and runs a thermal test. No radio needed.
#
#     pi/dspbench.sh                     # about 3 minutes
#     pi/dspbench.sh --sustain 300       # 5-minute thermal test
#     pi/dspbench.sh --quick
#
# The report goes to recordings/dspbench_<date>-<time>/report.txt.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
need_venv
cd "$ROOT"
exec "$PY" -u radio/dsp_bench.py "$@"
