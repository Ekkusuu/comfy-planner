"""Deleting LoRAs, workflows and wildcard lists with their files.

Nothing here moves a real file: `trash_files` is replaced by a stand-in that only deletes inside the test's temp folder and
records what it was asked to move. The Windows calls in app/library/trash.py are tested with fakes.
"""
import ctypes
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.comfy.convert import convert
from app.db import dumps, now
from app.events import EventBus
from app.library import trash
from app.library.cards import CardStore
from app.library.deleting import Blocked, DeleteError, Deleter, sidecars
from app.library.lora_extras import LoraExtras
from app.library.loras import LoraScanner
from app.library.trash import TrashError
from app.main import create_app
from app.queue.jobs import JobStore
from app.wildcards.store import WildcardStore
from tests.conftest import FIXTURES, KREA, TAGWF, TAGWF_LORA, load_workflow
from tests.test_cards import STYLE_LORA, WORKFLOW_FILL, chat_transport
from tests.test_gpu import FakeServer, Recorder
from tests.test_lora_files import make_safetensors


REAL_CHECK, REAL_TRASH_FILES = trash.check_recyclable, trash.trash_files          # the fixture below replaces them for everything else


# ---------- the setup: a fake ComfyUI folder in a temp dir, and a stand-in for the Recycle Bin ----------

class Bin:
    """Replaces trash.trash_files: records the files, and removes them from the temp folder like the real bin would."""

    def __init__(self, tmp_path: Path):
        self.tmp, self.moved, self.fail_on = tmp_path, [], None

    def __call__(self, paths):
        done = []
        for path in paths:
            assert self.tmp in Path(path).parents, f"a test tried to move {path}"          # never anything outside the temp folder
            if self.fail_on and Path(path).name == self.fail_on:
                error = TrashError(f"{Path(path).name} could not be moved to the Recycle Bin.")
                error.moved = done
                raise error
            Path(path).unlink()
            done.append(Path(path))
            self.moved.append(Path(path))
        return done


@pytest.fixture
def comfy_dir(tmp_path, config):
    root = tmp_path / "comfy"
    for folder in ("models/loras", "models/diffusion_models", "models/text_encoders", "models/vae", "models/upscale_models",
                   "user/default/workflows"):
        (root / folder).mkdir(parents=True)
    config.comfy_install_dir = str(root)
    return root


@pytest.fixture(autouse=True)
def fake_bin(tmp_path, monkeypatch):
    bin_ = Bin(tmp_path)
    monkeypatch.setattr("app.library.deleting.trash.trash_files", bin_)
    monkeypatch.setattr("app.library.deleting.trash.check_recyclable", lambda path: None)
    return bin_


@pytest.fixture
def deleter(db, config, comfy, library, store, comfy_dir):
    wildcards = WildcardStore(db, config)
    return Deleter(db, config, LoraScanner(db, config, comfy), library, CardStore(db, config), LoraExtras(db), wildcards, store, EventBus())


def make_lora(comfy_dir: Path, name: str = STYLE_LORA, *extra: str) -> Path:
    path = comfy_dir / "models" / "loras" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    make_safetensors(path)
    for side in extra:
        (path.parent / side).write_text("x", encoding="utf-8")
    return path


def add_card(deleter, key, sha="abc123", kind="lora"):
    from app.library.card_models import LoraCard
    card = LoraCard(file=key, sha256=sha, what_it_does="x")
    deleter.cards.save_built(kind, key, card)


def add_job(db, status, loras=(), workflow_id=None):
    fields = {"loras": {"value": [{"name": n, "weight": 0.8} for n in loras], "source": "auto", "reason": ""}}
    return db.execute("INSERT INTO jobs (workflow_id, status, description, count, fields, created_at) VALUES (?,?,?,1,?,?)",
                      (workflow_id, status, "a job", dumps(fields), now())).lastrowid


# ---------- the path guard ----------

def test_inside_accepts_files_below_a_root_only(tmp_path):
    root = tmp_path / "models"
    (root / "sub").mkdir(parents=True)
    ok, other = root / "sub" / "a.safetensors", tmp_path / "other.safetensors"
    ok.write_text("x"); other.write_text("x")
    assert trash.inside(ok, [root]) == ok
    assert trash.inside(other, [root]) is None                                  # outside
    assert trash.inside(root, [root]) is None and trash.inside(root / "sub", [root]) is None     # a folder, or the root itself
    assert trash.inside(root / "missing.safetensors", [root]) is None
    assert trash.inside(root / "sub" / ".." / ".." / "other.safetensors", [root]) is None        # walking out with ..
    assert trash.inside(root / "sub" / ".." / "sub" / "a.safetensors", [root]) is not None       # walking around inside is fine
    assert trash.inside(ok, []) is None


