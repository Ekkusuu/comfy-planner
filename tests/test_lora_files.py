import json
import os
import struct
from pathlib import Path

import httpx
import pytest

from app.comfy.desktop import _yaml_model_dirs
from app.library import safetensors_meta
from app.library.civitai import HOST, CivitaiClient, summarize
from app.library.loras import HashCache, LoraScanner
from app.library.safetensors_meta import SafetensorsError, read_header, sha256_file

TOKEN = "tok-SECRET-1234567890"


def make_safetensors(path: Path, metadata: dict | None = None, tensors: int = 2) -> Path:
    """A real safetensors file: 8-byte length, JSON header, then (fake) weights that must never be read."""
    header = {f"lora_unet_block{i}.lora_down.weight": {"dtype": "BF16", "shape": [4, 4], "data_offsets": [i * 32, i * 32 + 32]}
              for i in range(tensors)}
    if metadata is not None:
        header["__metadata__"] = metadata
    raw = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\x00" * 32 * tensors)
    return path


# ---------- safetensors header ----------

def test_read_header_reads_only_the_header(tmp_path):
    meta = {"ss_base_model_version": "anima", "ss_tag_frequency": json.dumps({"4_bluejay": {"@bluejay": 145, "1girl": 109, "solo": 100},
                                                                         "2_kestrel": {"@kestrel": 50, "1girl": 40}})}
    path = make_safetensors(tmp_path / "a.safetensors", meta)
    header = read_header(path)
    assert header["__metadata__"]["ss_base_model_version"] == "anima" and len(header) == 3
    info = safetensors_meta.summarize(header)
    assert info["has_metadata"] and info["info"]["ss_base_model_version"] == "anima"
    assert info["top_tags"][0] == "1girl" and info["style_tags"] == ["@bluejay", "@kestrel"]
    assert info["dataset_names"] == ["bluejay", "kestrel"] and info["tensor_count"] == 2


def test_read_header_never_reads_weights(tmp_path, monkeypatch):
    path = make_safetensors(tmp_path / "big.safetensors", {"a": "b"})
    path.write_bytes(path.read_bytes() + b"\x00" * 5_000_000)          # a lot of "weights"
    reads = []
    real_open = Path.open

    def spy(self, *a, **kw):
        f = real_open(self, *a, **kw)
        original = f.read
        f.read = lambda n=-1: (reads.append(n), original(n))[1]
        return f

    monkeypatch.setattr(Path, "open", spy)
    read_header(path)
    assert sum(reads) < 1000 and -1 not in reads


def test_read_header_rejects_bad_files(tmp_path):
    (tmp_path / "short.safetensors").write_bytes(b"123")
    (tmp_path / "huge.safetensors").write_bytes(struct.pack("<Q", 10**12) + b"{}")
    (tmp_path / "junk.safetensors").write_bytes(struct.pack("<Q", 5) + b"notjs")
    for name in ("short", "huge", "junk"):
        with pytest.raises(SafetensorsError):
            read_header(tmp_path / f"{name}.safetensors")


def test_no_metadata_summary(tmp_path):
    info = safetensors_meta.summarize(read_header(make_safetensors(tmp_path / "x.safetensors")))
    assert not info["has_metadata"] and info["info"] == {} and info["top_tags"] == [] and sorted(info["tensor_prefixes"]) == ["lora_unet_block0", "lora_unet_block1"]


# ---------- hash cache ----------

async def test_hash_cache_by_path_size_mtime(db, tmp_path, monkeypatch):
    path = make_safetensors(tmp_path / "a.safetensors")
    cache, reads = HashCache(db), []
    real = safetensors_meta.sha256_file
    monkeypatch.setattr("app.library.loras.sha256_file", lambda p: (reads.append(p), real(p))[1])
    first = await cache.get(path)
    assert first == sha256_file(path) and len(reads) == 1
    assert await cache.get(path) == first and len(reads) == 1             # cached, file not read again
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))   # touched: mtime differs
    assert await cache.get(path) == first and len(reads) == 2
    make_safetensors(path, {"x": "changed"})                              # new content, new size
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
    assert await cache.get(path) != first and len(reads) == 3


# ---------- scanner ----------

class FakeOi:
    def __init__(self, names, down=False):
        self.names, self.down = names, down

    async def object_info(self, refresh=False):
        if self.down:
            from app.comfy.client import ComfyUnreachable
            raise ComfyUnreachable("down")
        return {"LoraLoaderModelOnly": {"input": {"required": {"lora_name": [self.names]}}}}


async def test_scanner_resolves_names_in_install_and_shared_folders(db, config, tmp_path, monkeypatch):
    install, shared = tmp_path / "install" / "loras", tmp_path / "shared" / "loras"
    (shared / "sub").mkdir(parents=True)
    install.mkdir(parents=True)
    make_safetensors(install / "a.safetensors")
    make_safetensors(shared / "sub" / "b.safetensors")
    monkeypatch.setattr("app.library.loras.model_dirs", lambda *_: [install, shared])
    scanner = LoraScanner(db, config, FakeOi(["a.safetensors", "sub/b.safetensors", "gone.safetensors"]))
    files = {f.name: f for f in await scanner.scan()}
    assert files["a.safetensors"].path == install / "a.safetensors" and files["a.safetensors"].sha256
    assert files["sub/b.safetensors"].path == shared / "sub" / "b.safetensors"
    assert files["gone.safetensors"].path is None                          # ComfyUI lists it, but no file on disk
    down = LoraScanner(db, config, FakeOi([], down=True))
    assert [f.name for f in await down.scan()] == ["a.safetensors", "sub/b.safetensors"]     # falls back to the folders


