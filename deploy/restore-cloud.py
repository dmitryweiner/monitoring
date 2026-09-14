#!/usr/bin/env python3
"""Restore a backup into separate D1/R2 resources and verify it against the files.

Never restores into the resources the backup came from: the target names must
differ from the manifest. Verification compares the target with the local backup,
not with production, which keeps receiving events while the check runs.

The D1 dump creates its tables, loads the rows and only then creates the indexes
and triggers, so the quota and usage triggers do not fire during the load. The
target database must therefore be empty: do not apply migrations to it first.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib import import_module

backup_cloud = import_module("backup-cloud")
digest, paged, safe_key, wrangler = (backup_cloud.digest, backup_cloud.paged,
                                     backup_cloud.safe_key, backup_cloud.wrangler)

TABLES = ["events", "latest", "sessions", "devices", "usage", "daily", "maintenance"]


def normalise(rows):
    """Compare values, not their JSON spelling: D1 may return 1.0 as 1."""
    def cell(value):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
    return sorted(json.dumps([cell(v) for v in row], sort_keys=True) for row in rows)


def local_rows(dump, table):
    """Load the dump into a throwaway SQLite file and read one table back."""
    with tempfile.TemporaryDirectory() as tmp:
        connection = sqlite3.connect(Path(tmp) / "backup.db")
        try:
            connection.executescript(dump.read_text())
            return connection.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            connection.close()


def restore(cloud_dir, backup_dir, database, bucket, create):
    manifest = json.loads((backup_dir / "manifest.json").read_text())
    if database == manifest["database"] or bucket == manifest["bucket"]:
        raise ValueError("target must differ from the resources the backup came from")
    dump = backup_dir / "d1.sql"
    if digest(dump) != manifest["d1"]["sha256"]:
        raise ValueError("d1.sql does not match the manifest checksum")

    if create:
        wrangler(cloud_dir, "d1", "create", database)
        wrangler(cloud_dir, "r2", "bucket", "create", bucket)
    # A fresh D1 already carries the internal _cf_KV table; only user tables count.
    existing = backup_cloud.query(cloud_dir, database,
                                  "SELECT count(*) AS n FROM sqlite_master WHERE type='table' "
                                  "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
                                  "AND name NOT LIKE '\\_cf\\_%' ESCAPE '\\'")
    if existing[0]["n"]:
        raise ValueError(f"{database} already has {existing[0]['n']} table(s); "
                         "restore needs a fresh database")

    wrangler(cloud_dir, "d1", "execute", database, "--remote", "-y", "--file", str(dump))
    for item in manifest["objects"]:
        key = safe_key(item["key"])
        source = backup_dir / "r2" / key
        if digest(source) != item["sha256"]:
            raise ValueError(f"{key} does not match the manifest checksum")
        wrangler(cloud_dir, "r2", "object", "put", f"{bucket}/{key}", "--remote",
                 "--file", str(source), "--content-type", "image/jpeg")
    return manifest, dump


def verify(cloud_dir, backup_dir, database, bucket, manifest, dump):
    failures = []
    for table in TABLES:
        expected = normalise(local_rows(dump, table))
        actual = normalise([tuple(row.values()) for row in
                            paged(cloud_dir, database, f"SELECT * FROM {table}")])
        if expected == actual:
            print(f"  {table}: {len(expected)} rows match")
        else:
            failures.append(f"{table}: backup {len(expected)} rows, restored {len(actual)}")

    with tempfile.TemporaryDirectory() as tmp:
        for item in manifest["objects"]:
            target = Path(tmp) / "object"
            wrangler(cloud_dir, "r2", "object", "get", f"{bucket}/{item['key']}",
                     "--remote", "--file", str(target))
            if digest(target) != item["sha256"]:
                failures.append(f"{item['key']}: restored bytes differ")
            target.unlink()
    print(f"  R2: {len(manifest['objects'])} objects compared byte for byte")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    default_cloud = Path(__file__).resolve().parent.parent / "cloud"
    parser.add_argument("--backup", type=Path, required=True, help="directory from backup-cloud.py")
    parser.add_argument("--database", required=True, help="target D1 name, must be empty")
    parser.add_argument("--bucket", required=True, help="target R2 bucket name")
    parser.add_argument("--cloud-dir", type=Path, default=default_cloud)
    parser.add_argument("--create", action="store_true", help="create the target resources first")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        manifest, dump = restore(args.cloud_dir, args.backup, args.database, args.bucket, args.create)
        print("restored; verifying against the backup files")
        failures = verify(args.cloud_dir, args.backup, args.database, args.bucket, manifest, dump)
    except (RuntimeError, ValueError, sqlite3.Error) as error:
        sys.exit(f"restore failed: {error}")
    if failures:
        sys.exit("VERIFY FAILED:\n  " + "\n  ".join(failures))
    print("PASS: D1 tables and R2 objects match the backup")


if __name__ == "__main__":
    main()
