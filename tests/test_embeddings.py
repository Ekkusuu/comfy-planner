"""Search by meaning (app/embeddings.py, planning/semantic.py): a fake embedding server, no real model."""
import asyncio
import json
import logging
import random

import httpx
import pytest

from app.config import Config
from app.db import dumps, now
from app.embeddings import Embedder, EmbeddingError, dot
from app.library.card_models import LoraCard, Range
from app.planning import pick_wildcards as pw
from app.planning import semantic
from app.planning.context import MAX_LIKED, PlanContext
from app.planning.loras import candidates_for, lora_text
from app.wildcards import ideas as wi
from app.wildcards.store import WildList
from tests.test_planning_flow import app  # noqa: F401  (the planning app fixture)
from tests.test_wildcard_picks import offered_lists, plan_girl, writer  # noqa: F401  (helpers)

# Words that mean the same to the fake model (a real model knows this from reading a lot).
CONCEPTS = [{"moody", "melancholy", "gloomy", "sad", "brooding", "somber"}, {"steampunk", "brass", "gears", "victorian", "airship"},
            {"cat", "kitten", "feline"}, {"garden", "flowers", "lawn", "meadow"}, {"adultmark", "adultword", "adultterm", "adultitem"},
            {"fog", "foggy", "mist", "misty"}, {"forest", "woods", "trees"}, {"watercolor", "aquarelle", "wash"}]
DIMS = 4096
DIM_OF: dict = {}       # every concept or word gets a dimension of its own
STOP_WORDS = {"a", "an", "the", "in", "on", "of", "and", "with", "at", "me", "her", "to"}


def fake_vector(text: str, config: Config) -> list[float]:
    for prefix in (config.embeddings_query_prefix, config.embeddings_doc_prefix):
        text = text.removeprefix(prefix)
    vec = [0.0] * DIMS
    for word in "".join(c if c.isalnum() else " " for c in text.lower()).split():
        if word in STOP_WORDS:
            continue
        key = next((i for i, group in enumerate(CONCEPTS) if word in group), word)
        vec[DIM_OF.setdefault(key, len(DIM_OF))] += 1.0
    return vec


class FakeEmbeddingServer:
    def __init__(self):
        self.starts = self.stops = 0
        self.fail = False

    async def start(self):
        if self.fail:
            raise EmbeddingError("the fake model did not load")
        self.starts += 1

    async def stop(self):
        self.stops += 1


class FakeModel:
    """The httpx side: /v1/embeddings. `requests` records every input list as (kind, text)."""

    def __init__(self, config: Config):
        self.config, self.requests, self.status = config, [], 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        for text in body["input"]:
            self.requests.append(("query" if text.startswith(self.config.embeddings_query_prefix) else "doc", text))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "boom"})
        return httpx.Response(200, json={"data": [{"index": i, "embedding": fake_vector(t, self.config)} for i, t in enumerate(body["input"])]})

    def count(self, kind=None) -> int:
        return len([k for k, _ in self.requests if kind in (None, k)])


@pytest.fixture
def model_config(config, tmp_path) -> Config:
    config.embeddings_enabled = True
    config.embeddings_model = tmp_path / "fake-model.gguf"
    config.embeddings_model.write_bytes(b"not a real model")
    config.embeddings_floor, config.embeddings_margin, config.embeddings_lora_floor = 0.05, 0.05, 0.2      # the fake model's scores are smaller than a real one's
    return config


@pytest.fixture
def parts(model_config):
    server, model = FakeEmbeddingServer(), FakeModel(model_config)
    return Embedder(model_config, server, httpx.MockTransport(model)), server, model


@pytest.fixture
def emb(parts):
    return parts[0]


def ctx_for(config, emb, db) -> PlanContext:
    return PlanContext(config, None, None, None, db, embedder=emb)


def wlist(name: str, role: str, entries: list[str]) -> WildList:
    return WildList(name, name.split("/")[0], role, role, True, entries)


LISTS = [wlist("background/ambience", "setting", ["melancholy dusk", "bright morning", "heavy rain", "quiet night"]),
         wlist("background/background-nature", "setting", ["misty forest", "mountain lake", "meadow", "rocky shore"]),
         wlist("color/color-red", "palette", ["crimson", "scarlet", "dark red"]),
         wlist("style/style-steampunk", "style", ["brass gears", "victorian airship", "copper pipes"]),
         wlist("NSFW/nsfw-adult", "nsfw", ["adultmark one", "adultword two", "adultterm three"]),
         wlist("camera/camera-shot", "shot", ["close-up", "wide-angle shot", "low angle"])]


