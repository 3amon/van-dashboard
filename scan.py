"""Scan for BLE devices and highlight van power gear (Victron etc.)."""

import asyncio

from bleak import BleakScanner

VICTRON_MFG_ID = 0x02E1

MFG_NAMES = {
    0x02E1: "Victron Energy",
    0x004C: "Apple",
    0x050E: "Afterburner",
}


async def main() -> None:
    print("Scanning for 12s...")
    results = await BleakScanner.discover(timeout=12.0, return_adv=True)

    rows = sorted(results.values(), key=lambda r: -(r[1].rssi or -999))
    for device, adv in rows:
        mfg_ids = list(adv.manufacturer_data)
        is_victron = VICTRON_MFG_ID in mfg_ids
        marker = " <-- VICTRON" if is_victron else ""
        name = adv.local_name or device.name or ""
        mfg = ", ".join(MFG_NAMES.get(m, f"0x{m:04X}") for m in mfg_ids)
        print(f"{device.address}  RSSI={adv.rssi:>4}  {name!r:30} [{mfg}]{marker}")
        if is_victron:
            print(f"    raw: {adv.manufacturer_data[VICTRON_MFG_ID].hex()}")


if __name__ == "__main__":
    asyncio.run(main())
