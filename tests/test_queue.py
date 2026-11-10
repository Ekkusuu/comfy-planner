import asyncio

import pytest

from app.comfy.client import ComfyError, ComfyInterrupted
from app.queue.jobs import JobError
from tests.conftest import KREA, QWEN, TAGWF, TAGWF_MISSING, import_fixture

PROMPT = "masterpiece, 1girl, solo, smile, outdoors"


@pytest.fixture
async def tagwf(library):
    return await import_fixture(library, TAGWF)


def make_job(store, wf_id, count=1, queue=False, **fields):
    return store.create_job(wf_id, count, {"prompts": [PROMPT], "seed": 100, "seed_strategy": "increment", **fields}, queue=queue)


async def run_until_idle(worker):
    """Run worker ticks until no job is queued or running."""
    for _ in range(50):
        if worker.store.next_queued() is None:
            return
        await worker.tick()


# ---------- import / registry ----------

async def test_import_marks_runnable_and_missing(library):
    tagwf = await import_fixture(library, TAGWF)
    assert tagwf["runnable"] == 1 and tagwf["kind"] == "t2i" and tagwf["missing"] == []
    original = await import_fixture(library, TAGWF_MISSING)
    assert original["runnable"] == 0 and {m["value"] for m in original["missing"]} >= {"upscaler_4x_a.pth"}
    qwen = await import_fixture(library, QWEN)
    assert qwen["runnable"] == 0 and qwen["kind"] == "edit" and len(qwen["missing"]) == 4
    assert [w["id"] for w in library.list()] == sorted([tagwf["id"], original["id"], qwen["id"]],
                                                        key=lambda i: library.get(i)["title"])


async def test_import_is_idempotent_and_keeps_confirmed_slots(library):
    first = await import_fixture(library, TAGWF)
    again = await import_fixture(library, TAGWF)
    assert first["id"] == again["id"] and len(library.list()) == 1
    edited = {**again["slots"], "steps": {"node": "47", "input": "steps"}}
    library.update_slots(first["id"], edited)
    assert (await import_fixture(library, TAGWF))["slots"]["steps"]["node"] == "47"      # confirmed map survives re-import


async def test_update_slots_rejects_unknown_node(library):
    wf = await import_fixture(library, TAGWF)
    bad = {**wf["slots"], "seed": {"node": "999", "input": "seed"}}
    with pytest.raises(Exception):
        library.update_slots(wf["id"], bad)


# ---------- job creation ----------

async def test_create_job_builds_one_recipe_per_image(store, tagwf):
    job = store.create_job(tagwf["id"], 3, {"prompts": ["a", "b", "c"], "seed": 10, "seed_strategy": "increment", "steps": 999,
                                             "cfg": 6, "width": 700, "loras": [{"name": "model_styles_anima-base1_v6.safetensors", "weight": 0.8}]})
    assert job["status"] == "ready" and job["fields"]["steps"]["source"] == "locked"
    assert job["fields"]["steps"]["value"] == 200 and "clamped" in job["fields"]["steps"]["note"]
    images = job["images"]
    assert [i["recipe"]["prompt"] for i in images] == ["a", "b", "c"]
    assert [i["seed"] for i in images] == [10, 11, 12]
    assert images[0]["recipe"]["width"] == 704 and images[0]["recipe"]["loras"][0]["weight"] == 0.8
    assert images[0]["recipe"]["hires"] is True and images[0]["recipe"]["negative"].startswith("lowres")   # workflow defaults filled in


