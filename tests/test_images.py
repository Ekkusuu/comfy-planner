"""Image thumbnails, deleting images, and workflow covers over the HTTP API."""
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.db import Database
from app.main import create_app
from tests.conftest import FIXTURES, TAGWF


@pytest.fixture
def client(config, comfy):
    with TestClient(create_app(config, comfy=comfy, start_worker=False)) as c:
        yield c


def make_images(client, config, sizes=((800, 600), (600, 800))) -> tuple[str, list[int]]:
    """Import a workflow, make one job with a finished image per size, return (workflow id, image ids)."""
    wf = client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{TAGWF}.json")}).json()
    job = client.post("/api/jobs", json={"workflow_id": wf["id"], "count": len(sizes), "fields": {"prompts": ["a"]}}).json()
    store = client.app.state.store
    for image, size in zip(job["images"], sizes):
        rel = f"2026-01-01/{job['id']}-{image['idx']}.png"
        (config.images_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (110, 231, 183)).save(config.images_dir / rel)
        store.set_image(image["id"], "done", file_path=rel, recipe=image["recipe"])
    return wf["id"], [i["id"] for i in job["images"]]


def test_thumb_is_cached_clamped_and_regenerated(client, config):
    _, (first, _) = make_images(client, config)
    res = client.get(f"/api/images/{first}/thumb")
    assert res.status_code == 200 and res.headers["content-type"] == "image/webp"
    cached = config.thumbs_dir / f"{first}-512.webp"
    assert cached.is_file()
    with Image.open(cached) as img:
        assert img.size == (512, 384)           # longest edge 512, aspect kept

    assert client.get(f"/api/images/{first}/thumb?size=100000").status_code == 200
    assert (config.thumbs_dir / f"{first}-1024.webp").is_file()
    assert client.get(f"/api/images/{first}/thumb?size=1").status_code == 200
    assert (config.thumbs_dir / f"{first}-64.webp").is_file()

    cached.unlink()
    assert client.get(f"/api/images/{first}/thumb").status_code == 200
    assert cached.is_file()
    assert client.get("/api/images/999/thumb").status_code == 404


def test_thumb_without_file_is_404(client, config):
    wf = client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{TAGWF}.json")}).json()
    job = client.post("/api/jobs", json={"workflow_id": wf["id"], "fields": {"prompts": ["a"]}}).json()
    assert client.get(f"/api/images/{job['images'][0]['id']}/thumb").status_code == 404


def test_delete_image_removes_row_file_thumbs_and_cover_pin(client, config):
    wf_id, (first, second) = make_images(client, config)
    path = config.images_dir / client.get(f"/api/images/{first}").json()["file_path"]
    client.get(f"/api/images/{first}/thumb?size=256")
    assert client.put(f"/api/workflows/{wf_id}/cover", json={"image_id": first}).json()["cover_image_id"] == first

    assert client.delete(f"/api/images/{first}").json() == {"deleted": first}
    assert client.get(f"/api/images/{first}").status_code == 404
    assert not path.exists() and not list(config.thumbs_dir.glob(f"{first}-*"))
    assert [i["id"] for i in client.get("/api/images").json()] == [second]
    wf = next(w for w in client.get("/api/workflows").json() if w["id"] == wf_id)
    assert wf["cover_pinned"] is False and wf["cover_image_id"] == second   # pin cleared, newest image shown
    assert client.get(f"/api/jobs/{client.get(f'/api/images/{second}').json()['job_id']}").status_code == 200  # job stays
    assert client.delete(f"/api/images/{first}").status_code == 404


def test_delete_refuses_unfinished_images(client, config):
    wf = client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{TAGWF}.json")}).json()
    job = client.post("/api/jobs", json={"workflow_id": wf["id"], "count": 2, "queue": True, "fields": {"prompts": ["a"]}}).json()
    pending, running = (i["id"] for i in job["images"])
    client.app.state.store.set_image(running, "running")
    for image_id in (pending, running):
        res = client.delete(f"/api/images/{image_id}")
        assert res.status_code == 409 and "finished" in res.json()["detail"]
        assert client.get(f"/api/images/{image_id}").status_code == 200


def test_cover_defaults_to_newest_pins_and_validates(client, config):
    wf_id, (first, second) = make_images(client, config)
    other = client.post("/api/workflows/import", json={"path": str(FIXTURES / "Subgraph T2I - natural language.json")}).json()

    listed = {w["id"]: w for w in client.get("/api/workflows").json()}
    assert listed[other["id"]]["cover_image_id"] is None and listed[other["id"]]["cover_pinned"] is False
    assert listed[wf_id]["cover_image_id"] in (first, second) and listed[wf_id]["cover_pinned"] is False

    pinned = client.put(f"/api/workflows/{wf_id}/cover", json={"image_id": first}).json()
    assert pinned["cover_image_id"] == first and pinned["cover_pinned"] is True
    assert next(w for w in client.get("/api/workflows").json() if w["id"] == wf_id)["cover_image_id"] == first

    # an image from another workflow, a missing image and an unfinished one are refused
    assert client.put(f"/api/workflows/{other['id']}/cover", json={"image_id": first}).status_code == 422
    assert client.put(f"/api/workflows/{wf_id}/cover", json={"image_id": 999}).status_code == 404
    job = client.post("/api/jobs", json={"workflow_id": wf_id, "fields": {"prompts": ["a"]}}).json()
    assert client.put(f"/api/workflows/{wf_id}/cover", json={"image_id": job["images"][0]["id"]}).status_code == 422
    assert client.put("/api/workflows/nope/cover", json={"image_id": None}).status_code == 404

    unpinned = client.put(f"/api/workflows/{wf_id}/cover", json={"image_id": None}).json()
    assert unpinned["cover_pinned"] is False and unpinned["cover_image_id"] in (first, second)


def test_cover_column_is_added_to_an_older_database(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE workflows (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
    old.commit()
    old.close()
    db = Database(path)
    assert "cover_image_id" in {r["name"] for r in db.conn.execute("PRAGMA table_info(workflows)")}
    db.close()
