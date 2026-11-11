#!/usr/bin/env python3
"""Flash a rusEFI/mck1117 STM32F042 f0_module wideband via Tactrix OpenPort 2.0.

Requires the third-party slomkowski/openport-j2534 libj2534.so on Linux.
This is an independently written client for the public rusEFI WBO CAN bootloader.
No CAN hardware is available in this build environment: TEST ON A BENCH FIRST.
"""

from __future__ import annotations

import argparse
import ctypes as C
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sys
import time
import zlib

# WBO identifiers: mck1117/wideband/for_rusefi/wideband_can.h
WB_ACK = 0x00727573  # ASCII 'rus', extended CAN ID
WB_BL_ENTER = 0x0EF00000
WB_BL_ERASE = 0x0EF15A5A
WB_BL_DATA_BASE = 0x0EF20000
WB_BL_REBOOT = 0x0EF30000

BOOTLOADER_SIZE = 6 * 1024
APP_SIZE = 25 * 1024                 # includes last 4 bytes of CRC32
COMBINED_SIZE = BOOTLOADER_SIZE + APP_SIZE
CRC_REGION = APP_SIZE - 4

# J2534-1 04.04 values and ABI from slomkowski/openport-j2534/include/j2534/j2534.h
J_U32 = C.c_ulong                   # NOT c_uint32 on 64-bit Linux
CAN = 5
CAN_29BIT_ID = 0x100
CAN_ID_BOTH = 0x800
PASS_FILTER = 1
CLEAR_RX_BUFFER = 8
ERR_TIMEOUT = 9
ERR_BUFFER_EMPTY = 16
J2534_MSG_DATA_MAX = 4128


class PassThruMsg(C.Structure):
    _fields_ = [
        ("ProtocolID", J_U32), ("RxStatus", J_U32),
        ("TxFlags", J_U32), ("Timestamp", J_U32),
        ("DataSize", J_U32), ("ExtraDataIndex", J_U32),
        ("Data", C.c_ubyte * J2534_MSG_DATA_MAX),
    ]


@dataclass(frozen=True)
class CanFrame:
    can_id: int
    payload: bytes


@dataclass(frozen=True)
class Firmware:
    app: bytes
    original_size: int
    is_combined: bool
    sha256: str
    crc32: int


class FlashError(RuntimeError):
    pass


def load_firmware(path: str | Path) -> Firmware:
    raw = Path(path).read_bytes()
    if len(raw) == APP_SIZE:
        app = raw
        combined = False
    elif len(raw) == COMBINED_SIZE:
        app = raw[BOOTLOADER_SIZE:]
        combined = True
    else:
        raise FlashError(
            f"Invalid image size: {len(raw):,} bytes. This utility only supports "
            f"f0_module app ({APP_SIZE:,} B) or bootloader+app ({COMBINED_SIZE:,} B). "
            "Do not flash a naked .elf, .hex, or unfinished build/wideband.bin."
        )
    if not app or app[:64] == b"\xff" * 64 or app[:64] == b"\x00" * 64:
        raise FlashError("The application area is empty, erased, or not a valid f0_module image.")
    calculated = zlib.crc32(app[:CRC_REGION]) & 0xffffffff
    expected = int.from_bytes(app[CRC_REGION:APP_SIZE], "big")
    if calculated != expected:
        raise FlashError(
            f"Image CRC32 mismatch: calculated {calculated:08X}, "
            f"stored {expected:08X}. Refusing to erase device. "
            "Use the output of boards/f0_module/build_wideband.sh."
        )
    return Firmware(
        app=app, original_size=len(raw), is_combined=combined,
        sha256=hashlib.sha256(raw).hexdigest(), crc32=expected,
    )


