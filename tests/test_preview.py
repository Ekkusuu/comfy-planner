"""Live step previews: parsing ComfyUI's binary frames, keeping the newest frame of the running image, the endpoint."""
import asyncio
import json
import struct

import httpx
import pytest
from fastapi.testclient import TestClient

from app.comfy.client import ComfyClient, ComfyError, parse_preview_frame
from app.main import create_app
from app.queue import worker as worker_module
from tests.conftest import PNG, TAGWF, import_fixture

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20 + b"\xff\xd9"


def frame(kind: int, payload: bytes) -> bytes:
    return struct.pack(">I", kind) + payload


def preview_frame(image: bytes = JPEG, fmt: int = 1) -> bytes:
    return frame(1, struct.pack(">I", fmt) + image)


def metadata_frame(image: bytes, **meta) -> bytes:
    raw = json.dumps(meta).encode()
    return frame(4, struct.pack(">I", len(raw)) + raw + image)


# ---------- parsing ----------

def test_parses_jpeg_and_png_preview_frames():
    assert parse_preview_frame(preview_frame(JPEG, 1)) == JPEG
    assert parse_preview_frame(preview_frame(PNG, 2)) == PNG


def test_parses_metadata_frames_and_skips_other_prompts():
    assert parse_preview_frame(metadata_frame(JPEG, prompt_id="p1", node_id="3"), "p1") == JPEG
    assert parse_preview_frame(metadata_frame(JPEG, node_id="3"), "p1") == JPEG                 # no prompt id in it: accept
    assert parse_preview_frame(metadata_frame(JPEG, prompt_id="other"), "p1") is None


@pytest.mark.parametrize("raw", [
    b"",
    b"\x00\x00",
    frame(1, b"\x00\x00\x00\x01"),                      # no image bytes
    frame(2, struct.pack(">I", 1) + JPEG),              # unencoded preview: never sent to a socket
    frame(3, b"text frame"),                            # unknown event type
    frame(99, struct.pack(">I", 1) + JPEG),
    preview_frame(b"not an image at all"),
    frame(4, struct.pack(">I", 9999) + b"{}"),          # metadata length runs past the end
    frame(4, struct.pack(">I", 3) + b"{{{" + JPEG),     # metadata is not JSON
    frame(4, struct.pack(">I", 2) + b"[]" + JPEG),      # metadata is not an object
])
def test_ignores_unknown_short_or_bad_frames(raw):
    assert parse_preview_frame(raw, "p1") is None


# ---------- following the websocket ----------

class FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)

    async def recv(self):
        return self.messages.pop(0)


def finished(prompt_id="p1"):
    return json.dumps({"type": "execution_success", "data": {"prompt_id": prompt_id}})


async def test_follow_hands_frames_to_on_preview_and_skips_bad_ones():
    client = ComfyClient("http://127.0.0.1:1")
    got = []
    ws = FakeSocket([preview_frame(JPEG), b"\x00\x00\x00", frame(3, b"x"),
                     json.dumps({"type": "progress", "data": {"value": 1, "max": 2, "prompt_id": "p1"}}),
                     preview_frame(PNG, 2), finished()])
    events = []
    await client._follow(ws, "p1", events.append, got.append)
    assert got == [JPEG, PNG] and [e["type"] for e in events] == ["progress"]
    await client.close()


async def test_follow_without_on_preview_just_skips_frames():
    client = ComfyClient("http://127.0.0.1:1")
    events = []
    await client._follow(FakeSocket([preview_frame(JPEG), finished()]), "p1", events.append)
    assert events == []
    await client.close()


async def test_a_failing_on_preview_does_not_stop_the_job():
    client = ComfyClient("http://127.0.0.1:1")

    def broken(image):
        raise RuntimeError("boom")

    await client._follow(FakeSocket([preview_frame(JPEG), finished()]), "p1", lambda e: None, broken)   # returns normally
    await client.close()


