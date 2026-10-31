#!/usr/bin/env bash
# One-time setup of a Raspberry Pi (Raspberry Pi OS / Debian, 64-bit) for
# recording airband with a Pluto over Ethernet (USB works too). Safe to run
# again: it only adds what's missing, then checks everything, the Pluto included.
#
#     bash pi/setup.sh --pluto 192.168.3.2                    # Pluto on the LAN / a switch
#     bash pi/setup.sh --pluto 192.168.3.2 --eth-ip 192.168.3.1/24   # cabled straight to the Pi
#     bash pi/setup.sh                                        # Pluto on USB (auto-detected)
#     bash pi/setup.sh --check                                # only the checks
#
# The Pluto's own Ethernet address is set in config.txt on its USB drive
# ([NETWORK] ipaddr_eth / netmask_eth); see the README. --pluto is saved in
# pi/pluto.conf, so record.sh and characterize.sh use it from then on.
#
# What it does:
#   1. apt: Python, numpy, scipy, libiio (+ Python bindings, iio_info), lame
#   2. with --eth-ip: a static address on the Pi's Ethernet port (NetworkManager),
#      for a direct cable; internet keeps going over Wi-Fi
#   3. a udev rule so a Pluto on USB works without sudo (for configuring it)
#   4. a virtualenv in .venv (with the system numpy/scipy) holding noisereduce and numba
#   5. checks: Python modules, lame, the Pluto reachable, and that it tunes airband
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

RULE=/etc/udev/rules.d/53-adi-plutosdr-usb.rules
CHECK_ONLY=0
PLUTO_GIVEN=0
ETH_IP=""
ETH_IF=eth0
usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --check)  CHECK_ONLY=1 ;;
        --pluto)  PLUTO="$2"; PLUTO_GIVEN=1; shift ;;
        --eth-ip) ETH_IP="$2"; shift ;;
        --eth-if) ETH_IF="$2"; shift ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
    shift
done
# A bare address means the network backend.
[ -n "$PLUTO" ] && [ "${PLUTO#*:}" = "$PLUTO" ] && PLUTO="ip:$PLUTO"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILED=1; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
FAILED=0

if [ "$(uname -s)" != "Linux" ] || ! command -v apt-get >/dev/null; then
    echo "This script is for Raspberry Pi OS / Debian (apt). On macOS see the README." >&2
    exit 1
fi
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

# ---------------------------------------------------------------- install
if [ "$CHECK_ONLY" -eq 0 ]; then
    say "System packages"
    $SUDO apt-get update -q
    $SUDO apt-get install -y -q python3 python3-venv python3-pip python3-numpy \
        python3-scipy python3-matplotlib python3-tqdm python3-joblib \
        libiio-utils lame usbutils iputils-ping
    # The libiio Python bindings: from apt if this release packages them, else pip.
    if apt-cache show python3-libiio >/dev/null 2>&1; then
        $SUDO apt-get install -y -q python3-libiio
        PIP_IIO=""
    else
        $SUDO apt-get install -y -q libiio0 || $SUDO apt-get install -y -q 'libiio0*'
        PIP_IIO="pylibiio==0.25"
    fi

    if [ -n "$ETH_IP" ]; then
        say "Static address $ETH_IP on $ETH_IF (direct cable to the Pluto)"
        # Don't cut off the session running this script.
        if [ -n "${SSH_CONNECTION:-}" ] && \
           ip route get "$(echo "$SSH_CONNECTION" | cut -d' ' -f1)" 2>/dev/null | grep -q "dev $ETH_IF "; then
            echo "  This SSH session runs over $ETH_IF, which --eth-ip would take over." >&2
            echo "  Connect over Wi-Fi (or a keyboard) and run it again." >&2
            exit 1
        fi
        if command -v nmcli >/dev/null; then
            $SUDO nmcli connection delete pluto-link >/dev/null 2>&1 || true
            # never-default: no gateway here, so internet stays on Wi-Fi.
            $SUDO nmcli connection add type ethernet ifname "$ETH_IF" con-name pluto-link \
                ipv4.method manual ipv4.addresses "$ETH_IP" ipv4.never-default yes \
                ipv6.method disabled connection.autoconnect yes >/dev/null
            $SUDO nmcli connection up pluto-link >/dev/null || \
                warn "pluto-link saved, but $ETH_IF isn't up yet (is the cable in and the Pluto on?)"
            ok "NetworkManager connection pluto-link: $ETH_IF = $ETH_IP"
        else
            bad "NetworkManager (nmcli) not found. On older Raspberry Pi OS add to /etc/dhcpcd.conf:"
            echo "          interface $ETH_IF"
            echo "          static ip_address=$ETH_IP"
            echo "          nogateway"
        fi
    fi

    say "USB access to the Pluto (udev)"
    # 0456:b673 = Pluto (and clones), 0456:b674 = Pluto in firmware-update (DFU) mode.
    # usbfs_memory_mb=0 lifts the kernel's 16 MB cap on USB transfer buffers.
    $SUDO tee "$RULE" >/dev/null <<'EOF'
