"""Shared fixtures: trimmed object_info, workflow JSONs, and a fake ComfyUI client (no GPU, no server)."""
import asyncio
import json
from pathlib import Path

import pytest

from app.comfy.client import ComfyUnreachable, OutputImage
from app.comfy.convert import convert
from app.comfy.slots import detect_slots
from app.config import Config
from app.db import Database
from app.events import EventBus
from app.library.workflows import WorkflowLibrary
from app.planning import content_safety
from app.queue.jobs import JobStore
from app.queue.worker import QueueWorker
from tests.content_probes import write_rules

FIXTURES = Path(__file__).parent / "fixtures"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")

KREA = "Subgraph T2I - natural language"
TAGWF = "Tag T2I hires - reference"
TAGWF_LORA = "Tag T2I hires - style LoRA"
TAGWF_MISSING = "Tag T2I hires - models not installed"
QWEN = "Image edit - single image"
ALL_FIXTURES = (KREA, TAGWF, TAGWF_LORA, TAGWF_MISSING, QWEN)


def load_workflow(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def oi() -> dict:
    return json.loads((FIXTURES / "object_info.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def graphs(oi) -> dict:
    """API graphs of all fixture workflows by name."""
    return {n: convert(load_workflow(n), oi).graph for n in ALL_FIXTURES}


@pytest.fixture(scope="session")
def slot_maps(graphs) -> dict:
    return {n: detect_slots(g) for n, g in graphs.items()}


class FakeComfy:
    """Stands in for ComfyClient. Records graphs; can be 'down', fail, or block until released."""

    def __init__(self, oi: dict):
        self.oi = oi
        self.up = True
        self.graphs: list[dict] = []
        self.fail_with: Exception | None = None
        self.gate: asyncio.Event | None = None      # set to make run() wait until released
        self.started = asyncio.Event()
        self.interrupted = False
        self.deleted: list = []
        self.preview_frames: list[bytes] = []       # handed to on_preview while run() is in progress

    async def object_info(self, refresh=False):
        return self.oi

    async def is_up(self):
        return self.up

    async def system_stats(self):
        if not self.up:
            raise ComfyUnreachable("down")
        return {"system": {"comfyui_version": "test"}}

    async def run(self, graph, on_event=None, on_preview=None):
        if not self.up:
            raise ComfyUnreachable("down")
        self.graphs.append(graph)
        if on_event:
            on_event({"type": "queued", "prompt_id": f"p{len(self.graphs)}"})
        self.started.set()
        for frame in self.preview_frames if on_preview else []:
            on_preview(frame)
        if self.gate:
            await self.gate.wait()
        if self.fail_with:
            raise self.fail_with
        if on_event:
            on_event({"type": "progress", "value": 1, "max": 1, "node": "1"})
        return [OutputImage(f"out_{len(self.graphs)}.png")]

    async def view(self, image):
        return PNG

    async def interrupt(self):
        self.interrupted = True

    async def delete_queued(self, ids=None):
        self.deleted.append(ids)

    async def upload_image(self, name, data):
        return name

    async def free(self, unload_models=True):
        self.freed = True

    async def set_url(self, base_url):
        self.base_url = base_url

    async def close(self):
        pass


@pytest.fixture
def comfy(oi):
    return FakeComfy(oi)


@pytest.fixture(scope="session")
def probe_rules(tmp_path_factory) -> Path:
    """The synthetic rules file (tests/content_probes.py) that stands in for app/planning/content_rules.toml in every test."""
    return write_rules(tmp_path_factory.mktemp("content_rules"))


@pytest.fixture(autouse=True)
def content_rules(monkeypatch, probe_rules):
    """Every test runs with the synthetic rules active, loaded through the app's own loader and self-test. create_app activates the same file."""
    monkeypatch.setattr(content_safety, "RULES_PATH", probe_rules)
    assert content_safety.activate()


@pytest.fixture
def real_rules(monkeypatch):
    """The app's own rules file, active for this test (the one place the real rules are used); returns the loaded rules."""
    path = Path(content_safety.__file__).with_name("content_rules.toml")
    monkeypatch.setattr(content_safety, "RULES_PATH", path)
    assert content_safety.activate()
    return content_safety.load_rules(path)


@pytest.fixture(autouse=True)
def no_nvidia_smi(monkeypatch):
    """The lease logs free VRAM after every release; tests must not run the real nvidia-smi (a slow call hung the loop teardown)."""
    async def fake_free_vram():
        return "?"
    monkeypatch.setattr("app.gpu.free_vram", fake_free_vram)


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(data_dir=tmp_path / "data", safety_negative=["nsfw", "nude", "nudity"], embeddings_enabled=False,
                  llm_keep_loaded_seconds=0)    # tests never start a real embedding server; the LLM stops after each session (tests/test_gpu.py covers keeping it)


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def library(db, config, comfy):
    return WorkflowLibrary(db, config, comfy)


@pytest.fixture
def store(db, config, library):
    return JobStore(db, config, library)


@pytest.fixture
def worker(store, library, comfy, config):
    return QueueWorker(store, library, comfy, config, EventBus())


async def import_fixture(library, name: str) -> dict:
    return await library.import_path(FIXTURES / f"{name}.json")
