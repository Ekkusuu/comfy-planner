"""MCP server (app/mcp_server.py): tools over the app's own routes, via the SDK server in process."""
import asyncio
import base64
import io
import json
import socket
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image as PilImage

import app.mcp_server as mcp_mod
from app.config import Config
from app.main import create_app
from app.mcp_server import AgentApi, build_server
from mcp.server.mcpserver.exceptions import ToolError
from tests.conftest import FIXTURES, KREA, TAGWF_LORA, FakeComfy
from tests.test_cards import STYLE_LORA
from tests.test_gpu import FakeServer, Recorder
from tests.test_planning_flow import FakeLlama, anima_card, krea_card
from app.library.card_models import LoraCard, Range


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    """A running app (TestClient) on a free port with a temp toml + temp data dir, stubbed LLM/ComfyUI, one approved workflow."""
    monkeypatch.setenv("CIVITAI_API_TOKEN", "")
    port = free_port()
    toml = tmp_path / "app.toml"
    toml.write_text("[embeddings]\nenabled = false\n[jobs]\nsafety_negative = []\n[llm]\nkeep_loaded_seconds = 0\n", encoding="utf-8")
    config = Config(data_dir=tmp_path / "data", safety_negative=[], embeddings_enabled=False, port=port, llm_keep_loaded_seconds=0)
    oi = json.loads((FIXTURES / "object_info.json").read_text(encoding="utf-8"))
    comfy = FakeComfy(oi)
    rec = Recorder()
    llama = FakeLlama(rec)
    flask = create_app(config, comfy=comfy, start_worker=False, llm_server=FakeServer(rec),
                       llm_transport=httpx.MockTransport(llama), config_path=toml)
    with TestClient(flask) as client:
        st = client.app.state
        wf_id = client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{TAGWF_LORA}.json")}).json()["id"]
        st.cards.save_built("workflow", wf_id, anima_card())
        st.cards.approve("workflow", wf_id)
        st.cards.save_built("lora", STYLE_LORA, LoraCard(file=STYLE_LORA, family="Anima", kind="style",
                                                         what_it_does="Applies artist styles.", trigger_words=["@style_name"],
                                                         style_tags=["@bluejay"], weight=Range(rec=1.0, min=0.5, max=1.2)))
        st.cards.approve("lora", STYLE_LORA)
        krea_id = client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{KREA}.json")}).json()["id"]
        yield {"client": client, "config": config, "comfy": comfy, "rec": rec, "llama": llama,
               "wf_id": wf_id, "krea_id": krea_id, "toml": toml, "port": port}


def make_server(app):
    api = AgentApi(app)
    return api, build_server(api)


async def call_ok(server, name, args):
    result = await server.call_tool(name, args)
    assert not getattr(result, "is_error", False), f"{name} failed: {result.content}"
    texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    assert texts, f"{name} returned no text"
    return json.loads(texts[0])


async def call_err(server, name, args):
    try:
        result = await server.call_tool(name, args)
    except ToolError as e:
        return str(e)
    if getattr(result, "is_error", False):
        return "\n".join(getattr(c, "text", "") for c in result.content)
    raise AssertionError(f"{name} should have failed")


async def wait_plan_done(client, plan_id, timeout=20):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        plan = client.get(f"/api/plans/{plan_id}").json()
        if plan["status"] in ("done", "failed", "cancelled"):
            return plan
        if loop.time() > deadline:
            raise AssertionError(f"plan {plan_id} stuck at {plan['status']}")
        await asyncio.sleep(0.05)


