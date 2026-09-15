#!/usr/bin/env python3
"""Exempt the Tailscale domains from mihomo's fake-ip, so tailscaled can connect.

mihomo answers DNS with addresses from its fake-ip range and maps them back when
the packet enters the TUN. tailscaled marks its own sockets, and that mark routes
around the TUN by policy rule, so a fake address never reaches the mapping: the
packet goes to the LAN gateway, which has nowhere to send 198.18.x.x. Nothing is
rejected, so `tailscale login` simply hangs. The CLI is unmarked and keeps
working through the TUN, which makes the board look healthy.

Filtering by domain rather than by address also survives DERP renumbering.

Backs the configuration up, validates with `mihomo -t` before restarting, and
restores the backup if the new configuration or the restarted service fails.
Prints no credentials.
"""
import argparse
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DOMAINS = ["+.tailscale.com", "+.tailscale.io"]
NAMES = ["controlplane.tailscale.com", "login.tailscale.com", "derp1.tailscale.com"]
ANCHOR = "  fake-ip-filter:"


def run(*args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, **kwargs)


def fake_range(text):
    for line in text.splitlines():
        if line.strip().startswith("fake-ip-range:"):
            value = line.split(":", 1)[1].strip()
            # The config writes a host address; the pool is the whole network.
            return ipaddress.ip_network(value, strict=False)
    return ipaddress.ip_network("198.18.0.0/16")


def resolved(name):
    try:
        return {info[4][0] for info in socket.getaddrinfo(name, None, socket.AF_INET)}
    except socket.gaierror:
        return set()


def report(pool, label):
    bad = []
    for name in NAMES:
        addresses = resolved(name)
        fake = {a for a in addresses if ipaddress.ip_address(a) in pool}
        state = "fake-ip" if fake else ("real" if addresses else "NO ANSWER")
        print(f"  {label:7} {name:30} {state}")
        if fake or not addresses:
            bad.append(name)
    return bad


def edit(text):
    if all(domain in text for domain in DOMAINS):
        return None
    if ANCHOR not in text:
        raise ValueError("no fake-ip-filter block in the configuration")
    added = "\n".join(f'    - "{domain}"' for domain in DOMAINS)
    return text.replace(ANCHOR, f"{ANCHOR}\n{added}", 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path("/etc/mihomo/config.yaml"))
    parser.add_argument("--backup-dir", type=Path, default=Path("/root/mihomo-backup"))
    parser.add_argument("--dry-run", action="store_true", help="show the change, touch nothing")
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0 and not args.dry_run:
        sys.exit("Run as root")

    text = args.config.read_text()
    pool = fake_range(text)
    print(f"fake-ip pool: {pool}")
    print("before:")
    report(pool, "before")

    updated = edit(text)
    if updated is None:
        print(f"\nThe Tailscale domains are already exempt: {', '.join(DOMAINS)}")
        print("If the names above still answer with fake-ip, mihomo is serving a cached")
        print("mapping: stop mihomo, move /etc/mihomo/cache.db aside and start it again.")
        return
    if args.dry_run:
        print(f"\nWould add to fake-ip-filter: {', '.join(DOMAINS)}")
        return

    args.backup_dir.mkdir(parents=True, exist_ok=True)
    args.backup_dir.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.backup_dir / f"config.yaml.{stamp}"
    shutil.copy2(args.config, backup)
    backup.chmod(0o600)
    print(f"\nbackup: {backup}")

    def restore(reason):
        shutil.copy2(backup, args.config)
        run("systemctl", "restart", "mihomo")
        sys.exit(f"{reason}; configuration restored from the backup")

    args.config.write_text(updated)
    args.config.chmod(0o600)

    check = run("mihomo", "-t", "-d", str(args.config.parent))
    if check.returncode != 0:
        # Show only the last line: the output can quote configuration values.
        restore(f"mihomo rejected the configuration: {check.stderr.strip().splitlines()[-1:]}")
    print("mihomo -t: configuration valid")

    run("systemctl", "restart", "mihomo")
    time.sleep(8)
    if run("systemctl", "is-active", "--quiet", "mihomo").returncode != 0:
        restore("mihomo did not come back after the restart")
    run("resolvectl", "flush-caches")
    time.sleep(2)

    print("after:")
    bad = report(pool, "after")
    if bad:
        print("\nStill fake-ip or unresolved: " + ", ".join(bad))
        print("mihomo keeps its fake-ip pool in cache.db. Try:")
        print("  systemctl stop mihomo && mv /etc/mihomo/cache.db /root/mihomo-backup/ "
              "&& systemctl start mihomo")
        print(f"The previous configuration remains at {backup}.")
        sys.exit(1)
    print("\nPASS: the Tailscale names resolve to real addresses.")
    print("Now run: sudo tailscale up --accept-dns=false --accept-routes=false --timeout=30s")


if __name__ == "__main__":
    main()