# ADALM-PLUTO and AD936x clones: usable without root (written by ATC_SDR pi/setup.sh)
SUBSYSTEM=="usb", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b673", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b674", MODE="0666"
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b673", RUN+="/bin/sh -c 'echo 0 > /sys/module/usbcore/parameters/usbfs_memory_mb'"
EOF
    $SUDO udevadm control --reload-rules
    $SUDO udevadm trigger --subsystem-match=usb --attr-match=idVendor=0456 || true
    ok "$RULE"

    say "Python environment ($VENV)"
    [ -x "$PY" ] || python3 -m venv --system-site-packages "$VENV"
    "$PY" -m pip install -q --upgrade pip
    # shellcheck disable=SC2086
    # numba: compiled, multi-core DSP (radio/dsp_bench.py).
    "$PY" -m pip install -q noisereduce numba $PIP_IIO
    ok "noisereduce, numba${PIP_IIO:+, $PIP_IIO} installed"

    mkdir -p "$ROOT/recordings/logs"
    chmod +x "$ROOT/pi/"*.sh "$ROOT/radio/"*.py 2>/dev/null || true
fi
if [ "$CHECK_ONLY" -eq 0 ] || [ "$PLUTO_GIVEN" -eq 1 ]; then
    save_conf
    ok "pi/pluto.conf: PLUTO=${PLUTO:-(auto-detect on USB)}"
fi

# ---------------------------------------------------------------- check
say "Checks"
if [ -x "$PY" ]; then ok "virtualenv: $("$PY" --version)"; else bad "no $VENV: run bash pi/setup.sh"; PY=python3; fi

for mod in numpy scipy iio noisereduce numba; do
    if "$PY" -c "import $mod" 2>/dev/null; then ok "python module $mod"; else bad "python module $mod missing"; fi
done
if command -v lame >/dev/null; then ok "lame (MP3 clips)"; else warn "lame missing: clips will be WAV only"; fi
if command -v iio_info >/dev/null; then ok "iio_info"; else bad "iio_info missing (apt install libiio-utils)"; fi

case "$PLUTO" in
    ip:*)
        HOST="${PLUTO#ip:}"
        if ping -c 1 -W 2 "$HOST" >/dev/null 2>&1; then
            ok "Pluto answers ping at $HOST ($(ip route get "$HOST" 2>/dev/null | grep -o 'dev [^ ]*' || true))"
        else
            bad "no ping reply from $HOST: check the cable, the Pluto's ipaddr_eth, and that"
            echo "        the Pi has an address on that subnet (--eth-ip for a direct cable)"
        fi ;;
    "")
        [ -f "$RULE" ] && ok "udev rule" || bad "udev rule $RULE missing"
        if lsusb 2>/dev/null | grep -qi '0456:b67'; then
            ok "Pluto on USB: $(lsusb | grep -i '0456:b67' | head -1 | cut -d' ' -f6-)"
        else
            warn "no Pluto on USB. For Ethernet, run: bash pi/setup.sh --pluto <its address>"
        fi ;;
esac

# Open the Pluto through libiio and make sure it tunes airband (118-137 MHz):
# a stock AD9363 Pluto stops at 325 MHz until it's switched to AD9364 mode.
cd "$ROOT"
if PLUTO="$PLUTO" "$PY" - <<'EOF'
import os, sys
sys.path.insert(0, ".")
from atcsdr import pluto
iio = pluto._iio()
uri = pluto.find_uri(os.environ["PLUTO"])
try:
    ctx = iio.Context(uri)
except OSError as e:
    print(f"  \033[31mFAIL\033[0m  libiio can't open the Pluto at {uri}: {e}")
    print("        Make sure no other program is using it, and power-cycle it if needed.")
    sys.exit(1)
lo = ctx.find_device("ad9361-phy").find_channel("altvoltage0", True)
before = lo.attrs["frequency"].value
try:
    lo.attrs["frequency"].value = "120000000"
    got = int(lo.attrs["frequency"].value)
except OSError:
    got = 0
finally:
    try:
        lo.attrs["frequency"].value = before
    except OSError:
        pass
print(f"  \033[32mok\033[0m    libiio opens the Pluto at {uri}")
if abs(got - 120_000_000) > 1000:
    host = uri[3:] if uri.startswith("ip:") else "192.168.2.1"
    print("  \033[31mFAIL\033[0m  the Pluto won't tune to 120 MHz: it's in AD9363 mode (325 MHz-3.8 GHz).")
    print("        Unlock it (README, 'Pluto: unlock the tuning range'):")
    print(f"          ssh root@{host}   # password: analog")
    print("          fw_setenv attr_name compatible; fw_setenv attr_val ad9364; reboot")
    sys.exit(1)
print("  \033[32mok\033[0m    tunes airband (120 MHz)")
EOF
then :; else FAILED=1; fi

say "Result"
if [ "$FAILED" -eq 0 ]; then
    cat <<EOF
  Everything is ready. Next, measure what this Pi and antenna can do (about two
  minutes); it saves the best --max-rate and --gain as record.sh's defaults:

    pi/characterize.sh

  To see whether the link to the Pluto ever drops, leave this running (e.g. overnight):

    pi/linktest.sh 8h

  Then record, by hand or on a schedule with cron (crontab -e), e.g. 07:00-09:00:

    pi/record.sh 2h --area sfo
    0 7 * * * $ROOT/pi/record.sh 2h --area sfo
EOF
else
    echo "  Some checks failed (above). Fix them and run: bash pi/setup.sh --check"
    exit 1
fi