# ---------- the client ----------

async def test_vectors_are_unit_length_and_remembered_on_disk(parts, model_config):
    emb, server, model = parts
    texts = ["melancholy dusk", "quiet night"]
    first = await emb.embed(texts)
    assert abs(dot(first[0], first[0]) - 1) < 1e-5 and server.starts == 1 and model.count() == 2
    again = await emb.embed(texts + ["bright morning"])
    assert model.count() == 3 and again[0] == first[0]                       # only the new text was computed
    fresh = Embedder(model_config, FakeEmbeddingServer(), httpx.MockTransport(model))      # a new run: the vectors come from disk
    assert fresh.cached(texts) == first and model.count() == 3
    assert (await fresh.embed(texts)) == first and model.count() == 3 and fresh.server.starts == 0
    assert (model_config.cache_dir / "embeddings").is_dir()
    await emb.close()


async def test_documents_and_queries_get_the_models_own_prefixes(parts, model_config):
    emb, _, model = parts
    await emb.embed(["a quiet harbor"])
    await emb.embed(["a moody girl"], query=True)
    await emb.embed(["a moody girl"], query=True)                            # a query asked twice is computed once
    assert model.requests == [("doc", model_config.embeddings_doc_prefix + "a quiet harbor"),
                              ("query", model_config.embeddings_query_prefix + "a moody girl")]
    q = (await emb.embed(["moody"], query=True))[0]
    d = (await emb.embed(["melancholy"]))[0]
    assert dot(q, d) > 0.99 and dot(q, (await emb.embed(["brass gears"]))[0]) < 0.1       # synonyms are close, strangers are not


async def test_texts_are_embedded_in_batches(parts):
    emb, _, model = parts
    texts = [f"entry {i}" for i in range(70)]
    assert len(await emb.embed(texts)) == 70 and model.count() == 70
    assert len(await emb.embed(texts + texts)) == 140 and model.count() == 70


async def test_switched_off_means_no_server_and_no_vectors(parts, model_config):
    emb, server, model = parts
    model_config.embeddings_enabled = False
    assert await emb.embed(["x"]) is None and emb.cached(["x"]) == [None] and not emb.enabled
    assert server.starts == 0 and model.requests == []


async def test_a_server_that_cannot_start_falls_back_with_one_warning(parts, caplog):
    emb, server, model = parts
    server.fail = True
    caplog.set_level(logging.WARNING, logger="planning")
    assert await emb.embed(["x"]) is None
    assert any("search by meaning is not available" in r.getMessage() and "did not load" in r.getMessage() for r in caplog.records)
    assert await emb.embed(["y"]) is None and not emb.enabled               # not retried for a while
    assert len([r for r in caplog.records if "not available" in r.getMessage()]) == 1
    emb._down_until = 0                                                     # ... and tried again later
    server.fail = False
    assert await emb.embed(["y"]) is not None


async def test_a_server_that_answers_with_an_error_falls_back(parts):
    emb, _, model = parts
    model.status = 500
    assert await emb.embed(["x"]) is None and not emb.enabled


async def test_the_real_server_needs_the_program_and_the_model_file(model_config, tmp_path):
    model_config.embeddings_model = tmp_path / "missing.gguf"
    emb = Embedder(model_config)                                           # the real EmbeddingServer
    assert await emb.embed(["x"]) is None
    model_config.llm_binary = tmp_path / "no-llama-server.exe"
    emb._down_until = 0
    assert await emb.embed(["x"]) is None
    command = emb.server.command()
    assert "--embeddings" in command and command[command.index("--device") + 1] == "none" and command[command.index("-ngl") + 1] == "0"
    assert command[command.index("--port") + 1] == str(model_config.embeddings_port)


async def test_the_server_stops_when_idle_and_starts_again_when_needed(parts, model_config):
    emb, server, _ = parts
    model_config.embeddings_idle_stop = 0.05
    await emb.embed(["one"])
    await asyncio.sleep(0.2)
    assert server.stops >= 1
    await emb.embed(["two"])
    assert server.starts == 2
    await emb.close()
    assert server.stops >= 2


async def test_warm_embeds_in_the_background_and_cached_finds_it(parts):
    emb, _, model = parts
    texts = [f"entry {i}" for i in range(40)]
    emb.warm(texts)
    assert emb.warming
    while emb.warming:
        await asyncio.sleep(0.01)
    assert all(v is not None for v in emb.cached(texts)) and model.count() == 40