@pytest.mark.skipif(not hasattr(subprocess, "STARTUPINFO"), reason="junctions are a Windows feature")
def test_inside_refuses_a_junction_that_leads_out_of_the_root(tmp_path):
    root, elsewhere = tmp_path / "models", tmp_path / "elsewhere"
    root.mkdir(); elsewhere.mkdir()
    (elsewhere / "secret.safetensors").write_text("x")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), str(elsewhere)], capture_output=True)
    if made.returncode:
        pytest.skip("could not make a junction here")
    assert trash.inside(root / "link" / "secret.safetensors", [root]) is None
    assert (elsewhere / "secret.safetensors").exists()


def test_inside_refuses_a_symlinked_file(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    target = tmp_path / "real.safetensors"
    target.write_text("x")
    try:
        (root / "link.safetensors").symlink_to(target)
    except OSError:
        pytest.skip("no permission to make symlinks")
    assert trash.inside(root / "link.safetensors", [root]) is None


# ---------- the Recycle Bin module (Windows calls faked) ----------

class FakeWindows:
    def __init__(self, drive=3, code=0, remove=False):
        self.drive, self.code, self.remove, self.calls = drive, code, remove, []
        self.kernel32 = SimpleNamespace(GetDriveTypeW=lambda root: self.drive)
        self.shell32 = SimpleNamespace(SHFileOperationW=self.shell)

    def shell(self, ref):
        op = ref._obj
        names = ctypes.wstring_at(op.pFrom, 400).split("\0")[:2]            # the list ends with two nulls
        self.calls.append((op.wFunc, op.fFlags, names))
        if self.remove:
            Path(names[0]).unlink()
        return self.code


def test_to_recycle_bin_asks_windows_to_recycle_with_undo(tmp_path, monkeypatch):
    file = tmp_path / "a.safetensors"
    file.write_text("x")
    win = FakeWindows(remove=True)
    monkeypatch.setattr(ctypes, "windll", win, raising=False)
    monkeypatch.setattr(trash, "recycle_limit", lambda path: None)
    trash.to_recycle_bin(file)
    func, flags, names = win.calls[0]
    assert func == trash.FO_DELETE and flags == 0x40 | 0x10 | 0x4 | 0x400            # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    assert names == [str(file), ""]


def test_a_failed_move_raises_and_the_file_stays(tmp_path, monkeypatch):
    file = tmp_path / "a.safetensors"
    file.write_text("x")
    monkeypatch.setattr(ctypes, "windll", FakeWindows(code=0x7C), raising=False)
    monkeypatch.setattr(trash, "recycle_limit", lambda path: None)
    with pytest.raises(TrashError, match="could not be moved"):
        trash.to_recycle_bin(file)
    assert file.exists()


def test_files_that_windows_would_delete_for_good_are_refused(tmp_path, monkeypatch):
    file = tmp_path / "big.safetensors"
    file.write_bytes(b"x" * 3000)
    monkeypatch.setattr(ctypes, "windll", FakeWindows(), raising=False)
    monkeypatch.setattr(trash, "recycle_limit", lambda path: None)
    assert REAL_CHECK(file) is None
    monkeypatch.setattr(trash, "recycle_limit", lambda path: 0)
    assert "switched off" in REAL_CHECK(file)
    monkeypatch.setattr(trash, "recycle_limit", lambda path: 1000)
    assert "larger than the Recycle Bin" in REAL_CHECK(file)
    monkeypatch.setattr(ctypes, "windll", FakeWindows(drive=4), raising=False)          # a network drive
    assert "no Recycle Bin" in REAL_CHECK(file)
    monkeypatch.setattr(trash, "check_recyclable", REAL_CHECK)             # to_recycle_bin looks it up by name
    with pytest.raises(TrashError, match="was not moved"):
        trash.to_recycle_bin(file)
    assert file.exists()


def test_trash_files_stops_at_the_first_failure_and_says_what_went_before(tmp_path, monkeypatch):
    names = [tmp_path / n for n in ("a", "b", "c")]
    for n in names:
        n.write_text("x")
    gone = []

    def move(path):
        if path.name == "b":
            raise TrashError("b failed")
        gone.append(path.name)

    monkeypatch.setattr(trash, "to_recycle_bin", move)
    with pytest.raises(TrashError) as e:
        REAL_TRASH_FILES(names)
    assert gone == ["a"] and e.value.moved == [names[0]]


# ---------- sidecar files ----------

def test_sidecars_are_the_files_that_belong_to_this_model_only(tmp_path):
    folder = tmp_path
    for name in ("foo.safetensors", "foo.preview.png", "foo.civitai.info", "foo.json", "foo.v2.safetensors", "foo.v2.preview.png", "foo.v2.json",
                 "foobar.png", "foo.safetensors.bak", "other.png"):
        (folder / name).write_text("x")
    assert [p.name for p in sidecars(folder / "foo.safetensors")] == ["foo.civitai.info", "foo.json", "foo.preview.png"]
    assert [p.name for p in sidecars(folder / "foo.v2.safetensors")] == ["foo.v2.json", "foo.v2.preview.png"]


# ---------- LoRAs ----------

def test_lora_plan_lists_the_file_its_sidecars_and_what_goes_with_it(deleter, db, comfy_dir):
    path = make_lora(comfy_dir, STYLE_LORA, STYLE_LORA.replace(".safetensors", ".preview.png"), STYLE_LORA.replace(".safetensors", ".json"))
    make_lora(comfy_dir, "other.safetensors", "other.png")
    add_card(deleter, STYLE_LORA)
    deleter.extras.change_tags([STYLE_LORA], ["pastel"])
    plan = deleter.lora_plan(STYLE_LORA)
    assert [f["path"] for f in plan["files"]] == [STYLE_LORA, STYLE_LORA.replace(".safetensors", ".json"), STYLE_LORA.replace(".safetensors", ".preview.png")]
    assert plan["files"][0]["kind"] == "LoRA file" and plan["files"][0]["size"] == path.stat().st_size
    assert plan["files"][0]["full"] == str(path) and plan["blocked"] is None and plan["models"] == []
    assert "Its card (draft)" in plan["records"] and "Your tags and cover choice" in plan["records"]
    assert not any("other" in f["path"] for f in plan["files"])


def test_lora_plan_for_a_lora_only_the_app_remembers(deleter):
    add_card(deleter, "gone.safetensors")
    plan = deleter.lora_plan("gone.safetensors")
    assert plan["files"] == [] and "already gone" in plan["records"][0]
    with pytest.raises(KeyError):
        deleter.lora_plan("never-heard-of-it.safetensors")


def test_lora_plan_counts_the_jobs_that_use_it_and_blocks_while_one_runs(deleter, db, comfy_dir):
    make_lora(comfy_dir)
    queued, draft = add_job(db, "queued", [STYLE_LORA]), add_job(db, "ready", [STYLE_LORA, "x.safetensors"])
    add_job(db, "queued", ["x.safetensors"]); add_job(db, "done", [STYLE_LORA])
    plan = deleter.lora_plan(STYLE_LORA)
    assert [j["id"] for j in plan["jobs"]["queued"]] == [queued] and [j["id"] for j in plan["jobs"]["open"]] == [draft]
    assert plan["blocked"] is None                                               # queued jobs only warn
    running = add_job(db, "running", [STYLE_LORA])
    plan = deleter.lora_plan(STYLE_LORA)
    assert f"#{running}" in plan["blocked"] and "generating" in plan["blocked"]


async def test_deleting_a_lora_moves_the_files_and_cleans_every_record(deleter, db, config, comfy_dir, fake_bin):
    side = STYLE_LORA.replace(".safetensors", ".preview.png")
    path = make_lora(comfy_dir, STYLE_LORA, side)
    other = make_lora(comfy_dir, "krea2_inkbrush.safetensors")
    db.execute("INSERT INTO file_hashes (path, size, mtime_ns, sha256) VALUES (?,?,?,?)", (str(path), path.stat().st_size, path.stat().st_mtime_ns, "sha-one"))
    db.execute("INSERT INTO file_hashes (path, size, mtime_ns, sha256) VALUES (?,?,?,?)", (str(other), 1, 1, "sha-two"))
    db.execute("INSERT INTO lora_preview_misses (sha256, reason, at) VALUES ('sha-one', 'none', 'now')")
    add_card(deleter, STYLE_LORA, "sha-one"); add_card(deleter, "krea2_inkbrush.safetensors", "sha-two")
    deleter.extras.change_tags([STYLE_LORA, "krea2_inkbrush.safetensors"], ["pastel"])
    from app.library.lora_covers import name_hash
    thumbs = config.thumbs_dir / "lora"
    cache = {thumbs / "civitai-sha-one.webp", thumbs / f"upload-{name_hash(STYLE_LORA)}.webp", thumbs / "civitai-sha-two.webp",
             config.cache_dir / "safetensors" / "sha-one.json", config.cache_dir / "civitai" / "sha-one.json"}
    for p in cache:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")

    out = await deleter.delete_lora(STYLE_LORA)

    assert out == {"deleted": STYLE_LORA, "files": 2}
    assert [p.name for p in fake_bin.moved] == [STYLE_LORA, side]                  # the LoRA itself goes first
    assert not path.exists() and other.exists()
    assert deleter.cards.get("lora", STYLE_LORA) is None and deleter.extras.get(STYLE_LORA) is None
    assert db.row("SELECT 1 FROM file_hashes WHERE path = ?", (str(path),)) is None and db.row("SELECT 1 FROM lora_preview_misses") is None
    assert [p.name for p in cache if p.exists()] == ["civitai-sha-two.webp"]      # only the other LoRA's cover stays
    assert deleter.cards.get("lora", "krea2_inkbrush.safetensors") and deleter.extras.get("krea2_inkbrush.safetensors")
    assert db.row("SELECT 1 FROM file_hashes WHERE path = ?", (str(other),))


async def test_a_copy_of_the_file_under_another_name_keeps_the_shared_covers(deleter, db, config, comfy_dir):
    one, two = make_lora(comfy_dir, "one.safetensors"), make_lora(comfy_dir, "two.safetensors")
    for p in (one, two):
        db.execute("INSERT INTO file_hashes (path, size, mtime_ns, sha256) VALUES (?,?,?,?)", (str(p), p.stat().st_size, p.stat().st_mtime_ns, "same"))
    cover = config.thumbs_dir / "lora" / "civitai-same.webp"
    cover.parent.mkdir(parents=True)
    cover.write_text("x")
    await deleter.delete_lora("one.safetensors")
    assert cover.exists()


async def test_a_failed_move_changes_nothing_in_the_database(deleter, db, comfy_dir, fake_bin):
    side = "a.preview.png"
    path = make_lora(comfy_dir, "a.safetensors", side)
    add_card(deleter, "a.safetensors")
    deleter.extras.change_tags(["a.safetensors"], ["x"])
    queued = add_job(db, "queued", ["a.safetensors"])
    fake_bin.fail_on = "a.safetensors"
    with pytest.raises(TrashError):
        await deleter.delete_lora("a.safetensors", cancel_jobs=True)
    assert path.exists() and deleter.cards.get("lora", "a.safetensors") and deleter.extras.get("a.safetensors")
    assert db.row("SELECT status FROM jobs WHERE id = ?", (queued,))["status"] == "queued"
    fake_bin.fail_on = side                                       # the LoRA moved but its picture could not: still nothing in the database
    with pytest.raises(TrashError) as e:
        await deleter.delete_lora("a.safetensors")
    assert e.value.moved == [path] and deleter.cards.get("lora", "a.safetensors")
    fake_bin.fail_on = None                                       # delete again: the rest goes and the records follow
    out = await deleter.delete_lora("a.safetensors")
    assert out["files"] == 1 and deleter.cards.get("lora", "a.safetensors") is None


async def test_a_running_job_stops_the_delete_and_queued_ones_are_cancelled_only_when_asked(deleter, db, comfy_dir, fake_bin):
    make_lora(comfy_dir)
    queued = add_job(db, "queued", [STYLE_LORA])
    running = add_job(db, "running", [STYLE_LORA])
    with pytest.raises(Blocked):
        await deleter.delete_lora(STYLE_LORA)
    assert fake_bin.moved == []
    db.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (running,))
    make_lora(comfy_dir, "b.safetensors")
    other = add_job(db, "queued", ["b.safetensors"])
    await deleter.delete_lora("b.safetensors")                  # not ticked: the queued job is left as it is (it will fail when it runs)
    assert db.row("SELECT status FROM jobs WHERE id = ?", (other,))["status"] == "queued"
    await deleter.delete_lora(STYLE_LORA, cancel_jobs=True)
    assert db.row("SELECT status FROM jobs WHERE id = ?", (queued,))["status"] == "cancelled"
    assert db.row("SELECT status FROM jobs WHERE id = ?", (running,))["status"] == "done"      # history stays


