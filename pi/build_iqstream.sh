#!/usr/bin/env bash
# Build iqstream (radio/iqstream/iqstream.c), the 12-bit sample streamer:
#   build/iqstream-armhf   for the radio: ARM Cortex-A9, static (no libraries needed there)
#   build/iqstream-native  for this Pi, for the loopback self-test
# The Pi copies iqstream-armhf to the radio's /tmp when it streams (atcsdr/iqstream.py).
#
#     bash pi/build_iqstream.sh
#
# Needs gcc and the ARM cross compiler: sudo apt install gcc gcc-arm-linux-gnueabihf
# (pi/setup.sh installs both).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/radio/iqstream/iqstream.c"
OUT="$ROOT/build"
CROSS="${CROSS:-arm-linux-gnueabihf-gcc}"
mkdir -p "$OUT"

if ! command -v "$CROSS" >/dev/null; then
    echo "$CROSS not found: sudo apt install gcc-arm-linux-gnueabihf" >&2
    exit 1
fi
# Cortex-A9 with NEON and hard float: the Zynq-7000 in the Pluto and its clones.
"$CROSS" -O3 -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard -static -pthread -Wall -Wextra \
    -o "$OUT/iqstream-armhf" "$SRC"
echo "built $OUT/iqstream-armhf ($(stat -c %s "$OUT/iqstream-armhf") bytes)"

if command -v gcc >/dev/null; then
    gcc -O3 -pthread -Wall -Wextra -o "$OUT/iqstream-native" "$SRC"
    echo "built $OUT/iqstream-native"
else
    echo "gcc not found: skipped the native build (only needed for the self-test)" >&2
fi
