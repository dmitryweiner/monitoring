#!/usr/bin/env python3
"""Prepare a candidate config; never overwrite a live config or start networking."""
import argparse
from pathlib import Path
import yaml


def prepare(config, uid):
    tun = config.setdefault("tun", {})
    tun["enable"] = True
    tun["auto-route"] = True
    # Do not let nftables redirection override the independent per-UID route.
    tun["auto-redirect"] = False
    tun["strict-route"] = False
    excluded = tun.setdefault("exclude-uid", [])
    if uid not in excluded:
        excluded.append(uid)
    # Administration uses literal IPv4; avoid any dependency on fake-IP DNS.
    config["allow-lan"] = False
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--tunnel-uid", type=int, required=True)
    args = parser.parse_args()
    candidate = prepare(yaml.safe_load(Path(args.source).read_text()), args.tunnel_uid)
    import os
    fd = os.open(args.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        yaml.safe_dump(candidate, output, allow_unicode=True, sort_keys=False)
    print("Candidate written; proxy credentials were not printed.")


if __name__ == "__main__":
    main()