class OpenPort:
    """Thin ctypes wrapper for slomkowski/bisak libj2534.so."""

    def __init__(self, library: str | None, bitrate: int, receive_all: bool = False):
        self.bitrate = bitrate
        self.receive_all = receive_all
        self.device = J_U32(0)
        self.channel = J_U32(0)
        self.filter_id = J_U32(0)
        self.opened = False
        self.connected = False
        candidates = [library] if library else [
            os.environ.get("J2534_LIB"),
            "/usr/local/lib/libj2534.so", "/usr/lib/libj2534.so",
            "/usr/lib/x86_64-linux-gnu/libj2534.so", "libj2534.so",
        ]
        errors = []
        self.lib = None
        for name in candidates:
            if not name:
                continue
            try:
                self.lib = C.CDLL(name)
                self.loaded_from = name
                break
            except OSError as exc:
                errors.append(str(exc))
        if self.lib is None:
            raise FlashError(
                "Cannot load libj2534.so. Build/install "
                "https://github.com/slomkowski/openport-j2534 and pass --lib "
                "/path/to/libj2534.so if necessary. " + "; ".join(errors[-2:])
            )
        self._set_types()

    def _set_types(self):
        # 'long' return codes; all J2534 unsigned integers use unsigned long.
        def bind(name, argtypes):
            fn = getattr(self.lib, name)
            fn.restype = C.c_long
            fn.argtypes = argtypes
        bind("PassThruOpen", [C.c_void_p, C.POINTER(J_U32)])
        bind("PassThruClose", [J_U32])
        bind("PassThruConnect", [J_U32, J_U32, J_U32, J_U32, C.POINTER(J_U32)])
        bind("PassThruDisconnect", [J_U32])
        bind("PassThruReadMsgs", [J_U32, C.POINTER(PassThruMsg), C.POINTER(J_U32), J_U32])
        bind("PassThruWriteMsgs", [J_U32, C.POINTER(PassThruMsg), C.POINTER(J_U32), J_U32])
        bind("PassThruStartMsgFilter", [J_U32, J_U32, C.POINTER(PassThruMsg),
                                         C.POINTER(PassThruMsg), C.c_void_p,
                                         C.POINTER(J_U32)])
        bind("PassThruIoctl", [J_U32, J_U32, C.c_void_p, C.c_void_p])
        bind("PassThruGetLastError", [C.c_void_p])

    def _last_error(self):
        buf = C.create_string_buffer(1024)
        self.lib.PassThruGetLastError(buf)
        return buf.value.decode("utf-8", "replace")

    def _check(self, rc: int, operation: str):
        if rc:
            raise FlashError(f"{operation}: J2534 error {rc}: {self._last_error()}")

    def __enter__(self):
        try:
            self._check(self.lib.PassThruOpen(None, C.byref(self.device)), "PassThruOpen")
            self.opened = True
            flags = CAN_ID_BOTH if self.receive_all else CAN_29BIT_ID
            self._check(self.lib.PassThruConnect(
                self.device, CAN, flags, self.bitrate, C.byref(self.channel)),
                "PassThruConnect",
            )
            self.connected = True
            # An explicit PASS_FILTER is required by J2534 to allow receive traffic.
            # Filter all frames for 'probe'; only 29-bit WB_ACK for 'flash'.
            mask = 0 if self.receive_all else 0x1FFFFFFF
            pattern = 0 if self.receive_all else WB_ACK
            m = self._msg(mask, b"", is_filter=True)
            p = self._msg(pattern, b"", is_filter=True)
            if self.receive_all:
                m.TxFlags = 0
                p.TxFlags = 0
            self._check(self.lib.PassThruStartMsgFilter(
                self.channel, PASS_FILTER, C.byref(m), C.byref(p), None,
                C.byref(self.filter_id)), "PassThruStartMsgFilter")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, _typ, _value, _tb):
        if self.connected:
            self.lib.PassThruDisconnect(self.channel)
            self.connected = False
        if self.opened:
            self.lib.PassThruClose(self.device)
            self.opened = False

    @staticmethod
    def _msg(can_id: int, payload: bytes, is_filter: bool = False) -> PassThruMsg:
        if not (0 <= can_id <= 0x1FFFFFFF and len(payload) <= 8):
            raise ValueError("Invalid classical CAN frame")
        m = PassThruMsg()
        m.ProtocolID = CAN
        m.TxFlags = CAN_29BIT_ID
        data = can_id.to_bytes(4, "big") + payload
        m.DataSize = len(data)
        m.ExtraDataIndex = len(data)
        m.Data[:len(data)] = data
        return m

    def clear_rx(self):
        self._check(self.lib.PassThruIoctl(self.channel, CLEAR_RX_BUFFER, None, None),
                    "CLEAR_RX_BUFFER")

    def send(self, can_id: int, payload: bytes = b""):
        msg = self._msg(can_id, payload)
        count = J_U32(1)
        rc = self.lib.PassThruWriteMsgs(self.channel, C.byref(msg), C.byref(count), 250)
        self._check(rc, f"PassThruWriteMsgs(CAN ID 0x{can_id:08X})")
        if count.value != 1:
            raise FlashError(f"CAN ID 0x{can_id:08X} was not transmitted (count={count.value})")

    def recv(self, timeout_ms: int) -> CanFrame | None:
        msg = PassThruMsg()
        count = J_U32(1)
        rc = self.lib.PassThruReadMsgs(self.channel, C.byref(msg), C.byref(count),
                                        max(0, timeout_ms))
        if rc in (ERR_TIMEOUT, ERR_BUFFER_EMPTY):
            return None
        self._check(rc, "PassThruReadMsgs")
        if not count.value:
            return None
        if msg.ProtocolID != CAN or not (4 <= msg.DataSize <= 12):
            return None
        raw = bytes(msg.Data[:msg.DataSize])
        return CanFrame(int.from_bytes(raw[:4], "big"), raw[4:])

    def command(self, can_id: int, payload: bytes = b"", timeout_ms: int = 1000,
                expect_len: int = 0) -> bool:
        # Drain previous ACKs so the unsequenced protocol won't accept a stale ACK.
        self.clear_rx()
        self.send(can_id, payload)
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            frame = self.recv(min(remaining, 150))
            if frame is not None and frame.can_id == WB_ACK and len(frame.payload) == expect_len:
                return True
        return False


