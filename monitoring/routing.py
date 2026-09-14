"""Maintain an IPv4 route for the dedicated SSH UID, independently of mihomo."""
import argparse
import ipaddress
import json
import logging
import os
import pwd
import signal
import subprocess
import threading


def ip(*args):
    return subprocess.run(["ip", *args], capture_output=True, text=True, check=True, timeout=10).stdout


def desired_route(routes, interface):
    choices = [r for r in routes if r.get("dst") == "default" and r.get("dev") == interface and r.get("gateway")]
    if not choices:
        return None
    return min(choices, key=lambda r: r.get("metric", 0))


def update(uid, interface, table=100, priority=100):
    routes = json.loads(ip("-j", "-4", "route", "show", "table", "main"))
    route = desired_route(routes, interface)
    rules = json.loads(ip("-j", "-4", "rule", "show"))
    existing = [r for r in rules if r.get("priority") == priority]
    if existing and any(str(r.get("table")) != str(table) or
                        r.get("uidrange") not in (f"{uid}-{uid}", {"start": uid, "end": uid}) for r in existing):
        raise ValueError("routing priority already occupied; refusing to overwrite")
    if route:
        gateway = str(ipaddress.IPv4Address(route["gateway"]))
        ip("-4", "route", "replace", "table", str(table), "default", "via", gateway,
           "dev", interface, "onlink")
    else:
        # Fail closed for this UID instead of falling through into the TUN table.
        ip("-4", "route", "replace", "unreachable", "default", "table", str(table))
    if not existing:
        ip("-4", "rule", "add", "priority", str(priority), "uidrange", f"{uid}-{uid}",
           "lookup", str(table))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="monitor-tunnel")
    parser.add_argument("--interface", default=os.environ.get("MONITOR_UPLINK", "wlan0"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    uid = pwd.getpwnam(args.user).pw_uid
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO)
    while not stop.is_set():
        try:
            update(uid, args.interface)
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            logging.error("direct route update failed: %s", type(exc).__name__)
            if args.once:
                raise
        if args.once:
            return
        stop.wait(5)


if __name__ == "__main__":
    main()