async def test_a_lora_outside_the_lora_folders_is_never_touched(deleter, tmp_path, comfy_dir, fake_bin, monkeypatch):
    stray = tmp_path / "elsewhere" / "stray.safetensors"
    stray.parent.mkdir()
    make_safetensors(stray)
    monkeypatch.setattr(deleter.scanner, "resolve", lambda name: stray)           # even if something resolved a name to it
    add_card(deleter, "stray.safetensors")
    with pytest.raises(DeleteError, match="outside the folders"):
        deleter.lora_plan("stray.safetensors")
    assert stray.exists() and fake_bin.moved == []


# ---------- workflows ----------

def put_models(comfy_dir: Path, deleter, graph_name: str):
    """Create the model files a fixture workflow loads (100 bytes each) so the plan can find them."""
    oi = json.loads((FIXTURES / "object_info.json").read_text(encoding="utf-8"))
    for _, folders, value in deleter.model_refs(convert(load_workflow(graph_name), oi).graph):
        path = comfy_dir / "models" / folders[0] / value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * 100)


async def import_in_comfy(library, comfy_dir: Path, name: str) -> dict:
    """Import a fixture workflow from ComfyUI's own workflow folder, as the Library does."""
    target = comfy_dir / "user" / "default" / "workflows" / f"{name}.json"
    shutil.copyfile(FIXTURES / f"{name}.json", target)
    return await library.import_path(target)


