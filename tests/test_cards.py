import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.events import EventBus
from app.gpu import GpuLease
from app.library import civitai
from app.library.card_builder import CardBuilder, clean_words, lora_candidates, merge_lora, merge_workflow, prefill_workflow
from app.library.card_models import LoraCard, WorkflowCard
from app.library.cards import CardError, CardStore
from app.library.civitai import CivitaiClient
from app.library.loras import LoraScanner
from app.library.safetensors_meta import is_caption_text, summarize as summarize_header
from app.planning.prompts import finalize_prompts, lora_requirements
from app.main import create_app
from tests.conftest import FIXTURES, KREA, TAGWF, TAGWF_LORA, import_fixture
from tests.test_gpu import FakeServer, FakeWorker, Recorder
from tests.test_lora_files import TOKEN, VERSION, make_safetensors

STYLE_LORA = "model_styles_anima-base1_v6.safetensors"
WORKFLOW_FILL = {
    "purpose": "Anime images with a tag prompt.", "best_for": "characters", "family": "Anima",
    "prompt_style": {"kind": "tags", "quality_prefix": "masterpiece, best quality", "rules": ["Tags only."]},
    "settings": {"cfg": {"rec": 4, "min": 3, "max": 6}, "steps": {"rec": 30, "min": 20, "max": 40}, "samplers": ["er_sde", "euler"],
                 "schedulers": ["simple"], "sizes": [{"w": 1024, "h": 1536, "label": "portrait"}], "pixel_budget": {"min": 500000, "max": 2000000},
                 "hires": {"steps": {"rec": 15, "min": 10, "max": 25}, "denoise": {"rec": 0.5, "min": 0.3, "max": 0.7}}},
    "warnings": ["Keep CFG under 6."], "examples": ["masterpiece, 1girl, solo", "masterpiece, 1boy, solo"], "use_full_notes": False}
LORA_FILL = {"what_it_does": "Applies one of many artist styles.", "kind": "style", "family": "Anima", "trigger_words": ["@bluejay", "madeup"],
             "weight": {"rec": 0.8, "min": 0.5, "max": 1.2}, "usage_notes": ["Pick one @style tag."]}
HEADER_META = {"ss_base_model_version": "anima", "ss_tag_frequency": json.dumps({"4_bluejay": {"@bluejay": 145, "1girl": 109}})}


# ---------- pre-fill and merge (pure code) ----------

async def test_prefill_reads_certain_facts_from_slots_notes_and_graph(library):
    wf = await import_fixture(library, TAGWF_LORA)
    card = prefill_workflow(library.get(wf["id"]), library.get_graph(wf["id"]))
    d = wf["slots"]["defaults"]
    assert card.kind == "t2i" and card.settings.cfg.rec == d["cfg"] and card.settings.steps.rec == d["steps"]
    assert card.settings.samplers == [d["sampler"]] and card.settings.sizes[0].w == d["width"]
    assert card.settings.hires.denoise.rec == d["hires"]["denoise"]
    assert card.default_loras[0].name == STYLE_LORA and card.models and all(m.lower().endswith((".safetensors", ".gguf")) for m in card.models)
    assert not any("lora" in m.lower() and m == STYLE_LORA for m in card.models)         # LoRAs are listed separately
    assert card.notes_full.startswith("###") and card.default_negative == d["negative"]
    assert card.purpose == "" and card.needs_attention == []


async def test_prefill_for_workflow_without_hires(library):
    wf = await import_fixture(library, KREA)
    card = prefill_workflow(library.get(wf["id"]), library.get_graph(wf["id"]))
    assert card.settings.hires is None and card.settings.cfg.rec == wf["slots"]["defaults"]["cfg"]


async def test_default_negative_on_the_card_is_the_cleaned_one(library, config):
    config.strip_from_default_negative = ["lowres"]
    wf = await import_fixture(library, TAGWF)
    card = prefill_workflow(library.get(wf["id"]), library.get_graph(wf["id"]))
    assert "lowres" not in [t.strip() for t in card.default_negative.split(",")]
    assert card.default_negative                                           # the rest of the negative stays


