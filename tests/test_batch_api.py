"""The batch as a whole: Run all, cancel, clear and remove for the jobs one request made."""
from tests.test_planning_flow import app  # noqa: F401  (the planning app fixture)


def batch(app):
    """A request that made three jobs of 2, 2 and 1 images."""
    app.script_plan({"description": "a cat", "count": 2}, {"description": "a dog", "count": 2}, {"description": "a bird", "count": 1})
    plan = app.plan("pets", on="tagwf")
    return plan, [app.job(i) for i in plan["job_ids"]]


def statuses(app, ids):
    return [app.job(i)["status"] for i in ids]


def test_jobs_list_carries_the_batch_and_its_request(app):
    plan, jobs = batch(app)
    listed = {j["id"]: j for j in app.client.get("/api/jobs").json()}
    assert {listed[j["id"]]["batch_id"] for j in jobs} == {plan["id"]} and {listed[j["id"]]["batch_request"] for j in jobs} == {"pets"}
    assert listed[jobs[0]["id"]]["image_counts"] == {"pending": 2}


def test_run_all_queues_the_ready_jobs_and_counts_what_it_skipped(app):
    plan, jobs = batch(app)
    db = app.state.store.db
    db.execute("UPDATE jobs SET status = 'draft', error = 'Planning failed' WHERE id = ?", (jobs[1]["id"],))                  # not planned
    db.execute("UPDATE jobs SET issues = ? WHERE id = ?", ('[{"field": "prompts", "severity": "blocking", "code": "x", "message": "m"}]', jobs[2]["id"]))
    assert app.client.post(f"/api/batches/{plan['id']}/queue").json() == {"queued": 1, "skipped": 2}
    assert statuses(app, [j["id"] for j in jobs]) == ["queued", "draft", "ready"]
    assert app.client.post("/api/batches/9999/queue").status_code == 404


def test_cancel_clear_and_remove(app):
    plan, jobs = batch(app)
    ids = [j["id"] for j in jobs]
    assert app.client.delete(f"/api/batches/{plan['id']}").json() == {"deleted": 3}
    assert app.client.delete(f"/api/batches/{plan['id']}").status_code == 404                    # the batch is gone
    plan, jobs = batch(app)
    ids = [j["id"] for j in jobs]
    assert app.client.post(f"/api/batches/{plan['id']}/queue").json() == {"queued": 3, "skipped": 0}
    assert statuses(app, ids) == ["queued"] * 3
    assert app.client.delete(f"/api/batches/{plan['id']}").status_code == 400                    # queued jobs stay
    assert app.client.post(f"/api/batches/{plan['id']}/clear").json() == {"cleared": 0}          # nothing finished yet
    assert app.client.post(f"/api/batches/{plan['id']}/cancel").json() == {"cancelled": 3}
    assert statuses(app, ids) == ["cancelled"] * 3
    assert app.client.post(f"/api/batches/{plan['id']}/cancel").json() == {"cancelled": 0}
    assert app.client.post(f"/api/batches/{plan['id']}/clear").json() == {"cleared": 3}
    assert all(app.job(i)["cleared_at"] for i in ids)
    assert app.client.post("/api/batches/9999/cancel").status_code == 404
