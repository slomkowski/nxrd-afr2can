#!/usr/bin/env bash
# Enter the rusEFI WBO CAN bootloader right after powering the module on.
# Start this script, THEN switch the module's power on (or start it just after;
# both work). It repeatedly sends the bootloader-entry request until the module
# ACKs, confirms with a second ACK, and exits. Nothing is erased or flashed.
#
#   ./wideband_bootloader.sh                 # listen/try for 30 s
#   DURATION=60 ./wideband_bootloader.sh
#   BITRATE=250000 ./wideband_bootloader.sh
#
# Use exactly ONE wideband on an isolated bench CAN bus.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENPORT_DIR="${OPENPORT_DIR:-$HOME/projects/openport-j2534}"
LIB="${J2534_LIB:-$OPENPORT_DIR/libj2534.so}"
BITRATE="${BITRATE:-500000}"
DURATION="${DURATION:-30}"

die() { echo "ERROR: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH"
[[ -f "$SCRIPT_DIR/wideband_flash.py" ]] || die "wideband_flash.py not found next to this script"
[[ -f "$LIB" ]] || die "libj2534.so not found at $LIB. Build it: (cd \"$OPENPORT_DIR\" && make), or set OPENPORT_DIR / J2534_LIB."

if command -v lsusb >/dev/null 2>&1; then
    lsusb -d 0403:cc4d >/dev/null 2>&1 \
        || die "OpenPort 2.0 (USB 0403:cc4d) not detected. Plug in the cable; remove its microSD card first."
fi

echo "Library: $LIB | CAN $BITRATE bit/s | trying for ${DURATION}s"
echo "Power the wideband ON now (or power-cycle it)."

SCRIPT_DIR="$SCRIPT_DIR" LIB="$LIB" BITRATE="$BITRATE" DURATION="$DURATION" python3 - <<'PYEOF'
import os
import sys
import time

sys.path.insert(0, os.environ["SCRIPT_DIR"])
import wideband_flash as w

lib = os.environ["LIB"]
bitrate = int(os.environ["BITRATE"])
duration = float(os.environ["DURATION"])

try:
    with w.OpenPort(lib, bitrate, receive_all=False) as bus:
        deadline = time.monotonic() + duration
        attempts = 0
        last_err = None
        entered = False
        while time.monotonic() < deadline:
            attempts += 1
            try:
                # Empty payload: bootloader (and 'recover') style entry.
                if bus.command(w.WB_BL_ENTER, b"", timeout_ms=100):
                    entered = True
                    break
            except w.FlashError as exc:
                # Typically TX timeout while the module is still unpowered
                # (nobody ACKs on the bus). Keep trying until the deadline.
                last_err = str(exc)
            time.sleep(0.02)
        if not entered:
            print(f"\nNo ACK after {attempts} attempt(s) in {duration:g}s.", file=sys.stderr)
            if last_err:
                print(f"Last error: {last_err}", file=sys.stderr)
                print("TX errors usually mean no powered node on the bus: check power, "
                      "CAN-H/CAN-L, 120 ohm termination, bitrate.", file=sys.stderr)
            sys.exit(2)
        print(f"ACK received after {attempts} attempt(s). Confirming...")
        time.sleep(0.2)
        if not bus.command(w.WB_BL_ENTER, b"", timeout_ms=1500):
            print("Second ACK missing; bootloader mode NOT confirmed.", file=sys.stderr)
            sys.exit(3)
        print("OK: module is in bootloader mode (2x ACK). Nothing was erased.")
        print("Next: ./wideband_flash.py ... flash IMAGE --yes  (it will re-enter the bootloader itself).")
except w.FlashError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(2)
PYEOF