async def test_create_job_validation(store, library, tagwf, oi):
    with pytest.raises(JobError, match="At least one prompt"):
        store.create_job(tagwf["id"], 1, {})
    with pytest.raises(JobError, match="exactly 3"):
        store.create_job(tagwf["id"], 3, {"prompts": ["a", "b"]})
    with pytest.raises(JobError, match="between 1 and 16"):
        store.create_job(tagwf["id"], 17, {"prompts": ["a"]})
    with pytest.raises(JobError, match="At most 3"):
        store.create_job(tagwf["id"], 1, {"prompts": ["a"], "loras": [{"name": str(i)} for i in range(4)]})
    with pytest.raises(JobError, match="not found in ComfyUI"):
        store.create_job(tagwf["id"], 1, {"prompts": ["a"], "loras": [{"name": "nope.safetensors"}]}, object_info=oi)
    with pytest.raises(JobError, match="sampler 'bogus'"):
        store.create_job(tagwf["id"], 1, {"prompts": ["a"], "sampler": "bogus"}, object_info=oi)
    original = await import_fixture(library, TAGWF_MISSING)
    with pytest.raises(JobError, match="not runnable"):
        store.create_job(original["id"], 1, {"prompts": ["a"]})


# ---------- states, order, cancel, retry ----------

async def test_state_transitions_and_run(store, worker, tagwf, config):
    job = make_job(store, tagwf["id"], count=2)
    assert job["status"] == "ready"
    assert store.queue_job(job["id"])["status"] == "queued"
    with pytest.raises(JobError):
        store.queue_job(job["id"])                                       # already queued
    await run_until_idle(worker)
    done = store.get_job(job["id"])
    assert done["status"] == "done" and [i["status"] for i in done["images"]] == ["done", "done"]
    for image in done["images"]:
        path = config.images_dir / image["file_path"]
        assert path.is_file() and image["file_path"].endswith(f"{job['id']}-{image['idx']}.png")
        assert image["recipe"]["negative_sent"].endswith("nudity") and image["prompt_id"].startswith("p")
    assert [g["41"]["inputs"]["seed"] for g in worker.comfy.graphs] == [100, 101]


async def test_failed_image_marks_job_failed_and_retry_only_reruns_failed(store, worker, tagwf, comfy):
    job = make_job(store, tagwf["id"], count=2, queue=True)
    comfy.fail_with = ComfyError("node 41: out of memory")
    await run_until_idle(worker)
    failed = store.get_job(job["id"])
    assert failed["status"] == "failed" and "out of memory" in failed["error"]
    comfy.fail_with = None
    store.retry_job(job["id"], "failed")
    assert store.get_job(job["id"])["status"] == "queued"
    await run_until_idle(worker)
    assert store.get_job(job["id"])["status"] == "done"
    with pytest.raises(JobError, match="no failed"):
        store.retry_job(job["id"], "failed")                              # nothing left to retry
    assert store.retry_job(job["id"], "all")["status"] == "queued"        # whole job again


async def test_reorder_and_move(store, tagwf):
    ids = [make_job(store, tagwf["id"], queue=True)["id"] for _ in range(3)]
    assert store.queued_ids() == ids
    store.move_job(ids[2], -2)
    assert store.queued_ids() == [ids[2], ids[0], ids[1]]
    store.move_job(ids[2], 1)
    assert store.queued_ids() == [ids[0], ids[2], ids[1]]
    store.set_order([ids[1]])
    assert store.queued_ids() == [ids[1], ids[0], ids[2]]
    with pytest.raises(JobError):
        store.set_order([999])


async def test_jobs_run_in_queue_order(store, worker, tagwf):
    a, b = (make_job(store, tagwf["id"], queue=True, seed=s)["id"] for s in (1, 2))
    store.move_job(b, -1)
    await run_until_idle(worker)
    assert [g["41"]["inputs"]["seed"] for g in worker.comfy.graphs] == [2, 1]


async def test_cancel_queued_job(store, worker, tagwf):
    job = make_job(store, tagwf["id"], count=2, queue=True)
    cancelled = await worker.cancel_job(job["id"])
    assert cancelled["status"] == "cancelled" and {i["status"] for i in cancelled["images"]} == {"cancelled"}
    assert store.next_queued() is None
    assert store.retry_job(job["id"], "failed")["status"] == "queued"     # cancelled images can be retried