async def two_workflows(library, deleter, comfy_dir):
    krea = await import_in_comfy(library, comfy_dir, KREA)
    tagwf = await import_in_comfy(library, comfy_dir, TAGWF)
    for name in (KREA, TAGWF):
        put_models(comfy_dir, deleter, name)
    return krea, tagwf


async def test_workflow_plan_offers_models_nobody_else_uses_and_names_who_keeps_the_rest(deleter, library, comfy_dir):
    krea, tagwf = await two_workflows(library, deleter, comfy_dir)
    plan = deleter.workflow_plan(krea["id"])
    models = {m["path"]: m for m in plan["models"]}
    assert models["nl_diffusion_int8.safetensors"]["removable"] and models["nl_text_encoder.safetensors"]["removable"]
    shared = models["shared_vae.safetensors"]                                  # the tag workflow loads the same VAE
    assert not shared["removable"] and shared["used_by"] == [tagwf["title"]] and tagwf["title"] in shared["why_kept"]
    assert models["nl_diffusion_int8.safetensors"]["kind"] == "Diffusion model" and models["nl_text_encoder.safetensors"]["kind"] == "Text encoder"
    assert models["nl_diffusion_int8.safetensors"]["size"] == 100 and models["nl_diffusion_int8.safetensors"]["folder"] == "diffusion_models"
    assert [m["removable"] for m in plan["models"]] == [True, True, False]        # offered ones first
    assert [f["kind"] for f in plan["files"]] == ["ComfyUI workflow file"] and plan["files"][0]["path"] == f"{KREA}.json"
    assert plan["blocked"] is None
    plan = deleter.workflow_plan(tagwf["id"])           # TAGWF shares only its VAE with Krea; the rest are its own
    assert {m["path"] for m in plan["models"] if m["removable"]} == {"tagmodel_base_v14.safetensors", "tagmodel_text_v14.safetensors", "upscaler_4x_a.safetensors"}


