#!/bin/sh
# Rebind the Goodix DSI touch controller after boot.
#
# On this 7" ili9881c panel the Goodix-TS driver probes at ~2.9s — before the
# DSI panel has powered its I2C rail — and fails with "I2C communication
# failure: -121", then gives up. Once the panel is fully up, a rebind succeeds
# (the controller reports ID 911). This runs late in boot and retries until the
# touch input device appears.
DEV=11-005d
DRV=/sys/bus/i2c/drivers/Goodix-TS

# Already bound (probe happened to succeed)? Nothing to do.
[ -e "$DRV/$DEV" ] && exit 0

for i in $(seq 1 15); do
    echo "$DEV" > "$DRV/unbind" 2>/dev/null
    echo "$DEV" > "$DRV/bind"   2>/dev/null
    if [ -e "$DRV/$DEV" ]; then
        echo "van-touch: Goodix rebound on attempt $i"
        exit 0
    fi
    sleep 2
done
echo "van-touch: Goodix rebind failed after 15 attempts" >&2
exit 0