async def test_clear_hides_a_finished_job_and_keeps_its_images(store, worker, tagwf):
    waiting = make_job(store, tagwf["id"], queue=True)
    with pytest.raises(JobError):
        store.clear_job(waiting["id"])                                   # still queued: cancel it first
    await run_until_idle(worker)
    cleared = store.clear_job(waiting["id"])
    assert cleared["status"] == "done" and cleared["cleared_at"]
    assert store.get_job(waiting["id"])["images"][0]["status"] == "done"  # the image stays for the Gallery
    assert store.retry_job(waiting["id"], "all")["cleared_at"] is None    # retrying brings it back to the list


async def test_cancel_running_job_interrupts_comfy(store, worker, tagwf, comfy):
    job = make_job(store, tagwf["id"], count=2, queue=True)
    comfy.gate = asyncio.Event()                                            # run() blocks until released
    ticking = asyncio.create_task(worker.tick())
    await asyncio.wait_for(comfy.started.wait(), 5)
    assert store.get_job(job["id"])["status"] == "running"
    await worker.cancel_job(job["id"])
    await asyncio.wait_for(ticking, 5)
    final = store.get_job(job["id"])
    assert final["status"] == "cancelled" and comfy.interrupted and comfy.deleted == [["p1"]]
    assert {i["status"] for i in final["images"]} == {"cancelled"}
    assert len(comfy.graphs) == 1                                           # second image never started


async def test_interrupted_by_comfy_marks_image_cancelled(store, worker, tagwf, comfy):
    job = make_job(store, tagwf["id"], queue=True)
    comfy.fail_with = ComfyInterrupted("Interrupted")
    await run_until_idle(worker)
    assert store.get_job(job["id"])["images"][0]["status"] == "cancelled"


# ---------- persistence and ComfyUI down ----------

async def test_restart_resets_running_to_queued(store, worker, tagwf, db):
    job = make_job(store, tagwf["id"], count=2, queue=True)
    store.start_job(job["id"])
    store.set_image(store.get_job(job["id"])["images"][0]["id"], "running")
    store.recover()                                                         # what happens at app start
    again = store.get_job(job["id"])
    assert again["status"] == "queued" and [i["status"] for i in again["images"]] == ["pending", "pending"]


async def test_comfy_down_pauses_with_message_then_continues(store, worker, tagwf, comfy):
    job = make_job(store, tagwf["id"], queue=True)
    comfy.up = False
    await worker.tick()
    assert "isn't reachable" in worker.unreachable and store.get_job(job["id"])["status"] == "queued"
    assert comfy.graphs == []
    comfy.up = True
    await run_until_idle(worker)
    assert worker.unreachable is None and store.get_job(job["id"])["status"] == "done"


async def test_comfy_goes_down_mid_job_requeues(store, worker, tagwf, comfy):
    from app.comfy.client import ComfyUnreachable
    job = make_job(store, tagwf["id"], queue=True)
    comfy.fail_with = ComfyUnreachable("down")
    await worker.tick()
    again = store.get_job(job["id"])
    assert again["status"] == "queued" and again["images"][0]["status"] == "pending" and worker.unreachable


async def test_user_pause(store, worker, tagwf):
    job = make_job(store, tagwf["id"], queue=True)
    worker.pause()
    worker._wake.set()
    await asyncio.wait_for(worker.tick(), 5)
    assert store.get_job(job["id"])["status"] == "queued"
    worker.resume()
    await run_until_idle(worker)
    assert store.get_job(job["id"])["status"] == "done"


async def test_edit_job_requires_input_image(store, library):
    qwen = await import_fixture(library, QWEN)
    assert qwen["runnable"] == 0                                             # edit workflow with missing models
    with pytest.raises(JobError, match="not runnable"):
        store.create_job(qwen["id"], 1, {"prompts": ["make it blue"], "input_image": "x.png"})
