#!/usr/bin/env bash
# Restart mihomo when traffic stops reaching the internet through its tunnel.
#
# Twice now the board has sat without connectivity until mihomo was restarted by
# hand: the network came back, mihomo noticed nothing and logged nothing, and
# only a restart made it dial again. It happened both with
# auto-detect-interface true and with the interface pinned, so this watchdog
# treats the symptom rather than guessing at the setting.
#
# Restarting is the last step, and only when the evidence says the fault is
# mihomo's: the local network answers, mihomo is running, and two probes in a row
# failed. Before restarting it writes a short snapshot to the journal, so the next
# occurrence can be diagnosed without turning mihomo's logging back up — that is
# what filled the disk before.
set -uo pipefail
test "$(id -u)" = 0 || { echo 'Run as root'; exit 1; }

URL=${WATCHDOG_URL:-https://home-monitoring-poc.dmitry-weiner.workers.dev/healthz}
CONFIRM_SECONDS=${WATCHDOG_CONFIRM:-20}
MIN_INTERVAL=${WATCHDOG_MIN_INTERVAL:-600}   # never restart more often than this
STATE=/run/mihomo-watchdog
DRY_RUN=${WATCHDOG_DRY_RUN:-0}

probe() {
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
        -A monitoring-watchdog/1 "$URL" 2>/dev/null)
    test "$code" = 200
}

gateway_answers() {
    local gateway
    gateway=$(ip route show default 0.0.0.0/0 | awk '/ dev wlan0 | dev end0 /{print $3; exit}')
    test -n "$gateway" || return 1
    ping -c 1 -W 2 "$gateway" >/dev/null 2>&1
}

snapshot() {
    echo "diagnostics before the restart:"
    echo "  default route: $(ip route show default | head -1 | tr -s ' ')"
    echo "  route to 1.1.1.1: $(ip route get 1.1.1.1 2>&1 | head -1 | tr -s ' ')"
    echo "  tun device: $(ip -brief addr show Meta 2>/dev/null | tr -s ' ' || echo 'absent')"
    echo "  policy rules for the tun table: $(ip -4 rule | grep -c 2022)"
    echo "  established connections of mihomo: $(ss -tnH state established 2>/dev/null | wc -l)"
    # The external controller answers locally even when the tunnel is dead.
    echo "  controller: $(curl -sS --max-time 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:9090/version 2>/dev/null || echo unreachable)"
}

mkdir -p "$STATE"

if systemctl is-active --quiet monitoring-net-test; then
    echo "network test is running; standing aside"
    exit 0
fi
if ! systemctl is-active --quiet mihomo; then
    echo "mihomo is not running; leaving it alone"
    exit 0
fi

probe && exit 0

sleep "$CONFIRM_SECONDS"
probe && { echo "first probe failed, second succeeded; no action"; exit 0; }

if ! gateway_answers; then
    echo "the local network is down as well; nothing mihomo can fix"
    exit 0
fi

now=$(date +%s)
last=$(cat "$STATE/last-restart" 2>/dev/null || echo 0)
if [ $((now - last)) -lt "$MIN_INTERVAL" ]; then
    echo "restarted $((now - last))s ago, waiting out the $MIN_INTERVAL s interval"
    exit 0
fi

echo "no route to the internet through mihomo, and the local network answers"
snapshot
if [ "$DRY_RUN" != 0 ]; then
    echo "dry run: mihomo would be restarted now"
    exit 0
fi
echo "$now" > "$STATE/last-restart"
systemctl restart mihomo
sleep 15
if probe; then
    echo "restarted mihomo; the internet is back"
else
    echo "restarted mihomo; still no internet, so the fault is elsewhere"
fi
