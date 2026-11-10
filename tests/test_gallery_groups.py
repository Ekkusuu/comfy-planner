"""Gallery folders: a batch is one folder, a hand-made job with several images too, single images stay single;
the folder's own listing, deleting a folder, and data that was made before batches existed."""
import sqlite3

from app.config import Config
from app.db import Database, dumps
from app.queue.groups import Groups
from app.queue.jobs import JobStore
from tests.conftest import PNG
from tests.test_planning_flow import app  # noqa: F401  (the planning app fixture)


def finish(app, job_id, stamp):
    """Pretend ComfyUI made every image of the job (a file on disk, status done)."""
    store = app.state.store
    for n, image in enumerate(store.get_job(job_id)["images"]):
        path = f"2026-11-01/{job_id}-{image['idx']}.png"
        (store.config.images_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (store.config.images_dir / path).write_bytes(PNG)
        store.set_image(image["id"], "done", file_path=path)
        store.db.execute("UPDATE images SET finished_at = ? WHERE id = ?", (f"2026-11-01T10:{stamp:02d}:{n:02d}+00:00", image["id"]))
    store.db.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,))


def make_world(app):
    """A batch of two jobs (2 + 3 images), a request with one image, a hand-made job with 3 images, a hand-made single image."""
    app.script_plan({"description": "knights", "count": 2}, {"description": "dragons", "count": 3})
    batch = app.plan("2 knights and 3 dragons", on="tagwf")
    app.script_plan({"description": "a lone cat", "count": 1})
    single = app.plan("a cat", on="tagwf")
    manual3 = app.client.post("/api/jobs", json={"workflow_id": app.ids["tagwf"], "count": 3, "description": "three by hand", "fields": {"prompts": ["a", "b", "c"]}}).json()
    manual1 = app.client.post("/api/jobs", json={"workflow_id": app.ids["tagwf"], "fields": {"prompts": ["solo"]}}).json()
    for stamp, job_id in enumerate([*batch["job_ids"], single["job_ids"][0], manual3["id"], manual1["id"]], 1):
        finish(app, job_id, stamp)
    return batch, single, manual3, manual1


def test_a_batch_is_one_folder_and_single_images_stay_single(app):
    batch, single, manual3, manual1 = make_world(app)
    res = app.client.get("/api/gallery").json()
    kinds = [(e["type"], e.get("key") or e["job_id"]) for e in res["entries"]]
    assert kinds == [("image", app.job(manual1["id"])["images"][0]["job_id"]), ("group", f"j{manual3['id']}"),
                     ("image", single["job_ids"][0]), ("group", f"b{batch['id']}")]            # newest first
    assert res["total"] == 4 and res["images"] == 5 + 1 + 3 + 1
    folder = res["entries"][3]
    assert folder["count"] == 5 and folder["request"] == "2 knights and 3 dragons" and folder["title"] == "2 knights and 3 dragons"
    by_hand = res["entries"][1]
    assert by_hand["count"] == 3 and by_hand["title"] == "three by hand"
    # a single image entry is the full image (the viewer needs its recipe)
    image = res["entries"][0]
    assert image["recipe"]["prompt"] == "solo" and image["workflow_id"] == app.ids["tagwf"] and image["status"] == "done"


def test_the_cover_is_the_first_image_or_the_first_favourite(app):
    batch, *_ = make_world(app)
    jobs = [app.job(i) for i in batch["job_ids"]]
    group = lambda: next(e for e in app.client.get("/api/gallery").json()["entries"] if e["type"] == "group" and e["key"] == f"b{batch['id']}")
    assert group()["cover_image_id"] == jobs[0]["images"][0]["id"]
    app.state.store.db.execute("UPDATE images SET rating = 1 WHERE id = ?", (jobs[1]["images"][1]["id"],))
    assert group()["cover_image_id"] == jobs[1]["images"][1]["id"]
    app.state.store.db.execute("UPDATE images SET rating = 1 WHERE id = ?", (jobs[0]["images"][1]["id"],))
    assert group()["cover_image_id"] == jobs[0]["images"][1]["id"]                              # the first favourite
    assert group()["cover_ratio"] == jobs[0]["images"][1]["recipe"]["width"] / jobs[0]["images"][1]["recipe"]["height"]


def test_unfinished_images_are_not_in_a_folder(app):
    app.script_plan({"description": "knights", "count": 3})
    batch = app.plan("3 knights", on="tagwf")
    job_id = batch["job_ids"][0]
    store = app.state.store
    images = store.get_job(job_id)["images"]
    store.set_image(images[0]["id"], "done", file_path="x/1.png")
    store.set_image(images[1]["id"], "failed", error="boom")
    group = app.client.get(f"/api/gallery/groups/b{batch['id']}").json()
    assert group["count"] == 1 and [i["id"] for i in group["images"]] == [images[0]["id"]]
    assert app.client.get("/api/gallery").json()["entries"][0]["count"] == 1