async def test_a_workflow_that_shares_everything_offers_nothing(deleter, library, comfy_dir):
    tagwf = await import_in_comfy(library, comfy_dir, TAGWF)
    style = await import_in_comfy(library, comfy_dir, TAGWF_LORA)
    put_models(comfy_dir, deleter, TAGWF)
    plan = deleter.workflow_plan(style["id"])
    assert plan["models"] and not any(m["removable"] for m in plan["models"])
    assert all(m["used_by"] == [tagwf["title"]] for m in plan["models"])


async def test_a_model_in_a_lora_folder_is_kept_and_missing_files_are_not_listed(deleter, library, comfy_dir):
    krea = await import_in_comfy(library, comfy_dir, KREA)
    put_models(comfy_dir, deleter, KREA)
    (comfy_dir / "models" / "diffusion_models" / "nl_diffusion_int8.safetensors").unlink()       # not on disk
    deleter.model_roots = lambda folders=(): [comfy_dir / "models" / f for f in folders]
    deleter.lora_roots = lambda: [comfy_dir / "models" / "vae"]                            # pretend the VAE sits in a LoRA folder
    models = {m["path"]: m for m in deleter.workflow_plan(krea["id"])["models"]}
    assert "nl_diffusion_int8.safetensors" not in models
    assert not models["shared_vae.safetensors"]["removable"] and "LoRA folder" in models["shared_vae.safetensors"]["why_kept"]


