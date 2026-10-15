"""Standalone Raspberry Pi 5 power-health monitor.

Watches the Pi's OWN power (not the van's Victron gear): input-rail voltage,
board power draw, temperature, and — most importantly — the sticky
`get_throttled` bitmask that records undervoltage / brownout / throttle events.

Deliberately stdlib-only and runnable with the system interpreter
(`/usr/bin/python3 -m vandash.pihealth`) so it keeps logging even when the
dashboard's venv is broken, being rebuilt, or the kiosk has crashed — which is
exactly when you most want to know what the power did.

Usage:
    python3 -m vandash.pihealth            # run the logger loop (systemd)
    python3 -m vandash.pihealth status     # one-shot human-readable snapshot

Env:
    VANDASH_PIHEALTH_DIR       data dir (default ~/vandash-pihealth)
    VANDASH_PIHEALTH_INTERVAL  poll seconds (default 15)
    VANDASH_PIHEALTH_RETAIN    days of daily CSVs to keep (default 14)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- config ----------------------------------------------------------------

DATA_DIR = Path(
    os.environ.get("VANDASH_PIHEALTH_DIR", str(Path.home() / "vandash-pihealth"))
)
INTERVAL = float(os.environ.get("VANDASH_PIHEALTH_INTERVAL", "15"))
RETAIN_DAYS = int(os.environ.get("VANDASH_PIHEALTH_RETAIN", "14"))

# Input-voltage thresholds (V) with hysteresis, so a supply that hovers near the
# edge doesn't spam one event per poll. The Pi 5 PMIC flags undervoltage in
# firmware below ~4.63V; we warn earlier to catch a sagging supply/cable first.
VOLT_WARN_ENTER = 4.80
VOLT_WARN_EXIT = 4.90

# vcgencmd get_throttled bit meanings. Low bits = happening now; high bits are
# sticky "has occurred since boot" latches — the whole reason this is useful.
THROTTLED_BITS = {
    0: "under-voltage NOW",
    1: "arm frequency capped NOW",
    2: "currently throttled",
    3: "soft temperature limit active NOW",
    16: "under-voltage HAS OCCURRED",
    17: "arm frequency capping HAS OCCURRED",
    18: "throttling HAS OCCURRED",
    19: "soft temperature limit HAS OCCURRED",
}

CSV_HEADER = (
    "epoch,iso,throttled_hex,uv_now,throttled_now,freq_capped_now,"
    "temp_soft_now,uv_occurred,throttle_occurred,temp_c,ext5v_v,board_w,load1\n"
)

# --- vcgencmd helpers ------------------------------------------------------


def _vcgencmd(*args: str) -> str:
    try:
        out = subprocess.run(
            ["vcgencmd", *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout
    except Exception as exc:  # noqa: BLE001 - never let a poll kill the loop
        return f"__error__ {exc}"


def read_throttled() -> int | None:
    text = _vcgencmd("get_throttled")
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", text)
    return int(m.group(1), 16) if m else None


def throttled_labels(value: int, mask: int | None = None) -> list[str]:
    """Human labels for the set bits (optionally restricted to `mask`)."""
    return [
        label
        for bit, label in THROTTLED_BITS.items()
        if value & (1 << bit) and (mask is None or mask & (1 << bit))
    ]


_ADC_RE = re.compile(r"(\S+?)_(A|V)\s+(?:current|volt)\(\d+\)=([\d.]+)")


def read_pmic() -> tuple[float | None, float | None]:
    """Return (ext5v_volts, summed_board_watts) from the Pi 5 PMIC ADC.

    board_w is the sum of measured downstream regulator rails (current*volt).
    It is a good *relative* draw signal but underestimates true 5V input power
    (regulator losses + the input rail has no current channel to measure).
    """
    text = _vcgencmd("pmic_read_adc")
    if text.startswith("__error__"):
        return None, None
    currents: dict[str, float] = {}
    volts: dict[str, float] = {}
    for name, kind, val in _ADC_RE.findall(text):
        (currents if kind == "A" else volts)[name] = float(val)
    ext5v = volts.get("EXT5V")
    board_w = sum(
        amps * volts[name] for name, amps in currents.items() if name in volts
    )
    return ext5v, (round(board_w, 3) if currents else None)


def read_temp() -> float | None:
    m = re.search(r"temp=([\d.]+)", _vcgencmd("measure_temp"))
    return float(m.group(1)) if m else None


def read_load1() -> float | None:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except Exception:  # noqa: BLE001
        return None


def read_wifi() -> tuple[str | None, float | None]:
    """Return the connected Wi-Fi name and signal in dBm, if available."""
    interface = None
    signal_dbm = None
    try:
        for line in Path("/proc/net/wireless").read_text().splitlines()[2:]:
            fields = line.replace(":", " ").split()
            if len(fields) >= 5:
                interface = fields[0]
                signal_dbm = float(fields[3].rstrip("."))
                break
    except (OSError, ValueError):
        pass
    if not interface:
        return None, None
    try:
        result = subprocess.run(
            ["iwgetid", interface, "--raw"], capture_output=True, text=True, timeout=3,
        )
        ssid = result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        ssid = None
    return ssid, signal_dbm


# --- logging ---------------------------------------------------------------


def _log(msg: str) -> None:
    """Print a timestamped line to stdout → captured by journald."""
    print(f"{datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


def _event(msg: str) -> None:
    """Record a discrete power event: journald (stderr) + persistent events.log."""
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (DATA_DIR / "events.log").open("a") as f:
            f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"event-log write failed: {exc}", file=sys.stderr, flush=True)


def _csv_path(now: float) -> Path:
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d")
    return DATA_DIR / f"pihealth-{day}.csv"


def _prune_old_csvs() -> None:
    cutoff = time.time() - RETAIN_DAYS * 86400
    for p in DATA_DIR.glob("pihealth-*.csv"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


# --- main loop -------------------------------------------------------------


def sample() -> dict:
    now = time.time()
    throttled = read_throttled()
    ext5v, board_w = read_pmic()
    wifi_ssid, wifi_dbm = read_wifi()
    return {
        "epoch": now,
        "throttled": throttled,
        "ext5v": ext5v,
        "board_w": board_w,
        "temp": read_temp(),
        "load1": read_load1(),
        "wifi_ssid": wifi_ssid,
        "wifi_dbm": wifi_dbm,
    }


def _bit(throttled: int | None, bit: int) -> str:
    if throttled is None:
        return ""
    return "1" if throttled & (1 << bit) else "0"


def run() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _log(
        f"pihealth monitor starting: interval={INTERVAL}s dir={DATA_DIR} "
        f"retain={RETAIN_DAYS}d volt_warn<{VOLT_WARN_ENTER}V"
    )

    # Report the sticky state at startup so a brownout that happened while the
    # monitor was DOWN (e.g. the crash that took the SD card) still gets flagged
    # the moment we come back up.
    first = read_throttled()
    if first:
        _event(
            f"THROTTLED flags already set at startup: 0x{first:x} -> "
            + "; ".join(throttled_labels(first))
        )
    else:
        _log("clean start: get_throttled=0x0 (no events latched)")

    last_throttled = first
    low_volt = False
    last_day = None

    while True:
        s = sample()
        now = s["epoch"]
        thr = s["throttled"]

        # --- throttle-bit transitions -> events ---
        if thr is not None and thr != last_throttled:
            newly = thr & ~(last_throttled or 0)
            if newly:
                _event(
                    f"THROTTLE change 0x{last_throttled or 0:x}->0x{thr:x} NEW: "
                    + "; ".join(throttled_labels(thr, mask=newly))
                )
            else:
                _log(f"throttle flags cleared: 0x{last_throttled or 0:x}->0x{thr:x}")
            last_throttled = thr

        # --- input-voltage sag with hysteresis -> events ---
        v = s["ext5v"]
        if v is not None:
            if not low_volt and v < VOLT_WARN_ENTER:
                low_volt = True
                _event(f"INPUT VOLTAGE LOW: EXT5V={v:.3f}V (< {VOLT_WARN_ENTER}V)")
            elif low_volt and v > VOLT_WARN_EXIT:
                low_volt = False
                _event(f"input voltage recovered: EXT5V={v:.3f}V")

        # --- append time-series row (daily CSV) ---
        path = _csv_path(now)
        day = path.name
        try:
            new_file = not path.exists()
            with path.open("a") as f:
                if new_file:
                    f.write(CSV_HEADER)
                f.write(
                    f"{now:.0f},{datetime.fromtimestamp(now, timezone.utc).isoformat()},"
                    f"{'' if thr is None else f'0x{thr:x}'},"
                    f"{_bit(thr,0)},{_bit(thr,2)},{_bit(thr,1)},{_bit(thr,3)},"
                    f"{_bit(thr,16)},{_bit(thr,18)},"
                    f"{'' if s['temp'] is None else s['temp']},"
                    f"{'' if v is None else f'{v:.3f}'},"
                    f"{'' if s['board_w'] is None else s['board_w']},"
                    f"{'' if s['load1'] is None else s['load1']}\n"
                )
        except OSError as exc:
            print(f"csv write failed: {exc}", file=sys.stderr, flush=True)

        if day != last_day:  # once per new day: tidy old files
            _prune_old_csvs()
            last_day = day

        time.sleep(INTERVAL)


def status() -> None:
    """One-shot human-readable snapshot for quick CLI checks."""
    s = sample()
    thr = s["throttled"]
    print(f"time:      {datetime.now().isoformat(timespec='seconds')}")
    print(f"input 5V:  {s['ext5v']:.3f} V" if s["ext5v"] else "input 5V:  n/a")
    print(f"board:     {s['board_w']} W (measured rails)")
    print(f"temp:      {s['temp']} C")
    print(f"load1:     {s['load1']}")
    wifi = s.get("wifi_ssid") or "not connected"
    signal = "" if s.get("wifi_dbm") is None else f" ({s['wifi_dbm']:.0f} dBm)"
    print(f"wifi:      {wifi}{signal}")
    if thr is None:
        print("throttled: n/a")
    elif thr == 0:
        print("throttled: 0x0  (clean — no undervoltage/throttle since boot)")
    else:
        print(f"throttled: 0x{thr:x}")
        for label in throttled_labels(thr):
            print(f"           - {label}")
    events = DATA_DIR / "events.log"
    if events.exists():
        lines = events.read_text().splitlines()
        print(f"\nlast events ({events}):")
        for line in lines[-5:]:
            print(f"  {line}")


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "status":
        status()
        return 0
    try:
        run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