async def test_merge_keeps_code_facts_and_current_values(library):
    wf_row = await import_fixture(library, TAGWF_LORA)
    wf = library.get(wf_row["id"])
    base = prefill_workflow(wf, library.get_graph(wf["id"]))
    d = wf["slots"]["defaults"]
    card = merge_workflow(base, WORKFLOW_FILL, wf)
    assert card.family == "Anima" and card.purpose and card.prompt_style.kind == "tags" and card.needs_attention == []
    assert card.settings.samplers[0] == d["sampler"] and "er_sde" in card.settings.samplers      # current first, LLM extras after
    assert card.settings.cfg.min <= d["cfg"] <= card.settings.cfg.max and card.settings.steps.min <= d["steps"] <= card.settings.steps.max
    assert [(s.w, s.h) for s in card.settings.sizes][0] == (d["width"], d["height"]) and (1024, 1536) in [(s.w, s.h) for s in card.settings.sizes]
    assert card.models == base.models and card.notes_full == base.notes_full and card.default_loras == base.default_loras


async def test_merge_drops_hires_the_workflow_does_not_have_and_handles_no_answer(library):
    wf_row = await import_fixture(library, KREA)
    wf = library.get(wf_row["id"])
    base = prefill_workflow(wf, library.get_graph(wf["id"]))
    assert merge_workflow(base, WORKFLOW_FILL, wf).settings.hires is None
    failed = merge_workflow(base, None, wf)
    assert "purpose" in failed.needs_attention and failed.examples == [wf["slots"]["defaults"]["positive"]] and failed.settings.cfg.rec is not None


async def test_merge_lists_no_sizes_for_a_workflow_that_takes_its_size_from_the_input_image(library):
    wf_row = await import_fixture(library, KREA)
    wf = library.get(wf_row["id"])
    base = prefill_workflow(wf, library.get_graph(wf["id"]))
    edit = {**wf, "slots": {**wf["slots"], "width": None, "height": None}}
    base.settings.sizes = []
    assert merge_workflow(base, WORKFLOW_FILL, edit).settings.sizes == []            # the LLM's sizes (1024x1536) are not taken
    assert [(s.w, s.h) for s in merge_workflow(base, WORKFLOW_FILL, wf).settings.sizes] == [(1024, 1536)]


def test_merge_lora_validates_trigger_words_and_uses_civitai_when_known(tmp_path):
    header = {"__metadata__": HEADER_META, "a.lora_down.weight": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]}}
    meta = summarize_header(header)
    nothing = civitai.CivitaiResult("no_token", message="No CIVITAI_API_TOKEN found, so Civitai wasn't asked.")
    card = merge_lora("a.safetensors", "abc", meta, None, nothing, LORA_FILL, [{"workflow": "w", "weight": 0.9, "family": None}])
    assert card.trigger_words == ["@bluejay"]                                  # "madeup" isn't in the training data
    assert card.metadata_only and "metadata only" in card.notice and "No CIVITAI_API_TOKEN" in card.notice and card.civitai is None
    assert card.weight.rec == 0.9 and card.weight.min <= 0.9 <= card.weight.max   # a workflow's own strength wins
    summary = civitai.summarize(VERSION)
    known = merge_lora("a.safetensors", "abc", meta, summary, civitai.CivitaiResult("ok", VERSION), LORA_FILL, [])
    assert known.trigger_words == ["inkstyle", "ink wash"] and not known.metadata_only and known.notice == ""
    assert known.civitai.url.endswith("modelVersionId=77") and known.civitai.example_prompts == ["inkstyle, a cat", "inkstyle, a dog"]
    assert lora_candidates(meta, summary)[:2] == ["inkstyle", "ink wash"]
    failed = merge_lora("a.safetensors", "abc", meta, None, nothing, None, [])
    assert failed.needs_attention and failed.what_it_does == ""


# ---------- LoRA cards: captions are not tags, trigger words found in what Civitai says ----------

# the shapes cedarv2's ss_tag_frequency really has: full captions split at their commas, a few plain tags among them
CAPTION_TAGS = {"@cedar. aria vale": 1, "@cedar. a girl with light blue hair": 1, "@cedar. two friends": 1,
                "@cedar. four girls stand side by side against a plain white background. on the left": 1,
                "she stands in front of a window and waves at the camera.": 1, "and a dark pleated skirt while sitting on a bench": 1,
                "blue eyes": 5, "a teal tie": 6}
