#!/usr/bin/env bash
set -euo pipefail
umask 077
test "$(id -u)" = 0
state=/var/lib/monitoring-mihomo
install -d -m 0700 "$state"
exec 9>"$state/activation.lock"
flock -x 9
action=${1:?Usage: activate-mihomo.sh activate|confirm|rollback}
case "$action" in
activate)
    test ! -e "$state/pending" || { echo 'A previous activation is pending'; exit 1; }
    test ! -e "$state/previous" || { echo 'Archive the previous transaction before another activation'; exit 1; }
    systemctl is-active --quiet monitoring-tunnel.service
    systemctl is-active --quiet monitoring-route.service
    test -x "$state/candidate/mihomo"
    # The caller must have verified a NEW external SSH session; no check here can
    # prove reachability from the administrator's network.
    install -d -m 0700 "$state/previous"
    for pair in '/usr/local/bin/mihomo:binary' '/etc/mihomo:config' '/etc/systemd/system/mihomo.service:unit'; do
        src=${pair%:*}; name=${pair##*:}
        if [ -e "$src" ]; then cp -a "$src" "$state/previous/$name"; fi
    done
    systemctl is-active --quiet mihomo && touch "$state/previous/active" || true
    systemctl is-enabled --quiet mihomo && touch "$state/previous/enabled" || true
    touch "$state/pending"
    install -m 0700 "$0" "$state/rollback.sh"
    systemd-run --unit=monitoring-mihomo-rollback --on-active=180s "$state/rollback.sh" rollback
    trap 'flock -u 9; "$state/rollback.sh" rollback' ERR
    systemctl stop mihomo || true
    # An unconfirmed candidate must not start automatically after a reboot.
    systemctl disable mihomo || true
    install -d -m 0700 /etc/mihomo
    install -m 0755 "$state/candidate/mihomo" /usr/local/bin/mihomo
    install -m 0600 "$state/candidate/config.yaml" /etc/mihomo/config.yaml
    install -m 0644 "$state/candidate/mihomo.service" /etc/systemd/system/mihomo.service
    systemctl daemon-reload
    systemctl start mihomo
    echo 'Open a NEW external SSH session and run confirm within 180 seconds.'
    ;;
confirm)
    test -e "$state/pending"
    systemctl stop monitoring-mihomo-rollback.timer
    systemctl enable mihomo
    mv "$state/pending" "$state/confirmed"
    echo 'Confirmed. Keep the previous backup until failure tests pass.'
    ;;
rollback)
    test -e "$state/pending" || exit 0
    systemctl stop mihomo || true
    if [ -e "$state/previous/binary" ]; then install -m 0755 "$state/previous/binary" /usr/local/bin/mihomo; fi
    if [ -d "$state/previous/config" ]; then cp -a "$state/previous/config/." /etc/mihomo/; fi
    if [ -f "$state/previous/unit" ]; then install -m 0644 "$state/previous/unit" /etc/systemd/system/mihomo.service; fi
    systemctl daemon-reload
    if [ -e "$state/previous/enabled" ]; then systemctl enable mihomo; else systemctl disable mihomo; fi
    if [ -e "$state/previous/active" ]; then systemctl start mihomo; fi
    mv "$state/pending" "$state/rolled-back"
    ;;
*) exit 2 ;;
esac
