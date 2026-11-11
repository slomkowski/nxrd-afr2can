# afr-nxrd-logger

Log and display the lambda / AFR of a rusEFI (mck1117 `f0_module`, STM32F042)
wideband controller over CAN, using a Tactrix OpenPort 2.0 cable on Linux.

| File | What it is |
|---|---|
| `afr_logger.c` | CLI logger. Prints one CSV line per wideband status frame to stdout. |
| `afr_gui.py` | Live GUI (big AFR / lambda readouts, scrolling chart). Runs `afr_logger` as a child process. |
| `wideband_flash.py` | CAN bootloader client: validate an image, passively monitor the bus, flash the module. |
| `wideband_probe.sh` | Read-only CAN monitor wrapper around `wideband_flash.py probe`. |
| `wideband_bootloader.sh` | Retries bootloader entry until the module ACKs (start it, then power the module on). Erases nothing. |

## Requirements

- Linux, a Tactrix OpenPort 2.0 (USB `0403:cc4d`).
- [openport-j2534](https://github.com/slomkowski/openport-j2534), built (`make`
  there). The `Makefile` expects it in `~/projects/openport-j2534`; override with
  `OPENPORT_DIR=/path`.
- A C11 compiler and `make` for `afr_logger`.
- Python 3.10+. The GUI also needs `PySide6`, `pyqtgraph` and `numpy`.
  The flash tools use only the standard library.

Cable setup, once (from the openport-j2534 README):

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="cc4d", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/70-openport.rules
```

Re-plug the cable afterwards and take the microSD card out of it (with a card
inserted the cable shows up as a USB disk). Only one program can open the cable
at a time: stop the GUI before running the logger or the flash tools.

## Logger

```bash
make
./afr_logger                      # until Ctrl-C
./afr_logger -n 30 -s 9.76 > e85.csv
```

Options: `-b` CAN bit rate (default 500000), `-s` stoichiometric AFR (default
14.7), `-n` stop after N seconds.

It listens passively for the standard wideband frames (11-bit ids, little-endian):

- `0x190 + 2N` status: version, valid flag, lambda × 10000, temperature in °C
- `0x191 + 2N` diagnostics: ESR (Ω), Nernst (mV), pump %, status byte, heater %

`N` is the controller index. CSV columns: `time_s, controller, valid, lambda,
afr, temp_c, esr_ohm, nernst_v, pump_pct, heater_pct, status`. `afr` is
`lambda × stoich`.

## GUI

```bash
./afr_gui.py                      # start immediately
./afr_gui.py --demo               # synthetic data, no cable needed
./afr_gui.py --stoich 9.76 --window 60 --no-log
```

Shows AFR and lambda, a rich / stoich / lean indication, sensor temperature and
the diagnostics. The chart has AFR on the left axis and lambda on the right.
Fuel (stoichiometric ratio) and chart window can be changed live; changing the
fuel re-scales the whole history, because AFR is computed in the GUI from the
controller's lambda. A CSV log (`afr_log_<timestamp>.csv`, ignored by git) is
written by default; turn it off with `--no-log`.

Some readings you may meet:

- **NOT VALID**: the controller reports the sensor value as invalid, usually
  while the sensor is heating up.
- **NO DATA**: no frames for the selected controller for more than 1.5 s.
- **Lambda 2.0000 / AFR 29.4 in free air**: the sensor reads maximum lean and
  the value is clamped. It does not change when you blow on the probe. Judge the
  sensor by the other values instead (about 780 °C, ESR about 300 Ω, Nernst about
  0.45 V) and by exhaust gas from a running engine.

If both PyQt6 and PySide6 are installed: `afr_gui.py` pins pyqtgraph to PySide6
before importing it. Mixing the two bindings in one process segfaults.

## Wideband firmware tools

`wideband_flash.py` is an independently written client for the public rusEFI
WBO CAN bootloader. It finds `libj2534.so` via `--lib`, `$J2534_LIB` or the usual
system paths. The shell wrappers default to `$OPENPORT_DIR/libj2534.so`.

```bash
# Validate size and CRC32 of an image. No hardware needed.
./wideband_flash.py check wideband_image.bin

# Passively list CAN frames. Sends nothing.
./wideband_probe.sh --seconds 10

# Request bootloader entry repeatedly; start it, then power the module on.
./wideband_bootloader.sh

# Flash. ERASES the existing application.
export J2534_LIB=$HOME/projects/openport-j2534/libj2534.so
./wideband_flash.py flash wideband_image.bin --yes
./wideband_flash.py flash wideband_image.bin --yes --recover   # power-cycle during the 15 s window
```

Accepted images: the application alone (25 KiB) or bootloader + application
(31 KiB), with a valid CRC32 in the last four bytes of the application. Build it
with `boards/f0_module/build_wideband.sh` in the wideband firmware repository.
Bare `.elf` / `.hex` files are rejected.

**Safety.** `flash` erases the application and the bootloader gives no readback,
so only per-frame ACKs and the image CRC are checked. Use exactly one module on
an isolated bench CAN bus with stable power. If a transfer fails, re-run the
whole update (with `--recover`); individual frames are never retried because the
protocol has no sequence numbers. Do not interrupt it after the erase has
started. Test on a bench module first: the flash path has not been verified
against real hardware in this repository.

CAN needs 120 Ω termination at both ends. A silent bus or `ERR_TIMEOUT` on
transmit usually means no powered node: check power, CAN-H / CAN-L, termination
and bit rate (default 500 kbit/s).