async def test_previews_are_asked_for_per_prompt_only_when_wanted():
    sent = []

    def answer(request: httpx.Request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"prompt_id": "p1"})

    client = ComfyClient("http://127.0.0.1:1")
    client._http = httpx.AsyncClient(base_url="http://127.0.0.1:1", transport=httpx.MockTransport(answer))
    await client.queue_prompt({"1": {}})
    await client.queue_prompt({"1": {}}, "auto")
    assert "extra_data" not in sent[0] and sent[1]["extra_data"] == {"preview_method": "auto"}
    await client.close()


# ---------- worker: newest frame, signal rate, cleanup ----------

async def queued_job(store, worker):
    wf = await import_fixture(worker.library, TAGWF)
    return store.create_job(wf["id"], 1, {"prompts": ["a lake"], "seed": 1, "seed_strategy": "increment"}, queue=True)


async def test_keeps_only_the_newest_frame_and_drops_it_when_the_image_ends(store, worker, comfy):
    comfy.gate = asyncio.Event()
    comfy.preview_frames = [JPEG, PNG]
    job = await queued_job(store, worker)
    ticking = asyncio.create_task(worker.tick())
    await comfy.started.wait()
    assert worker.preview_for(job["id"]) == PNG                      # the second frame replaced the first
    assert worker.preview_for(job["id"] + 1) is None                 # another job has none
    comfy.gate.set()
    await ticking
    assert worker.preview_for(job["id"]) is None and store.get_job(job["id"])["status"] == "done"


async def test_frame_is_dropped_when_the_image_fails(store, worker, comfy):
    comfy.preview_frames = [JPEG]
    comfy.fail_with = ComfyError("node 41: out of memory")
    job = await queued_job(store, worker)
    await worker.tick()
    assert store.get_job(job["id"])["status"] == "failed" and worker.preview_for(job["id"]) is None


async def test_frame_is_dropped_when_the_job_is_cancelled(store, worker, comfy):
    comfy.gate = asyncio.Event()
    comfy.preview_frames = [JPEG]
    job = await queued_job(store, worker)
    ticking = asyncio.create_task(worker.tick())
    await comfy.started.wait()
    assert worker.preview_for(job["id"]) == JPEG
    await worker.cancel_job(job["id"])
    await ticking
    assert worker.preview_for(job["id"]) is None


async def test_late_frames_of_an_image_that_is_over_are_ignored(worker):
    worker._on_preview(7, JPEG)                                      # nothing is running
    assert worker.preview_for(7) is None
    worker.current = {"job_id": 8, "image_id": 1, "idx": 0, "prompt_id": None}
    worker._on_preview(7, JPEG)                                      # another job is running
    assert worker.preview_for(7) is None


async def test_signals_are_throttled_and_carry_no_image_bytes(worker, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: now[0])
    queue = worker.events.subscribe()
    worker.current = {"job_id": 5, "image_id": 1, "idx": 0, "prompt_id": None}
    for i in range(8):                                               # eight frames inside one second (0.125 s apart)
        worker._on_preview(5, JPEG + bytes([i]))
        now[0] += 0.125
    signals = []
    while not queue.empty():
        signals.append(queue.get_nowait())
    assert 2 <= len(signals) <= 3 and all(s["type"] == "preview" for s in signals)
    assert all(set(s) == {"type", "job_id", "number"} for s in signals)
    assert worker.preview_for(5) == JPEG + bytes([7])                # the newest frame is still the one served


# ---------- endpoint ----------

def test_preview_endpoint(config, comfy):
    with TestClient(create_app(config, comfy=comfy, start_worker=False)) as client:
        worker = client.app.state.worker
        assert client.get("/api/jobs/3/preview").status_code == 404
        worker.current = {"job_id": 3, "image_id": 1, "idx": 0, "prompt_id": None}
        worker._on_preview(3, JPEG)
        ok = client.get("/api/jobs/3/preview")
        assert ok.status_code == 200 and ok.content == JPEG and ok.headers["content-type"] == "image/jpeg"
        assert ok.headers["cache-control"] == "no-store"
        worker._on_preview(3, PNG)
        assert client.get("/api/jobs/3/preview").headers["content-type"] == "image/png"
        assert client.get("/api/jobs/4/preview").status_code == 404
        worker.current = worker._preview = None
        assert client.get("/api/jobs/3/preview").status_code == 404
