import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from monitoring.agent import event, read_sources, Uploader
from monitoring.common import database, digest
from monitoring.server import create_app, DAY
from monitoring.spool import Spool


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "server", "home", digest("d"*40), digest("a"*40), ["https://ui.example"])
    with TestClient(app, base_url="https://api.example") as client:
        yield client, app.state.store


def sample(kind="measurement", **kwargs):
    return {**event("home", kind, "cpu" if kind == "measurement" else "camera",
                    {"cpu_temperature_c": 35.9} if kind == "measurement" else {}, synchronized=True), **kwargs}


def jpeg():
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), "white").save(stream, format="JPEG")
    return stream.getvalue()


DEVICE = {"Authorization": "Bearer " + "d"*40}


def login(client):
    response = client.post("/v1/session", json={"key": "a"*40})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["session_key"]}


def test_auth_and_session_lifecycle(api):
    client, store = api
    assert client.get("/v1/latest").status_code == 401
    assert client.get("/v1/latest", headers=DEVICE).status_code == 401
    headers = login(client)
    assert client.get("/v1/latest", headers=headers).status_code == 200
    assert client.post("/v1/measurements", headers=headers, json={"events": [sample()]}).status_code == 401
    assert client.delete("/v1/session", headers=headers).status_code == 204
    assert client.get("/v1/latest", headers=headers).status_code == 401
    headers = login(client)
    with database(store.path) as db:
        db.execute("UPDATE sessions SET expires=0")
    assert client.get("/v1/latest", headers=headers).status_code == 401


