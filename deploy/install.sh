#!/usr/bin/env bash
set -euo pipefail
umask 077
role=${1:?Usage: sudo bash deploy/install.sh agent|server}
case "$role" in agent|server) ;; *) exit 2 ;; esac
test "$(id -u)" = 0 || { echo 'Run as root'; exit 1; }
project_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
if ! id monitoring >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/monitoring --shell /usr/sbin/nologin monitoring
fi
install -d -m 0755 /opt/monitoring /opt/monitoring/monitoring /opt/monitoring/deploy
install -d -m 0750 -o root -g monitoring /etc/monitoring
install -d -m 0700 -o monitoring -g monitoring "/var/lib/monitoring-$role"
if [ "$project_dir" != /opt/monitoring ]; then
    install -m 0644 "$project_dir"/monitoring/*.py /opt/monitoring/monitoring/
    find "$project_dir/deploy" -maxdepth 1 -type f -exec install -m 0644 '{}' /opt/monitoring/deploy/ \;
fi
apt-get update
if [ "$role" = agent ]; then
    apt-get install -y --no-install-recommends python3 ffmpeg v4l-utils
    usermod -a -G video,audio monitoring
    getent group gpio >/dev/null || groupadd --system gpio
    getent group sensors >/dev/null || groupadd --system sensors
    install -m 0644 "$project_dir/deploy/99-monitoring-gpio.rules" \
        "$project_dir/deploy/99-monitoring-i2c.rules" /etc/udev/rules.d/
    udevadm control --reload
    udevadm trigger --action=change --subsystem-match=gpio
    udevadm trigger --action=change --subsystem-match=i2c-dev
    if [ ! -e /etc/monitoring/agent.toml ]; then
        install -m 0640 -g monitoring "$project_dir/deploy/agent.toml.example" /etc/monitoring/agent.toml
    fi
else
    apt-get install -y --no-install-recommends python3 python3-venv caddy
    python3 -m venv /opt/monitoring/.venv
    /opt/monitoring/.venv/bin/pip install -r "$project_dir/requirements-server.txt"
    install -m 0644 "$project_dir/deploy/monitoring-cleanup.service" "$project_dir/deploy/monitoring-cleanup.timer" /etc/systemd/system/
    install -m 0644 "$project_dir/deploy/monitoring-backup.service" "$project_dir/deploy/monitoring-backup.timer" /etc/systemd/system/
fi
install -m 0644 "$project_dir/deploy/monitoring-$role.service" /etc/systemd/system/
systemctl daemon-reload
echo "Installed $role. Configure credentials and endpoint before enabling the service (see README)."
