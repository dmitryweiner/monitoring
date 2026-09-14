#!/usr/bin/env bash
set -euo pipefail
umask 077
# Offline, reviewed release artifacts. SHA-256 must come from a trusted release source.
archive=${1:?Usage: sudo bash deploy/install-mihomo.sh release.gz trusted-sha256 candidate.yaml}
expected=${2:?SHA-256 required}
candidate=${3:?candidate configuration required}
test "$(id -u)" = 0
case "$(uname -m)" in
    armv7l) arch=armv7 ;;
    aarch64|arm64) arch=arm64 ;;
    x86_64|amd64) arch=amd64 ;;
    *) echo 'Unsupported architecture'; exit 1 ;;
esac
[[ "$expected" =~ ^[0-9a-f]{64}$ ]] || exit 2
actual=$(sha256sum "$archive")
test "${actual%% *}" = "$expected" || { echo 'Checksum mismatch'; exit 1; }
project_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
install -d -m 0700 /var/lib/monitoring-mihomo/candidate
gzip -dc "$archive" > /var/lib/monitoring-mihomo/candidate/mihomo
chmod 0700 /var/lib/monitoring-mihomo/candidate/mihomo
# Running -v proves the selected binary is executable on the target architecture.
/var/lib/monitoring-mihomo/candidate/mihomo -v
install -m 0600 "$candidate" /var/lib/monitoring-mihomo/candidate/config.yaml
/var/lib/monitoring-mihomo/candidate/mihomo -t -d /var/lib/monitoring-mihomo/candidate
install -m 0600 "$project_dir/deploy/mihomo.service" /var/lib/monitoring-mihomo/candidate/mihomo.service
echo "Prepared $arch candidate. Use activate-mihomo.sh only after direct SSH succeeds."
