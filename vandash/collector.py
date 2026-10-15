"""Passive BLE collector for Victron instant-readout advertisements.

Devices are identified by encryption key (via the key-check byte) plus expected
device kind — never by Bluetooth address, which differs between macOS (UUIDs)
and Linux/Raspberry Pi (MACs).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from enum import Enum
from typing import Any

from victron_ble.devices import (
    AcCharger,
    BatteryMonitor,
    DcDcConverter,
    SolarCharger,
    detect_device_type,
)
from victron_ble.devices.base import BitReader, ChargerError, OperationMode
from victron_ble.exceptions import AdvertisementKeyMismatchError

from vandash.ble import BleScanner

logger = logging.getLogger(__name__)

VICTRON_MFG_ID = 0x02E1

# VE.Direct "off reason" is a bitfield; victron-ble 0.9.x models it as a plain
# enum and crashes on combined flags (e.g. 0x81 = no input power + engine
# shutdown), so we decode it ourselves.
OFF_REASON_BITS = {
    0x00000001: "No input power",
    0x00000002: "Power switch off",
    0x00000004: "Soft power switch off",
    0x00000008: "Remote input",
    0x00000010: "Protection active",
    0x00000020: "Paygo",
    0x00000040: "BMS shutdown",
    0x00000080: "Engine shutdown",
    0x00000100: "Analysing input voltage",
}


def off_reason_labels(value: int) -> list[str]:
    labels = [label for bit, label in OFF_REASON_BITS.items() if value & bit]
    unknown = value & ~sum(OFF_REASON_BITS)
    if unknown:
        labels.append(f"Unknown (0x{unknown:X})")
    return labels


def _safe_enum(enum_cls: type[Enum], value: int) -> Enum | int:
    try:
        return enum_cls(value)
    except ValueError:
        return value


class TolerantDcDcConverter(DcDcConverter):
    """DcDcConverter that survives bitfield off_reason and unknown enum values."""

    def parse_decrypted(self, decrypted: bytes) -> dict:
        reader = BitReader(decrypted)
        device_state = reader.read_unsigned_int(8)
        charger_error = reader.read_unsigned_int(8)
        input_voltage = reader.read_unsigned_int(16)
        output_voltage = reader.read_signed_int(16)
        off_reason = reader.read_unsigned_int(32)
        return {
            "device_state": (
                _safe_enum(OperationMode, device_state)
                if device_state != 0xFF
                else None
            ),
            "charger_error": (
                _safe_enum(ChargerError, charger_error)
                if charger_error != 0xFF
                else None
            ),
            "input_voltage": input_voltage / 100 if input_voltage != 0xFFFF else None,
            "output_voltage": (
                output_voltage / 100 if output_voltage != 0x7FFF else None
            ),
            "off_reason": off_reason,
        }


KIND_PARSERS = {
    "solar": SolarCharger,
    "dcdc": TolerantDcDcConverter,
    "shunt": BatteryMonitor,
    "ac": AcCharger,  # Blue Smart IP22 — only advertises while on shore power
}

# Victron's instant readout is AES-CTR with no authentication tag; the only
# integrity check is a one-byte key check, so a neighboring device whose check
# byte collides (1 in 256) decodes to random garbage. These bounds plus the
# RSSI floor reject such readings.
PLAUSIBLE_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "solar": {
        "battery_voltage": (15.0, 35.0),
        "battery_charging_current": (-5.0, 60.0),
        "solar_power": (0.0, 1200.0),
        "yield_today": (0.0, 10_000.0),
    },
    "dcdc": {
        "input_voltage": (0.0, 35.0),
        "output_voltage": (0.0, 35.0),
    },
    "shunt": {
        "voltage": (15.0, 35.0),
        "current": (-500.0, 500.0),
    },
    "ac": {
        "output_voltage1": (0.0, 35.0),
        "output_current1": (0.0, 25.0),
        "ac_current": (0.0, 16.0),
    },
}


def plausible(kind: str, fields: dict[str, Any]) -> bool:
    for field_name, (low, high) in PLAUSIBLE_RANGES.get(kind, {}).items():
        value = fields.get(field_name)
        if value is not None and not low <= value <= high:
            return False
    return True


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.name.replace("_", " ").title()
    return value


class ConfiguredDevice:
    def __init__(self, name: str, kind: str, key: str) -> None:
        if kind not in KIND_PARSERS:
            raise ValueError(f"Unknown device kind {kind!r} for {name!r}")
        self.name = name
        self.kind = kind
        self.parser = KIND_PARSERS[kind](key)


# Decimal places per history field
_FIELD_ROUNDING = {
    "w": 1, "v": 2, "a": 1, "temp": 1, "rh": 0, "soc": 1, "solar_wh": 0,
    "alt_w": 1, "ac_w": 1,
}


class PowerState:
    """Latest reading per device plus bucketed history (solar, battery, cabin).

    History rows are dicts {t, w, v, a, temp, rh, soc} (missing fields None); each
    completed bucket is persisted to the optional HistoryStore, and recent rows
    are loaded back on startup so charts survive restarts.
    """

    def __init__(
        self,
        bucket_seconds: int = 30,
        retain_hours: float = 6.0,
        store: Any = None,
    ) -> None:
        self.bucket_seconds = bucket_seconds
        self.retain_seconds = retain_hours * 3600
        self.devices: dict[str, dict[str, Any]] = {}
        self._store = store
        self._rows: deque[dict[str, Any]] = deque()
        self._cur_ts: float | None = None
        self._cur_acc: dict[str, list[float]] = {}  # field -> [sum, count]
        if store is not None:
            self._rows.extend(store.load_since(time.time() - self.retain_seconds))

    def update(self, device: ConfiguredDevice, fields: dict[str, Any], rssi: int | None) -> None:
        self.devices[device.name] = {
            "kind": device.kind,
            "rssi": rssi,
            "ts": time.time(),
            **{k: _jsonable(v) for k, v in fields.items()},
        }
        if device.kind == "shunt":
            alt_w, ac_w = self._charge_source_watts(fields)
            self._note(v=fields.get("voltage"), a=fields.get("current"),
                       soc=fields.get("soc"), alt_w=alt_w, ac_w=ac_w)
        elif device.kind == "ac":
            # Direct measurement, so the shore series survives a shunt outage.
            v1 = fields.get("output_voltage1")
            a1 = fields.get("output_current1")
            if v1 is not None and a1 is not None:
                self._note(ac_w=v1 * a1)
        elif device.kind == "solar":
            # A reporting shunt owns battery v/a history: it measures net bank
            # current, where the MPPT only sees its own charge current.
            if self._shunt_reporting():
                self._note(w=fields.get("solar_power"), solar_wh=fields.get("yield_today"))
            else:
                self._note(
                    w=fields.get("solar_power"),
                    solar_wh=fields.get("yield_today"),
                    v=fields.get("battery_voltage"),
                    a=fields.get("battery_charging_current"),
                )

    def _fresh(self, kind: str) -> dict[str, Any] | None:
        for d in self.devices.values():
            if d["kind"] == kind and time.time() - d["ts"] < 240:
                return d
        return None

    def _charge_source_watts(
        self, shunt_fields: dict[str, Any]
    ) -> tuple[float | None, float | None]:
        """Charge power from the non-solar sources: (alternator W, shore W).

        Shore is measured — the IP22 advertises its real output volts/amps,
        and unplugged it stops advertising entirely, so a silent charger
        honestly means 0 W. The Orion advert carries NO current field, so
        alternator watts are an estimate: net power into the bank beyond what
        solar and shore are providing, counted only while the Orion reports
        an active state. House loads make it an underestimate while driving;
        it still shows when and roughly how hard the alternator charged.
        None means unknown (advert gap), which charts render as a break.
        """
        ac = self._fresh("ac")
        if ac is None:
            ac_w: float | None = 0.0
        else:
            v1, a1 = ac.get("output_voltage1"), ac.get("output_current1")
            ac_w = v1 * a1 if v1 is not None and a1 is not None else None

        dcdc = self._fresh("dcdc")
        if dcdc is None:
            alt_w = None
        elif dcdc.get("device_state") in (None, "Off"):
            alt_w = 0.0
        else:
            v, a = shunt_fields.get("voltage"), shunt_fields.get("current")
            if v is None or a is None:
                alt_w = None
            else:
                solar = self._fresh("solar")
                solar_w = 0.0
                if solar is not None:
                    sv = solar.get("battery_voltage")
                    sa = solar.get("battery_charging_current")
                    solar_w = (sv * sa if sv is not None and sa is not None
                               else solar.get("solar_power") or 0.0)
                alt_w = max(0.0, v * a - solar_w - (ac_w or 0.0))
        return alt_w, ac_w

    def _shunt_reporting(self) -> bool:
        # Generous window: advert gaps of ~2 min are normal while the heater
        # holds a GATT session, and flapping here would mix net bank current
        # with MPPT charge-only current in the same history series.
        return any(
            d["kind"] == "shunt" and time.time() - d["ts"] < 240
            for d in self.devices.values()
        )

    def set_battery(self, name: str, fields: dict[str, Any], rssi: int | None = None) -> None:
        # GATT-polled BMS (Epoch): tile data only — the shunt owns v/a history.
        self.devices[name] = {
            "kind": "battery", "rssi": rssi, "ts": time.time(), **fields,
        }
        if not self._shunt_reporting():
            self._note(soc=fields.get("soc"))

    def set_heater(self, name: str, fields: dict[str, Any], source: str = "wifi") -> None:
        self.devices[name] = {
            "kind": "heater", "rssi": None, "ts": time.time(), "source": source, **fields,
        }
        self._note(temp=fields.get("cabin_temp"), rh=fields.get("humidity"))

    def ingest(self, devices: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> None:
        """Mirror mode: adopt the van's device table and merge pushed history rows."""
        self.devices = devices
        if not rows:
            return
        if self._store is not None:
            for row in rows:
                self._store.upsert(row)
        known = {r["t"]: r for r in self._rows}
        for row in rows:
            known[row["t"]] = {**{k: row.get(k) for k in ("t", *_FIELD_ROUNDING)}}
        cutoff = time.time() - self.retain_seconds
        self._rows = deque(sorted((r for r in known.values() if r["t"] >= cutoff), key=lambda r: r["t"]))

    def _note(self, **fields: float | None) -> None:
        now = time.time()
        bucket_ts = now - (now % self.bucket_seconds)
        if bucket_ts != self._cur_ts:
            self.flush()
            self._cur_ts = bucket_ts
        for field, value in fields.items():
            if value is None:
                continue
            acc = self._cur_acc.setdefault(field, [0.0, 0])
            acc[0] += value
            acc[1] += 1

    def _finalize(self, bucket_ts: float, acc: dict[str, list[float]]) -> dict[str, Any]:
        row: dict[str, Any] = {"t": bucket_ts}
        for field, digits in _FIELD_ROUNDING.items():
            if field in acc:
                total, count = acc[field]
                row[field] = round(total / count, digits)
            else:
                row[field] = None
        return row

    def flush(self) -> None:
        """Finalize the in-progress bucket: append to memory and persist."""
        if self._cur_ts is None or not self._cur_acc:
            self._cur_acc = {}
            return
        row = self._finalize(self._cur_ts, self._cur_acc)
        self._rows.append(row)
        self._cur_acc = {}
        if self._store is not None:
            self._store.upsert(row)
        cutoff = time.time() - self.retain_seconds
        while self._rows and self._rows[0]["t"] < cutoff:
            self._rows.popleft()

    def snapshot(self) -> dict[str, Any]:
        history = list(self._rows)
        if self._cur_ts is not None and self._cur_acc:
            history.append(self._finalize(self._cur_ts, self._cur_acc))
        return {
            "now": time.time(),
            "bucket_seconds": self.bucket_seconds,
            "devices": self.devices,
            "history": history,
        }


