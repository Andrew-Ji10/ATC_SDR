#!/usr/bin/env bash
# One-time setup of a Mac for the reception tests (RTL-SDR). Safe to run again:
# it only adds what's missing, then checks everything, the dongle included.
#
#     bash reception_test/setup.sh            # install what's missing, then check
#     bash reception_test/setup.sh --check    # only the checks
#     bash reception_test/setup.sh --full     # the whole repo's environment (GNU Radio, Pluto: ~2 GB)
#
# What it does:
#   1. conda: uses yours if there is one, else installs Miniforge in ~/miniforge3
#   2. the `sdr` conda environment, with what the tests need: numpy, scipy, lame
#      (MP3), rtl-sdr (the dongle's tools and libusb: macOS needs no other driver),
#      pyModeS (ADS-B) and noisereduce (cleaned clips). An existing `sdr`
#      environment (e.g. from environment.yml) just gets the missing pieces.
#   3. checks: the Python modules, lame, rtl_sdr, the dongle on USB and opening
#      it, no other program holding it, and the scanner's own tests.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_NAME=sdr
CHECK_ONLY=0
FULL=0
usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --full)  FULL=1 ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
    shift
done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILED=1; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
FAILED=0

if [ "$(uname -s)" != "Darwin" ]; then
    echo "This script is for macOS. On a Raspberry Pi use pi/setup.sh." >&2
    exit 1
fi

# ---------------------------------------------------------------- 1. conda

find_conda() {
    command -v conda 2>/dev/null && return
    for d in ~/miniforge3 ~/mambaforge ~/miniconda3 ~/anaconda3 /opt/homebrew/Caskroom/miniforge/base; do
        [ -x "$d/bin/conda" ] && { echo "$d/bin/conda"; return; }
    done
}
say "conda"
CONDA="$(find_conda || true)"
if [ -z "$CONDA" ]; then
    if [ "$CHECK_ONLY" = 1 ]; then
        bad "conda not found (run this script without --check to install Miniforge)"
        exit 1
    fi
    ARCH="$(uname -m)"                     # arm64 (Apple Silicon) or x86_64 (Intel)
    echo "  Installing Miniforge ($ARCH) in ~/miniforge3..."
    curl -fsSLo /tmp/miniforge.sh \
        "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-MacOSX-$ARCH.sh"
    bash /tmp/miniforge.sh -b -p ~/miniforge3
    rm -f /tmp/miniforge.sh
    CONDA=~/miniforge3/bin/conda
    "$CONDA" init zsh bash >/dev/null      # `conda activate` in new terminals
    NEW_CONDA=1
fi
ok "$("$CONDA" --version) at $CONDA"
BASE="$("$CONDA" info --base)"
PREFIX="$BASE/envs/$ENV_NAME"
PY="$PREFIX/bin/python"

# ---------------------------------------------------------------- 2. environment