NO_CIVITAI_WORDS = {"model_id": 1, "version_id": 2, "url": "https://civitai.com/models/1", "name": "Cedar", "version_name": "v2", "type": "LORA",
                    "base_model": "Anima", "trained_words": [], "description": "trigger word:@cedar. Improved style, works with @cedars too.",
                    "example_prompts": ["@cedar, 1girl, blue eyes"]}
CAPTION_META = {"ss_base_model_version": "anima", "ss_tag_frequency": json.dumps({"1_cedar": CAPTION_TAGS})}
OK = civitai.CivitaiResult("ok", VERSION)


def fill_with(*words):
    return {**LORA_FILL, "trigger_words": list(words)}


def test_captions_are_not_tags_or_style_tags():
    meta = summarize_header({"__metadata__": CAPTION_META, "a.lora_down.weight": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]}})
    assert meta["style_tags"] == [] and meta["top_tags"] == ["a teal tie", "blue eyes"]       # a short fragment like "a teal tie" stays
    assert meta["caption_starts"] == ["@cedar"] and "@cedar" in lora_candidates(meta, None)
    assert not any("." in w or len(w.split()) > 5 for w in lora_candidates(meta, None))
    assert is_caption_text("she waves. on the left") and is_caption_text("one two three four five six") and not is_caption_text("@artist name (x)")


def test_style_selector_keeps_its_style_tags_and_has_no_trigger_words():
    styles = {"@artist name (x)": 40, "@marlow (blue sedge)": 30, "@_@": 3, "@plain": 20, "1girl": 100, "touwata (kunoichi tsubaki no mune no uchi)": 2}
    meta = summarize_header({"__metadata__": {"ss_tag_frequency": json.dumps({"4_mixed": styles})}})
    assert meta["style_tags"] == ["@artist name (x)", "@marlow (blue sedge)", "@plain", "@_@"] and meta["caption_starts"] == []
    card = merge_lora("s.safetensors", "abc", meta, None, OK, {**LORA_FILL, "trigger_words": []}, [])
    assert card.trigger_words == [] and card.style_tags == ['@artist name (x)', '@marlow (blue sedge)', '@plain']      # '@_@' is the swirly-eyes face, not a selector
    assert lora_requirements({"s": card}) == ([], card.style_tags)


def test_normal_lora_tags_are_unchanged():
    tags = {"blue eyes": 50, "1girl": 100, "black detached sleeves": 20, "solo": 90, "": 4}
    meta = summarize_header({"__metadata__": {"ss_tag_frequency": json.dumps({"4_girl": tags})}})
    assert meta["top_tags"] == ["1girl", "solo", "blue eyes", "black detached sleeves"] and meta["style_tags"] == []


def test_leading_caption_word_is_the_trigger_word_and_written_once():
    meta = summarize_header({"__metadata__": CAPTION_META})
    card = merge_lora("c.safetensors", "abc", meta, None, civitai.CivitaiResult("no_token", message="No token."), fill_with("@cedar"), [])
    assert card.trigger_words == ["@cedar"] and card.style_tags == []
    triggers, styles = lora_requirements({"c": card})
    assert (triggers, styles) == (["@cedar"], [])                                             # a trigger word, not a style choice
    prompts, _ = finalize_prompts(["masterpiece, @cedar, 1girl", "masterpiece, 1girl"], WorkflowCard(), triggers, styles)
    assert [p.count("@cedar") for p in prompts] == [1, 1]


def test_trigger_word_found_in_the_civitai_text_is_accepted_when_civitai_lists_none():
    meta = summarize_header({"__metadata__": {"ss_tag_frequency": json.dumps({"1_x": {"1girl": 3}})}})
    card = lambda *words: merge_lora("c.safetensors", "abc", meta, NO_CIVITAI_WORDS, OK, fill_with(*words), []).trigger_words
    assert card("@cedar") == ["@cedar"] and card("@CEDAR") == ["@cedar"]                # spelled as the description spells it
    assert card("@cedar,") == ["@cedar"]                                                    # stray punctuation from the LLM
    assert card("@cedars", "invented", "cedarx") == ["@cedars"]                           # the description does say "@cedars"; the others are nowhere
    assert card("invented", "ceda") == []                                                      # nowhere, or only part of a word
    assert card("1girl") == ["1girl"]                                                           # a candidate (a top tag) as before
    no_plural = {**NO_CIVITAI_WORDS, "description": "trigger word:@cedars", "example_prompts": []}
    assert merge_lora("c.safetensors", "abc", meta, no_plural, OK, fill_with("@cedar"), []).trigger_words == []     # not a whole word
    only_prompt = {**NO_CIVITAI_WORDS, "description": "", "example_prompts": ["x, @Cedar, y"]}
    assert merge_lora("c.safetensors", "abc", meta, only_prompt, OK, fill_with("@cedar"), []).trigger_words == ["@Cedar"]