def test_cookie_csrf_and_cors(api):
    client, _ = api
    login(client)
    assert client.delete("/v1/session").status_code == 403
    assert client.delete("/v1/session", headers={"Origin": "https://evil.example", "X-CSRF-Token": "1"}).status_code == 403
    assert client.delete("/v1/session", headers={"Origin": "https://ui.example", "X-CSRF-Token": "1"}).status_code == 204
    assert client.post("/v1/session", json={"key": "a"*40}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_partial_batch_duplicates_conflict_and_time(api):
    client, _ = api
    item = sample()
    def send(items):
        return client.post("/v1/measurements", headers=DEVICE, json={"events": items}).json()["results"]
    assert [r["status"] for r in send([item, {"event_id": "broken"}])] == ["stored", "invalid"]
    assert send([item])[0]["status"] == "duplicate"
    assert send([{**item, "values": {"cpu_temperature_c": 99}}])[0]["status"] == "conflict"
    assert send([sample(device_id="other")])[0]["status"] == "invalid"
    assert send([sample(observed_at=time.time()+600)])[0]["status"] == "invalid_time"
    assert send([sample(observed_at=time.time()-91*DAY)])[0]["status"] == "expired"
    headers = login(client)
    assert len(client.get("/v1/measurements", headers=headers).json()["items"]) == 1


def test_photo_atomic_retry_auth_and_validation(api):
    client, store = api
    item, data = sample("photo"), jpeg()
    headers = {**DEVICE, "Content-Type": "image/jpeg", "X-Event": json.dumps(item)}
    url = "/v1/photos/" + item["event_id"]
    assert client.post("/v1/photos", headers=headers, content=data).json()["status"] == "stored"
    assert client.get(url).status_code == 401
    # Simulate a missing file after restore; a retry repairs it.
    next(store.photos.glob("*.jpg")).unlink()
    assert client.post("/v1/photos", headers=headers, content=data).json()["status"] == "duplicate"
    reader = login(client)
    assert client.get(url, headers=reader).content == data
    assert client.post("/v1/photos", headers=headers, content=b"not jpeg").status_code == 422
    assert client.post("/v1/photos", headers=headers, content=b"x"*(4*1024**2+1)).status_code == 413


def test_stable_pagination_and_latest_uses_capture_time(api):
    client, _ = api
    timestamp = time.time()-100
    items = [sample(observed_at=timestamp+i) for i in range(5)]
    client.post("/v1/measurements", headers=DEVICE, json={"events": list(reversed(items))})
    reader = login(client)
    found, cursor = [], None
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/v1/measurements", headers=reader, params=params).json()
        found += [x["event_id"] for x in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert found == [x["event_id"] for x in items]
    assert client.get("/v1/latest", headers=reader).json()["items"][0]["event_id"] == items[-1]["event_id"]
    assert client.get("/v1/measurements?cursor=bad", headers=reader).status_code == 422


def test_server_retention(api):
    client, store = api
    item = sample("photo", observed_at=time.time()-29*DAY)
    client.post("/v1/photos", headers={**DEVICE, "Content-Type": "image/jpeg", "X-Event": json.dumps(item)}, content=jpeg())
    client.post("/v1/measurements", headers=DEVICE, json={"events": [sample(observed_at=time.time()-89*DAY)]})
    store.cleanup(time.time()+2*DAY)
    with database(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert not list(store.photos.glob("*.jpg"))


def test_queue_survives_restart_and_ack(tmp_path):
    spool = Spool(tmp_path, reserve_bytes=0)
    item = sample("photo")
    spool.enqueue(item, jpeg())
    spool = Spool(tmp_path, reserve_bytes=0)
    assert spool.batch("photo")[0] == (item, jpeg())
    spool.acknowledge([item["event_id"]])
    assert spool.status()["queued"] == 0


def test_queue_bounds_expiration_and_backward_clock(tmp_path):
    spool = Spool(tmp_path, max_bytes=1800, reserve_bytes=0)
    for i in range(20):
        spool.enqueue(sample("photo"), jpeg(), now=1000+i)
    assert spool.status()["bytes"] <= 1800
    assert spool.status()["dropped"] > 0
    spool.prune(now=90000)
    assert spool.status()["queued"] == 0
    spool.enqueue(sample(), now=90000)
    spool.prune(now=1000)
    assert spool.status()["queued"] == 0


def test_queue_concurrent_producers(tmp_path):
    spool = Spool(tmp_path, reserve_bytes=0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: spool.enqueue(sample()), range(50)))
    assert spool.status()["queued"] == 50


def test_offline_retry_lost_ack_end_to_end(api, tmp_path):
    client, _ = api
    spool = Spool(tmp_path / "queue", reserve_bytes=0)
    token = tmp_path / "token"
    token.write_text("d"*40)
    spool.enqueue(sample())
    spool.enqueue(sample("photo"), jpeg())
    uploader = Uploader({"server_url": "https://api.example", "token_file": str(token)}, spool)
    lose_response = True
    def request(path, data, headers):
        nonlocal lose_response
        response = client.post(path, content=data, headers={**DEVICE, **headers})
        assert response.status_code == 200
        if lose_response:
            lose_response = False
            raise TimeoutError()
        return response.json()
    uploader.request = request
    with pytest.raises(TimeoutError):
        uploader.send_once()
    assert spool.status()["queued"] == 2
    uploader.send_once()
    assert spool.status()["queued"] == 0
    reader = login(client)
    assert len(client.get("/v1/measurements", headers=reader).json()["items"]) == 1
    assert len(client.get("/v1/photos", headers=reader).json()["items"]) == 1


def test_source_error_is_not_zero(tmp_path):
    config = {"device_id": "home", "sources": [{"name": "cpu", "fields": {
        "cpu_temperature_c": {"path": str(tmp_path / "missing"), "scale": .001}}}]}
    item = next(read_sources(config))
    assert item["status"] == "error"
    assert item["values"] == {}


def test_rate_limit_login(api):
    client, _ = api
    for _ in range(10):
        assert client.post("/v1/session", json={"key": "z"*40}).status_code == 401
    assert client.post("/v1/session", json={"key": "z"*40}).status_code == 429


def test_aggregation_excludes_missing_and_obeys_limit(api):
    client, _ = api
    now = int(time.time() // 3600) * 3600
    items = [sample(observed_at=now-3000, values={"cpu_temperature_c": 20}),
             sample(observed_at=now-2000, values={"cpu_temperature_c": 40}),
             sample(observed_at=now-1000, values={}, status="error")]
    client.post("/v1/measurements", headers=DEVICE, json={"events": items})
    headers = login(client)
    params = dict(start=now-3600, end=now, source="cpu", metric="cpu_temperature_c")
    result = client.get("/v1/measurements/aggregate", params=params, headers=headers)
    assert result.status_code == 200
    assert result.json()["items"][0]["mean"] == 30
    assert result.json()["items"][0]["count"] == 2
    params.update(start=now-89*DAY, bucket_seconds=600)
    assert client.get("/v1/measurements/aggregate", params=params, headers=headers).status_code == 422


def test_backup_restores_database_and_photos(api, tmp_path):
    import tarfile
    from monitoring.admin import backup, provision
    client, store = api
    item = sample("photo")
    client.post("/v1/photos", headers={**DEVICE, "Content-Type": "image/jpeg", "X-Event": json.dumps(item)}, content=jpeg())
    target = tmp_path / "backup.tar.gz"
    backup(store.directory, target)
    restored = tmp_path / "restored"
    with tarfile.open(target) as archive:
        archive.extractall(restored, filter="data")
    with database(restored / "server.db") as db:
        filename = db.execute("SELECT filename FROM events").fetchone()[0]
    assert (restored / "photos" / filename).read_bytes() == jpeg()
    provision(tmp_path / "secrets")
    with pytest.raises(ValueError):
        provision(tmp_path / "secrets")
    assert (tmp_path / "secrets" / "admin.key").stat().st_mode & 0o777 == 0o600


def test_route_selects_only_physical_interface():
    from monitoring.routing import desired_route
    routes = [{"dst": "default", "dev": "Meta", "gateway": "198.18.0.1", "metric": 0},
              {"dst": "default", "dev": "wlan0", "gateway": "192.168.1.1", "metric": 10}]
    assert desired_route(routes, "wlan0")["gateway"] == "192.168.1.1"
    assert desired_route(routes, "eth0") is None


def test_rejected_measurement_does_not_block_photo(tmp_path):
    spool = Spool(tmp_path / "spool", reserve_bytes=0)
    measurement, photo = sample(), sample("photo")
    spool.enqueue(measurement)
    spool.enqueue(photo, jpeg())
    token = tmp_path / "token"
    token.write_text("d"*40)
    uploader = Uploader({"server_url": "https://api.example", "token_file": str(token)}, spool)
    def request(path, data, headers):
        if path == "/v1/measurements":
            return {"results": [{"event_id": measurement["event_id"], "status": "invalid"}]}
        return {"event_id": photo["event_id"], "status": "stored"}
    uploader.request = request
    with pytest.raises(ValueError):
        uploader.send_once()
    assert spool.status()["queued"] == 1
    assert spool.batch("measurement")[0][0]["event_id"] == measurement["event_id"]
    assert not spool.batch("photo")


def test_capture_accepts_camera_nul_padding_but_not_truncation(monkeypatch):
    import subprocess
    from monitoring.agent import capture
    config = {"camera": {"device": "/dev/test", "input_format": "mjpeg"}}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, jpeg()+b"\x00\x00"))
    assert capture(config) == jpeg()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, jpeg()[:-2]))
    with pytest.raises(ValueError):
        capture(config)


