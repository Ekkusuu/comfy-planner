"""First-run setup: status, the downloader (resume, hash check, cancel, allowed hosts), GGUF checks, the install job and the settings it writes.
No real network: downloads go to a fake server (httpx.MockTransport) and the tag-library scripts are replaced by a fake runner."""
import asyncio
import hashlib
import io
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import settings
from app.comfy.client import ComfyUnreachable
from app.config import CONFIG_PATH, Config, load_config
from app.main import create_app
from app.setup import catalog, status
from app.setup.catalog import Asset
from app.setup.comfy_detect import find_installs, resolve_install_dir
from app.setup.downloader import Cancelled, DownloadError, Downloader, extract_zip, host_allowed
from app.setup.installer import SetupManager
from app.setup.models import check_model_file, check_model_name
from app.setup.routes import save_settings

EXAMPLE_PATH = CONFIG_PATH.with_name("app.example.toml")
GGUF = b"GGUF" + b"\x03\x00\x00\x00" + b"x" * 64


def make_asset(name: str, data: bytes, host: str = "https://github.com/pinned/") -> Asset:
    return Asset(name, f"{host}{name}", len(data), hashlib.sha256(data).hexdigest())


def fake_server(files: dict[str, bytes], log: list | None = None, honour_range: bool = True, chunked: bool = False):
    """A transport that serves `files` by URL. It answers Range requests with 206 (unless honour_range is off)."""
    class Chunks(httpx.AsyncByteStream):
        def __init__(self, data):
            self.data = data

        async def __aiter__(self):
            for i in range(0, len(self.data), 1 << 20):
                yield self.data[i:i + (1 << 20)]
                await asyncio.sleep(0)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        rng = request.headers.get("range")
        if log is not None:
            log.append((url, rng))
        if url not in files:
            return httpx.Response(404)
        data = files[url]
        if isinstance(data, int):                       # a status code to answer with
            return httpx.Response(data)
        if rng and honour_range:
            start = int(rng.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, stream=Chunks(data[start:]), headers={"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        return httpx.Response(200, stream=Chunks(data)) if chunked else httpx.Response(200, content=data)

    return httpx.MockTransport(handler)


def anywhere(url: str, first: bool) -> bool:
    return True


def no_progress(done: int, total: int):
    pass


BIG = bytes(range(256)) * 12_000          # about 3 MB: several chunks of 1 MB


# ---------- the downloader ----------

async def test_download_checks_size_and_hash_and_reports_progress(tmp_path):
    asset = make_asset("model.bin", BIG)
    seen = []
    dl = Downloader(tmp_path, fake_server({asset.url: BIG}, chunked=True), anywhere)
    path = await dl.fetch(asset, lambda done, total: seen.append((done, total)), asyncio.Event())
    assert path == tmp_path / "model.bin" and path.read_bytes() == BIG
    assert not (tmp_path / "model.bin.part").exists()
    assert seen[-1] == (len(BIG), len(BIG)) and [d for d, _ in seen] == sorted(d for d, _ in seen)


async def test_download_resumes_from_the_part_file(tmp_path):
    asset = make_asset("model.bin", BIG)
    (tmp_path / "model.bin.part").write_bytes(BIG[:1_000_000])
    log = []
    await Downloader(tmp_path, fake_server({asset.url: BIG}, log), anywhere).fetch(asset, no_progress, asyncio.Event())
    assert log == [(asset.url, "bytes=1000000-")]                       # only the rest was asked for
    assert (tmp_path / "model.bin").read_bytes() == BIG


async def test_download_starts_over_when_the_server_ignores_the_resume_request(tmp_path):
    asset = make_asset("model.bin", BIG)
    (tmp_path / "model.bin.part").write_bytes(BIG[:500_000])
    await Downloader(tmp_path, fake_server({asset.url: BIG}, honour_range=False), anywhere).fetch(asset, no_progress, asyncio.Event())
    assert (tmp_path / "model.bin").read_bytes() == BIG


async def test_a_wrong_hash_deletes_the_file_and_says_so(tmp_path):
    asset = make_asset("model.bin", BIG)
    tampered = BIG[:-1] + b"\x00"                                       # right size, wrong bytes
    with pytest.raises(DownloadError, match="checksum"):
        await Downloader(tmp_path, fake_server({asset.url: tampered}), anywhere).fetch(asset, no_progress, asyncio.Event())
    assert list(tmp_path.iterdir()) == []                               # neither the file nor the .part is left


async def test_a_finished_file_with_a_wrong_hash_is_replaced(tmp_path):
    asset = make_asset("model.bin", BIG)
    (tmp_path / "model.bin").write_bytes(b"old and wrong")
    await Downloader(tmp_path, fake_server({asset.url: BIG}), anywhere).fetch(asset, no_progress, asyncio.Event())
    assert (tmp_path / "model.bin").read_bytes() == BIG


async def test_a_right_file_is_not_downloaded_again(tmp_path):
    asset = make_asset("model.bin", BIG)
    (tmp_path / "model.bin").write_bytes(BIG)
    log = []
    await Downloader(tmp_path, fake_server({asset.url: BIG}, log), anywhere).fetch(asset, no_progress, asyncio.Event())
    assert log == []


async def test_cancel_keeps_the_part_file_and_a_second_try_continues(tmp_path):
    asset = make_asset("model.bin", BIG)
    cancel = asyncio.Event()
    log = []
    dl = Downloader(tmp_path, fake_server({asset.url: BIG}, log, chunked=True), anywhere)
    with pytest.raises(Cancelled):
        await dl.fetch(asset, lambda done, total: cancel.set() if done >= 1 << 20 else None, cancel)
    part = tmp_path / "model.bin.part"
    assert 0 < part.stat().st_size < len(BIG) and not (tmp_path / "model.bin").exists()
    kept = part.stat().st_size
    await dl.fetch(asset, no_progress, asyncio.Event())
    assert log[-1] == (asset.url, f"bytes={kept}-") and (tmp_path / "model.bin").read_bytes() == BIG


async def test_a_server_error_is_reported_in_plain_words(tmp_path):
    asset = make_asset("model.bin", BIG)
    with pytest.raises(DownloadError, match="answered 500"):
        await Downloader(tmp_path, fake_server({asset.url: 500}), anywhere).fetch(asset, no_progress, asyncio.Event())


def test_only_the_allowed_hosts_are_accepted():
    assert host_allowed("https://github.com/ggml-org/llama.cpp/releases/download/b11960/x.zip", first=True)
    assert host_allowed("https://huggingface.co/ggml-org/x/resolve/abc/x.gguf", first=True)
    for url in ("http://github.com/x", "https://evil.example/x", "https://github.com.evil.example/x", "https://notgithub.com/x",
                "https://user:pw@github.com/x", "https://github.com:8443/x", "ftp://github.com/x"):
        assert not host_allowed(url, first=True), url
    # a download may be sent on to the sites' own download servers, but may not start there
    for url in ("https://release-assets.githubusercontent.com/x", "https://objects.githubusercontent.com/x", "https://cas-bridge.xethub.hf.co/x",
                "https://cdn-lfs.huggingface.co/x"):
        assert host_allowed(url, first=False) and not host_allowed(url, first=True), url
    assert not host_allowed("https://evil.example/x", first=False) and not host_allowed("https://hf.co.evil.example/x", first=False)


async def test_a_download_from_another_host_is_refused_before_any_request(tmp_path):
    asset = make_asset("model.bin", BIG, host="https://evil.example/")
    log = []
    with pytest.raises(DownloadError, match="not one of the allowed"):
        await Downloader(tmp_path, fake_server({asset.url: BIG}, log)).fetch(asset, no_progress, asyncio.Event())
    assert log == []


async def test_a_redirect_to_an_allowed_download_server_is_followed(tmp_path):
    asset = make_asset("model.bin", BIG)
    cdn = "https://release-assets.githubusercontent.com/signed/model.bin"
    inner = fake_server({cdn: BIG})
    transport = httpx.MockTransport(lambda r: httpx.Response(302, headers={"Location": cdn}) if str(r.url) == asset.url else inner.handler(r))
    path = await Downloader(tmp_path, transport).fetch(asset, no_progress, asyncio.Event())
    assert path.read_bytes() == BIG


async def test_a_redirect_to_another_host_is_refused(tmp_path):
    asset = make_asset("model.bin", BIG)
    log = []

    def handler(request):
        log.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/model.bin"})

    with pytest.raises(DownloadError, match="evil.example"):
        await Downloader(tmp_path, httpx.MockTransport(handler)).fetch(asset, no_progress, asyncio.Event())
    assert log == [asset.url] and not list(tmp_path.glob("*.bin"))


# ---------- unpacking ----------

def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_only_the_expected_files_are_unpacked(tmp_path):
    z = tmp_path / "a.zip"
    z.write_bytes(make_zip({"llama-server.exe": b"exe", "ggml.dll": b"dll", "LICENSE-LLVM-OpenMP": b"lic", "../escape.dll": b"bad",
                            "sub/inner.dll": b"bad", "notes.txt": b"bad", "run.bat": b"bad", "/abs.dll": b"bad"}))
    out = tmp_path / "out"
    names = extract_zip(z, out, catalog.llama_member_ok)
    assert sorted(names) == ["LICENSE-LLVM-OpenMP", "ggml.dll", "llama-server.exe"]
    assert sorted(p.name for p in out.iterdir()) == sorted(names)
    assert not (tmp_path / "escape.dll").exists()


def test_the_second_zip_gives_only_the_cuda_libraries(tmp_path):
    z = tmp_path / "c.zip"
    z.write_bytes(make_zip({"cudart64_12.dll": b"1", "cublas64_12.dll": b"2", "cublasLt64_12.dll": b"3", "evil.exe": b"4"}))
    assert sorted(extract_zip(z, tmp_path / "out", lambda n: n in catalog.CUDART_FILES)) == sorted(catalog.CUDART_FILES)


def test_a_zip_that_unpacks_to_too_much_is_refused(tmp_path):
    z = tmp_path / "bomb.zip"
    z.write_bytes(make_zip({"a.dll": b"\x00" * 10_000}))
    with pytest.raises(DownloadError, match="far more"):
        extract_zip(z, tmp_path / "out", catalog.llama_member_ok, max_bytes=100)


# ---------- the pinned files ----------

def test_the_pinned_files_come_from_the_expected_places():
    for a in (catalog.LLAMA_ZIP, catalog.CUDART_ZIP, catalog.EMBEDDINGS_MODEL, catalog.TAG_COUNTS, catalog.COOCCURRENCE):
        assert host_allowed(a.url, first=True), a.url
        assert len(a.sha256) == 64 and int(a.sha256, 16) >= 0 and a.size > 0
    assert catalog.LLAMA_ZIP.url.startswith("https://github.com/ggml-org/llama.cpp/releases/download/b11960/")
    assert catalog.EMBEDDINGS_MODEL.url.startswith("https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/")


# ---------- GGUF files ----------

def test_a_gguf_file_is_accepted_and_others_are_not(tmp_path):
    good = tmp_path / "m.gguf"
    good.write_bytes(GGUF)
    assert check_model_file(str(good)) == good
    assert check_model_file(f'  "{good}"  ') == good                    # pasted with quotes
    assert check_model_file("m.gguf", root=tmp_path) == good             # relative to the app folder
    bad = tmp_path / "other.gguf"
    bad.write_bytes(b"GGML nope")
    for text, message in ((str(bad), "isn't a GGUF"), (str(tmp_path / "missing.gguf"), "doesn't exist"), (str(tmp_path), "folder"), ("  ", "Enter the path")):
        with pytest.raises(ValueError, match=message):
            check_model_file(text)


def test_model_names():
    assert check_model_name("  My   model ") == "My model"
    for bad in ("", "   ", "x" * 61, "a\x00b"):
        with pytest.raises(ValueError):
            check_model_name(bad)


# ---------- finding ComfyUI ----------

def test_comfyui_folders_are_found_in_the_usual_places(tmp_path, monkeypatch):
    local, home = tmp_path / "local", tmp_path / "home"
    desktop = local / "Comfy-Desktop" / "ComfyUI-Installs" / "ComfyUI" / "ComfyUI"
    portable = home / "Desktop" / "ComfyUI_windows_portable" / "ComfyUI"
    for folder in (desktop, portable):
        folder.mkdir(parents=True)
        (folder / "main.py").write_text("")
    (home / "ComfyUI").mkdir()                                          # a folder without main.py is not an install
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("USERPROFILE", str(home))
    found = find_installs()
    assert {"path": str(desktop), "kind": "Comfy Desktop"} in found and {"path": str(portable), "kind": "Portable"} in found
    assert resolve_install_dir(portable.parent) == portable             # the outer folder of a portable download leads to the inner one
    assert resolve_install_dir(home / "ComfyUI") is None


# ---------- status ----------

def make_config(tmp_path: Path) -> Config:
    """A config whose every path is under tmp_path, so nothing of the owner's setup is read."""
    c = Config()
    c.data_dir = tmp_path / "data"
    c.llm_binary = tmp_path / "tools" / "llama-server.exe"
    c.embeddings_model = tmp_path / "models" / "emb.gguf"
    c.llm_models = {"gemma": tmp_path / "models" / "gemma.gguf"}
    c.llm_model = "gemma"
    c.comfy_install_dir = ""
    return c


def by_id(items: list[dict]) -> dict:
    return {i["id"]: i for i in items}


async def test_a_fresh_install_is_missing_everything(tmp_path, comfy):
    comfy.up = False
    c = make_config(tmp_path)
    items = by_id(await status.compute(c, comfy))
    assert {k: v["state"] for k, v in items.items()} == {"comfyui": "missing", "llama_server": "missing", "llm_model": "missing",
                                                         "embeddings_model": "missing", "tag_library": "missing"}
    assert items["llama_server"]["size_bytes"] == catalog.LLAMA_ZIP.size + catalog.CUDART_ZIP.size
    assert items["embeddings_model"]["size_bytes"] == catalog.EMBEDDINGS_MODEL.size
    assert items["tag_library"]["size_bytes"] == catalog.TAGLIB_ESTIMATE + catalog.TAG_COUNTS.size + catalog.COOCCURRENCE.size
    assert "size_bytes" not in items["comfyui"] and "size_bytes" not in items["llm_model"]    # nothing to download for these
    assert status.needs_setup(list(items.values()), c)


async def test_everything_in_place_is_ok(tmp_path, comfy):
    c = make_config(tmp_path)
    for path, data in ((c.llm_binary, b"exe"), (c.embeddings_model, GGUF), (c.llm_models["gemma"], GGUF)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    taglib, co = catalog.taglib_folder(c.data_dir), catalog.contradictions_folder(c.data_dir)
    taglib.mkdir(parents=True)
    co.mkdir(parents=True)
    for name in catalog.TAGLIB_FILES:
        (taglib / name).write_text("x")
    (co / catalog.CONTRADICTIONS_FILE).write_text("{}")
    items = await status.compute(c, comfy)
    assert all(i["state"] == "ok" for i in items) and not status.needs_setup(items, c)
    assert all("size_bytes" not in i for i in items)


async def test_the_model_item_needs_only_one_configured_file(tmp_path, comfy):
    c = make_config(tmp_path)
    c.llm_models = {"gemma": tmp_path / "missing.gguf", "qwen": tmp_path / "q.gguf"}
    (tmp_path / "q.gguf").write_bytes(GGUF)
    item = by_id(await status.compute(c, comfy))["llm_model"]
    assert item["state"] == "ok" and "qwen" in item["detail"] and "gemma" in item["detail"]      # and it says the chosen one has no file


async def test_embeddings_turned_off_is_not_asked_for(tmp_path, comfy):
    c = make_config(tmp_path)
    c.embeddings_enabled = False
    item = by_id(await status.compute(c, comfy))["embeddings_model"]
    assert item["state"] == "ok" and "size_bytes" not in item
    assert "embeddings_model" not in SetupManager(c, None, root=tmp_path).todo()


async def test_a_half_built_tag_library_counts_only_what_is_missing(tmp_path, comfy):
    c = make_config(tmp_path)
    taglib = catalog.taglib_folder(c.data_dir)
    taglib.mkdir(parents=True)
    for name in catalog.TAGLIB_FILES:
        (taglib / name).write_text("x")
    item = by_id(await status.compute(c, comfy))["tag_library"]
    assert item["state"] == "missing" and item["size_bytes"] == catalog.TAG_COUNTS.size + catalog.COOCCURRENCE.size


async def test_comfyui_state_and_when_setup_opens_by_itself(tmp_path, comfy):
    c = make_config(tmp_path)
    for path in (c.llm_binary, c.embeddings_model, c.llm_models["gemma"]):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(GGUF)
    comfy.up = True
    items = await status.compute(c, comfy)
    assert by_id(items)["comfyui"]["state"] == "ok" and not status.needs_setup(items, c)
    comfy.up = False                                                    # off, and no folder to start it from: setup opens
    items = await status.compute(c, comfy)
    assert by_id(items)["comfyui"]["state"] == "missing" and status.needs_setup(items, c)
    folder = tmp_path / "ComfyUI"
    folder.mkdir()
    (folder / "main.py").write_text("")
    c.comfy_install_dir = str(folder)                                   # off, but it can be started: the person is not nagged
    items = await status.compute(c, comfy)
    assert by_id(items)["comfyui"]["state"] == "missing" and "not running" in by_id(items)["comfyui"]["detail"] and not status.needs_setup(items, c)
    c.comfy_install_dir = str(tmp_path)                                 # a folder without main.py
    items = await status.compute(c, comfy)
    assert "main.py" in by_id(items)["comfyui"]["detail"] and status.needs_setup(items, c)


# ---------- the routes, the install job and the settings they write ----------

@pytest.fixture
def toml_path(tmp_path):
    """A copy of the example settings file (CRLF, comments) whose paths all point into tmp_path."""
    path = tmp_path / "config" / "app.toml"
    path.parent.mkdir()
    text = EXAMPLE_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))
    settings.write(path, {"llm_binary": str(tmp_path / "tools" / "llama" / "llama-server.exe"),
                          "embeddings_model": str(tmp_path / "models" / "emb" / "emb.gguf")})
    return path


@pytest.fixture
def client(toml_path, comfy, tmp_path):
    config = load_config(toml_path)
    config.data_dir = tmp_path / "data"
    with TestClient(create_app(config, comfy=comfy, start_worker=False, config_path=toml_path)) as c:
        yield c


def comments(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith("#")]


def test_status_route_lists_the_items(client):
    body = client.get("/api/setup/status").json()
    assert [i["id"] for i in body["items"]] == ["comfyui", "llama_server", "llm_model", "embeddings_model", "tag_library"]
    assert body["install"]["state"] == "idle" and body["total_bytes"] == sum(i.get("size_bytes", 0) for i in body["items"])
    states = {i["id"]: i["state"] for i in body["items"]}
    assert states["comfyui"] == "ok" and states["llama_server"] == "missing"       # the fake ComfyUI is up; the language model parts are not there
    assert body["needs_setup"] is True


def test_the_comfyui_folder_is_saved_through_the_settings_file(client, toml_path, tmp_path):
    inner = tmp_path / "portable" / "ComfyUI"
    inner.mkdir(parents=True)
    (inner / "main.py").write_text("")
    before = toml_path.read_text(encoding="utf-8")
    res = client.post("/api/setup/comfy", json={"install_dir": str(inner.parent), "url": "http://127.0.0.1:9000/"})
    assert res.status_code == 200
    assert res.json()["comfy"] == {"url": "http://127.0.0.1:9000", "install_dir": str(inner), "install_dir_ok": True}
    after = toml_path.read_text(encoding="utf-8")
    assert comments(before) == comments(after) and load_config(toml_path).comfy_install_dir == str(inner)
    assert "max_loras = 3" in after and "[llm.models]" in after          # other settings are untouched
    assert client.app.state.config.comfy_url == "http://127.0.0.1:9000"


def test_a_folder_without_main_py_and_a_bad_address_are_refused(client, toml_path, tmp_path):
    before = toml_path.read_bytes()
    res = client.post("/api/setup/comfy", json={"install_dir": str(tmp_path)})
    assert res.status_code == 422 and "main.py" in res.json()["errors"]["comfy_install_dir"]
    res = client.post("/api/setup/comfy", json={"url": "localhost:8188"})
    assert res.status_code == 422 and "comfy_url" in res.json()["errors"]
    assert toml_path.read_bytes() == before


def test_choosing_a_model_adds_it_and_keeps_everything_else(client, toml_path, tmp_path):
    model = tmp_path / "my models" / "Nice Model.gguf"
    model.parent.mkdir()
    model.write_bytes(GGUF)
    before = toml_path.read_text(encoding="utf-8")
    res = client.post("/api/setup/llm-model", json={"name": "Nice model", "path": f'"{model}"'})
    assert res.status_code == 200
    items = by_id(res.json()["items"])
    assert items["llm_model"]["state"] == "ok" and "Nice model" in items["llm_model"]["detail"]
    after = toml_path.read_text(encoding="utf-8")
    assert comments(before) == comments(after) and "max_loras = 3" in after and "[embeddings]" in after
    assert 'gemma = "models/llm/gemma-12b-instruct-q4_0.gguf"' in after          # the other models stay as they were
    cfg = load_config(toml_path)
    assert cfg.llm_model == "Nice model" and cfg.llm_models["Nice model"] == model and cfg.llm_models["gemma"].name == "gemma-12b-instruct-q4_0.gguf"
    live = client.app.state.config
    assert live.llm_model == "Nice model" and live.llm_models["Nice model"] == model
    assert model.read_bytes() == GGUF                                     # never copied, moved or changed
    assert client.get("/api/config").json()["llm_model"] == "Nice model"


def test_a_model_that_is_not_gguf_is_refused_with_nothing_written(client, toml_path, tmp_path):
    fake = tmp_path / "fake.gguf"
    fake.write_bytes(b"not a model")
    before = toml_path.read_bytes()
    res = client.post("/api/setup/llm-model", json={"name": "x", "path": str(fake)})
    assert res.status_code == 422 and "GGUF" in res.json()["errors"]["path"]
    res = client.post("/api/setup/llm-model", json={"name": " ", "path": str(tmp_path / "nope.gguf")})
    assert res.status_code == 422 and set(res.json()["errors"]) == {"name", "path"}
    assert toml_path.read_bytes() == before


def test_save_settings_is_the_settings_path_and_refuses_bad_values(client):
    with pytest.raises(settings.SettingsError):
        asyncio.run(save_settings(client.app.state, {"max_loras": 99}))


def test_the_mcp_server_has_no_way_to_install_or_download():
    source = (Path(settings.__file__).parent / "mcp_server.py").read_text(encoding="utf-8")
    assert "/api/setup" not in source and "app.setup" not in source


def test_embeddings_model_is_a_setting_like_the_binary(client, toml_path):
    res = client.put("/api/config", json={"embeddings_model": "models/emb/other.gguf"})
    assert res.status_code == 200 and "model = " in toml_path.read_text(encoding="utf-8")
    assert client.put("/api/config", json={"embeddings_model": " "}).status_code == 422


# ----- the install job -----

LLAMA_ZIP_DATA = make_zip({"llama-server.exe": b"server", "ggml.dll": b"ggml", "LICENSE-LLVM-OpenMP": b"lic", "run.bat": b"not taken"})
CUDART_ZIP_DATA = make_zip({"cudart64_12.dll": b"1", "cublas64_12.dll": b"2", "cublasLt64_12.dll": b"3", "other.dll": b"not taken"})
EMB_DATA = GGUF * 10
COUNTS_DATA, PAIRS_DATA = b"tag,count\n", b"a,b,100\n"


@pytest.fixture
def pins(monkeypatch):
    """Small stand-ins for the pinned files (same shape, same hosts), and the server that serves them."""
    pinned = {"LLAMA_ZIP": make_asset("llama.zip", LLAMA_ZIP_DATA), "CUDART_ZIP": make_asset("cudart.zip", CUDART_ZIP_DATA),
              "EMBEDDINGS_MODEL": make_asset("emb.gguf", EMB_DATA, "https://huggingface.co/m/"),
              "TAG_COUNTS": make_asset("danbooru_tags.csv", COUNTS_DATA, "https://huggingface.co/d/"),
              "COOCCURRENCE": make_asset("danbooru_tags_cooccurrence.csv", PAIRS_DATA, "https://huggingface.co/d/")}
    for name, asset in pinned.items():
        monkeypatch.setattr(catalog, name, asset)
    pinned["files"] = {a.url: d for a, d in ((pinned["LLAMA_ZIP"], LLAMA_ZIP_DATA), (pinned["CUDART_ZIP"], CUDART_ZIP_DATA),
                                             (pinned["EMBEDDINGS_MODEL"], EMB_DATA), (pinned["TAG_COUNTS"], COUNTS_DATA),
                                             (pinned["COOCCURRENCE"], PAIRS_DATA))}
    return pinned


def fake_scripts(calls: list, fail: str | None = None):
    """Stands in for update_taglib.py and build_contradictions.py: writes what they would write."""
    async def run(args: list[str], on_line, cancel) -> int:
        calls.append(args)
        if fail and fail in " ".join(args):
            on_line("could not reach the server")
            return 1
        on_line("working ...")
        if args[0] == "-c":                                              # update_taglib.py via the driver: the target folder is the last argument
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            for name in catalog.TAGLIB_FILES:
                (target / name).write_text("data")
        else:                                                            # build_contradictions.py: --out <file>
            Path(args[args.index("--out") + 1]).write_text("{}")
        return 0

    return run


def install_with(client, tmp_path, pins, files=None, scripts=None, **kw):
    """Give the app an install job that talks to the fake server and writes under tmp_path."""
    st = client.app.state
    st.setup = SetupManager(st.config, lambda changes: save_settings(st, changes), root=tmp_path,
                            transport=fake_server(files if files is not None else pins["files"]), allow=anywhere,
                            run_script=scripts or fake_scripts([]), **kw)
    return st.setup


def wait_for_install(client, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        body = client.get("/api/setup/status").json()
        if body["install"]["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the install did not finish")


def test_install_everything_needed(client, toml_path, tmp_path, pins):
    calls = []
    install_with(client, tmp_path, pins, scripts=fake_scripts(calls))
    before = toml_path.read_text(encoding="utf-8")
    res = client.post("/api/setup/install")
    assert res.status_code == 200 and res.json()["install"]["state"] == "running"
    body = wait_for_install(client)
    assert body["install"]["state"] == "done" and body["install"]["error"] is None
    states = {i["id"]: i["state"] for i in body["items"]}
    assert states["llama_server"] == states["embeddings_model"] == states["tag_library"] == "ok" and states["llm_model"] == "missing"
    # files are where the app looks for them
    folder = tmp_path / catalog.LLAMA_FOLDER
    assert sorted(p.name for p in folder.iterdir()) == sorted(["llama-server.exe", "ggml.dll", "LICENSE-LLVM-OpenMP", *catalog.CUDART_FILES])
    assert (tmp_path / catalog.EMBEDDINGS_FOLDER / "emb.gguf").read_bytes() == EMB_DATA
    assert not list((tmp_path / "data" / "downloads").glob("*"))                       # the zips are removed
    co = tmp_path / "data" / "cooccurrence"
    assert (co / "contradictions.json").is_file() and not (co / "danbooru_tags.csv").exists() and not (co / "danbooru_tags_cooccurrence.csv").exists()
    assert len(calls) == 2 and calls[1][0].endswith("build_contradictions.py")
    # the settings were written through the settings file, comments kept, and are live
    after = toml_path.read_text(encoding="utf-8")
    assert comments(before) == comments(after) and "max_loras = 3" in after
    cfg = load_config(toml_path)
    assert cfg.llm_binary == tmp_path / catalog.LLAMA_BINARY and cfg.embeddings_model == tmp_path / catalog.EMBEDDINGS_FOLDER / "emb.gguf"
    live = client.app.state.config
    assert live.llm_binary == cfg.llm_binary and live.embeddings_enabled is True
    # nothing left to do: a second request has nothing to download
    assert client.post("/api/setup/install").json()["install"]["state"] == "done"


def test_a_failed_part_shows_a_message_and_the_others_still_install(client, tmp_path, pins):
    files = {**pins["files"], pins["EMBEDDINGS_MODEL"].url: 500}
    install_with(client, tmp_path, pins, files=files)
    client.post("/api/setup/install")
    body = wait_for_install(client)
    items = by_id(body["items"])
    assert body["install"]["state"] == "failed" and "1 part" in body["install"]["error"]
    assert items["embeddings_model"]["state"] == "error" and "500" in items["embeddings_model"]["detail"]
    assert items["llama_server"]["state"] == "ok" and items["tag_library"]["state"] == "ok"
    # Retry: the server is fine now, and only the failed part is done again
    client.app.state.setup.transport = fake_server(pins["files"])
    client.post("/api/setup/install")
    body = wait_for_install(client)
    assert body["install"]["state"] == "done" and by_id(body["items"])["embeddings_model"]["state"] == "ok"


def test_a_failing_script_is_reported_in_words(client, tmp_path, pins):
    install_with(client, tmp_path, pins, scripts=fake_scripts([], fail="update_taglib"))
    client.post("/api/setup/install")
    items = by_id(wait_for_install(client)["items"])
    assert items["tag_library"]["state"] == "error" and "could not reach the server" in items["tag_library"]["detail"]
    assert items["llama_server"]["state"] == "ok"


def test_a_wrong_download_is_never_installed(client, toml_path, tmp_path, pins):
    bad = pins["LLAMA_ZIP"].url
    data = pins["files"][bad]
    files = {**pins["files"], bad: data[:10] + bytes([data[10] ^ 1]) + data[11:]}      # same size, one byte different
    install_with(client, tmp_path, pins, files=files)
    client.post("/api/setup/install")
    items = by_id(wait_for_install(client)["items"])
    assert items["llama_server"]["state"] == "error" and "checksum" in items["llama_server"]["detail"]
    assert not (tmp_path / catalog.LLAMA_FOLDER).exists() and not list((tmp_path / "data" / "downloads").glob("*"))
    assert load_config(toml_path).llm_binary != tmp_path / catalog.LLAMA_BINARY


def test_cancel_stops_the_job_and_keeps_the_partial_download(client, tmp_path, pins, monkeypatch):
    big = bytes(range(256)) * 20_000                                     # 5 MB
    asset = make_asset("llama.zip", big)
    monkeypatch.setattr(catalog, "LLAMA_ZIP", asset)
    release = asyncio.Event()

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield big[:1 << 20]
            await release.wait()                                         # stalls until the test lets go (it never needs to)
            yield big[1 << 20:]

    transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=Slow()) if str(r.url) == asset.url else httpx.Response(404))
    st = client.app.state
    st.setup = SetupManager(st.config, lambda changes: save_settings(st, changes), root=tmp_path, transport=transport, allow=anywhere,
                            run_script=fake_scripts([]))
    client.post("/api/setup/install")
    end = time.time() + 10
    while time.time() < end and (client.get("/api/setup/status").json()["install"]["items"].get("llama_server", {}).get("done", 0) < 1 << 20):
        time.sleep(0.05)
    assert client.post("/api/setup/install").status_code == 409          # one job at a time
    client.post("/api/setup/install/cancel")
    # the stalled stream cannot notice a cancel until data arrives, so let it
    st.setup._task.get_loop().call_soon_threadsafe(release.set)
    body = wait_for_install(client)
    assert body["install"]["state"] == "cancelled" and by_id(body["items"])["llama_server"]["state"] == "missing"
    assert (tmp_path / "data" / "downloads" / "llama.zip.part").stat().st_size >= 1 << 20


def test_not_enough_disk_space_is_said_up_front(client, tmp_path, pins, monkeypatch):
    install_with(client, tmp_path, pins)
    monkeypatch.setattr("app.setup.installer.shutil.disk_usage", lambda p: type("U", (), {"free": 1000})())
    res = client.post("/api/setup/install")
    assert res.status_code == 409 and "free disk space" in res.json()["detail"]
    assert client.get("/api/setup/status").json()["install"]["state"] == "idle"