def test_civitai_list_stays_first_authority_and_is_cleaned():
    meta = summarize_header({"__metadata__": CAPTION_META})
    civ = {**NO_CIVITAI_WORDS, "trained_words": ["@l7m3n,", " lumen ", "LUMEN", ";"]}
    assert merge_lora("c.safetensors", "abc", meta, civ, OK, fill_with("@cedar"), []).trigger_words == ["@l7m3n", "lumen"]
    assert clean_words(["@l7m3n,", "a, b", "x.", "", " . ", "Word", "word", "@artist name (x)"]) == ["@l7m3n", "a, b", "x", "Word", "@artist name (x)"]


# ---------- store: draft, approve, pending ----------

@pytest.fixture
def cards(db, config):
    return CardStore(db, config)


def test_store_draft_approve_regenerate_accept_reject(cards, config):
    first = cards.save_built("workflow", "wf", WorkflowCard(purpose="one"))
    assert first["status"] == "draft" and first["pending"] is None
    assert cards.usable("workflow") == []                                      # drafts aren't used by default
    config.allow_draft_cards = True
    assert [c["key"] for c in cards.usable("workflow")] == ["wf"]
    config.allow_draft_cards = False
    assert cards.approve("workflow", "wf")["status"] == "approved" and [c["key"] for c in cards.usable("workflow")] == ["wf"]

    again = cards.save_built("workflow", "wf", WorkflowCard(purpose="two"))        # re-generate: waits as pending
    assert again["card"]["purpose"] == "one" and again["pending"]["purpose"] == "two" and again["status"] == "approved"
    assert cards.reject_pending("workflow", "wf")["pending"] is None
    cards.save_built("workflow", "wf", WorkflowCard(purpose="three"))
    accepted = cards.accept_pending("workflow", "wf")
    assert accepted["card"]["purpose"] == "three" and accepted["pending"] is None and accepted["status"] == "draft"
    with pytest.raises(CardError, match="no re-generated"):
        cards.accept_pending("workflow", "wf")


def test_store_edit_validates_and_keeps_status(cards):
    cards.save_built("lora", "a.safetensors", LoraCard(file="a.safetensors"))
    cards.approve("lora", "a.safetensors")
    edited = cards.save_edit("lora", "a.safetensors", {"file": "a.safetensors", "kind": "style", "trigger_words": ["x"]})
    assert edited["status"] == "approved" and edited["card"]["trigger_words"] == ["x"]
    with pytest.raises(CardError, match="isn't valid"):
        cards.save_edit("lora", "a.safetensors", {"file": "a.safetensors", "kind": "banana"})
    with pytest.raises(KeyError):
        cards.save_edit("lora", "missing.safetensors", {"file": "x"})
    with pytest.raises(KeyError):
        cards.get_kind = cards.approve("nothing", "x")


# ---------- builder and API with fakes ----------

class FakeLlm:
    """Stands in for LlmClient: canned answers per task, records the inputs."""

    def __init__(self):
        self.calls, self.fail = [], False

    async def call(self, task, inputs, enums=None):
        self.calls.append((task, inputs, enums))
        if self.fail:
            return {"needs_attention": True, "error": "bad json"}
        return WORKFLOW_FILL if task == "workflow_card" else LORA_FILL


@pytest.fixture
def lora_dir(tmp_path, monkeypatch):
    folder = tmp_path / "loras"
    folder.mkdir()
    make_safetensors(folder / STYLE_LORA, HEADER_META)
    make_safetensors(folder / "krea2_inkbrush.safetensors")
    monkeypatch.setattr("app.library.loras.model_dirs", lambda *_: [folder])
    return folder