def load_deploy_script(name):
    """deploy/ scripts are hyphenated commands, not importable module names."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "deploy" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backup_rejects_object_keys_that_escape_the_directory():
    safe_key = load_deploy_script("backup-cloud").safe_key
    assert safe_key("home/6a5d940b.jpg") == "home/6a5d940b.jpg"
    for key in ["/etc/passwd", "../../secrets/admin.key", "home/../../x.jpg", "home\\x.jpg", ""]:
        with pytest.raises(ValueError):
            safe_key(key)


def test_restore_comparison_ignores_json_number_spelling():
    normalise = load_deploy_script("restore-cloud").normalise
    # D1 returns a whole REAL as an int; SQLite returns a float. Same row.
    assert normalise([("home", 1789385121.0, 1)]) == normalise([("home", 1789385121, 1)])
    assert normalise([("home", 1.5)]) != normalise([("home", 1.6)])


def test_restore_refuses_to_overwrite_the_source_resources(tmp_path):
    restore_cloud = load_deploy_script("restore-cloud")
    manifest = {"database": "prod", "bucket": "prod-photos", "d1": {"bytes": 0, "sha256": ""}, "objects": []}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "d1.sql").write_bytes(b"")
    for database_name, bucket in [("prod", "other"), ("other", "prod-photos")]:
        with pytest.raises(ValueError, match="must differ"):
            restore_cloud.restore(tmp_path, tmp_path, database_name, bucket, False)
