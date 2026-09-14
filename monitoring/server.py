"""Single-worker private API. Run behind the supplied HTTPS reverse proxy."""
import base64
import hashlib
import hmac
import io
import json
import math
import os
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .common import atomic_write, canonical, database, digest

DAY = 86400
MAX_PHOTO = 4 * 1024**2


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1]
    device_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    event_id: UUID
    observed_at: float = Field(ge=0)
    kind: Literal["measurement", "photo"]
    source: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    status: Literal["ok", "error"]
    values: dict[str, float] = Field(default_factory=dict, max_length=32)
    clock_synchronized: bool

    @field_validator("values")
    @classmethod
    def valid_values(cls, values):
        import re
        if any(not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", k) or not math.isfinite(v)
               for k, v in values.items()):
            raise ValueError("invalid metric")
        return values


class Login(BaseModel):
    key: str = Field(min_length=32, max_length=256, repr=False)


class Batch(BaseModel):
    # Individual items are validated separately, permitting partial acknowledgements.
    events: list[dict] = Field(min_length=1, max_length=64)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.photos = self.directory / "photos"
        self.photos.mkdir(exist_ok=True)
        self.path = self.directory / "server.db"
        self.lock = threading.RLock()
        with database(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                  device TEXT NOT NULL, id TEXT NOT NULL, observed REAL NOT NULL,
                  received REAL NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
                  payload TEXT NOT NULL, fingerprint TEXT NOT NULL, filename TEXT,
                  PRIMARY KEY(device,id));
                CREATE INDEX IF NOT EXISTS timeline ON events(device,kind,observed,id);
                CREATE INDEX IF NOT EXISTS by_source ON events(device,source,observed);
                CREATE TABLE IF NOT EXISTS sessions (
                  hash TEXT PRIMARY KEY, expires REAL NOT NULL, key_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, last_seen REAL NOT NULL);
            """)

    def put(self, event: Event, photo=None):
        item = event.model_dump(mode="json")
        encoded = canonical(item)
        fingerprint = digest(encoded.encode() + (photo or b""))
        now = time.time()
        if event.observed_at > now + 300:
            return "invalid_time"
        retention = (30 if event.kind == "photo" else 90) * DAY
        if event.observed_at < now - retention:
            return "expired"
        filename = digest(event.device_id + ":" + str(event.event_id)) + ".jpg" if photo else None
        with self.lock, database(self.path) as db:
            old = db.execute("SELECT fingerprint,filename FROM events WHERE device=? AND id=?",
                             (event.device_id, str(event.event_id))).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    return "conflict"
                if photo and not (self.photos / old["filename"]).exists():
                    atomic_write(self.photos / old["filename"], photo)
                return "duplicate"
            if photo:
                # File is durable before the row is committed; crashes can leave only
                # harmless orphan files, removed by maintenance after a grace period.
                atomic_write(self.photos / filename, photo)
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                       (event.device_id, str(event.event_id), event.observed_at, now,
                        event.kind, event.source, encoded, fingerprint, filename))
            db.execute("INSERT INTO devices VALUES (?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
                       (event.device_id, now))
        return "stored"

    def cleanup(self, now=None):
        now = time.time() if now is None else now
        with self.lock, database(self.path) as db:
            rows = db.execute("SELECT filename FROM events WHERE (kind='photo' AND observed<?) "
                              "OR (kind='measurement' AND observed<?)", (now-30*DAY, now-90*DAY)).fetchall()
            db.execute("DELETE FROM events WHERE (kind='photo' AND observed<?) "
                       "OR (kind='measurement' AND observed<?)", (now-30*DAY, now-90*DAY))
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
        # Delete after committing row removal; an interrupted cleanup is recoverable.
        for row in rows:
            if row[0]:
                (self.photos / row[0]).unlink(missing_ok=True)
        with self.lock, database(self.path) as db:
            referenced = {r[0] for r in db.execute("SELECT filename FROM events WHERE filename IS NOT NULL")}
            for path in self.photos.iterdir():
                if path.name not in referenced and path.stat().st_mtime < now - 3600:
                    path.unlink()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def create_app(directory=None, device_id=None, device_hash=None, admin_hash=None, origins=None):
    directory = directory or os.environ.get("MONITOR_DATA", "/var/lib/monitoring-server")
    device_id = device_id or os.environ.get("MONITOR_DEVICE_ID", "home")
    device_hash = device_hash or os.environ.get("MONITOR_DEVICE_HASH", "")
    admin_hash = admin_hash or os.environ.get("MONITOR_ADMIN_HASH", "")
    if any(len(key) != 64 or any(c not in "0123456789abcdef" for c in key)
           for key in (device_hash, admin_hash)):
        raise ValueError("configure SHA-256 hashes of device/admin keys")
    origins = origins if origins is not None else os.environ.get("MONITOR_ORIGINS", "").split()
    store = Store(directory)
    app = FastAPI(title="Home monitoring", version="1.0.0", docs_url=None, redoc_url=None)
    app.state.store = store
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"])
    bearer = HTTPBearer(auto_error=False)
    rates = OrderedDict()

    @app.middleware("http")
    async def limits(request, call_next):
        now = time.monotonic()
        address = request.client.host if request.client else "unknown"
        category = "login" if request.url.path == "/v1/session" and request.method == "POST" else "api"
        key = (address, category)
        for old in list(rates):
            if now - rates[old][0] > 60:
                del rates[old]
        window, count = rates.get(key, (now, 0))
        if count >= (10 if category == "login" else 300):
            return JSONResponse({"detail": "rate limited"}, 429, headers={"Retry-After": "60"})
        if len(rates) >= 4096 and key not in rates:
            return JSONResponse({"detail": "busy"}, 429)
        rates[key] = (window, count + 1)
        size = MAX_PHOTO if request.url.path == "/v1/photos" and request.method == "POST" else 256*1024
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > size:
                return JSONResponse({"detail": "body too large"}, 413)
        request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def device_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or not hmac.compare_digest(digest(credentials.credentials), device_hash):
            raise HTTPException(401, "invalid device credential")

    def session_auth(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        token = credentials.credentials if credentials else request.cookies.get("monitor_session", "")
        with database(store.path) as db:
            row = db.execute("SELECT expires,key_hash FROM sessions WHERE hash=?", (digest(token),)).fetchone()
        if not row or row[0] <= time.time() or not hmac.compare_digest(row[1], admin_hash):
            raise HTTPException(401, "invalid or expired session")
        if not credentials and request.method not in ("GET", "HEAD"):
            if request.headers.get("origin") not in origins or request.headers.get("x-csrf-token") != "1":
                raise HTTPException(403, "CSRF check failed")
        return digest(token)

    @app.post("/v1/session")
    def login(payload: Login, request: Request, response: Response):
        if request.headers.get("origin") and request.headers["origin"] not in origins:
            raise HTTPException(403, "origin not allowed")
        if not hmac.compare_digest(digest(payload.key), admin_hash):
            raise HTTPException(401, "invalid key")
        token = secrets.token_urlsafe(32)
        expires = time.time() + 7 * DAY
        with database(store.path) as db:
            db.execute("DELETE FROM sessions WHERE expires<=? OR key_hash<>?", (time.time(), admin_hash))
            # Bound session storage even for a valid, repeatedly used login key.
            db.execute("DELETE FROM sessions WHERE hash IN (SELECT hash FROM sessions ORDER BY expires DESC LIMIT -1 OFFSET 63)")
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(token), expires, admin_hash))
        response.set_cookie("monitor_session", token, max_age=7*DAY, secure=True, httponly=True, samesite="strict")
        return {"session_key": token, "expires_at": expires}

    @app.delete("/v1/session", status_code=204)
    def logout(response: Response, token_hash=Depends(session_auth)):
        with database(store.path) as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (token_hash,))
        response.delete_cookie("monitor_session", secure=True, httponly=True, samesite="strict")

    @app.post("/v1/measurements", dependencies=[Depends(device_auth)])
    def measurements(batch: Batch):
        results = []
        for raw in batch.events:
            try:
                item = Event.model_validate(raw)
                if item.kind != "measurement" or item.device_id != device_id:
                    raise ValueError("wrong device or kind")
                status = store.put(item)
            except (ValidationError, ValueError):
                status = "invalid"
            results.append({"event_id": str(raw.get("event_id", ""))[:64], "status": status})
        return {"results": results}

    @app.post("/v1/photos", dependencies=[Depends(device_auth)], openapi_extra={
        "requestBody": {"required": True, "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}}},
        "parameters": [{"name": "X-Event", "in": "header", "required": True,
                        "schema": {"type": "string"}, "description": "JSON event metadata; see Event schema"}]})
    async def photo(request: Request):
        metadata = request.headers.get("x-event", "")
        if len(metadata) > 8192 or request.headers.get("content-type") != "image/jpeg":
            raise HTTPException(400, "invalid photo headers")
        try:
            item = Event.model_validate_json(metadata)
        except ValidationError:
            raise HTTPException(422, "invalid metadata") from None
        if item.kind != "photo" or item.status != "ok" or item.device_id != device_id:
            raise HTTPException(422, "wrong device or photo kind/status")
        data = await request.body()
        try:
            with Image.open(io.BytesIO(data)) as img:
                if img.format != "JPEG" or img.width * img.height > 16_000_000:
                    raise ValueError("invalid dimensions/format")
                img.load()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise HTTPException(422, "invalid JPEG") from None
        result = store.put(item, data)
        return {"event_id": str(item.event_id), "status": result}

    def decode_cursor(cursor):
        try:
            observed, identifier = json.loads(base64.urlsafe_b64decode(cursor))
            if not isinstance(observed, (float, int)) or not math.isfinite(observed):
                raise ValueError()
            return observed, str(UUID(identifier))
        except (ValueError, TypeError, json.JSONDecodeError):
            raise HTTPException(422, "invalid cursor") from None

    def page(kind, start, end, source, cursor, limit):
        now = time.time()
        start = max(start or 0, now - (30 if kind == "photo" else 90) * DAY)
        end = end if end is not None else now + 300
        if not math.isfinite(start) or not math.isfinite(end) or end < start or end-start > 91*DAY:
            raise HTTPException(422, "invalid time range")
        sql = "SELECT payload,received,observed,id FROM events WHERE device=? AND kind=? AND observed>=? AND observed<=?"
        args = [device_id, kind, start, end]
        if source:
            sql += " AND source=?"
            args.append(source)
        if cursor:
            observed, identifier = decode_cursor(cursor)
            sql += " AND (observed,id)>(?,?)"
            args += [observed, identifier]
        sql += " ORDER BY observed,id LIMIT ?"
        args.append(limit+1)
        with database(store.path) as db:
            rows = db.execute(sql, args).fetchall()
        items = [{**json.loads(row["payload"]), "received_at": row["received"]} for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            row = rows[limit-1]
            next_cursor = base64.urlsafe_b64encode(canonical([row["observed"], row["id"]]).encode()).decode()
        return {"items": items, "next_cursor": next_cursor}

    @app.get("/v1/measurements", dependencies=[Depends(session_auth)])
    def history(start: float | None = None, end: float | None = None,
                source: str | None = Query(None, max_length=64), cursor: str | None = Query(None, max_length=256),
                limit: int = Query(100, ge=1, le=1000)):
        return page("measurement", start, end, source, cursor, limit)

    @app.get("/v1/photos", dependencies=[Depends(session_auth)])
    def photos(start: float | None = None, end: float | None = None,
               cursor: str | None = Query(None, max_length=256), limit: int = Query(100, ge=1, le=1000)):
        return page("photo", start, end, None, cursor, limit)

    @app.get("/v1/measurements/aggregate", dependencies=[Depends(session_auth)])
    def aggregate(start: float, end: float, source: str = Query(max_length=64),
                  metric: str = Query(pattern=r"^[a-zA-Z0-9_]{1,64}$"),
                  bucket_seconds: int = Query(3600, ge=600, le=86400)):
        start = max(start, time.time()-90*DAY)
        if not math.isfinite(start) or not math.isfinite(end) or end <= start or end-start > 90*DAY:
            raise HTTPException(422, "invalid time range")
        if (end-start) / bucket_seconds > 1000:
            raise HTTPException(422, "too many buckets; increase bucket_seconds")
        with database(store.path) as db:
            rows = db.execute("""SELECT CAST(observed/? AS INTEGER)*? AS bucket,
                COUNT(*) AS count,AVG(json_extract(payload,?)) AS mean,
                MIN(json_extract(payload,?)) AS minimum,MAX(json_extract(payload,?)) AS maximum
                FROM events WHERE device=? AND kind='measurement' AND source=?
                AND observed>=? AND observed<? AND json_extract(payload,?) IS NOT NULL
                GROUP BY bucket ORDER BY bucket""",
                (bucket_seconds, bucket_seconds, *(["$.values."+metric]*3), device_id,
                 source, start, end, "$.values."+metric)).fetchall()
        return {"source": source, "metric": metric, "bucket_seconds": bucket_seconds,
                "items": [dict(r) for r in rows]}

    @app.get("/v1/latest", dependencies=[Depends(session_auth)])
    def latest():
        with database(store.path) as db:
            rows = db.execute("""SELECT payload,received FROM (
                SELECT *,ROW_NUMBER() OVER(PARTITION BY kind,source ORDER BY observed DESC,id DESC) AS n
                FROM events WHERE device=? AND ((kind='photo' AND observed>=?) OR
                (kind='measurement' AND observed>=?))) WHERE n=1""",
                              (device_id, time.time()-30*DAY, time.time()-90*DAY)).fetchall()
            seen = db.execute("SELECT last_seen FROM devices WHERE id=?", (device_id,)).fetchone()
        return {"device_id": device_id, "last_seen": seen[0] if seen else None,
                "items": [{**json.loads(r[0]), "received_at": r[1]} for r in rows]}

    @app.get("/v1/photos/{event_id}", dependencies=[Depends(session_auth)])
    def get_photo(event_id: UUID):
        with store.lock, database(store.path) as db:
            row = db.execute("SELECT filename FROM events WHERE device=? AND id=? AND kind='photo' AND observed>=?",
                             (device_id, str(event_id), time.time()-30*DAY)).fetchone()
            if not row:
                raise HTTPException(404, "photo not found")
            try:
                data = (store.photos / row[0]).read_bytes()
            except FileNotFoundError:
                raise HTTPException(503, "photo temporarily unavailable") from None
        return Response(data, media_type="image/jpeg")

    @app.get("/healthz")
    def health():
        with database(store.path) as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    # Event is parsed separately for partial batches; explicitly include its schema.
    base_openapi = app.openapi
    def openapi():
        schema = base_openapi()
        schema.setdefault("components", {}).setdefault("schemas", {})["Event"] = Event.model_json_schema()
        return schema
    app.openapi = openapi
    return app