async def test_tool_list_has_expected_tools_and_no_delete(mcp_env):
    api, server = make_server(mcp_env["client"].app)
    try:
        tools = await server.list_tools()
        names = [t.name for t in tools]
        assert len(names) == len(set(names))
        banned = ("delete", "remove", "import", "download", "trash", "clear")
        assert not [n for n in names if any(b in n for b in banned)], names
        wanted = {"upload_image", "get_settings", "update_settings", "list_cards", "get_card",
                  "update_card", "build_card", "approve_card", "accept_card", "reject_card",
                  "generate_images", "plan_images", "wait_for_results", "get_plan", "cancel_plan"}
        assert wanted <= set(names), wanted - set(names)
        by = {t.name: t for t in tools}
        assert by["update_settings"].annotations.destructive_hint is True
        assert by["update_card"].annotations.destructive_hint is True
        assert by["get_settings"].annotations.read_only_hint
    finally:
        await api.close()


async def test_generate_images_queues_jobs_when_plan_is_done(mcp_env, monkeypatch):
    monkeypatch.setattr(mcp_mod, "FOLLOW_SECONDS", 0.1)
    env = mcp_env
    env["llama"].scripts["plan_request"] = {"jobs": [
        {"constraints": [], "issues": [], "reason": "fits", "variety": "variations", "description": "a girl in a park", "count": 1},
        {"constraints": [], "issues": [], "reason": "fits", "variety": "variations", "description": "a girl in a cafe", "count": 1}]}
    worker = env["client"].app.state.worker
    worker.start()
    api, server = make_server(env["client"].app)
    try:
        started = await call_ok(server, "generate_images", {"request": "2 images of a girl", "workflow_id": env["wf_id"]})
        plan_id = started["plan_id"]
        report = await call_ok(server, "wait_for_results", {"plan_id": plan_id, "wait_seconds": 30})
        assert report["status"] == "done" and report["finished"]
        assert report["auto_queue"]["queued"] and not report["auto_queue"]["skipped"]
        assert report["counts"]["jobs"].get("done") == 2 or report["counts"]["images"].get("done") == 2
    finally:
        await api.close()
        await worker.stop()


async def test_failed_plan_queues_nothing(mcp_env, monkeypatch):
    monkeypatch.setattr(mcp_mod, "FOLLOW_SECONDS", 0.1)
    env = mcp_env
    env["llama"].scripts["plan_request"] = {"jobs": []}
    api, server = make_server(env["client"].app)
    try:
        started = await call_ok(server, "generate_images", {"request": "something", "workflow_id": env["wf_id"]})
        report = await call_ok(server, "wait_for_results", {"plan_id": started["plan_id"], "wait_seconds": 20})
        assert report["status"] == "failed" and report["finished"]
        assert report["auto_queue"]["queued"] == [] and "nothing was queued" in report["auto_queue"]["note"]
    finally:
        await api.close()


async def test_cancelled_plan_queues_nothing(mcp_env, monkeypatch):
    monkeypatch.setattr(mcp_mod, "FOLLOW_SECONDS", 0.1)
    env = mcp_env
    client, comfy = env["client"], env["comfy"]
    worker = client.app.state.worker
    worker.start()
    comfy.gate = asyncio.Event()
    comfy.started.clear()
    blocking = client.post("/api/jobs", json={"workflow_id": env["wf_id"], "count": 1,
                                              "fields": {"prompts": ["busy"]}, "queue": True}).json()
    await asyncio.wait_for(comfy.started.wait(), 10)
    env["llama"].scripts["plan_request"] = {"jobs": [
        {"constraints": [], "issues": [], "reason": "fits", "variety": "variations", "description": "a dog", "count": 1}]}
    api, server = make_server(client.app)
    try:
        started = await call_ok(server, "generate_images", {"request": "a dog", "workflow_id": env["wf_id"]})
        plan_id = started["plan_id"]
        cancelled = await call_ok(server, "cancel_plan", {"plan_id": plan_id})
        assert cancelled["status"] in ("cancelled", "queued", "waiting", "planning", "loading")
        comfy.gate.set()
        report = await call_ok(server, "wait_for_results", {"plan_id": plan_id, "wait_seconds": 20})
        assert report["status"] == "cancelled" and report["finished"]
        assert report["auto_queue"]["queued"] == []
        jobs = [j for j in client.get("/api/jobs").json() if j.get("plan_id") == plan_id]
        assert not [j for j in jobs if j["status"] in ("queued", "running", "done")]
    finally:
        comfy.gate.set()
        await api.close()
        # let the blocking job finish
        for _ in range(100):
            if client.get(f"/api/jobs/{blocking['id']}").json()["status"] == "done":
                break
            await asyncio.sleep(0.05)
        await worker.stop()