def enter_bootloader(bus: OpenPort, hw_index: int | None, recover: bool,
                     recovery_seconds: float):
    payload = bytes((hw_index,)) if hw_index is not None else b""
    if recover:
        print(f"RECOVERY: power-cycle the ONE wideband controller now; listening for "
              f"bootloader ACK for up to {recovery_seconds:g} s.", flush=True)
        deadline = time.monotonic() + recovery_seconds
        found = False
        while time.monotonic() < deadline:
            if bus.command(WB_BL_ENTER, b"", timeout_ms=150):
                found = True
                break
            time.sleep(0.05)
        if not found:
            raise FlashError("No WBO bootloader ACK. Check CAN wiring/bitrate/power. "
                             "The bootloader might be overwritten: SWD recovery required.")
        print("Bootloader/app entry ACK received.")
    else:
        print("Requesting bootloader from running firmware...")
        if not bus.command(WB_BL_ENTER, payload, timeout_ms=1500):
            raise FlashError("No ACK from application. Use --recover and power-cycle "
                             "the module if its application is corrupted.")
    time.sleep(0.2)
    print("Confirming bootloader mode...")
    if not bus.command(WB_BL_ENTER, b"", timeout_ms=1500):
        raise FlashError("Second bootloader-entry ACK missing; not erasing flash.")
    time.sleep(0.2)