def make_builder(db, config, library, comfy, token=TOKEN, handler=None):
    rec = Recorder()
    llm, events = FakeLlm(), EventBus()
    seen = []

    def default(request):
        seen.append(request)
        return httpx.Response(200, json=VERSION)

    civ = CivitaiClient(config.cache_dir, token_getter=lambda: token, transport=httpx.MockTransport(handler or default))
    builder = CardBuilder(CardStore(db, config), library, LoraScanner(db, config, comfy), civ, llm,
                          GpuLease(FakeWorker(rec), comfy, FakeServer(rec)), comfy, events, config)
    return builder, llm, rec, seen, events


async def wait_for(builder, kind, key, stages=("done", "failed")):
    for _ in range(200):
        if builder.builds.get((kind, key), {}).get("stage") in stages:
            return builder.builds[(kind, key)]
        await asyncio.sleep(0.02)
    raise AssertionError(f"build of {key} never finished: {builder.builds.get((kind, key))}")


async def test_builder_builds_workflow_card_under_the_lease(db, config, library, comfy):
    wf = await import_fixture(library, TAGWF_LORA)
    builder, llm, rec, _, events = make_builder(db, config, library, comfy)
    queue = events.subscribe()
    builder.request("workflow", wf["id"])
    state = await wait_for(builder, "workflow", wf["id"])
    assert state["stage"] == "done" and state["seconds"] >= 0
    assert rec.calls == ["worker.hold", "server.start", "server.stop", "worker.release"]       # lease around the build
    row = builder.cards.get("workflow", wf["id"])
    assert row["status"] == "draft" and row["card"]["family"] == "Anima" and row["card"]["built_with"] == config.llm_model
    task, inputs, enums = llm.calls[0]
    assert task == "workflow_card" and enums["family"] and "er_sde" in enums["samplers"]
    assert inputs["notes"].startswith("<<<BEGIN DATA: workflow notes>>>") and inputs["facts"]["has_hires"] is True
    stages = []
    while not queue.empty():
        stages.append(queue.get_nowait()["stage"])
    assert stages[0] == "queued" and "reading" in stages and "writing" in stages and stages[-1] == "done"


async def test_builder_batches_cards_in_one_lease_and_regenerate_makes_pending(db, config, library, comfy):
    a, b = await import_fixture(library, TAGWF), await import_fixture(library, KREA)
    builder, llm, rec, _, _ = make_builder(db, config, library, comfy)
    builder.request("workflow", a["id"])
    builder.request("workflow", b["id"])
    builder.request("workflow", a["id"])                                      # already queued: ignored
    await wait_for(builder, "workflow", a["id"])
    await wait_for(builder, "workflow", b["id"])
    assert rec.calls.count("server.start") == 1 and len(llm.calls) == 2
    builder.cards.approve("workflow", a["id"])
    builder.request("workflow", a["id"])
    state = await wait_for(builder, "workflow", a["id"])
    assert state["pending"] is True and builder.cards.get("workflow", a["id"])["pending"]["purpose"]


async def test_builder_reports_llm_failure_as_needs_attention_and_server_failure_as_failed(db, config, library, comfy):
    wf = await import_fixture(library, TAGWF)
    builder, llm, rec, _, _ = make_builder(db, config, library, comfy)
    llm.fail = True
    builder.request("workflow", wf["id"])
    state = await wait_for(builder, "workflow", wf["id"])
    assert state["stage"] == "done" and "purpose" in state["needs_attention"]
    card = builder.cards.get("workflow", wf["id"])["card"]
    assert card["needs_attention"] and card["settings"]["cfg"]["rec"] is not None            # the pre-filled part is kept

    from app.llm.server import LlmServerError
    builder.lease.server.fail_start = True
    builder.request("workflow", wf["id"])
    state = await wait_for(builder, "workflow", wf["id"], stages=("failed",))
    assert "boom" in state["message"] and rec.calls[-1] == "worker.release"               # the queue is released again


async def test_builder_builds_lora_card_with_civitai(db, config, library, comfy, lora_dir):
    await import_fixture(library, TAGWF_LORA)                                  # uses the style LoRA at some strength
    builder, llm, _, seen, _ = make_builder(db, config, library, comfy)
    builder.request("lora", STYLE_LORA)
    state = await wait_for(builder, "lora", STYLE_LORA)
    assert state["stage"] == "done"
    card = builder.cards.get("lora", STYLE_LORA)["card"]
    assert card["metadata_only"] is False and card["trigger_words"] == ["inkstyle", "ink wash"]
    assert card["civitai"]["url"].endswith("models/12?modelVersionId=77") and card["sha256"]
    assert len(seen) == 1 and seen[0].url.host == "civitai.com" and seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    task, inputs, _ = llm.calls[0]
    assert task == "lora_card" and "<<<BEGIN DATA: Civitai>>>" in inputs["civitai"] and "<<<BEGIN DATA: file metadata>>>" in inputs["metadata"]
    assert inputs["facts"]["used_by_workflows"] and inputs["facts"]["used_by_workflows"][0]["weight"] is not None
    everything = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in config.data_dir.rglob("*") if p.is_file() and p.suffix in (".json", ".txt"))
    assert TOKEN not in everything and TOKEN not in json.dumps(card)               # the token is never written anywhere


