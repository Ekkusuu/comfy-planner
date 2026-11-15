import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest

from app.comfy.client import ComfyUnreachable
from app.gpu import GpuLease
from app.llm.server import LlamaServer, LlmServerError
from tests.conftest import TAGWF, import_fixture
from tests.test_queue import make_job

FAKE = Path(__file__).parent / "fake_llama_server.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def llm_config(config, tmp_path):
    config.llm_port = free_port()
    config.llm_binary = Path(sys.executable)           # any existing file; the command below is replaced
    config.llm_models = {"gemma": tmp_path / "model.gguf"}
    config.llm_models["gemma"].write_bytes(b"x")
    config.llm_start_timeout, config.llm_stop_timeout = 20, 5
    return config


class FakeScriptServer(LlamaServer):
    """Runs tests/fake_llama_server.py instead of llama-server."""
    delay = 0

    def command(self):
        return [sys.executable, str(FAKE), str(self.config.llm_port), str(self.delay)]


async def test_server_starts_waits_for_health_and_stops(llm_config):
    server = FakeScriptServer(llm_config)
    server.delay = 1
    await server.start()
    assert server.running
    async with httpx.AsyncClient() as http:
        assert (await http.get(f"{server.url}/health")).status_code == 200
    proc = server.proc
    await server.stop()
    assert proc.returncode is not None and not server.running       # the process is confirmed gone
    with pytest.raises(httpx.HTTPError):
        async with httpx.AsyncClient() as http:
            await http.get(f"{llm_config.llm_url}/health")


async def test_server_refuses_when_port_is_taken_or_files_missing(llm_config):
    first = FakeScriptServer(llm_config)
    await first.start()
    try:
        with pytest.raises(LlmServerError, match="already answering"):
            await FakeScriptServer(llm_config).start()
    finally:
        await first.stop()
    llm_config.llm_models["gemma"].unlink()
    with pytest.raises(LlmServerError, match="model file not found"):
        await LlamaServer(llm_config).start()
    llm_config.llm_binary = Path("nope/llama-server.exe")
    with pytest.raises(LlmServerError, match="llama-server not found"):
        await LlamaServer(llm_config).start()


async def test_server_start_timeout_stops_the_process(llm_config):
    llm_config.llm_start_timeout = 1
    server = FakeScriptServer(llm_config)
    server.delay = 30                                                 # never ready in time
    with pytest.raises(LlmServerError, match="didn't become ready"):
        await server.start()
    assert not server.running


def test_memory_flags_only_when_the_build_knows_them_and_the_user_has_not_set_them():
    from app.llm.server import memory_flags
    new_build = "-cram, --cache-ram N   max prompt cache size\n-ctxcp, --ctx-checkpoints N  max checkpoints per slot\n"
    assert memory_flags(new_build, []) == ["--cache-ram", "0", "--ctx-checkpoints", "0"]
    assert memory_flags(new_build, ["--cache-ram", "512"]) == ["--ctx-checkpoints", "0"]      # the user's own value wins
    assert memory_flags("--ctx-size N\n--parallel N\n", []) == []                               # an older build: none
    assert memory_flags("", []) == []                                                            # help not readable: none
    assert memory_flags("--cache-ram-extra N\n", []) == []                                      # only the exact flag name


async def test_the_start_command_carries_the_memory_flags_the_binary_supports(llm_config, monkeypatch):
    import app.llm.server as srv
    async def fake_help(binary):
        return "--cache-ram N\n--ctx-checkpoints N\n"
    monkeypatch.setattr(srv, "help_text", fake_help)
    server = FakeScriptServer(llm_config)
    await server.start()
    try:
        assert server.memory_flags == ["--cache-ram", "0", "--ctx-checkpoints", "0"]
        assert LlamaServer.command(server)[-4:] == ["--cache-ram", "0", "--ctx-checkpoints", "0"]
    finally:
        await server.stop()


