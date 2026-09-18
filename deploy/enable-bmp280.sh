#!/usr/bin/env bash
# Put the BMP280 into the running agent: I2C access, new code, config and unit.
#
# Safe to run again. Expects overlays=i2c0 already in /boot/armbianEnv.txt and a
# reboot behind it, so that /dev/i2c-0 exists. The sensor is read once as the
# agent's own user before the service is touched; if that fails, nothing past the
# code copy changes.
set -euo pipefail
test "$(id -u)" = 0 || { echo 'Run as root: sudo bash deploy/enable-bmp280.sh'; exit 1; }
src=$(cd -- "$(dirname -- "$0")/.." && pwd)
conf=/etc/monitoring/agent.toml

test -e /dev/i2c-0 || { echo '/dev/i2c-0 is missing: add overlays=i2c0 to /boot/armbianEnv.txt and reboot'; exit 1; }

echo "=== 1. I2C access for the monitoring user, bus 0 only ==="
getent group sensors >/dev/null || groupadd --system sensors
install -m 0644 "$src/deploy/99-monitoring-i2c.rules" /etc/udev/rules.d/
udevadm control --reload
udevadm trigger --action=change --subsystem-match=i2c-dev
udevadm settle
stat -c '%U:%G %a %n' /dev/i2c-*

echo
echo "=== 2. Agent code ==="
install -m 0644 "$src"/monitoring/*.py /opt/monitoring/monitoring/
ls -l /opt/monitoring/monitoring/bmp280.py

echo
echo "=== 3. Reading as the agent would, before changing the service ==="
systemd-run --wait --pipe --quiet \
    -p User=monitoring -p Group=monitoring -p SupplementaryGroups="video gpio sensors" \
    -p WorkingDirectory=/opt/monitoring \
    /usr/bin/python3 -m monitoring.bmp280 --count 3

echo
echo "=== 4. Configuration ==="
if ! grep -q '^type *= *"bmp280"' "$conf"; then
    cp -a "$conf" "$conf.bak-bmp280"
    cat >> "$conf" <<'EOF'

[[sources]]
name = "barometer"
type = "bmp280"
bus = 0
address = 0x76
EOF
    echo "added the barometer source; previous file kept as $conf.bak-bmp280"
fi
python3 -c 'import sys, tomllib
c = tomllib.load(open(sys.argv[1], "rb"))
print("sources:", [(s["name"], s.get("type", "sysfs")) for s in c["sources"]])' "$conf"

echo
echo "=== 5. Service ==="
install -m 0644 "$src/deploy/monitoring-agent.service" /etc/systemd/system/
systemctl daemon-reload
systemctl restart monitoring-agent
sleep 15
systemctl --no-pager show monitoring-agent -p ActiveState -p SubState -p NRestarts -p SupplementaryGroups
journalctl --no-pager -u monitoring-agent -n 10 --since '-1min'
echo
echo "The agent reads sources as soon as it starts; source=barometer should reach the cloud within a minute."