# ---------- wildcard lists ----------

def test_a_count_is_taken_out_of_the_brief_before_it_is_read_by_the_model():
    clean = semantic.clean_query
    assert clean("2 images of a moody girl") == "a moody girl" and clean("Four pictures of a steampunk inventor") == "a steampunk inventor"
    assert clean("a moody girl, 3 variations") == "a moody girl," and clean("a girl with 2 swords") == "a girl with 2 swords"
    assert clean("images of the sea") == "images of the sea" and clean("  ") == ""


async def test_a_list_is_related_by_meaning_even_when_nothing_matches_by_keyword(emb, model_config, db):
    ctx = ctx_for(model_config, emb, db)
    related = await semantic.related_lists(ctx, LISTS, "a moody girl")
    assert related[0][0].name == "background/ambience"                       # "moody" ~ "melancholy dusk"; BM25 finds nothing
    assert [l.name for l, _ in related] == ["background/ambience"]            # the rest is too far
    steam = await semantic.related_lists(ctx, LISTS, "airship over a victorian town")
    assert steam[0][0].name == "style/style-steampunk"


async def test_a_list_for_adult_content_is_related_like_any_other_list(emb, model_config, db):
    """No role is held back (the old `guarded_roles` rule is gone): a list is related when its score passes the floor and the margin."""
    ctx = ctx_for(model_config, emb, db)
    for brief in ("a cat in a garden", "a girl in a foggy forest, watercolor", "surprise me"):
        names = [l.name for l, _ in await semantic.related_lists(ctx, LISTS, brief) or []]
        assert "NSFW/nsfw-adult" not in names, (brief, names)                  # the floor and the margin keep it out: it is far from these requests
    shown = [l.name for l, _ in await semantic.related_lists(ctx, LISTS, "an adultmark and adultword photo")]
    assert shown[0] == "NSFW/nsfw-adult"                                    # asked for, so it is the best list
    model_config.embeddings_floor, model_config.embeddings_margin = 0.0, 5.0
    crowd = [wlist(f"x/list-{i}", "setting", ["moody gloomy"]) for i in range(5)] + [wlist("NSFW/hidden", "nsfw", ["moody"])]
    assert "NSFW/hidden" in [l.name for l, _ in await semantic.related_lists(ctx, crowd, "moody")]        # no rank or keyword needed for its role


async def test_floor_and_margin_limit_what_is_related(emb, model_config, db):
    ctx = ctx_for(model_config, emb, db)
    model_config.embeddings_floor = 0.99
    assert await semantic.related_lists(ctx, LISTS, "a moody girl") == []
    model_config.embeddings_floor, model_config.embeddings_margin = 0.0, 5.0
    assert len(await semantic.related_lists(ctx, LISTS, "a moody girl")) == len(LISTS)      # every list: no role is held back


async def test_no_related_lists_when_meaning_search_is_off_or_down(emb, model_config, db, parts):
    ctx = ctx_for(model_config, emb, db)
    assert await semantic.related_lists(ctx, LISTS, "") is None and await semantic.related_lists(ctx, [], "moody") is None
    parts[1].fail = True
    assert await semantic.related_lists(ctx, LISTS, "a moody girl") is None
    assert await semantic.related_lists(PlanContext(model_config, None, None, None, db), LISTS, "moody") is None      # no embedder at all


async def test_a_library_not_embedded_yet_is_embedded_in_the_background_and_this_plan_uses_keywords(emb, model_config, db, parts):
    ctx = ctx_for(model_config, emb, db)
    big = [wlist(f"x/list-{i}", "setting", [f"entry {i} {j}" for j in range(30)]) for i in range(20)]       # 620 texts
    assert await semantic.related_lists(ctx, big, "entry 3") is None
    while emb.warming:
        await asyncio.sleep(0.01)
    assert await semantic.related_lists(ctx, big, "entry 3 4") is not None     # next time it is ready