async def test_wait_for_results_caps_its_wait(mcp_env, monkeypatch):
    monkeypatch.setattr(mcp_mod, "WAIT_MAX", 2)
    monkeypatch.setattr(mcp_mod, "POLL_SECONDS", 0.2)
    monkeypatch.setattr(mcp_mod, "FOLLOW_SECONDS", 0.1)
    env = mcp_env
    client, comfy = env["client"], env["comfy"]
    worker = client.app.state.worker
    worker.start()
    comfy.gate = asyncio.Event()
    comfy.started.clear()
    blocking = client.post("/api/jobs", json={"workflow_id": env["wf_id"], "count": 1,
                                              "fields": {"prompts": ["busy2"]}, "queue": True}).json()
    await asyncio.wait_for(comfy.started.wait(), 10)
    env["llama"].scripts["plan_request"] = {"jobs": [
        {"constraints": [], "issues": [], "reason": "fits", "variety": "variations", "description": "a bird", "count": 1}]}
    api, server = make_server(client.app)
    try:
        started = await call_ok(server, "plan_images", {"request": "a bird", "workflow_id": env["wf_id"]})
        t0 = asyncio.get_running_loop().time()
        report = await call_ok(server, "wait_for_results", {"plan_id": started["plan_id"], "wait_seconds": 500})
        el = asyncio.get_running_loop().time() - t0
        assert not report["finished"]
        assert 1.0 < el < 5.0, el
        await call_ok(server, "cancel_plan", {"plan_id": started["plan_id"]})
    finally:
        comfy.gate.set()
        await api.close()
        for _ in range(100):
            try:
                if client.get(f"/api/jobs/{blocking['id']}").json()["status"] == "done":
                    break
            except Exception:
                break
            await asyncio.sleep(0.05)
        await worker.stop()


async def test_upload_image_accepts_a_real_png(mcp_env, tmp_path):
    env = mcp_env
    png = tmp_path / "photo one.png"
    PilImage.new("RGB", (64, 48), (10, 200, 30)).save(png, "PNG")
    api, server = make_server(env["client"].app)
    try:
        out = await call_ok(server, "upload_image", {"path": str(png)})
        assert out["name"].endswith(".png") and (out["width"], out["height"], out["format"]) == (64, 48, "PNG")
    finally:
        await api.close()


async def test_upload_image_refuses_a_non_image_without_echoing_it(mcp_env, tmp_path):
    env = mcp_env
    fake = tmp_path / "fake.png"
    fake.write_text("SECRET-FILE-CONTENT not a picture", encoding="utf-8")
    api, server = make_server(env["client"].app)
    try:
        err = await call_err(server, "upload_image", {"path": str(fake)})
        assert "SECRET-FILE-CONTENT" not in err
        assert "not a picture" in err or "not be read" in err
    finally:
        await api.close()


async def test_update_settings_changes_a_harmless_value(mcp_env):
    env = mcp_env
    api, server = make_server(env["client"].app)
    try:
        before = await call_ok(server, "get_settings", {})
        new_val = 2 if before["settings"]["max_loras"] != 2 else 3
        out = await call_ok(server, "update_settings", {"changes": {"max_loras": new_val}})
        assert out["changed"] == ["max_loras"] and out["settings"] == {"max_loras": new_val}
        after = await call_ok(server, "get_settings", {})
        assert after["settings"]["max_loras"] == new_val
        assert "max_loras" in env["toml"].read_text(encoding="utf-8")
        assert env["client"].app.state.config_path == env["toml"]
    finally:
        await api.close()


