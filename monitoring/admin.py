import argparse
import os
import secrets
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

from .common import atomic_write, database, digest


def provision(directory, device_id="home"):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = [directory / x for x in ("device.token", "admin.key", "server.env")]
    if any(p.exists() for p in paths):
        raise ValueError("credential files already exist; refusing to rotate implicitly")
    device, admin = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    atomic_write(paths[0], (device + "\n").encode())
    atomic_write(paths[1], (admin + "\n").encode())
    atomic_write(paths[2], (f"MONITOR_DEVICE_ID={device_id}\nMONITOR_DEVICE_HASH={digest(device)}\n"
                           f"MONITOR_ADMIN_HASH={digest(admin)}\nMONITOR_DATA=/var/lib/monitoring-server\n"
                           "MONITOR_ORIGINS=\n").encode())


def backup(directory, destination):
    """Call with API stopped: the SQLite snapshot and JPEG set must describe one instant."""
    directory, destination = Path(directory), Path(destination)
    if destination.exists():
        raise ValueError("backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        temporary = Path(temporary)
        with sqlite3.connect(directory / "server.db") as source:
            with sqlite3.connect(temporary / "server.db") as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("invalid database")
        archive_path = temporary / "backup.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(temporary / "server.db", arcname="server.db")
            with database(temporary / "server.db") as db:
                for row in db.execute("SELECT filename FROM events WHERE filename IS NOT NULL"):
                    archive.add(directory / "photos" / row[0], arcname="photos/" + row[0])
        os.replace(archive_path, destination)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("credentials")
    init.add_argument("directory")
    init.add_argument("--device-id", default="home")
    for name in ("cleanup", "revoke-sessions"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--data", default="/var/lib/monitoring-server")
    dump = commands.add_parser("backup")
    dump.add_argument("--data", default="/var/lib/monitoring-server")
    dump.add_argument("--output", required=True)
    dump.add_argument("--api-stopped", action="store_true", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "credentials":
        import re
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.device_id):
            parser.error("invalid device ID")
        provision(args.directory, args.device_id)
        print("Credentials written with mode 0600; values are not printed.")
    elif args.command == "cleanup":
        from .server import Store
        Store(args.data).cleanup()
    elif args.command == "revoke-sessions":
        with database(Path(args.data) / "server.db") as db:
            db.execute("DELETE FROM sessions")
    elif args.command == "backup":
        backup(args.data, args.output)


if __name__ == "__main__":
    main()