async def test_builder_lora_card_without_token_is_metadata_only(db, config, library, comfy, lora_dir):
    builder, _, _, seen, _ = make_builder(db, config, library, comfy, token=None, handler=lambda r: pytest.fail("no request without a token"))
    builder.request("lora", STYLE_LORA)
    await wait_for(builder, "lora", STYLE_LORA)
    card = builder.cards.get("lora", STYLE_LORA)["card"]
    assert card["metadata_only"] is True and "CIVITAI_API_TOKEN" in card["notice"] and card["civitai"] is None
    assert card["trigger_words"] == ["@bluejay"] and card["style_tags"] == ["@bluejay"] and card["family"] == "Anima"


async def test_builder_unknown_lora_file_fails_cleanly(db, config, library, comfy, lora_dir):
    builder, *_ = make_builder(db, config, library, comfy)
    builder.request("lora", "gone.safetensors")
    state = await wait_for(builder, "lora", "gone.safetensors")
    assert state["stage"] == "failed" and "Rescan" in state["message"]
    with pytest.raises(KeyError):
        builder.request("workflow", "no-such-workflow")


# ---------- routes ----------

def chat_transport(calls):
    """The fake llama-server HTTP API: returns the card fill matching the schema asked for."""
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        calls.append(name)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(WORKFLOW_FILL if name == "workflow_card" else LORA_FILL)},
                                                       "finish_reason": "stop"}]})
    return httpx.MockTransport(handler)


def poll(client, predicate, tries=200):
    import time
    for _ in range(tries):
        builds = client.get("/api/cards/builds").json()
        if predicate(builds):
            return builds
        time.sleep(0.02)
    raise AssertionError(f"timed out: {builds}")