class Collector:
    """Decodes Victron instant-readout adverts off the shared BLE scanner."""

    def __init__(
        self,
        devices: list[ConfiguredDevice],
        state: PowerState,
        scanner: BleScanner,
        min_rssi: int = -85,
    ) -> None:
        self._devices = devices
        self._state = state
        self._scanner = scanner
        self._min_rssi = min_rssi

    def _on_advert(self, ble_device: Any, advert: Any) -> None:
        data = advert.manufacturer_data.get(VICTRON_MFG_ID)
        if not data or not data.startswith(b"\x10"):
            return
        if advert.rssi is not None and advert.rssi < self._min_rssi:
            return
        device_type = detect_device_type(data)
        if device_type is None:
            return
        for device in self._devices:
            # Kind must match so a neighbor's device of another type whose
            # key-check byte happens to collide never pollutes our readings.
            if not isinstance(device.parser, device_type):
                continue
            try:
                parsed = device.parser.parse(data)
            except AdvertisementKeyMismatchError:
                continue
            except Exception:
                logger.exception("Failed to parse advert for %s: %s", device.name, data.hex())
                continue
            fields = dict(parsed._data)  # getters mirror this dict 1:1
            if not plausible(device.kind, fields):
                logger.warning(
                    "Rejected implausible reading for %s (key-check collision?): %s",
                    device.name,
                    fields,
                )
                continue
            if device.kind == "dcdc":
                fields["off_reason"] = off_reason_labels(fields.get("off_reason", 0))
            self._state.update(device, fields, advert.rssi)
            return

    async def start(self) -> None:
        self._scanner.register(self._on_advert)
        logger.info(
            "Collector scanning for: %s",
            ", ".join(f"{d.name} ({d.kind})" for d in self._devices),
        )

    async def stop(self) -> None:
        # The shared scanner is owned and stopped by BleScanner — nothing to do.
        pass