def test_choose_puts_meaning_matches_after_phrases_and_before_keywords():
    config = Config()
    lists = LISTS + [wlist("concept/themes", "concept", ["steampunk"])]
    related = [(lists[0], 0.5), (lists[3], 0.4)]
    chosen = wi.choose(lists, "a quiet afternoon", "a quiet afternoon", config, random.Random(1), None, related)
    assert [(c.wild.name, c.by) for c in chosen[:2]] == [("background/ambience", "meaning"), ("style/style-steampunk", "meaning")]
    phrase = wi.choose(lists, "a steampunk scene", "a steampunk scene", config, random.Random(1), None, related)
    assert (phrase[0].wild.name, phrase[0].by) == ("concept/themes", "phrase") and phrase[1].by == "meaning"
    llm = wi.choose(lists, "a quiet afternoon", "a quiet afternoon", config, random.Random(1), [("camera/camera-shot", "framing")], related)
    assert (llm[0].wild.name, llm[0].by) == ("camera/camera-shot", "llm")
    given = wi.choose(lists, "a girl in a city", "a girl in a city", config, random.Random(1), None, related)     # "city" fixes the setting
    assert "background/ambience" not in [c.wild.name for c in given]
    assert [c.by for c in wi.choose(lists, "a quiet afternoon", "a quiet afternoon", config, random.Random(1))].count("meaning") == 0      # nothing related: as before


def test_the_catalog_marks_lists_close_in_meaning():
    config = Config()
    text, _ = pw.build_catalog(LISTS, config, {"style/style-steampunk": "brass gears"}, {"background/ambience", "style/style-steampunk"})
    lines = {l.split(" (")[0]: l for l in text.splitlines() if " (" in l}
    assert lines["background/ambience"].endswith("← close in meaning to the request")
    assert "has “brass gears”" in lines["style/style-steampunk"] and "close in meaning" not in lines["style/style-steampunk"]
    assert "←" not in lines["color/color-red"]


# ---------- LoRA cards ----------

def card(file, what, words=(), kind="style") -> LoraCard:
    return LoraCard(file=file, family="Anima", kind=kind, what_it_does=what, trigger_words=list(words), weight=Range(rec=1, min=0.5, max=1.2))


async def test_the_lora_shortlist_finds_a_card_by_meaning_where_no_word_matches(app, emb, model_config):  # noqa: F811
    ctx = app.state.planner.ctx
    cards = app.state.cards
    for name, c in (("aquarelle_wash.safetensors", card("aquarelle_wash.safetensors", "Soft aquarelle wash look")),
                    ("brass_gears.safetensors", card("brass_gears.safetensors", "Brass gears and victorian airships")),
                    ("pixel.safetensors", card("pixel.safetensors", "Chunky pixel sprites"))):
        cards.save_built("lora", name, c)
        cards.approve("lora", name)
    off = await candidates_for(ctx, "Anima", "a watercolor painting of a meadow", [])
    assert all(score == 0 for pair, score in off if pair[0] == "aquarelle_wash.safetensors")      # no shared word: BM25 can't tell
    ctx.embedder = emb
    on = await candidates_for(ctx, "Anima", "a watercolor painting of a meadow", [])
    assert on[0][0][0] == "aquarelle_wash.safetensors" and on[0][1] > 0                           # close in meaning: first, and "pointed at"
    assert [s for _, s in on if s > 0] == [on[0][1]]
    still = await candidates_for(ctx, "Anima", "a cat sleeping", [])
    assert all(s == 0 for _, s in still)                                                          # nothing points at any card


# ---------- liked examples ----------

def like(db, prompts: list[str]):
    job = db.execute("INSERT INTO jobs (workflow_id, description, status, count, fields, created_at) VALUES (NULL,'', 'done', 1, '{}', ?)", (now(),)).lastrowid
    for n, prompt in enumerate(prompts, 1):
        db.execute("INSERT INTO images (job_id, idx, status, seed, recipe, rating, created_at, finished_at) VALUES (?,?,?,?,?,?,?,?)",
                   (job, n, "done", n, dumps({"prompt": prompt}), 1, now(), f"2026-01-01T00:00:{n:02d}"))
    return job