def flash(bus: OpenPort, firmware: Firmware, hw_index: int | None,
          recover: bool, recovery_seconds: float, ack_timeout: int):
    enter_bootloader(bus, hw_index, recover, recovery_seconds)
    print("Erasing application flash (bootloader and config section are preserved)...", flush=True)
    if not bus.command(WB_BL_ERASE, timeout_ms=5000):
        raise FlashError("Erase ACK missing. Abort; don't disconnect power until recovered.")
    print(f"Transmitting {len(firmware.app):,} bytes in "
          f"{len(firmware.app) // 8:,} acknowledged frames...")
    t0 = time.monotonic()
    for offset in range(0, len(firmware.app), 8):
        block = firmware.app[offset:offset + 8]
        if len(block) != 8:
            raise AssertionError("Unexpected non-eight-byte image tail")
        can_id = WB_BL_DATA_BASE | offset
        # Never blindly retry a failed write: the bootloader ACK has no
        # sequence number. Re-running the entire update is safer.
        if not bus.command(can_id, block, timeout_ms=ack_timeout):
            raise FlashError(f"Missing data ACK at offset 0x{offset:04X} "
                             f"({offset:,}/{len(firmware.app):,} bytes). "
                             "Power-cycle and re-run with --recover to retry the entire image.")
        if (offset // 8) % 128 == 127 or offset + 8 == len(firmware.app):
            pct = 100.0 * (offset + 8) / len(firmware.app)
            print(f"\rUploading: {pct:5.1f}%  ({offset + 8:,}/{len(firmware.app):,})", end="", flush=True)
    print(f"\nAll frames acknowledged in {time.monotonic() - t0:.1f} seconds.")
    print("Rebooting controller...")
    if not bus.command(WB_BL_REBOOT, timeout_ms=2000):
        raise FlashError("Image transferred, but no reboot ACK. "
                         "Power-cycle and check LEDs/heartbeat before assuming success.")
    print("Reboot ACK received. Verify normal WBO CAN broadcasts or LED behavior.")
    print("NOTE: the bootloader does not provide readback; only per-frame ACK + image CRC were checked.")


def probe(bus: OpenPort, duration: float, max_frames: int):
    print(f"Listening passively for {duration:g}s on {bus.bitrate:,} bit/s CAN...", flush=True)
    end = time.monotonic() + duration
    count = 0
    seen = set()
    while time.monotonic() < end and count < max_frames:
        frame = bus.recv(min(200, max(1, int(1000 * (end - time.monotonic())))))
        if frame is None:
            continue
        count += 1
        seen.add(frame.can_id)
        payload = frame.payload.hex(" ").upper()
        suffix = ""
        # 0x190 + (2 * controller index) is the normal WBO status frame
        if 0x190 <= frame.can_id <= 0x1AE and frame.can_id % 2 == 0 and len(frame.payload) == 8:
            ver, valid = frame.payload[:2]
            raw_lambda = int.from_bytes(frame.payload[2:4], "little")
            suffix = f"  [WBO status ver=0x{ver:02X} valid={valid} lambda={raw_lambda / 10000:.4f}]"
        elif frame.can_id == WB_ACK:
            suffix = "  [WBO bootloader ACK / ping reply]"
        print(f"  0x{frame.can_id:08X}  DLC={len(frame.payload)}  {payload}{suffix}")
    print(f"Observed {count} CAN frame(s), {len(seen)} unique ID(s).")
    if count == 0:
        print("No traffic seen. This does not prove the module is dead: a broken application "
              "or bootloader may be silent; try 'flash --recover' if bootloader is intact.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="rusEFI f0_module wideband CAN updater via Tactrix OpenPort 2.0")
    p.add_argument("--lib", help="Path to openport-j2534 libj2534.so")
    p.add_argument("--bitrate", type=int, default=500000,
                   help="CAN bitrate; default 500000, must match target")
    sub = p.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="Validate firmware size/CRC; no hardware required")
    check.add_argument("image", type=Path)
    pr = sub.add_parser("probe", help="Read-only CAN bus monitor; no firmware changes")
    pr.add_argument("--seconds", type=float, default=4.0)
    pr.add_argument("--max-frames", type=int, default=30)
    fl = sub.add_parser("flash", help="Flash app by CAN (ERASES existing app)")
    fl.add_argument("image", type=Path)
    fl.add_argument("--yes", action="store_true", help="Required confirmation for erase")
    fl.add_argument("--recover", action="store_true",
                    help="Repeated bootloader entry during power-cycle (bad app recovery)")
    fl.add_argument("--recovery-seconds", type=float, default=15.0)
    fl.add_argument("--hw-index", type=lambda v: int(v, 0),
                    help="Optional 2025+ application target index (normal entry only)")
    fl.add_argument("--ack-timeout", type=int, default=1200)
    args = p.parse_args(argv)
    try:
        if args.bitrate < 10000 or args.bitrate > 1000000:
            raise FlashError("CAN bitrate out of range")
        if args.cmd in ("check", "flash"):
            fw = load_firmware(args.image)
            print(f"Firmware: {args.image}\n  Input size: {fw.original_size:,} B  "
                  f"({'bootloader+app' if fw.is_combined else 'app only'})\n"
                  f"  To be sent: {len(fw.app):,} B (APP ONLY)\n"
                  f"  CRC32: 0x{fw.crc32:08X} PASS\n  SHA256: {fw.sha256}")
            if args.cmd == "check":
                return 0
        if args.cmd == "probe":
            if args.seconds <= 0 or args.max_frames < 1:
                raise FlashError("Invalid --seconds or --max-frames")
            with OpenPort(args.lib, args.bitrate, receive_all=True) as bus:
                print(f"OpenPort driver: {bus.loaded_from}")
                probe(bus, args.seconds, args.max_frames)
            return 0
        if args.cmd == "flash":
            if args.hw_index is not None and not (0 <= args.hw_index <= 254):
                raise FlashError("--hw-index must be 0..254")
            if args.recover and args.hw_index is not None:
                raise FlashError("--hw-index cannot safely select a controller in "
                                 "recovery mode; physically isolate ONE WBO and omit --hw-index")
            if args.ack_timeout < 100 or args.recovery_seconds <= 0:
                raise FlashError("Invalid ACK or recovery timeout")
            if not args.yes:
                raise FlashError("Refusing to erase without --yes. Make sure exactly ONE "
                                 "WBO is connected on an isolated bench CAN bus.")
            print("WARNING: This ERASES the WBO APPLICATION. Use only with f0_module "
                  "STM32F042 hardware, stable power, and exactly ONE target on the CAN bus.")
            with OpenPort(args.lib, args.bitrate, receive_all=False) as bus:
                print(f"OpenPort driver: {bus.loaded_from}; CAN {args.bitrate:,} bit/s")
                flash(bus, fw, args.hw_index, args.recover,
                      args.recovery_seconds, args.ack_timeout)
            return 0
        raise AssertionError("Unknown command")
    except (FlashError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted; if erase began, module may need a full recovery flash.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
