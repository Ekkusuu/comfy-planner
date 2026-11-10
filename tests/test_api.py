from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import FIXTURES, TAGWF, TAGWF_MISSING


def test_api_import_create_queue_and_errors(config, comfy):
    with TestClient(create_app(config, comfy=comfy, start_worker=False)) as client:
        health = client.get("/api/health").json()
        assert health["app"] == "ok" and health["comfy"]["reachable"] is True

        tagwf = client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{TAGWF}.json")}).json()
        assert tagwf["runnable"] == 1 and tagwf["slots"]["hires"]["seed_offset"] == 1000
        original = client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{TAGWF_MISSING}.json")}).json()
        assert original["runnable"] == 0 and original["missing"]
        assert len(client.get("/api/workflows").json()) == 2

        bad = client.post("/api/workflows/import", json={"path": "C:/does/not/exist.json"})
        assert bad.status_code == 422 and "not found" in bad.json()["detail"].lower()

        job = client.post("/api/jobs", json={"workflow_id": tagwf["id"], "count": 2, "queue": True,
                                             "fields": {"prompts": ["a", "b"], "cfg": 5}}).json()
        assert job["status"] == "queued" and len(job["images"]) == 2
        assert client.get("/api/queue").json()["jobs"][0]["id"] == job["id"]
        assert client.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
        assert client.get("/api/jobs/999").status_code == 404
        refused = client.post("/api/jobs", json={"workflow_id": original["id"], "fields": {"prompts": ["a"]}})
        assert refused.status_code == 400 and "not runnable" in refused.json()["detail"]
        assert client.post(f"/api/jobs/{job['id']}/retry", json={"mode": "failed"}).json()["status"] == "queued"
        assert client.put("/api/queue/order", json={"job_ids": [job["id"]]}).status_code == 200


def test_api_with_comfy_down(config, comfy):
    comfy.up = False
    with TestClient(create_app(config, comfy=comfy, start_worker=False)) as client:
        health = client.get("/api/health").json()
        assert health["comfy"]["reachable"] is False and health["comfy"]["error"]