@pytest.mark.parametrize("key,value", [
    ("llm_binary", "C:/evil/run.exe"),
    ("llm_extra_args", ["--api-key", "x"]),
    ("comfy_cli", "calc.exe"),
    ("comfy_install_dir", "C:/evil"),
    ("comfy_url", "http://127.0.0.1:9999"),
])
async def test_update_settings_refuses_program_settings(mcp_env, key, value):
    env = mcp_env
    api, server = make_server(env["client"].app)
    try:
        snap_val = getattr(env["client"].app.state.config, key)
        toml_before = env["toml"].read_bytes()
        before_max = (await call_ok(server, "get_settings", {}))["settings"]["max_loras"]
        err = await call_err(server, "update_settings", {"changes": {key: value}})
        assert "Settings page" in err and key in err
        assert getattr(env["client"].app.state.config, key) == snap_val
        assert env["toml"].read_bytes() == toml_before
        assert (await call_ok(server, "get_settings", {}))["settings"]["max_loras"] == before_max
    finally:
        await api.close()


async def test_get_settings_shows_no_secret(mcp_env, monkeypatch):
    env = mcp_env
    monkeypatch.setenv("CIVITAI_API_TOKEN", "SECRET-TEST-TOKEN-123")
    st = env["client"].app.state
    st.config.llm_extra_args = ["--api-key", "SECRETKEY"]
    st.config.comfy_url = "http://user:hunter2@127.0.0.1:8188"
    api, server = make_server(env["client"].app)
    try:
        out = await call_ok(server, "get_settings", {})
        blob = json.dumps(out)
        assert "SECRET-TEST-TOKEN-123" not in blob
        assert "SECRETKEY" not in blob and "hunter2" not in blob
        assert out["settings"]["comfy_url"] == "http://127.0.0.1:8188"
        assert "not shown" in out["settings"]["llm_extra_args"]
        assert out["settings"]["civitai_token_found"] is True
    finally:
        st.config.llm_extra_args = []
        st.config.comfy_url = "http://127.0.0.1:8188"
        await api.close()


async def test_update_card_changes_one_field_and_leaves_the_others(mcp_env):
    env = mcp_env
    api, server = make_server(env["client"].app)
    try:
        before = await call_ok(server, "get_card", {"kind": "workflow", "key": env["wf_id"]})
        old_best = before["card"]["best_for"]
        out = await call_ok(server, "update_card", {"kind": "workflow", "key": env["wf_id"],
                                                    "changes": {"purpose": "Edited by the agent."}})
        assert out["status"] == "approved" and out["changed"] == ["purpose"]
        after = await call_ok(server, "get_card", {"kind": "workflow", "key": env["wf_id"]})
        assert after["card"]["purpose"] == "Edited by the agent."
        assert after["card"]["best_for"] == old_best
    finally:
        await api.close()


async def test_approve_card_works(mcp_env):
    env = mcp_env
    st = env["client"].app.state
    st.cards.save_built("workflow", env["krea_id"], krea_card())
    assert st.cards.get("workflow", env["krea_id"])["status"] == "draft"
    api, server = make_server(env["client"].app)
    try:
        out = await call_ok(server, "approve_card", {"kind": "workflow", "key": env["krea_id"]})
        assert out["status"] == "approved"
        got = await call_ok(server, "get_card", {"kind": "workflow", "key": env["krea_id"]})
        assert got["status"] == "approved"
    finally:
        await api.close()


def test_foreign_host_header_is_refused_at_mcp(mcp_env):
    env = mcp_env
    client, port = env["client"], env["port"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    r = client.post("/mcp", json=body, headers={**base, "Host": "evil.example.com"})
    assert r.status_code == 421, (r.status_code, r.text[:120])
    r = client.post("/mcp", json=body, headers={**base, "Host": f"127.0.0.1:{port}"})
    assert r.status_code == 200, (r.status_code, r.text[:120])
    assert len(r.json()["result"]["tools"]) == 33
