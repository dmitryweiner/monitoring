#!/usr/bin/env bash
# Exercise SSH-over-Tailscale independence from mihomo, with a rollback that does
# not depend on the session that started the test.
#
# Every mode arms a transient systemd timer BEFORE it changes anything. If this
# script, the SSH session or the whole shell dies mid-test, the timer still fires
# and puts the board back. The trap restores on any normal exit and cancels it.
#
# Run detached so a dropped connection cannot abort the test:
#
#   sudo systemd-run --unit=monitoring-net-test --collect \
#     /home/dmw/projects/monitoring/deploy/net-failover-test.sh vpn-down --hold 180
#   journalctl -u monitoring-net-test -f        # watch
#   cat /var/log/monitoring-net-test.log        # report afterwards
#
# Inbound SSH cannot be tested from the board itself: during the hold window,
# try `ssh dmw@<tailscale-ip>` from the admin machine and note whether it worked.
set -uo pipefail

STATE=/run/monitoring-net-test
REPORT=${REPORT:-/var/log/monitoring-net-test.log}
ROLLBACK_UNIT=monitoring-net-rollback
CONFIG=/etc/mihomo/config.yaml
HEALTH=https://home-monitoring-poc.dmitry-weiner.workers.dev/healthz

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$REPORT"; }

usage() {
    echo "usage: $0 baseline|no-mihomo|vpn-down|restart-mihomo|rollback [--hold SECONDS]"
    exit 2
}

# --- restore -----------------------------------------------------------------

restore() {
    local had=0
    if [ -s "$STATE/blackholes" ]; then
        while read -r family address; do
            ip "$family" route del blackhole "$address" 2>/dev/null && had=1
        done < "$STATE/blackholes"
        : > "$STATE/blackholes"
    fi
    if [ -f "$STATE/mihomo-was-active" ]; then
        systemctl start mihomo 2>/dev/null
        rm -f "$STATE/mihomo-was-active"
        had=1
    fi
    systemctl is-active --quiet tailscaled || systemctl start tailscaled 2>/dev/null
    test "$had" -ge 0
}

cleanup() {
    local code=$?
    log "restoring"
    restore
    systemctl stop "$ROLLBACK_UNIT.timer" 2>/dev/null
    systemctl reset-failed "$ROLLBACK_UNIT.timer" "$ROLLBACK_UNIT.service" 2>/dev/null
    log "restored; mihomo=$(systemctl is-active mihomo) tailscaled=$(systemctl is-active tailscaled)"
    log "internet via VPN: $(probe_health)"
    log "=== end (exit $code) ==="
}

arm_rollback() {
    local seconds=$1
    systemctl stop "$ROLLBACK_UNIT.timer" 2>/dev/null
    systemd-run --unit="$ROLLBACK_UNIT" --collect --on-active="$seconds" \
        "$(readlink -f "$0")" rollback >/dev/null 2>&1 \
        || { echo "could not arm the rollback timer; refusing to continue"; exit 1; }
    log "rollback armed: fires in ${seconds}s even if this session dies"
}

# --- observations ------------------------------------------------------------

probe_health() {
    local code
    # curl writes 000 and exits non-zero when it cannot connect; report one value.
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
        -A monitoring-check/1 "$HEALTH" 2>/dev/null)
    case $code in
        000|"") echo "unreachable" ;;
        *) echo "$code" ;;
    esac
}

observe() {
    local label=$1
    local backend peers
    backend=$(tailscale status --json 2>/dev/null | sed -n 's/.*"BackendState": *"\([^"]*\)".*/\1/p' | head -1)
    # Peer lines only; the Relay field is set even for direct links, so counting
    # it proves nothing. Use `tailscale ping <peer>` to tell direct from DERP.
    peers=$(tailscale status 2>/dev/null | grep -c '^100\.')
    log "[$label] tailscale=${backend:-unknown} nodes=$peers ip=$(tailscale ip -4 2>/dev/null | head -1)"
    log "[$label] control-plane DNS: $(getent ahostsv4 controlplane.tailscale.com 2>/dev/null | awk '{print $1; exit}' || echo FAILED)"
    log "[$label] route to 1.1.1.1: $(ip route get 1.1.1.1 2>&1 | head -1 | tr -s ' ')"
    log "[$label] route with tailscale mark: $(ip route get 1.1.1.1 mark 0x80000 2>&1 | head -1 | tr -s ' ')"
    log "[$label] worker /healthz: $(probe_health)"
    log "[$label] agent=$(systemctl is-active monitoring-agent) mihomo=$(systemctl is-active mihomo)"
}