async def test_deleting_a_workflow_removes_its_records_its_file_and_the_ticked_models(deleter, db, library, config, comfy_dir, fake_bin):
    krea, tagwf = await two_workflows(library, deleter, comfy_dir)
    deleter.cards.save_built("workflow", krea["id"], workflow_card())
    source, copy = Path(krea["source_path"]), Path(krea["api_path"])
    unet = comfy_dir / "models" / "diffusion_models" / "nl_diffusion_int8.safetensors"
    clip = comfy_dir / "models" / "text_encoders" / "nl_text_encoder.safetensors"
    vae = comfy_dir / "models" / "vae" / "shared_vae.safetensors"
    plan = deleter.workflow_plan(krea["id"])
    ticked = [m["id"] for m in plan["models"] if m["path"] == unet.name]                  # the user ticks the diffusion model only

    done = add_job(db, "done", workflow_id=krea["id"])
    image = db.execute("INSERT INTO images (job_id, idx, status, seed, recipe, created_at) VALUES (?,1,'done',1,?,?)", (done, dumps({"workflow_id": krea["id"]}), now())).lastrowid
    out = await deleter.delete_workflow(krea["id"], ticked)

    assert out["files"] == 2 and [p.name for p in fake_bin.moved] == [source.name, unet.name]       # the workflow file first, then the model
    assert not source.exists() and not unet.exists() and clip.exists() and vae.exists()       # unticked and shared models stay
    assert not copy.exists()                                                                   # the app's own converted copy goes too
    assert db.row("SELECT 1 FROM workflows WHERE id = ?", (krea["id"],)) is None and deleter.cards.get("workflow", krea["id"]) is None
    assert library.get(tagwf["id"])                                                             # the other workflow is untouched
    assert db.row("SELECT workflow_id FROM jobs WHERE id = ?", (done,))["workflow_id"] is None     # history stays, naming no workflow
    assert db.row("SELECT 1 FROM images WHERE id = ?", (image,))


def workflow_card():
    from app.library.card_models import WorkflowCard
    return WorkflowCard.model_validate(WORKFLOW_FILL)


async def test_only_models_from_the_plan_can_be_ticked(deleter, library, comfy_dir, fake_bin, tmp_path):
    krea, tagwf = await two_workflows(library, deleter, comfy_dir)
    vae = [m["id"] for m in deleter.workflow_plan(krea["id"])["models"] if m["path"] == "shared_vae.safetensors"][0]
    with pytest.raises(DeleteError, match="can't be deleted any more"):
        await deleter.delete_workflow(krea["id"], [vae])                             # shared with the tag workflow
    sneaky = tmp_path / "precious.txt"
    sneaky.write_text("x")
    with pytest.raises(DeleteError):
        await deleter.delete_workflow(krea["id"], [str(sneaky)])                     # an arbitrary path
    assert fake_bin.moved == [] and sneaky.exists() and library.get(krea["id"])


async def test_a_workflow_file_outside_comfys_workflow_folder_is_left_alone(deleter, library, comfy_dir, fake_bin):
    wf = await library.import_path(FIXTURES / f"{KREA}.json")                       # imported from the test fixtures folder
    plan = deleter.workflow_plan(wf["id"])
    assert plan["files"] == [] and any("outside ComfyUI's workflow folder" in k for k in plan["kept"])
    await deleter.delete_workflow(wf["id"])
    assert fake_bin.moved == [] and (FIXTURES / f"{KREA}.json").exists()
    assert library.list() == []                                                      # only the app's registration went


async def test_workflow_jobs_running_block_queued_ones_follow_the_choice(deleter, db, library, comfy_dir, fake_bin):
    krea = await import_in_comfy(library, comfy_dir, KREA)
    running = add_job(db, "running", workflow_id=krea["id"])
    with pytest.raises(Blocked, match="generating"):
        await deleter.delete_workflow(krea["id"])
    db.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (running,))
    queued, ready = add_job(db, "queued", workflow_id=krea["id"]), add_job(db, "ready", workflow_id=krea["id"])
    plan = deleter.workflow_plan(krea["id"])
    assert [j["id"] for j in plan["jobs"]["queued"]] == [queued] and [j["id"] for j in plan["jobs"]["open"]] == [ready]
    await deleter.delete_workflow(krea["id"])                                         # not asked to cancel: the jobs become drafts to re-home
    for job_id in (queued, ready):
        job = db.row("SELECT * FROM jobs WHERE id = ?", (job_id,))
        assert job["status"] == "draft" and job["workflow_id"] is None
        assert job["issues"][-1]["code"] == "no_workflow" and job["issues"][-1]["severity"] == "blocking"


