#!/usr/bin/env bash
# Put the DHT11 into the running agent: GPIO access, new code, config and unit.
#
# Safe to run again. The sensor is read once as the agent's own user before the
# service is touched; if that fails, nothing past the code copy changes.
set -euo pipefail
test "$(id -u)" = 0 || { echo 'Run as root: sudo bash deploy/enable-dht11.sh'; exit 1; }
src=$(cd -- "$(dirname -- "$0")/.." && pwd)
conf=/etc/monitoring/agent.toml

echo "=== 1. GPIO access for the monitoring user ==="
getent group gpio >/dev/null || groupadd --system gpio
install -m 0644 "$src/deploy/99-monitoring-gpio.rules" /etc/udev/rules.d/
udevadm control --reload
udevadm trigger --action=change --subsystem-match=gpio
udevadm settle
stat -c '%U:%G %a %n' /dev/gpiochip0 /dev/gpiochip1

echo
echo "=== 2. Agent code ==="
install -m 0644 "$src"/monitoring/*.py /opt/monitoring/monitoring/
ls -l /opt/monitoring/monitoring/dht11.py

echo
echo "=== 3. Reading as the agent would, before changing the service ==="
systemd-run --wait --pipe --quiet \
    -p User=monitoring -p Group=monitoring -p SupplementaryGroups="video gpio" \
    -p WorkingDirectory=/opt/monitoring \
    /usr/bin/python3 -m monitoring.dht11 --raw --count 3

echo
echo "=== 4. Configuration ==="
if ! grep -q '^type *= *"dht11"' "$conf"; then
    cp -a "$conf" "$conf.bak-dht11"
    # Without any [[sources]] the agent reads the CPU by default; listing the
    # sensor alone would silently drop that, so add the CPU explicitly first.
    if ! grep -q '^\[\[sources\]\]' "$conf"; then
        cat >> "$conf" <<'EOF'

[[sources]]
name = "cpu"
[sources.fields.cpu_temperature_c]
path = "/sys/class/thermal/thermal_zone0/temp"
scale = 0.001
EOF
    fi
    cat >> "$conf" <<'EOF'

[[sources]]
name = "room"
type = "dht11"
chip = "/dev/gpiochip0"
line = 119
attempts = 5
EOF
    echo "added the room source; previous file kept as $conf.bak-dht11"
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
echo "The agent reads sources as soon as it starts; source=room should reach the cloud within a minute."