async def test_liked_examples_are_the_most_similar_ones_when_meaning_search_is_on(emb, model_config, db):
    db.execute("INSERT INTO workflows (id, title, source_path, kind, runnable, missing, warnings, notes, slots, api_path, created_at, updated_at) "
               "VALUES ('wf-1','t','p','t2i',1,'[]','[]','[]','{}','a',?,?)", (now(), now()))
    prompts = [f"1girl, plain studio background {i}" for i in range(4)] + ["1girl, brass gears, victorian airship", "a cat in a garden", "a misty forest at dawn",
                                                                           "melancholy girl in the rain", "mountain lake"]
    job = like(db, prompts)
    db.execute("UPDATE jobs SET workflow_id = 'wf-1' WHERE id = ?", (job,))
    ctx = ctx_for(model_config, emb, db)
    newest = await PlanContext(model_config, None, None, None, db).liked_for("wf-1", "steampunk")        # no embedder: the newest five
    assert newest == ["mountain lake", "melancholy girl in the rain", "a misty forest at dawn", "a cat in a garden", "1girl, brass gears, victorian airship"]
    close = await ctx.liked_for("wf-1", "a steampunk girl on an airship")
    assert close[0] == "1girl, brass gears, victorian airship" and len(close) == MAX_LIKED
    assert (await ctx.liked_for("wf-1", "a moody girl"))[0] == "melancholy girl in the rain"
    emb.server.fail = True
    emb._down_until = 0
    emb._queries.clear()
    assert await ctx.liked_for("wf-1", "something new") == newest                                       # meaning search down: the newest again
    db.execute("DELETE FROM images WHERE idx > 3")
    assert len(await ctx.liked_for("wf-1", "x")) == 3                                                    # few liked: all of them


# ---------- in a plan ----------

def enable_meaning(app, model_config=None):
    """Turn search by meaning on in a running app, with the fake model."""
    config = app.state.config
    config.embeddings_enabled = True
    config.embeddings_floor, config.embeddings_margin, config.embeddings_lora_floor = 0.05, 0.05, 0.2
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.embeddings_model = config.data_dir / "fake-model.gguf"
    config.embeddings_model.write_bytes(b"x")
    model = FakeModel(config)
    embedder = Embedder(config, FakeEmbeddingServer(), httpx.MockTransport(model))
    app.state.planner.ctx.embedder = embedder
    return embedder, model


def test_a_plan_offers_the_list_that_is_close_in_meaning_and_says_so(app):  # noqa: F811
    path = app.state.config.wildcards_dir / "background"
    path.mkdir(parents=True)
    (path / "ambience.txt").write_text("melancholy dusk\nbright morning\nheavy rain\nquiet night\n", encoding="utf-8")
    (app.state.config.wildcards_dir / "color").mkdir()
    (app.state.config.wildcards_dir / "color" / "color-red.txt").write_text("crimson\nscarlet\n", encoding="utf-8")
    app.state.config.wildcards_llm_picks_lists = False                         # only the rules decide
    app.state.config.wildcards_ideas_per_prompt = 1
    app.state.config.wildcards_slot_roles = []                                  # no open-slot filling: only meaning can pick a list
    embedder, model = enable_meaning(app)
    app.state.config.wildcards_enabled = True
    plan = plan_girl(app, "a moody girl")
    lists = offered_lists(app, plan["job_ids"][0])
    assert [(l["list"], l["by"]) for l in lists] == [("background/ambience", "meaning")]
    assert "setting (background/ambience): " in writer(app)       # the fake model finds the entries close to no slot, so the list is shown by name
    assert model.count("query") >= 1
    # with the switch off, nothing is offered: no keyword, no slot
    app.state.config.embeddings_enabled = False
    plan = plan_girl(app, "a moody girl")
    assert "ideas" not in app.job(plan["job_ids"][0])["fields"]["prompts"].get("items", [{}])[0]


def test_the_llm_sees_which_lists_are_close_in_meaning(app):  # noqa: F811
    path = app.state.config.wildcards_dir / "background"
    path.mkdir(parents=True)
    (path / "ambience.txt").write_text("melancholy dusk\nbright morning\nheavy rain\nquiet night\n", encoding="utf-8")
    (app.state.config.wildcards_dir / "color").mkdir()
    (app.state.config.wildcards_dir / "color" / "color-red.txt").write_text("crimson\nscarlet\n", encoding="utf-8")
    enable_meaning(app)
    app.llama.scripts["pick_wildcards"] = {"picks": []}
    plan_girl(app, "a moody girl")
    message = app.llama.of("pick_wildcards")[0]["messages"][1]["content"]
    assert "background/ambience (4):" in message and "← close in meaning to the request" in message
    assert message.count("close in meaning") == 1


def test_embeddings_are_a_setting(app):  # noqa: F811
    from app.settings import validate
    assert validate({"embeddings_enabled": False}, app.state.config) == {"embeddings_enabled": False}
    with pytest.raises(Exception):
        validate({"embeddings_enabled": "no"}, app.state.config)
    app.state.config.embeddings_model = app.state.config.data_dir / "no-such-model.gguf"
    view = app.client.get("/api/config").json()
    assert view["embeddings_enabled"] is False and view["embeddings_model_found"] is False
    assert Config().embeddings_enabled is True                                  # on by default: the model won the comparison
