#!/usr/bin/env bash
# van-dashboard provisioning for a fresh Raspberry Pi OS Lite (Bookworm/Trixie)
# with the 7" ili9881c DSI touch panel. Run from the repo root as your normal
# user (it uses sudo where needed):
#
#     git clone <repo> ~/van-dashboard && cd ~/van-dashboard && ./setup.sh && sudo reboot
#
# Sets up: the Python env (uv), a labwc + Chromium kiosk on the DSI panel
# (rotated to landscape, touch-calibrated, no cursor), the Goodix touch rebind
# (works around the boot-time I2C probe race), and all three systemd services.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"
USER_HOME="$HOME"
USER_UID="$(id -u)"

if [ "$USER_UID" -eq 0 ]; then
  echo "Run as your normal user (it uses sudo internally), not as root." >&2
  exit 1
fi
echo "==> van-dashboard setup for '$USER_NAME' (uid $USER_UID) in $REPO"

# ── packages ─────────────────────────────────────────────────────────────────
echo "==> apt packages"
sudo apt-get update -qq
CHROME=chromium
apt-cache show chromium >/dev/null 2>&1 || CHROME=chromium-browser
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  "$CHROME" labwc wlr-randr seatd git rsync fonts-dejavu-core

# ── uv + python env ──────────────────────────────────────────────────────────
if ! [ -x "$USER_HOME/.local/bin/uv" ]; then
  echo "==> installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
echo "==> uv sync"
( cd "$REPO" && "$USER_HOME/.local/bin/uv" sync )

# ── labwc config + transparent cursor ────────────────────────────────────────
echo "==> labwc config + blank cursor theme"
mkdir -p "$USER_HOME/.config/labwc"
install -m644 "$REPO/deploy/labwc/rc.xml"      "$USER_HOME/.config/labwc/rc.xml"
install -m755 "$REPO/deploy/labwc/autostart"   "$USER_HOME/.config/labwc/autostart"
install -m644 "$REPO/deploy/labwc/environment" "$USER_HOME/.config/labwc/environment"
python3 "$REPO/deploy/make-blank-cursor.py"

# ── Goodix touch: rebind script + calibration udev rule ──────────────────────
echo "==> touch rebind + calibration"
sudo install -m755 "$REPO/deploy/van-touch-rebind.sh"    /usr/local/bin/van-touch-rebind.sh
sudo install -m644 "$REPO/deploy/99-vandash-touch.rules" /etc/udev/rules.d/99-vandash-touch.rules
sudo udevadm control --reload-rules

# ── system hardening ─────────────────────────────────────────────────────────
# BLE-only controller: the Afterburner's dual-mode HC-05 otherwise gets a
# doomed BR/EDR connect (br-connection-profile-unavailable).
echo "==> bluetooth LE-only mode"
sudo sed -i 's/^#\?ControllerMode = .*/ControllerMode = le/' /etc/bluetooth/main.conf
grep -q '^ControllerMode = le' /etc/bluetooth/main.conf || \
  echo 'ControllerMode = le' | sudo tee -a /etc/bluetooth/main.conf >/dev/null

# Hardware watchdog + persistent logs: the Pi hard-froze unattended on
# 2026-08-13 and stayed down 6 days; the watchdog force-reboots on hang and
# the journal keeps the evidence (size-capped for SD wear).
echo "==> hardware watchdog + persistent journald"
sudo mkdir -p /etc/systemd/system.conf.d /etc/systemd/journald.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' | \
  sudo tee /etc/systemd/system.conf.d/watchdog.conf >/dev/null
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\n' | \
  sudo tee /etc/systemd/journald.conf.d/persistent.conf >/dev/null

# ── systemd units (patched to this user / uid) ───────────────────────────────
echo "==> systemd units"
for unit in van-dashboard.service kiosk.service van-touch.service van-pihealth.service \
            van-dashboard-update.service van-dashboard-update.timer; do
  sed -e "s#/home/pi#$USER_HOME#g" \
      -e "s#/run/user/1000#/run/user/$USER_UID#g" \
      -e "s/\bpi\b/$USER_NAME/g" \
      "$REPO/deploy/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sed -e "s#/home/pi#$USER_HOME#g" \
    -e "s/\bpi\b/$USER_NAME/g" \
    "$REPO/deploy/van-dashboard-update" | \
  sudo tee /usr/local/sbin/van-dashboard-update >/dev/null
sudo chmod 755 /usr/local/sbin/van-dashboard-update

# A read-only GitLab deploy key is used by the unattended updater. The public
# half is safe to paste into GitLab: Settings -> Repository -> Deploy keys.
if ! [ -f "$USER_HOME/.ssh/vandash_deploy" ]; then
  echo "==> generating read-only updater deploy key"
  install -d -m700 "$USER_HOME/.ssh"
  ssh-keygen -q -t ed25519 -N '' -C "vandash auto-update" \
    -f "$USER_HOME/.ssh/vandash_deploy"
fi
echo "==> GitLab deploy key (grant read-only access):"
cat "$USER_HOME/.ssh/vandash_deploy.pub"

# ── free tty1 for the kiosk, enable everything ───────────────────────────────
echo "==> mask console getty on tty1; enable services"
sudo systemctl mask getty@tty1.service autovt@tty1.service
sudo systemctl daemon-reload
sudo systemctl enable --now seatd
sudo systemctl enable van-dashboard van-touch kiosk van-pihealth \
  van-dashboard-update.timer

cat <<EOF

==> Done. Reboot to bring up the kiosk:   sudo reboot
    Dashboard also on the LAN:            http://$(hostname).local:8080
    Bench/demo data (synthetic):          sudo mkdir -p /etc/systemd/system/van-dashboard.service.d
      printf '[Service]\\nEnvironment=VANDASH_DEMO=1\\n' | sudo tee \\
        /etc/systemd/system/van-dashboard.service.d/demo.conf
      sudo systemctl daemon-reload && sudo systemctl restart van-dashboard
EOF
