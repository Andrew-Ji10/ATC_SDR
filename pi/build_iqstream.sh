#!/usr/bin/env bash
# Build iqstream (radio/iqstream/iqstream.c), the 12-bit sample streamer:
#   build/iqstream-armhf   for the radio: 32-bit ARM (Cortex-A9), static, no libraries needed
#   build/iqstream-native  for this Pi, for the loopback self-test
# The Pi copies iqstream-armhf to the radio's /tmp when it streams (atcsdr/iqstream.py).
#
#     bash pi/build_iqstream.sh
#
# It compiles with Zig's C compiler (pip install ziglang, into .venv: pi/setup.sh
# does it), which cross-compiles for ARM with its own C library, so no apt
# packages are needed. Falls back to arm-linux-gnueabihf-gcc if that's installed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/radio/iqstream/iqstream.c"
OUT="$ROOT/build"
PY="$ROOT/.venv/bin/python"
FLAGS=(-O3 -static -pthread -Wall -Wextra)
mkdir -p "$OUT"

if [ -x "$PY" ] && "$PY" -m ziglang version >/dev/null 2>&1; then
    ZIG=("$PY" -m ziglang cc)
elif python3 -m ziglang version >/dev/null 2>&1; then
    ZIG=(python3 -m ziglang cc)
else
    ZIG=()
fi

if [ ${#ZIG[@]} -gt 0 ]; then
    "${ZIG[@]}" -target arm-linux-musleabihf -mcpu=cortex_a9 "${FLAGS[@]}" -o "$OUT/iqstream-armhf" "$SRC"
    "${ZIG[@]}" -target "$(uname -m)-linux-musl" "${FLAGS[@]}" -o "$OUT/iqstream-native" "$SRC"
elif command -v arm-linux-gnueabihf-gcc >/dev/null; then
    arm-linux-gnueabihf-gcc -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard "${FLAGS[@]}" \
        -o "$OUT/iqstream-armhf" "$SRC"
    if command -v gcc >/dev/null; then
        gcc -O3 -pthread -Wall -Wextra -o "$OUT/iqstream-native" "$SRC"
    fi
else
    echo "No compiler: run  $PY -m pip install ziglang  (or bash pi/setup.sh)" >&2
    exit 1
fi
for f in iqstream-armhf iqstream-native; do
    [ -f "$OUT/$f" ] && echo "built $OUT/$f ($(stat -c %s "$OUT/$f") bytes)"
done
