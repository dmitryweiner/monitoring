#!/usr/bin/env bash
set -euo pipefail
umask 077
test "$(id -u)" = 0
backup_dir=/var/backups/monitoring
install -d -m 0700 "$backup_dir"
filename="$backup_dir/monitoring-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
systemctl stop monitoring-cleanup.timer
systemctl stop monitoring-cleanup.service
systemctl stop monitoring-server.service
trap 'systemctl start monitoring-server.service monitoring-cleanup.timer' EXIT
/opt/monitoring/.venv/bin/python -m monitoring.admin backup --api-stopped --output "$filename"
systemctl start monitoring-server.service monitoring-cleanup.timer
trap - EXIT
# Optional encrypted off-host copy: requires a pre-initialized restic repository
# and /etc/monitoring/backup.env, loaded by the systemd unit.
if [ -n "${RESTIC_REPOSITORY:-}" ]; then
    restic backup "$filename" /etc/monitoring /etc/caddy/Caddyfile /etc/ssh/sshd_config.d/60-monitor-relay.conf
    restic forget --keep-daily 7 --group-by host --prune
fi
find "$backup_dir" -maxdepth 1 -type f -name 'monitoring-*.tar.gz' -mtime +7 -delete
