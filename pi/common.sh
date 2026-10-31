# Shared by the pi/ scripts (sourced, not run). pi/pluto.conf holds this Pi's
# settings, written by setup.sh and characterize.sh:
#   PLUTO        the Pluto's libiio URI, e.g. ip:192.168.3.2 (empty: auto-detect on USB)
#   RECORD_ARGS  default options for record.sh, from characterize.sh
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
CONF="$ROOT/pi/pluto.conf"
PLUTO=""
RECORD_ARGS=""
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
DEVICE=()                    # --device for the Python scripts, if a Pluto URI is set
[ -n "$PLUTO" ] && DEVICE=(--device "$PLUTO")

save_conf() {
    cat >"$CONF" <<EOF
# Written by pi/setup.sh and pi/characterize.sh; edit freely.
PLUTO="$PLUTO"
RECORD_ARGS="$RECORD_ARGS"
EOF
}

need_venv() {
    if [ ! -x "$PY" ]; then
        echo "No Python environment yet: run  bash $ROOT/pi/setup.sh" >&2
        exit 1
    fi
}