def test_a_folder_lists_its_images_in_plan_order(app):
    batch, single, manual3, _ = make_world(app)
    group = app.client.get(f"/api/gallery/groups/b{batch['id']}").json()
    assert group["key"] == f"b{batch['id']}" and group["count"] == 5 and group["request"] == "2 knights and 3 dragons"
    assert [(i["job_id"], i["idx"]) for i in group["images"]] == [(batch["job_ids"][0], 1), (batch["job_ids"][0], 2),
                                                                  (batch["job_ids"][1], 1), (batch["job_ids"][1], 2), (batch["job_ids"][1], 3)]
    assert all(i["recipe"] and i["workflow_id"] for i in group["images"])
    by_hand = app.client.get(f"/api/gallery/groups/j{manual3['id']}").json()
    assert by_hand["count"] == 3 and by_hand["title"] == "three by hand"
    for key in ("b9999", "j9999", "x1", "b", "b-1", "bx"):
        assert app.client.get(f"/api/gallery/groups/{key}").status_code == 404


def test_deleting_a_folder_deletes_its_images_and_files_only(app):
    batch, single, manual3, manual1 = make_world(app)
    store = app.state.store
    files = [store.config.images_dir / i["file_path"] for i in app.client.get(f"/api/gallery/groups/b{batch['id']}").json()["images"]]
    assert all(f.is_file() for f in files)
    other = store.get_job(single["job_ids"][0])["images"][0]
    assert app.client.delete(f"/api/gallery/groups/b{batch['id']}").json() == {"deleted": 5}
    assert not any(f.exists() for f in files) and app.client.get(f"/api/gallery/groups/b{batch['id']}").json()["count"] == 0
    assert store.get_image(other["id"]) and (store.config.images_dir / store.get_image(other["id"])["file_path"]).is_file()
    assert [e["type"] for e in app.client.get("/api/gallery").json()["entries"]] == ["image", "group", "image"]
    assert app.client.delete("/api/gallery/groups/b9999").status_code == 404
    assert len(app.client.get(f"/api/jobs/{batch['job_ids'][0]}").json()["images"]) == 0        # the job stays, without its images


def test_data_made_before_batches_still_shows(tmp_path):
    """A database from before jobs.batch_id existed: it is migrated, nothing is lost, and its images show as before."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT, description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            count INTEGER NOT NULL, fields TEXT NOT NULL, position REAL NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, started_at TEXT,
            finished_at TEXT, plan_id INTEGER, issues TEXT NOT NULL DEFAULT '[]', meta TEXT NOT NULL DEFAULT '{}', cleared_at TEXT);
        CREATE TABLE images (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, idx INTEGER NOT NULL, status TEXT NOT NULL, seed INTEGER NOT NULL,
            prompt_id TEXT, file_path TEXT, recipe TEXT NOT NULL, error TEXT, rating INTEGER, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            UNIQUE (job_id, idx));
    """)
    prompts = dumps({"prompts": {"value": ["old prompt"], "source": "auto", "reason": ""}})
    old.execute("INSERT INTO jobs (id, description, status, count, fields, created_at, plan_id, meta) VALUES (1, 'old planned', 'done', 2, ?, 'x', 7, ?)",
                (prompts, dumps({"request": "two old things"})))
    old.execute("INSERT INTO jobs (id, description, status, count, fields, created_at) VALUES (2, '', 'done', 1, ?, 'x')", (prompts,))
    for job_id, idx, stamp in ((1, 1, "10"), (1, 2, "11"), (2, 1, "12")):
        old.execute("INSERT INTO images (job_id, idx, status, seed, file_path, recipe, created_at, finished_at) VALUES (?,?,?,?,?,?,?,?)",
                    (job_id, idx, "done", 1, f"d/{job_id}-{idx}.png", dumps({"prompt": "old prompt"}), "x", f"2026-10-01T10:{stamp}:00+00:00"))
    old.commit()
    old.close()
    db = Database(path)
    assert "batch_id" in {r["name"] for r in db.conn.execute("PRAGMA table_info(jobs)")}
    store = JobStore(db, Config(data_dir=tmp_path), library=None)
    jobs = store.list_jobs()
    assert [(j["id"], j["batch_id"], j["batch_request"]) for j in jobs] == [(1, None, None), (2, None, None)]
    assert jobs[0]["image_counts"] == {"done": 2} and jobs[0]["meta"] == {"request": "two old things"}
    groups = Groups(store)
    res = groups.gallery()
    assert [(e["type"], e.get("key")) for e in res["entries"]] == [("image", None), ("group", "j1")]       # a job with two images is a folder
    assert res["entries"][1]["title"] == "two old things" and res["entries"][1]["count"] == 2 and res["entries"][1]["cover_ratio"] is None
    assert res["entries"][0]["recipe"] == {"prompt": "old prompt"}
    assert groups.get("j1")["count"] == 2
    Database(path).close()          # opening it again changes nothing
    db.close()