CONDA_PKGS=(numpy scipy lame rtl-sdr pip)
PIP_PKGS=("pyModeS>=3" noisereduce pytest)
if [ "$CHECK_ONLY" = 0 ]; then
    say "the '$ENV_NAME' environment"
    if [ ! -x "$PY" ]; then
        if [ "$FULL" = 1 ]; then
            echo "  Creating it from environment.yml (several minutes)..."
            "$CONDA" env create -n "$ENV_NAME" -f "$ROOT/environment.yml"
        else
            echo "  Creating it (a minute or two)..."
            "$CONDA" create -y -n "$ENV_NAME" -c conda-forge "python>=3.11" "${CONDA_PKGS[@]}"
        fi
    elif [ "$FULL" = 1 ]; then
        "$CONDA" env update -n "$ENV_NAME" -f "$ROOT/environment.yml"
    fi
    # Whatever the environment came from, add what the tests need and it lacks.
    missing=()
    [ -x "$PREFIX/bin/rtl_sdr" ] || missing+=(rtl-sdr)
    [ -x "$PREFIX/bin/lame" ]    || missing+=(lame)
    "$PY" -c "import numpy, scipy" 2>/dev/null || missing+=(numpy scipy)
    if [ ${#missing[@]} -gt 0 ]; then
        echo "  Adding ${missing[*]}..."
        "$CONDA" install -y -n "$ENV_NAME" -c conda-forge "${missing[@]}"
    fi
    "$PY" -c "import pyModeS, noisereduce, pytest" 2>/dev/null \
        || "$PY" -m pip install --quiet "${PIP_PKGS[@]}"
    ok "$PREFIX"
fi

# ---------------------------------------------------------------- 3. checks

say "software"
if [ ! -x "$PY" ]; then
    bad "no '$ENV_NAME' environment (run this script without --check)"
    exit 1
fi
ok "$("$PY" --version)"
for m in numpy scipy pyModeS noisereduce; do
    if v="$("$PY" -c "import $m, importlib.metadata as md; print(md.version('$m'))" 2>/dev/null)"; then
        ok "$m $v"
    else
        bad "Python module $m missing"
    fi
done
for t in rtl_sdr rtl_test lame; do
    [ -x "$PREFIX/bin/$t" ] && ok "$t" || bad "$t missing (conda install -n $ENV_NAME -c conda-forge ${t/rtl_*/rtl-sdr})"
done
(cd "$ROOT" && "$PY" -c "import sys; sys.path.insert(0, 'reception_test'); import atc_reception") 2>/dev/null \
    && ok "the reception scripts load" || bad "reception_test/atc_reception.py doesn't load"

say "RTL-SDR"
# rtl_test -t opens the dongle, reports the tuner and exits (killed after 8 s if it doesn't).
out="$(mktemp)"
"$PREFIX/bin/rtl_test" -t >"$out" 2>&1 & pid=$!
for _ in $(seq 16); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true
others="$(pgrep -fl 'rtl_sdr|rtl_tcp|rtl_fm|rtl_adsb|dump1090|SDR\+\+|sdrpp|gqrx|CubicSDR' | grep -v $$ || true)"
if grep -q "Found [1-9]" "$out" && ! grep -qiE "usb_open error|usb_claim_interface error|Failed to open" "$out"; then
    ok "$(grep -m1 -E '^ *0:' "$out" | sed 's/^ *//') opens"
    tuner="$(grep -m1 -i 'Found .* tuner' "$out" || true)"
    [ -n "$tuner" ] && ok "$tuner"
# An RTL2832U on USB has product ID 0x2838 or 0x2832 (10296 / 10290); the vendor ID alone (Realtek,
# 0x0bda) also matches Realtek network adapters.
elif ioreg -p IOUSB -l -w0 2>/dev/null | grep -qE '"idProduct" = (10296|10290)$'; then
    bad "the dongle is on USB but didn't open. rtl_test said:"
    sed 's/^/          /' "$out" | head -8
    echo "          Close other SDR programs, unplug it, plug it back in (straight into the Mac"
    echo "          if possible), and run: bash reception_test/setup.sh --check"
else
    warn "no RTL-SDR found: plug it in and run 'bash reception_test/setup.sh --check'"
fi
if [ -n "$others" ]; then
    warn "another program may be holding the dongle; close it before testing:"
    echo "$others" | sed 's/^/          /'
fi
rm -f "$out"

say "scanner self-test (simulated signals, ~15 s)"
if (cd "$ROOT" && "$PY" -m pytest -q tests/test_reception.py >/tmp/reception_selftest.log 2>&1); then
    ok "$(tail -1 /tmp/reception_selftest.log)"
else
    bad "self-test failed:"; tail -15 /tmp/reception_selftest.log | sed 's/^/          /'
fi

echo
if [ "$FAILED" = 0 ]; then
    printf '\033[32mReady.\033[0m'
    [ "${NEW_CONDA:-0}" = 1 ] && printf ' Open a new terminal first (conda was just installed).'
    cat <<EOF

    conda activate $ENV_NAME
    cd "$ROOT"
    python3 reception_test/atc_reception.py                      # long antenna, 20 min
    python3 reception_test/adsb_reception.py --ref LAT LON       # short antenna, 10 min
EOF
else
    printf '\033[31mSome checks failed (above).\033[0m Fix them and run: bash reception_test/setup.sh --check\n'
    exit 1
fi
