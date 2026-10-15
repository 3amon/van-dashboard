"""Verify the configured encryption keys decode live Victron advertisements."""

import asyncio
import tomllib
from pathlib import Path

from bleak import BleakScanner
from victron_ble.devices import detect_device_type
from victron_ble.exceptions import AdvertisementKeyMismatchError

with open(Path(__file__).parent / "config.toml", "rb") as f:
    _config = tomllib.load(f)
KEYS = {d["name"]: d["key"] for d in _config["devices"]}

decoded: dict[str, object] = {}


def on_advert(device, adv):
    data = adv.manufacturer_data.get(0x02E1)
    if not data or not data.startswith(b"\x10"):
        return
    device_type = detect_device_type(data)
    if device_type is None:
        print(f"unknown Victron model: {data.hex()}")
        return
    for label, key in KEYS.items():
        try:
            parsed = device_type(key).parse(data)
        except AdvertisementKeyMismatchError:
            continue
        decoded[label] = parsed
        return


async def main() -> None:
    scanner = BleakScanner(detection_callback=on_advert)
    await scanner.start()
    await asyncio.sleep(15)
    await scanner.stop()

    for label, parsed in decoded.items():
        print(f"\n=== {label}: {parsed.get_model_name()} ===")
        for name in dir(parsed):
            if name.startswith("get_") and name != "get_model_name":
                try:
                    value = getattr(parsed, name)()
                except Exception:
                    continue
                if value is not None:
                    print(f"  {name[4:]}: {value}")

    missing = KEYS.keys() - decoded.keys()
    if missing:
        print(f"\nNOT decoded (no matching advert seen): {sorted(missing)}")


if __name__ == "__main__":
    asyncio.run(main())