async def test_cancelling_the_queued_jobs_when_ticked(deleter, db, library, comfy_dir):
    krea = await import_in_comfy(library, comfy_dir, KREA)
    queued, ready = add_job(db, "queued", workflow_id=krea["id"]), add_job(db, "ready", workflow_id=krea["id"])
    await deleter.delete_workflow(krea["id"], cancel_jobs=True)
    assert db.row("SELECT status, workflow_id FROM jobs WHERE id = ?", (queued,)) == {"status": "cancelled", "workflow_id": None}
    assert db.row("SELECT status FROM jobs WHERE id = ?", (ready,))["status"] == "draft"


async def test_a_failed_move_leaves_the_workflow_registered(deleter, db, library, comfy_dir, fake_bin):
    krea = await import_in_comfy(library, comfy_dir, KREA)
    put_models(comfy_dir, deleter, KREA)
    unet = [m for m in deleter.workflow_plan(krea["id"])["models"] if m["removable"]][0]
    fake_bin.fail_on = Path(unet["full"]).name
    with pytest.raises(TrashError):
        await deleter.delete_workflow(krea["id"], [unet["id"]])
    assert library.get(krea["id"]) and Path(krea["api_path"]).exists()


# ---------- wildcard lists ----------

def make_list(config, name: str) -> Path:
    path = config.wildcards_dir / f"{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a\nb\nc\n", encoding="utf-8")
    return path


def test_wildcard_plan_and_delete(deleter, db, config, fake_bin):
    one, two = make_list(config, "background/forest"), make_list(config, "background/city")
    other = make_list(config, "clothing/hats")
    db.execute("INSERT INTO wildcard_lists (name, source, imported_at, enabled) VALUES (?,?,?,0)", ("background/forest", "C:\\forge\\wildcards", now()))
    plan = deleter.wildcard_plan("background/forest")
    assert plan["kind"] == "wildcard" and plan["entries"] == 3 and plan["blocked"] is None
    assert [f["path"] for f in plan["files"]] == ["background/forest.txt"] and plan["files"][0]["size"] == one.stat().st_size
    assert "Its on/off switch and role" in plan["records"] and "Re-importing" in plan["kept"][0] and "C:\\forge\\wildcards" in plan["kept"][0]

    out = deleter.delete_wildcard("background/forest")
    assert out == {"deleted": "background/forest", "files": 1} and fake_bin.moved == [one]
    assert two.exists() and other.exists()
    assert db.row("SELECT 1 FROM wildcard_lists WHERE name = 'background/forest'") is None
    assert [l.name for l in deleter.wildcards.lists()] == ["background/city", "clothing/hats"]
    deleter.delete_wildcard("clothing/hats")                                      # the folder's last list: the empty folder goes too
    assert not (config.wildcards_dir / "clothing").exists() and (config.wildcards_dir / "background").exists()
    with pytest.raises(KeyError):
        deleter.wildcard_plan("background/forest")


def test_a_failed_wildcard_move_keeps_the_row(deleter, db, config, fake_bin):
    make_list(config, "background/forest")
    db.execute("INSERT INTO wildcard_lists (name, source, imported_at) VALUES (?,?,?)", ("background/forest", "", now()))
    fake_bin.fail_on = "forest.txt"
    with pytest.raises(TrashError):
        deleter.delete_wildcard("background/forest")
    assert db.row("SELECT 1 FROM wildcard_lists WHERE name = 'background/forest'") and deleter.wildcards.get("background/forest")


# ---------- through the API ----------

def api_app(config, comfy):
    return create_app(config, comfy=comfy, start_worker=False, llm_server=FakeServer(Recorder()), llm_transport=chat_transport([]),
                      civitai_transport=httpx.MockTransport(lambda r: httpx.Response(404)))


