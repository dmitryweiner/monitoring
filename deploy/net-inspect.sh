#!/usr/bin/env bash
# Read-only network inventory for the Tailscale/mihomo work. Changes nothing.
#
# Prints the mihomo settings that decide whether Tailscale escapes the TUN, with
# credentials left out: the proxies, proxy-providers and proxy-groups blocks are
# never shown, and server addresses are reduced to a count.
set -uo pipefail
test "$(id -u)" = 0 || { echo 'Run as root: it reads /etc/mihomo/config.yaml'; exit 1; }
config=${1:-/etc/mihomo/config.yaml}

rule() { printf '\n=== %s ===\n' "$1"; }

rule 'Tailscale'
tailscale status 2>&1 | head -20
tailscale ip -4 2>&1 | head -2
echo "--- netcheck ---"
timeout 45 tailscale netcheck 2>&1 | head -25

rule 'Interfaces and routes'
ip -brief addr
ip -4 route
echo "--- policy rules v4 ---"; ip -4 rule
echo "--- policy rules v6 ---"; ip -6 rule
echo "--- tun table 2022 (first 5) ---"; ip -4 route show table 2022 2>/dev/null | head -5
echo "--- tailscale table 52 ---"; ip -4 route show table 52 2>/dev/null | head -10

rule 'Path selection'
for dst in 1.1.1.1 8.8.8.8 192.168.100.1; do
    printf '%-16s %s\n' "$dst" "$(ip route get "$dst" 2>&1 | head -1)"
    printf '%-16s %s\n' "  +tailscale mark" "$(ip route get "$dst" mark 0x80000 2>&1 | head -1)"
done

rule 'DNS'
resolvectl status 2>&1 | sed -n '1,10p'
echo "--- link with default route ---"
resolvectl status 2>&1 | grep -A4 'Default Route: yes' | head -20
echo "--- control plane and DERP resolution ---"
for name in controlplane.tailscale.com derp1.tailscale.com login.tailscale.com; do
    printf '%-28s %s\n' "$name" "$(getent ahostsv4 "$name" 2>/dev/null | awk '{print $1; exit}' || echo 'FAILED')"
done

rule 'Netfilter'
for tool in iptables nft; do
    if command -v "$tool" >/dev/null; then
        echo "--- $tool ruleset ---"
        "$tool" -S 2>/dev/null | head -40 || "$tool" list ruleset 2>/dev/null | head -40
    else
        echo "$tool: not installed"
    fi
done

rule 'mihomo service and listeners'
systemctl --no-pager show mihomo -p ActiveState -p SubState -p NRestarts -p ExecStart
ss -lntup 2>/dev/null | grep -E 'mihomo|:7890|:7891|:9090' | head

rule 'mihomo configuration (credentials removed)'
# Skip whole top-level blocks that hold servers, passwords and UUIDs.
awk '
/^[a-zA-Z]/ { skip = ($0 ~ /^(proxies|proxy-providers|proxy-groups|rules|rule-providers|sub-rules|listeners):/) }
skip { next }
/(password|uuid|secret|token|username|server|servername|sni|ws-opts|private-key|psk)/ { next }
{ print }
' "$config"
echo "--- counts only ---"
printf 'proxy entries: %s\n' "$(grep -c '^\s*-\s*{\?name:' "$config" 2>/dev/null || echo '?')"
printf 'distinct server values: %s\n' "$(grep -oE '(^|[ ,{])server: *[^,}]+' "$config" 2>/dev/null | sort -u | wc -l)"

rule 'sshd'
systemctl --no-pager is-active ssh sshd 2>&1 | head -2
sshd -T 2>/dev/null | grep -E '^(port|listenaddress|passwordauthentication|permitrootlogin)' | head
echo
echo 'Inventory complete. Nothing was changed.'