def test_card_routes_end_to_end(config, comfy, lora_dir, tmp_path):
    calls, rec = [], Recorder()
    civ_handler = httpx.MockTransport(lambda r: httpx.Response(404))
    app = create_app(config, comfy=comfy, start_worker=False, llm_server=FakeServer(rec), llm_transport=chat_transport(calls), civitai_transport=civ_handler)
    with TestClient(app) as client:
        wf = client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{TAGWF_LORA}.json")}).json()
        key = wf["id"]
        listing = client.get("/api/cards/workflows").json()
        assert listing[0]["key"] == key and listing[0]["status"] is None

        assert client.post(f"/api/cards/workflows/{key}/build").json()["stage"] == "queued"
        poll(client, lambda b: b and b[0]["stage"] == "done")
        card = client.get(f"/api/cards/workflows/{key}").json()
        assert card["status"] == "draft" and card["card"]["family"] == "Anima" and card["workflow"]["slots"]["positive"] and card["pending"] is None

        edited = {**card["card"], "purpose": "My own words."}
        assert client.put(f"/api/cards/workflows/{key}", json=edited).json()["card"]["purpose"] == "My own words."
        bad = client.put(f"/api/cards/workflows/{key}", json={**edited, "family": "Banana"})
        assert bad.status_code == 422 and "isn't valid" in bad.json()["detail"]
        assert client.post(f"/api/cards/workflows/{key}/approve").json()["status"] == "approved"

        client.post(f"/api/cards/workflows/{key}/build")                      # re-generate
        poll(client, lambda b: b and b[0]["stage"] == "done" and b[0].get("pending"))
        pending = client.get(f"/api/cards/workflows/{key}").json()
        assert pending["card"]["purpose"] == "My own words." and pending["pending"]["purpose"] == WORKFLOW_FILL["purpose"] and pending["status"] == "approved"
        assert client.post(f"/api/cards/workflows/{key}/reject").json()["pending"] is None
        client.post(f"/api/cards/workflows/{key}/build")
        poll(client, lambda b: b and b[0]["stage"] == "done")
        accepted = client.post(f"/api/cards/workflows/{key}/accept").json()
        assert accepted["card"]["purpose"] == WORKFLOW_FILL["purpose"] and accepted["status"] == "draft"
        assert client.post(f"/api/cards/workflows/{key}/accept").status_code == 422
        assert client.get("/api/cards/workflows/nope").status_code == 404
        assert client.get("/api/cards/banana").status_code == 404

        # LoRAs
        loras = client.post("/api/cards/loras/rescan").json()
        assert {(e["key"], e["state"]) for e in loras} == {(STYLE_LORA, "new"), ("krea2_inkbrush.safetensors", "new")}
        client.post(f"/api/cards/loras/{STYLE_LORA}/build")
        poll(client, lambda b: any(x["key"] == STYLE_LORA and x["stage"] == "done" for x in b))
        entry = next(e for e in client.get("/api/cards/loras").json() if e["key"] == STYLE_LORA)
        assert entry["state"] == "ok" and entry["status"] == "draft" and entry["card"]["metadata_only"] is True and entry["card"]["notice"]
        assert client.post(f"/api/cards/loras/{STYLE_LORA}/approve").json()["status"] == "approved"

        # a changed file is noticed by its hash, a deleted one stays as "removed" (its card is kept)
        make_safetensors(lora_dir / STYLE_LORA, {"changed": "yes"})
        (lora_dir / "krea2_inkbrush.safetensors").unlink()
        comfy.oi = {**comfy.oi, "LoraLoaderModelOnly": {"input": {"required": {"lora_name": [[STYLE_LORA]]}}}}
        states = {e["key"]: e["state"] for e in client.post("/api/cards/loras/rescan").json()}
        assert states == {STYLE_LORA: "changed"}
        (lora_dir / STYLE_LORA).unlink()
        assert {e["key"]: e["state"] for e in client.post("/api/cards/loras/rescan").json()} == {STYLE_LORA: "missing"}

        cfg = client.get("/api/config").json()
        assert cfg["allow_draft_cards"] is False and cfg["llm_model"] == config.llm_model
    assert "workflow_card" in calls and "lora_card" in calls
    assert rec.calls.count("worker.hold") == rec.calls.count("worker.release") and "server.start" in rec.calls


def test_cards_carry_no_rating_tags_or_safety_rules():
    from app.library.card_builder import drop_safety_wording
    card = WorkflowCard(prompt_style={"kind": "tags", "quality_prefix": "masterpiece, safe, newest, 1girl,",
                                      "rules": ["Start with the prefix.", "Negative must end with safety tags (nsfw, explicit)."]},
                        warnings=["Keep the safety tags.", "CFG stays at 4."], examples=["masterpiece, safe, 1girl, rain"],
                        notes_full="Use er_sde.\n- Keep the safety tags at the end.\nStart: score_9, safe, newest\nBase 896x1152.")
    drop_safety_wording(card)
    assert card.prompt_style.quality_prefix == "masterpiece, newest, 1girl"
    assert card.prompt_style.rules == ["Start with the prefix."] and card.warnings == ["CFG stays at 4."]
    assert card.examples == ["masterpiece, 1girl, rain"] and card.notes_full == "Use er_sde.\nStart: score_9, newest\nBase 896x1152."
    lora = drop_safety_wording(LoraCard(file="a.safetensors", usage_notes=["Use at 0.8.", "Add nsfw to the negative."]))
    assert lora.usage_notes == ["Use at 0.8."]


def test_a_style_tag_has_a_name_and_is_not_a_real_tag():
    from app.library.card_builder import real_style_tags
    from app.planning.taglib import TagLib, parse_tags
    tags, aliases = parse_tags("@_@,0,57000,\n1girl,0,8000000,\n")
    lib = TagLib(tags, aliases)
    assert real_style_tags(["@l7m3n", "@_@", "@ @", "@", "@marlow (blue sedge)", "@pine-owl"], lib) == ["@l7m3n", "@marlow (blue sedge)", "@pine-owl"]
    assert real_style_tags(["@_@", "@ @", "@@", "@cedar"], None) == ["@cedar"]       # without the library only the name test applies
    assert real_style_tags(["@1girl"], lib) == ["@1girl"]                                  # the "@" word is not the library tag "1girl"