# --- modes -------------------------------------------------------------------

block_vpn_endpoints() {
    local count=0 host address
    # Resolve the endpoints before blocking; never print them.
    for host in $(grep -ohE 'server: *[^,}[:space:]]+' "$CONFIG" 2>/dev/null | awk '{print $2}' | sort -u); do
        for address in $(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u); do
            ip route add blackhole "$address" 2>/dev/null && {
                echo "-4 $address" >> "$STATE/blackholes"; count=$((count + 1)); }
        done
        for address in $(getent ahostsv6 "$host" 2>/dev/null | awk '{print $1}' | sort -u); do
            ip -6 route add blackhole "$address" 2>/dev/null && {
                echo "-6 $address" >> "$STATE/blackholes"; count=$((count + 1)); }
        done
    done
    # proxy-providers keep their servers outside config.yaml; fall back to the
    # addresses mihomo is actually connected to.
    if [ "$count" = 0 ]; then
        log "no server: entries in the config, using mihomo's live connections"
        for address in $(ss -tnpH state established 2>/dev/null \
                | grep mihomo | awk '{print $4}' | sed 's/:[0-9]*$//' | sort -u); do
            case $address in
                192.168.*|127.*|10.*|198.18.*|\[*) continue ;;
            esac
            ip route add blackhole "$address" 2>/dev/null && {
                echo "-4 $address" >> "$STATE/blackholes"; count=$((count + 1)); }
        done
    fi
    log "blocked $count VPN endpoint address(es); addresses are not printed"
    test "$count" -gt 0 || log "WARNING: nothing was blocked, the vpn-down state is NOT real"
}

main() {
    local mode=${1:-} hold=180
    shift || usage
    while [ $# -gt 0 ]; do
        case $1 in
            --hold) hold=${2:?}; shift 2 ;;
            *) usage ;;
        esac
    done
    test "$(id -u)" = 0 || { echo 'Run as root'; exit 1; }
    mkdir -p "$STATE"; touch "$STATE/blackholes"

    if [ "$mode" = rollback ]; then
        log "=== rollback timer fired ==="
        restore
        log "rollback done; mihomo=$(systemctl is-active mihomo) tailscaled=$(systemctl is-active tailscaled)"
        exit 0
    fi

    log "=== $mode, hold ${hold}s ==="
    observe before

    case $mode in
        baseline)
            log "no changes requested"
            log "Try 'ssh dmw@<tailscale-ip>' from the admin machine now."
            sleep "$hold"
            ;;
        no-mihomo)
            arm_rollback $((hold + 120)); trap cleanup EXIT INT TERM
            systemctl is-active --quiet mihomo && touch "$STATE/mihomo-was-active"
            systemctl stop mihomo
            log "mihomo stopped; try SSH over Tailscale from the admin machine now"
            sleep "$hold"
            observe during
            ;;
        vpn-down)
            arm_rollback $((hold + 120)); trap cleanup EXIT INT TERM
            block_vpn_endpoints
            # auto-detect-interface binds mihomo's socket to wlan0, and a blackhole
            # route carries no device, so it may not catch that socket at all.
            # Traffic to the Worker only leaves through the VPN: if it still works,
            # the endpoints are still reachable and this run proves nothing.
            reached=$(probe_health)
            if [ "$reached" = 200 ]; then
                log "WARNING: worker still reachable ($reached) after blocking."
                log "WARNING: the VPN is NOT down; do not count this run as a test."
                log "Fallback: install nftables and drop the endpoints in the output hook."
            else
                log "block confirmed: worker unreachable through the VPN ($reached)"
            fi
            log "VPN endpoints blocked, mihomo left running; try SSH over Tailscale now"
            sleep 20; observe "during+20s"
            sleep $((hold > 20 ? hold - 20 : 0))
            observe "during+${hold}s"
            ;;
        restart-mihomo)
            arm_rollback $((hold + 120)); trap cleanup EXIT INT TERM
            systemctl restart mihomo
            log "mihomo restarted; watching whether Tailscale survives it"
            sleep 20; observe "after-restart+20s"
            sleep $((hold > 20 ? hold - 20 : 0))
            observe "after-restart+${hold}s"
            ;;
        *) usage ;;
    esac

    observe after
    log "report: $REPORT"
}

main "$@"
