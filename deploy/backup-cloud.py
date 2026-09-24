#!/usr/bin/env python3
"""Export D1 and the private R2 bucket to a local directory.

The export contains private photographs and session hashes: the directory is
created with mode 0700 and every file with 0600. Store it accordingly and apply
the local retention policy.

Object keys come from D1, which is the index the Worker reads photos through.
Wrangler cannot list a bucket, so an object with no D1 row is invisible here and
is not backed up; the Worker never serves such an object either. Finding orphans
needs an R2 API token and the S3 ListObjectsV2 call, which this script does not use.

--database-only skips R2: the measurements are what matters, while photos and
audio are expendable and take hours to copy, one wrangler call per object.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PAGE = 500


def wrangler(cloud_dir, *args, capture=True):
    """Run wrangler in the cloud project directory; never echo credentials."""
    result = subprocess.run(["npx", "wrangler", *args], cwd=cloud_dir, text=True,
                            capture_output=capture)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
        raise RuntimeError("wrangler " + " ".join(args[:2]) + " failed:\n" + "\n".join(tail))
    return result.stdout or ""


def query(cloud_dir, database, sql):
    """Run a read-only statement against the remote database and return rows."""
    out = wrangler(cloud_dir, "d1", "execute", database, "--remote", "--json", "--command", sql)
    # Wrangler prefixes the JSON payload with progress lines on some versions.
    start = out.find("[")
    if start < 0:
        raise RuntimeError("no JSON in wrangler output")
    return json.loads(out[start:])[0]["results"]


def paged(cloud_dir, database, sql):
    offset = 0
    while True:
        rows = query(cloud_dir, database, f"{sql} LIMIT {PAGE} OFFSET {offset}")
        yield from rows
        if len(rows) < PAGE:
            return
        offset += len(rows)


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def safe_key(key):
    """Object keys become relative paths; reject anything that could escape."""
    if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key:
        raise ValueError(f"unsafe object key: {key!r}")
    return key


def config_defaults(cloud_dir):
    text = (cloud_dir / "wrangler.jsonc").read_text()
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    config = json.loads(text)
    return config["d1_databases"][0]["database_name"], config["r2_buckets"][0]["bucket_name"]


def backup(cloud_dir, database, bucket, out, database_only=False):
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    dump = out / "d1.sql"
    wrangler(cloud_dir, "d1", "export", database, "--remote", "--output", str(dump), "-y")
    dump.chmod(0o600)

    objects = []
    photos = out / "r2"
    rows = [] if database_only else paged(cloud_dir, database,
                     "SELECT object_key, bytes, state FROM events "
                     "WHERE object_key IS NOT NULL ORDER BY object_key")
    for row in rows:
        key = safe_key(row["object_key"])
        target = photos / key
        target.parent.mkdir(parents=True, exist_ok=True)
        wrangler(cloud_dir, "r2", "object", "get", f"{bucket}/{key}", "--remote",
                 "--file", str(target))
        target.chmod(0o600)
        size = target.stat().st_size
        if row["state"] == "ready" and size != row["bytes"]:
            raise RuntimeError(f"size mismatch for {key}: R2 {size}, D1 {row['bytes']}")
        objects.append({"key": key, "bytes": size, "sha256": digest(target),
                        "state": row["state"]})

    manifest = {"database": database, "bucket": bucket,
                "d1": {"bytes": dump.stat().st_size, "sha256": digest(dump)},
                "objects": objects, "objects_skipped": database_only}
    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    path.chmod(0o600)
    print(f"D1 dump: {manifest['d1']['bytes']} bytes")
    if database_only:
        print("R2 objects: skipped (--database-only)")
    else:
        print(f"R2 objects: {len(objects)}, {sum(o['bytes'] for o in objects)} bytes")
    pending = [o["key"] for o in objects if o["state"] != "ready"]
    if pending:
        print(f"note: {len(pending)} object(s) still pending in D1")
    print(f"backup directory: {out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    default_cloud = Path(__file__).resolve().parent.parent / "cloud"
    parser.add_argument("--out", type=Path, required=True, help="destination directory")
    parser.add_argument("--cloud-dir", type=Path, default=default_cloud)
    parser.add_argument("--database")
    parser.add_argument("--bucket")
    parser.add_argument("--database-only", action="store_true",
                        help="export D1 only; skip photos and audio in R2")
    args = parser.parse_args()
    os.umask(0o077)
    database, bucket = config_defaults(args.cloud_dir)
    try:
        backup(args.cloud_dir, args.database or database, args.bucket or bucket, args.out,
               args.database_only)
    except (RuntimeError, ValueError) as error:
        sys.exit(f"backup failed: {error}")


if __name__ == "__main__":
    main()