def test_the_lora_endpoints_and_a_rescan_does_not_bring_it_back(config, comfy, comfy_dir, fake_bin):
    make_lora(comfy_dir, STYLE_LORA, STYLE_LORA.replace(".safetensors", ".png"))
    make_lora(comfy_dir, "krea2_inkbrush.safetensors")
    with TestClient(api_app(config, comfy)) as client:
        client.post("/api/cards/loras/rescan")
        client.post("/api/loras/tags", json={"keys": [STYLE_LORA], "add": ["pastel"]})
        plan = client.get(f"/api/loras/{STYLE_LORA}/delete-plan")
        assert plan.status_code == 200 and [f["kind"] for f in plan.json()["files"]] == ["LoRA file", "Preview picture"]
        assert client.get("/api/loras/nothing.safetensors/delete-plan").status_code == 404

        done = client.delete(f"/api/loras/{STYLE_LORA}/file")
        assert done.status_code == 200 and done.json()["files"] == 2
        keys = [e["key"] for e in client.post("/api/cards/loras/rescan").json()]
        assert STYLE_LORA not in keys and "krea2_inkbrush.safetensors" in keys
        assert client.get(f"/api/loras/{STYLE_LORA}/delete-plan").status_code == 404           # nothing of it is left (no card, no tags, no file)
        make_lora(comfy_dir)                                                                     # the file comes back (a restore from the bin)
        assert STYLE_LORA in [e["key"] for e in client.post("/api/cards/loras/rescan").json()]


def test_a_lora_delete_with_a_running_job_is_409_and_a_failed_move_explains_itself(config, comfy, comfy_dir, fake_bin):
    make_lora(comfy_dir)
    with TestClient(api_app(config, comfy)) as client:
        db = client.app.state.db
        job = add_job(db, "running", [STYLE_LORA])
        busy = client.delete(f"/api/loras/{STYLE_LORA}/file")
        assert busy.status_code == 409 and "generating" in busy.json()["detail"]
        db.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job,))
        fake_bin.fail_on = STYLE_LORA
        failed = client.delete(f"/api/loras/{STYLE_LORA}/file")
        assert failed.status_code == 409 and "Nothing was changed in the app" in failed.json()["detail"]
        assert (comfy_dir / "models" / "loras" / STYLE_LORA).exists()


def test_the_workflow_endpoints(config, comfy, comfy_dir, fake_bin):
    target = comfy_dir / "user" / "default" / "workflows" / f"{KREA}.json"
    shutil.copyfile(FIXTURES / f"{KREA}.json", target)
    with TestClient(api_app(config, comfy)) as client:
        wf = client.post("/api/workflows/import", json={"path": str(target)}).json()
        for _, folders, value in client.app.state.deleter.model_refs(client.app.state.library.get_graph(wf["id"])):
            (comfy_dir / "models" / folders[0] / value).write_text("x")
        plan = client.get(f"/api/workflows/{wf['id']}/delete-plan").json()
        assert plan["kind"] == "workflow" and sum(m["removable"] for m in plan["models"]) == 3
        assert client.get("/api/workflows/nope/delete-plan").status_code == 404
        bad = client.request("DELETE", f"/api/workflows/{wf['id']}", json={"models": ["C:\\Windows\\notepad.exe"]})
        assert bad.status_code == 400
        done = client.request("DELETE", f"/api/workflows/{wf['id']}", json={"models": [plan["models"][0]["id"]]})
        assert done.status_code == 200 and done.json()["files"] == 2
        assert client.get("/api/workflows").json() == [] and not target.exists()
        assert client.delete(f"/api/workflows/{wf['id']}").status_code == 404


def test_the_wildcard_endpoints_are_not_swallowed_by_the_list_routes(config, comfy, fake_bin):
    make_list(config, "background/forest")
    with TestClient(api_app(config, comfy)) as client:
        assert client.get("/api/wildcards/background/forest").json()["entries"] == ["a", "b", "c"]
        plan = client.get("/api/wildcards/background/forest/delete-plan")
        assert plan.status_code == 200 and plan.json()["key"] == "background/forest"
        assert client.delete("/api/wildcards/background/forest").json()["deleted"] == "background/forest"
        assert client.get("/api/wildcards").json()["lists"] == []
        assert client.delete("/api/wildcards/background/forest").status_code == 404


def test_the_ui_gets_an_event_after_a_delete(config, comfy, comfy_dir, fake_bin):
    make_lora(comfy_dir)
    with TestClient(api_app(config, comfy)) as client:
        queue = client.app.state.events.subscribe()
        client.delete(f"/api/loras/{STYLE_LORA}/file")
        assert queue.get_nowait() == {"type": "library", "kind": "lora", "key": STYLE_LORA}
