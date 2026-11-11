#!/usr/bin/env bash
# Read-only CAN probe for a rusEFI/mck1117 wideband via Tactrix OpenPort 2.0.
# Checks the openport-j2534 library and the cable, then runs `wideband_flash.py probe`.
# Sends nothing on the bus. Extra arguments are passed to `probe`, e.g.:
#   ./wideband_probe.sh --seconds 10 --max-frames 100

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENPORT_DIR="${OPENPORT_DIR:-$HOME/projects/openport-j2534}"
LIB="${J2534_LIB:-$OPENPORT_DIR/libj2534.so}"
BITRATE="${BITRATE:-500000}"

die() { echo "ERROR: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH"

[[ -f "$SCRIPT_DIR/wideband_flash.py" ]] || die "wideband_flash.py not found next to this script"

if [[ ! -f "$LIB" ]]; then
    die "libj2534.so not found at $LIB. Build it first: (cd \"$OPENPORT_DIR\" && make), or set OPENPORT_DIR / J2534_LIB."
fi

# Cable check: Tactrix OpenPort 2.0 is USB 0403:cc4d. Only checked if lsusb is available.
if command -v lsusb >/dev/null 2>&1; then
    if ! lsusb -d 0403:cc4d >/dev/null 2>&1; then
        die "OpenPort 2.0 (USB 0403:cc4d) not detected. Plug in the cable; remove its microSD card first."
    fi
    echo "Cable: $(lsusb -d 0403:cc4d | head -n1)"
else
    echo "Note: lsusb not found, skipping cable detection."
fi

echo "Library: $LIB"
echo "Bitrate: $BITRATE bit/s"
echo "Running read-only probe (nothing is transmitted)..."

exec python3 "$SCRIPT_DIR/wideband_flash.py" --lib "$LIB" --bitrate "$BITRATE" probe "$@"