def test_server_command_uses_config(llm_config):
    cmd = LlamaServer(llm_config).command()
    assert cmd[cmd.index("--port") + 1] == str(llm_config.llm_port) and "--jinja" in cmd
    assert cmd[cmd.index("--ctx-size") + 1] == "8192" and cmd[cmd.index("--model") + 1].endswith("model.gguf")


# ---------- GpuLease ordering ----------

class Recorder:
    def __init__(self):
        self.calls: list[str] = []


class FakeServer:
    def __init__(self, rec, fail_start=False):
        self.rec, self.fail_start = rec, fail_start

    async def start(self):
        self.rec.calls.append("server.start")
        if self.fail_start:
            raise LlmServerError("boom")

    async def stop(self):
        self.rec.calls.append("server.stop")


class FakeWorker:
    def __init__(self, rec):
        self.rec = rec

    async def hold(self):
        self.rec.calls.append("worker.hold")

    def release(self):
        self.rec.calls.append("worker.release")


class FakeFreeComfy:
    def __init__(self, rec, down=False):
        self.rec, self.down = rec, down

    async def free(self):
        self.rec.calls.append("comfy.free")
        if self.down:
            raise ComfyUnreachable("down")


async def test_lease_order():
    rec = Recorder()
    lease = GpuLease(FakeWorker(rec), FakeFreeComfy(rec), FakeServer(rec))
    async with lease:
        rec.calls.append("work")
        assert lease.active
    assert rec.calls == ["worker.hold", "comfy.free", "server.start", "work", "server.stop", "worker.release"]
    assert not lease.active


async def test_lease_releases_worker_when_start_fails_and_ignores_comfy_down():
    rec = Recorder()
    lease = GpuLease(FakeWorker(rec), FakeFreeComfy(rec, down=True), FakeServer(rec, fail_start=True))
    with pytest.raises(LlmServerError):
        async with lease:
            pass
    assert rec.calls == ["worker.hold", "comfy.free", "server.start", "server.stop", "worker.release"]
    assert not lease.active


async def test_lease_holders_take_turns():
    rec = Recorder()
    lease = GpuLease(FakeWorker(rec), FakeFreeComfy(rec), FakeServer(rec))

    async def use(name, pause):
        async with lease:
            rec.calls.append(f"{name} start")
            await asyncio.sleep(pause)
            rec.calls.append(f"{name} end")

    await asyncio.gather(use("a", 0.05), use("b", 0))
    assert [c for c in rec.calls if c.endswith(("start", "end")) and c[0] in "ab"] == ["a start", "a end", "b start", "b end"]


# ---------- the queue worker waits for the image boundary ----------

async def test_hold_waits_for_current_image_and_blocks_the_next(store, worker, tagwf, comfy):
    job = make_job(store, tagwf["id"], count=2, queue=True)
    comfy.gate = asyncio.Event()
    ticking = asyncio.create_task(worker.tick())
    await asyncio.wait_for(comfy.started.wait(), 5)                     # image 1 is generating
    holding = asyncio.create_task(worker.hold())
    await asyncio.sleep(0.1)
    assert not holding.done() and worker.state()["held"]                # waits for the image to finish
    comfy.gate.set()
    await asyncio.wait_for(holding, 5)
    await asyncio.sleep(0.1)
    assert len(comfy.graphs) == 1                                       # image 2 has not started
    assert store.get_job(job["id"])["images"][0]["status"] == "done"
    worker.release()
    await asyncio.wait_for(ticking, 5)
    assert store.get_job(job["id"])["status"] == "done" and len(comfy.graphs) == 2


async def test_hold_keeps_the_user_pause(worker):
    worker.pause()
    await worker.hold()
    worker.release()
    assert worker.paused_by_user and not worker.held                    # resumes only what the lease paused


@pytest.fixture
async def tagwf(library):
    return await import_fixture(library, TAGWF)