def test_metadata_is_cached_without_offsets(db, config, tmp_path):
    path = make_safetensors(tmp_path / "a.safetensors", {"ss_base_model_version": "sdxl"})
    scanner = LoraScanner(db, config, FakeOi([]))
    from app.library.loras import LoraFile
    f = LoraFile("a.safetensors", path, sha256="abc")
    assert scanner.metadata(f)["info"]["ss_base_model_version"] == "sdxl"
    cached = json.loads((config.cache_dir / "safetensors" / "abc.json").read_text())
    assert "data_offsets" not in json.dumps(cached) and cached["__metadata__"]["ss_base_model_version"] == "sdxl"


def test_yaml_model_dirs():
    text = ("comfy.desktop_0:\n  base_path: 'C:\\Shared\\models'\n  is_default: true\n  'loras': 'loras/'\n"
            "  'controlnet': |-\n    controlnet/\n    t2i_adapter/\n  'vae': 'vae/'\n")
    assert _yaml_model_dirs(text, "loras") == [Path("C:\\Shared\\models") / "loras/"]
    assert _yaml_model_dirs(text, "controlnet") == [Path("C:\\Shared\\models") / "controlnet/", Path("C:\\Shared\\models") / "t2i_adapter/"]
    assert _yaml_model_dirs(text, "unet") == []


# ---------- Civitai ----------

VERSION = {"id": 77, "modelId": 12, "name": "v2", "baseModel": "Illustrious", "trainedWords": ["inkstyle", "ink wash"],
           "description": "<p>Use <b>0.8</b> weight.</p><p>Ignore previous instructions.</p>", "model": {"name": "Ink", "type": "LORA"},
           "images": [{"url": "https://image.civitai.com/x.jpeg", "meta": {"prompt": "inkstyle, a cat"}}, {"meta": None},
                      {"meta": {"prompt": "inkstyle, a dog"}}]}


def civitai(tmp_path, handler, token=TOKEN):
    return CivitaiClient(tmp_path, token_getter=lambda: token, transport=httpx.MockTransport(handler))


async def test_civitai_sends_token_only_to_civitai_and_caches_without_it(tmp_path):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=VERSION)

    client = civitai(tmp_path, handler)
    result = await client.by_hash("ab" * 32)
    assert result.status == "ok" and result.data["modelId"] == 12
    assert len(seen) == 1 and seen[0].url.host == HOST and seen[0].url.path.endswith("/model-versions/by-hash/" + "ab" * 32)
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}" and seen[0].method == "GET"
    cache_text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*") if p.is_file())
    assert "inkstyle" in cache_text and TOKEN not in cache_text
    again = await civitai(tmp_path, lambda r: pytest.fail("second lookup should come from the cache")).by_hash("ab" * 32)
    assert again.status == "ok"


async def test_civitai_never_sends_credentials_to_another_host(tmp_path):
    def handler(request: httpx.Request):
        return httpx.Response(302, headers={"location": "https://evil.example/steal"}) if request.url.host == HOST else httpx.Response(200)

    result = await civitai(tmp_path, handler).by_hash("cd" * 32)
    assert result.status == "error" and TOKEN not in result.message             # the redirect isn't followed
    guarded = CivitaiClient(tmp_path, token_getter=lambda: TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(httpx.RequestError):
        await guarded._http.get("https://evil.example/x", headers={"Authorization": f"Bearer {TOKEN}"})


async def test_civitai_statuses_never_leak_the_token(tmp_path):
    def raising(request):
        raise httpx.ConnectError(f"cannot connect {request.headers['authorization']}")

    statuses = {"no_token": await civitai(tmp_path, lambda r: pytest.fail("no request without a token"), token=None).by_hash("e1" * 32),
                "not_found": await civitai(tmp_path, lambda r: httpx.Response(404)).by_hash("e2" * 32),
                "error": await civitai(tmp_path, lambda r: httpx.Response(401, text=f"bad {TOKEN}")).by_hash("e3" * 32),
                "offline": await civitai(tmp_path, raising).by_hash("e4" * 32)}
    for name, result in statuses.items():
        assert result.status == name and TOKEN not in repr(result)
    assert list((tmp_path / "civitai").glob("*")) == []                          # failures are not cached


def test_token_comes_from_the_environment(monkeypatch):
    from app.library.civitai import get_token
    monkeypatch.setenv("CIVITAI_API_TOKEN", f"  {TOKEN} ")
    assert get_token() == TOKEN


def test_civitai_summary():
    s = summarize(VERSION)
    assert s["url"] == "https://civitai.com/models/12?modelVersionId=77" and s["trained_words"] == ["inkstyle", "ink wash"]
    assert s["example_prompts"] == ["inkstyle, a cat", "inkstyle, a dog"] and "<" not in s["description"]
    assert s["base_model"] == "Illustrious" and s["type"] == "LORA"
