"""LoRA covers and the user's own LoRA tags. Fake HTTP transports only: nothing here reaches Civitai."""
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.db import dumps, now
from app.events import EventBus
from app.library.card_models import LoraCard, Range
from app.library.cards import CardStore
from app.library.civitai import CDN_HOST, HOST, MAX_IMAGE_BYTES, CivitaiClient, preview_urls
from app.library.lora_covers import LoraCovers
from app.library.lora_extras import LoraExtras, normalize_tag, normalize_tags
from app.library.loras import LoraFile, LoraScanner
from app.main import create_app
from app.planning.context import PlanContext
from app.planning.loras import candidates_for, lora_text
from tests.test_cards import STYLE_LORA, chat_transport, lora_dir, poll  # noqa: F401  (lora_dir is a fixture)
from tests.test_gpu import FakeServer, Recorder
from tests.test_lora_files import TOKEN, FakeOi, make_safetensors

PIC = f"https://{CDN_HOST}/abc/def/original=true/1.jpeg"
PIC2 = f"https://{CDN_HOST}/abc/ghi/original=true/2.jpeg"


def picture(fmt="JPEG", size=(800, 600), color=(200, 40, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


def version(*images) -> dict:
    return {"id": 77, "modelId": 12, "name": "v2", "trainedWords": [], "model": {"name": "Ink", "type": "LORA"}, "images": list(images)}


def still(url=PIC, **extra) -> dict:
    return {"url": url, "type": "image", "nsfwLevel": 1, **extra}


class Civ:
    """A fake Civitai: the version reply for any hash, and pictures by address. Records every request."""

    def __init__(self, reply=None, pictures=None, token=TOKEN):
        self.reply, self.pictures, self.token, self.seen = reply, pictures or {}, token, []

    def handler(self, request: httpx.Request):
        self.seen.append(request)
        if request.url.host == HOST:
            return httpx.Response(200, json=self.reply) if self.reply is not None else httpx.Response(404)
        body = self.pictures.get(str(request.url))
        return httpx.Response(200, content=body) if isinstance(body, bytes) else body or httpx.Response(404)

    def client(self, cache_dir):
        return CivitaiClient(cache_dir, token_getter=lambda: self.token, transport=httpx.MockTransport(self.handler))

    def pictures_asked(self):
        return [r for r in self.seen if r.url.host != HOST]


@pytest.fixture
def folder(tmp_path, monkeypatch):
    path = tmp_path / "loras"
    path.mkdir()
    monkeypatch.setattr("app.library.loras.model_dirs", lambda *_: [path])
    return path


def make_covers(db, config, civ: Civ, names=("a.safetensors",), background=False):
    scanner = LoraScanner(db, config, FakeOi(list(names)))
    covers = LoraCovers(db, config, scanner, civ.client(config.cache_dir), LoraExtras(db), EventBus(), background=background)
    return covers, scanner


async def files_of(scanner) -> dict[str, LoraFile]:
    return {f.name: f for f in await scanner.scan()}


def cover_source(covers, f: LoraFile) -> str | None:
    return covers.info(f.name, f.path, f.sha256, covers.extras.get(f.name))["source"]


def add_image(db, config, name="img.png") -> int:
    """A finished gallery image with a real file."""
    (config.images_dir).mkdir(parents=True, exist_ok=True)
    (config.images_dir / name).write_bytes(picture("PNG", (640, 960), (10, 120, 200)))
    job = db.execute("INSERT INTO jobs (status, count, fields, created_at) VALUES ('done', 1, '{}', ?)", (now(),)).lastrowid
    return db.execute("INSERT INTO images (job_id, idx, status, seed, file_path, recipe, created_at) VALUES (?, 1, 'done', 1, ?, '{}', ?)",
                      (job, name, now())).lastrowid


# ---------- the order ----------

async def test_cover_order_user_then_file_then_civitai_then_none(db, config, folder):
    make_safetensors(folder / "a.safetensors")
    civ = Civ(version(still()), {PIC: picture()})
    covers, scanner = make_covers(db, config, civ)
    f = (await files_of(scanner))["a.safetensors"]
    assert cover_source(covers, f) is None
    assert await covers.fetch_one(f) is True and cover_source(covers, f) == "civitai"
    (folder / "a.png").write_bytes(picture("PNG"))
    assert cover_source(covers, f) == "file"                                    # a file next to the LoRA beats Civitai
    (folder / "a.preview.jpg").write_bytes(picture())
    assert covers.preview_file(f.path).name == "a.preview.jpg"                  # and `.preview.` names come first
    covers.choose_upload("a.safetensors", picture("PNG", (50, 50)))
    assert cover_source(covers, f) == "user"                                    # the user's choice beats everything
    covers.go_automatic("a.safetensors")
    assert cover_source(covers, f) == "file" and not covers.upload_path("a.safetensors").exists()
    image_id = add_image(db, config)
    covers.choose_image("a.safetensors", image_id)
    assert cover_source(covers, f) == "user"
    db.execute("DELETE FROM images WHERE id = ?", (image_id,))                  # the image is deleted: back to automatic
    assert cover_source(covers, f) == "file"
    (folder / "a.png").unlink(); (folder / "a.preview.jpg").unlink()
    assert cover_source(covers, f) == "civitai"


async def test_the_pictures_served_are_small_copies_and_the_lora_folder_is_untouched(db, config, folder):
    make_safetensors(folder / "a.safetensors")
    (folder / "a.png").write_bytes(picture("PNG", (3000, 2000)))
    before = sorted((p.name, p.stat().st_mtime_ns) for p in folder.iterdir())
    covers, scanner = make_covers(db, config, Civ())
    f = (await files_of(scanner))["a.safetensors"]
    served = covers.serve_path("a.safetensors")
    with Image.open(served) as img:
        assert img.format == "WEBP" and max(img.size) <= 480
    assert served.is_relative_to(config.thumbs_dir) and sorted((p.name, p.stat().st_mtime_ns) for p in folder.iterdir()) == before
    assert covers.serve_path("nothing.safetensors") is None and f.path.exists()


# ---------- the Civitai fetch ----------

async def test_preview_comes_without_the_token_and_only_from_the_image_server(db, config, folder):
    make_safetensors(folder / "a.safetensors")
    civ = Civ(version(still()), {PIC: picture()})
    covers, scanner = make_covers(db, config, civ)
    f = (await files_of(scanner))["a.safetensors"]
    assert await covers.fetch_one(f)
    lookup, download = civ.seen
    assert lookup.url.host == HOST and lookup.headers["authorization"] == f"Bearer {TOKEN}"
    assert download.url.host == CDN_HOST and "authorization" not in download.headers
    assert TOKEN not in "".join(p.read_text(errors="ignore") for p in config.data_dir.rglob("*") if p.is_file() and p.suffix in {".json", ".txt", ".log"})
    with Image.open(covers.civitai_path(f.sha256)) as img:
        assert img.format == "WEBP" and max(img.size) <= 480                    # re-encoded, not the original bytes


async def test_other_hosts_are_refused(tmp_path):
    civ = Civ(version(still()), {PIC: picture()})
    client = civ.client(tmp_path)
    for url in ("https://evil.example/a.jpg", f"http://{CDN_HOST}/a.jpg", "https://civitai.com.evil.example/a.jpg", f"https://{CDN_HOST}.evil.example/a.jpg"):
        assert (await client.fetch_image(url)).status == "refused"
    assert civ.seen == []                                                       # not even a request was made
    for url, headers in (("https://evil.example/x", {}), (f"https://{CDN_HOST}/x", {"Authorization": "Bearer x"})):
        with pytest.raises(httpx.RequestError):
            await client._http.get(url, headers=headers)                        # the transport guard says no as well
    assert civ.seen == []
    assert civ.seen == []


STORAGE = "https://blobs-b2.civitai.com/file/blobs-managed-public/ABC"


async def test_the_redirect_to_the_image_storage_is_followed_without_the_token(tmp_path):
    """image.civitai.com answers with a redirect to blobs-b2.civitai.com (seen on the real server)."""
    civ = Civ(pictures={PIC: httpx.Response(301, headers={"location": STORAGE}), STORAGE: picture()})
    got = await civ.client(tmp_path).fetch_image(PIC)
    assert got.status == "ok" and [r.url.host for r in civ.seen] == [CDN_HOST, "blobs-b2.civitai.com"]
    assert all("authorization" not in r.headers for r in civ.seen)


async def test_a_redirect_anywhere_else_is_refused_and_loops_stop(tmp_path):
    for target in ("https://evil.example/x", "http://blobs-b2.civitai.com/x", "https://civitai.com/x", "https://civitai.com.evil.example/x"):
        civ = Civ(pictures={PIC: httpx.Response(302, headers={"location": target})})
        assert (await civ.client(tmp_path).fetch_image(PIC)).status == "refused"
        assert [str(r.url) for r in civ.seen] == [PIC]                           # the target was never asked
    loop = Civ(pictures={PIC: httpx.Response(302, headers={"location": PIC})})
    assert (await loop.client(tmp_path).fetch_image(PIC)).status == "error" and len(loop.seen) == 3


def test_which_pictures_are_candidates():
    data = version(still("https://evil.example/a.jpg"), {"url": f"https://{CDN_HOST}/v.mp4", "type": "image"}, still(f"https://{CDN_HOST}/clip.webm"),
                   {"url": f"https://{CDN_HOST}/vid", "type": "video"}, still(PIC2, minor=True), still(PIC), still(f"https://{CDN_HOST}/anim.gif"),
                   still(f"http://{CDN_HOST}/plain.jpg"), {"type": "image"}, "junk")
    assert preview_urls(data) == [PIC]
    assert preview_urls({}) == [] and preview_urls({"images": None}) == []


async def test_nsfw_level_does_not_matter(db, config, folder):
    make_safetensors(folder / "a.safetensors")
    civ = Civ(version(still(nsfwLevel=16)), {PIC: picture()})
    covers, scanner = make_covers(db, config, civ)
    assert await covers.fetch_one((await files_of(scanner))["a.safetensors"])


@pytest.mark.parametrize("body", [b"not a picture at all", b"", picture("BMP"), picture("GIF"), b"\x89PNG\r\n\x1a\n" + b"\x00" * 20])
async def test_bad_bytes_are_refused_quietly(db, config, folder, body):
    make_safetensors(folder / "a.safetensors")
    covers, scanner = make_covers(db, config, Civ(version(still()), {PIC: body}))
    f = (await files_of(scanner))["a.safetensors"]
    assert await covers.fetch_one(f) is False
    assert not covers.civitai_path(f.sha256).exists() and not list(covers.folder.glob("*.tmp")) and cover_source(covers, f) is None
    assert db.row("SELECT * FROM lora_preview_misses WHERE sha256 = ?", (f.sha256,))


async def test_oversized_pictures_are_refused(db, config, folder):
    make_safetensors(folder / "a.safetensors")
    big = httpx.Response(200, content=b"x" * (MAX_IMAGE_BYTES + 1))
    declared = httpx.Response(200, content=b"x", headers={"content-length": str(MAX_IMAGE_BYTES + 1)})
    covers, scanner = make_covers(db, config, Civ(version(still()), {PIC: big}))
    f = (await files_of(scanner))["a.safetensors"]
    assert await covers.fetch_one(f) is False
    assert (await covers.civitai.fetch_image(PIC)).status == "too_big"
    covers.civitai._http._transport = httpx.MockTransport(lambda r: declared)
    assert (await covers.civitai.fetch_image(PIC)).status == "too_big"


async def test_videos_are_skipped_and_the_next_still_is_used(db, config, folder):
    make_safetensors(folder / "a.safetensors")
    video = {"url": f"https://{CDN_HOST}/clip.mp4", "type": "video"}
    civ = Civ(version(video, still(PIC, minor=True), still(PIC2)), {PIC2: picture()})
    covers, scanner = make_covers(db, config, civ)
    f = (await files_of(scanner))["a.safetensors"]
    assert await covers.fetch_one(f) is True
    assert [str(r.url) for r in civ.pictures_asked()] == [PIC2]                 # the video and the flagged picture were never asked for
    only_video = Civ(version(video))
    covers2, _ = make_covers(db, config, only_video)
    other = LoraFile("b.safetensors", f.path, 1, 1, "f" * 64)
    assert await covers2.fetch_one(other) is False and only_video.pictures_asked() == []


async def test_a_bad_first_picture_does_not_stop_the_second(db, config, folder):
    make_safetensors(folder / "a.safetensors")
    civ = Civ(version(still(PIC), still(PIC2)), {PIC: b"junk", PIC2: picture()})
    covers, scanner = make_covers(db, config, civ)
    f = (await files_of(scanner))["a.safetensors"]
    assert await covers.fetch_one(f) is True and cover_source(covers, f) == "civitai"


async def test_a_cached_reply_costs_no_lookup(db, config, folder):
    make_safetensors(folder / "a.safetensors")
    covers, scanner = make_covers(db, config, Civ(version(still()), {PIC: picture()}))
    f = (await files_of(scanner))["a.safetensors"]
    (config.cache_dir / "civitai").mkdir(parents=True)
    (config.cache_dir / "civitai" / f"{f.sha256}.json").write_text(json.dumps(version(still())))
    civ = Civ(None, {PIC: picture()}, token=None)                               # no token, and Civitai would say 404
    covers, _ = make_covers(db, config, civ)
    assert await covers.fetch_one(f) is True and all(r.url.host == CDN_HOST for r in civ.seen)


async def test_a_failure_is_remembered_until_rescan(db, config, folder):
    make_safetensors(folder / "a.safetensors")
    make_safetensors(folder / "b.safetensors", {"x": "y"})
    civ = Civ(None)                                                             # Civitai doesn't know either file
    covers, scanner = make_covers(db, config, civ, ("a.safetensors", "b.safetensors"), background=True)
    files = list((await files_of(scanner)).values())

    async def settle():
        if covers._runner:
            await covers._runner

    covers.schedule(files)
    await settle()
    assert len(civ.seen) == 2 and db.row("SELECT COUNT(*) AS n FROM lora_preview_misses")["n"] == 2
    covers.schedule(files); covers.schedule(files)                              # opening the Library again and again
    await settle()
    assert len(civ.seen) == 2                                                   # nothing is asked twice
    covers.forget_misses()
    covers.schedule(files)
    await settle()
    assert len(civ.seen) == 4                                                   # a rescan asks again


async def test_schedule_fetches_in_the_background_and_says_so(db, config, folder):
    make_safetensors(folder / "a.safetensors")
    civ = Civ(version(still()), {PIC: picture()})
    covers, scanner = make_covers(db, config, civ, background=True)
    queue = covers.events.subscribe()
    f = (await files_of(scanner))["a.safetensors"]
    covers.schedule([f])
    assert cover_source(covers, f) is None                                      # schedule returned at once
    await covers._runner
    assert cover_source(covers, f) == "civitai" and queue.get_nowait() == {"type": "lora_cover", "key": "a.safetensors"}
    off, _ = make_covers(db, config, civ, background=False)
    off.schedule([f]); assert off._runner is None and off.queue == []


async def test_schedule_leaves_alone_a_lora_that_has_a_cover(db, config, folder):
    make_safetensors(folder / "a.safetensors")
    (folder / "a.webp").write_bytes(picture("WEBP"))
    civ = Civ(version(still()), {PIC: picture()})
    covers, scanner = make_covers(db, config, civ, background=True)
    covers.schedule(list((await files_of(scanner)).values()))
    assert covers._runner is None and civ.seen == []


# ---------- tags ----------

def test_tags_are_normalised():
    assert normalize_tag("  Soft   WATERCOLOR \t") == "soft watercolor"
    assert normalize_tag("a" * 100) == "a" * 40 and normalize_tag("x\x00y\nz") == "x y z" and normalize_tag("   ") == ""
    assert normalize_tags(["Pastel", "pastel ", " ", "Night  Sky", "PASTEL"]) == ["pastel", "night sky"]
    assert len(normalize_tags([str(i) for i in range(100)])) == 30


def test_tags_add_and_remove_on_many_loras(db):
    extras = LoraExtras(db)
    assert extras.change_tags(["a", "b"], add=["Pastel ", "soft"]) == {"a": ["pastel", "soft"], "b": ["pastel", "soft"]}
    assert extras.change_tags(["a"], add=["pastel", "Night"], remove=["SOFT"]) == {"a": ["pastel", "night"]}
    assert extras.tags_by_key() == {"a": ["pastel", "night"], "b": ["pastel", "soft"]}
    extras.change_tags(["a"], remove=["pastel", "night"])
    assert extras.tags_by_key() == {"b": ["pastel", "soft"]}                    # a LoRA with none is left out


def test_user_tags_are_in_the_search_text():
    card = LoraCard(file="a.safetensors", what_it_does="Soft colours.", tags=["training"])
    assert "dreamy" not in lora_text("a.safetensors", card)
    text = lora_text("a.safetensors", card, ["dreamy", "my favourites"])
    assert "dreamy" in text and "my favourites" in text and "training" in text


async def test_planning_ranks_a_lora_by_the_users_tags(db, config):
    cards = CardStore(db, config)
    for name in ("one.safetensors", "two.safetensors"):
        cards.save_built("lora", name, LoraCard(file=name, family="Anima", kind="style", what_it_does="A style.", weight=Range(rec=0.8)))
        cards.approve("lora", name)
    ctx = PlanContext(config, None, None, cards, db)
    plain = await candidates_for(ctx, "Anima", "a quiet moonlit harbour", [])
    assert all(score == 0 for _, score in plain)
    LoraExtras(db).change_tags(["two.safetensors"], add=["Moonlit Harbour"])
    ranked = await candidates_for(ctx, "Anima", "a quiet moonlit harbour", [])
    assert ranked[0][0][0] == "two.safetensors" and ranked[0][1] > 0 and ranked[1][1] == 0


# ---------- used counts ----------

def add_job(db, status, loras):
    fields = {"loras": {"value": [{"name": n, "weight": 0.8} for n in loras], "source": "auto", "reason": ""}}
    db.execute("INSERT INTO jobs (status, count, fields, created_at, finished_at) VALUES (?, 1, ?, ?, ?)", (status, dumps(fields), now(), now()))


def test_used_counts_come_from_finished_jobs_and_follow_new_ones(db):
    extras = LoraExtras(db)
    assert extras.used_counts() == {}
    add_job(db, "done", ["a", "b"]); add_job(db, "done", ["a", "a"]); add_job(db, "failed", ["a"]); add_job(db, "draft", ["b"]); add_job(db, "done", [])
    assert extras.used_counts() == {"a": 2, "b": 1}                             # once per job; unfinished jobs don't count
    add_job(db, "done", ["b"])
    assert extras.used_counts() == {"a": 2, "b": 2}
    db.execute("DELETE FROM jobs WHERE status = 'done'")
    assert extras.used_counts() == {}


# ---------- through the API ----------

def api_app(config, comfy, calls=None):
    return create_app(config, comfy=comfy, start_worker=False, llm_server=FakeServer(Recorder()), llm_transport=chat_transport(calls if calls is not None else []),
                      civitai_transport=httpx.MockTransport(lambda r: httpx.Response(404)))


def test_tagging_keeps_the_approval_and_a_rebuild_keeps_tags_and_cover(config, comfy, lora_dir):
    with TestClient(api_app(config, comfy)) as client:
        client.post("/api/cards/loras/rescan")
        client.post(f"/api/cards/loras/{STYLE_LORA}/build")
        poll(client, lambda b: any(x["key"] == STYLE_LORA and x["stage"] == "done" for x in b))
        client.post(f"/api/cards/loras/{STYLE_LORA}/approve")
        before = client.get(f"/api/cards/loras/{STYLE_LORA}").json()

        tagged = client.post("/api/loras/tags", json={"keys": [STYLE_LORA, "krea2_inkbrush.safetensors"], "add": ["Pastel ", "my  faves"]})
        assert tagged.json()["tags"] == {STYLE_LORA: ["pastel", "my faves"], "krea2_inkbrush.safetensors": ["pastel", "my faves"]}
        after = client.get(f"/api/cards/loras/{STYLE_LORA}").json()
        assert after["status"] == "approved" and after["card"] == before["card"] and after["updated_at"] == before["updated_at"]   # the card is not touched
        client.post("/api/loras/tags", json={"keys": [STYLE_LORA], "remove": ["PASTEL"]})
        entries = {e["key"]: e for e in client.get("/api/cards/loras").json()}
        assert entries[STYLE_LORA]["user_tags"] == ["my faves"] and entries["krea2_inkbrush.safetensors"]["user_tags"] == ["pastel", "my faves"]
        assert entries["krea2_inkbrush.safetensors"]["status"] is None            # a LoRA with no card can be tagged too

        assert client.post(f"/api/loras/{STYLE_LORA}/cover/upload", content=picture("PNG", (900, 1200))).json()["source"] == "user"
        chosen = client.get("/api/cards/loras").json()
        url = next(e for e in chosen if e["key"] == STYLE_LORA)["cover"]["url"]
        got = client.get(url)
        assert got.status_code == 200 and got.headers["content-type"] == "image/webp"
        with Image.open(io.BytesIO(got.content)) as img:
            assert img.format == "WEBP" and max(img.size) <= 480

        client.post(f"/api/cards/loras/{STYLE_LORA}/build")                         # re-generate and take the new card
        poll(client, lambda b: any(x["key"] == STYLE_LORA and x["stage"] == "done" and x.get("pending") for x in b))
        assert client.post(f"/api/cards/loras/{STYLE_LORA}/accept").json()["status"] == "draft"
        entry = next(e for e in client.get("/api/cards/loras").json() if e["key"] == STYLE_LORA)
        assert entry["user_tags"] == ["my faves"] and entry["cover"]["source"] == "user" and entry["cover"]["url"] == url

        reset = client.delete(f"/api/loras/{STYLE_LORA}/cover")
        assert reset.json() == {"source": None, "url": None, "chosen": False}
        assert client.get(url).status_code == 404


def test_cover_routes_refuse_bad_input(config, comfy, lora_dir):
    with TestClient(api_app(config, comfy)) as client:
        client.post("/api/cards/loras/rescan")
        for body in (b"", b"<html>not a picture</html>", picture("GIF")):
            assert client.post(f"/api/loras/{STYLE_LORA}/cover/upload", content=body).status_code == 422
        assert client.post(f"/api/loras/{STYLE_LORA}/cover/upload", content=b"x" * (MAX_IMAGE_BYTES + 1)).status_code == 413
        assert client.post("/api/loras/nope.safetensors/cover/upload", content=picture()).status_code == 404
        assert client.put(f"/api/loras/{STYLE_LORA}/cover", json={"image_id": 999}).status_code == 422
        assert client.post("/api/loras/tags", json={"keys": ["nope.safetensors"], "add": ["x"]}).status_code == 404
        assert client.get("/api/cards/loras").json()[0]["cover"]["source"] is None
        assert client.get(f"/api/loras/{STYLE_LORA}/cover").status_code == 404


def test_a_gallery_image_becomes_the_cover(config, comfy, lora_dir):
    with TestClient(api_app(config, comfy)) as client:
        client.post("/api/cards/loras/rescan")
        image_id = add_image(client.app.state.db, config)
        assert client.put(f"/api/loras/{STYLE_LORA}/cover", json={"image_id": image_id}).json()["source"] == "user"
        url = next(e for e in client.get("/api/cards/loras").json() if e["key"] == STYLE_LORA)["cover"]["url"]
        with Image.open(io.BytesIO(client.get(url).content)) as img:
            assert img.format == "WEBP" and img.size == (320, 480)


def test_the_list_carries_covers_tags_and_used_counts(config, comfy, lora_dir):
    with TestClient(api_app(config, comfy)) as client:
        add_job(client.app.state.db, "done", [STYLE_LORA])
        (lora_dir / "krea2_inkbrush.png").write_bytes(picture("PNG"))
        entries = {e["key"]: e for e in client.post("/api/cards/loras/rescan").json()}
        assert entries[STYLE_LORA]["modified"] > 0 and entries[STYLE_LORA]["used"] == 1 and entries["krea2_inkbrush.safetensors"]["used"] == 0
        assert entries["krea2_inkbrush.safetensors"]["cover"]["source"] == "file" and entries[STYLE_LORA]["cover"]["source"] is None
        assert client.get(entries["krea2_inkbrush.safetensors"]["cover"]["url"]).status_code == 200
