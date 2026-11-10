"""Planning, the pure parts: ranking, clamping, locks, fuzzy matching, prompt checks, schemas, the content rules."""
import json
import random

import pytest

from tests.content_probes import CONTENT_TERMS as CONTENT, MODIFIERS as MOD, NUMBERS as NUM, SUBJECT_TERMS as SUBJ
from app.db import Database
from app.fields import make_field
from app.library.card_models import LoraCard, PixelBudget, PromptStyle, Range, Size, WorkflowCard, CardSettings
from app.llm.client import LlmClient, fill_enums, shape_schema
from app.planning import clamp, locks
from app.planning.prompts import (duplicate_note, ensure_prefix, finalize_prompts, lora_requirements, near_duplicates)
from app.planning.rank import Bm25, rank, tokenize
from app.planning.content_safety import check_content
from app.planning.settings import allowed, parse_size


# ---------- ranking ----------

def lora(what, kind="style", family="Anima", **kw):
    return LoraCard(file="x.safetensors", family=family, kind=kind, what_it_does=what, **kw)


def test_tokenize_splits_names_drops_stop_words_and_plurals():
    assert tokenize("model_styles_anima-base1_v6.safetensors") == ["model", "style", "anima", "base1", "v6", "safetensor"]
    assert tokenize("The Cats and the dogs") == ["cat", "dog"]
    assert tokenize("darkBrush") == ["dark", "brush"]


def test_bm25_ranks_the_matching_card_first_and_keeps_order_on_ties():
    cards = [("a", "ink wash monochrome brush painting"), ("b", "neon cyberpunk city glow"), ("c", "watercolor landscape")]
    scores = Bm25([t for _, t in cards]).scores("neon city at night")
    assert scores[1] > 0 and scores[0] == scores[2] == 0
    assert [n for n, _ in rank(cards, lambda c: c[1], "neon city at night")] == ["b", "a", "c"]
    assert [n for n, _ in rank(cards, lambda c: c[1], "nothing matches xyz")] == ["a", "b", "c"]      # ties keep input order
    assert rank([], lambda c: c, "x") == []


def test_rank_keeps_the_top_n():
    items = [f"item{i} common" for i in range(30)]
    items[17] = "rare unique rainbow"
    top = rank(items, lambda t: t, "rainbow", top=15)
    assert len(top) == 15 and top[0] == "rare unique rainbow"


# ---------- clamping ----------

def test_clamp_range_integer_float_unknown_and_pinned():
    r = Range(rec=30, min=20, max=40)
    assert clamp.clamp_range(99, r, True) == (40, "asked 99, clamped to 40")
    assert clamp.clamp_range(25.4, r, True) == (25, "")
    assert clamp.clamp_range(None, r, True) == (30, "")                           # no answer -> the recommended value
    assert clamp.clamp_range(7.5, Range(rec=5, min=3, max=6)) == (6.0, "asked 7.5, clamped to 6")
    assert clamp.clamp_range(12, Range()) == (12.0, "")                           # no range known: kept
    assert clamp.pinned(Range(rec=8, min=8, max=8)) and clamp.pinned(Range(rec=8)) and not clamp.pinned(r) and not clamp.pinned(Range())
    assert clamp.clamp_range(30, Range(rec=8), True) == (8, "asked 30, clamped to 8")      # only a recommendation: that is the range


def test_clamp_choice_falls_back_to_default_then_first():
    assert clamp.clamp_choice("euler", ["euler", "er_sde"]) == ("euler", "")
    assert clamp.clamp_choice("bogus", ["euler", "er_sde"], "er_sde") == ("er_sde", "asked bogus, not available; used er_sde")
    assert clamp.clamp_choice("bogus", ["euler", "er_sde"], "nope")[0] == "euler"
    assert clamp.clamp_choice("anything", []) == ("anything", "")


def test_clamp_lora_weight_uses_the_lora_card_range():
    assert clamp.clamp_lora_weight(2.0, Range(rec=0.8, min=0.5, max=1.2)) == (1.2, "asked 2, clamped to 1.2")
    assert clamp.clamp_lora_weight(0.1, Range(rec=0.8, min=0.5, max=1.2))[0] == 0.5
    assert clamp.clamp_lora_weight(0.8, Range(rec=0.8, min=0.5, max=1.2)) == (0.8, "")
    assert clamp.clamp_lora_weight(3, Range())[0] == 1.2                              # no range on the card: 0.5 to 1.2
    assert clamp.clamp_lora_weight(3, Range(rec=0.8))[0] == pytest.approx(0.96)       # only a recommendation: half to 1.2x


def make_card(sizes=(), budget=None, **kw) -> WorkflowCard:
    return WorkflowCard(settings=CardSettings(sizes=[Size(w=w, h=h, label=l) for w, h, l in sizes],
                                              pixel_budget=PixelBudget(**(budget or {})), **kw))


def test_size_options_are_exactly_the_cards_sizes():
    sizes = [(896, 1152, "base"), (1024, 1024, "square"), (1344, 768, "wide")]
    card = make_card(sizes, {"min": 900_000, "max": 1_200_000})
    assert [(o["w"], o["h"], o["label"]) for o in clamp.size_options(card, (640, 640))] == sizes     # the workflow default is not added
    assert [o["size"] for o in clamp.size_options(make_card([(1024, 1024, "1 MP"), (1024, 1024, "again")]), None)] == ["1024x1024"]
    assert clamp.size_options(make_card(), (832, 1216))[0]["size"] == "832x1216"      # no sizes on the card: the workflow default
    assert clamp.size_options(make_card(), None) == []


def test_a_card_with_one_or_two_sizes_gets_no_invented_shapes():
    """Sizes a model was not shown to support are never offered: a short list stays short, even inside the pixel budget."""
    for sizes in ([(896, 1152, "base")], [(1024, 1024, "1024x1024"), (2048, 2048, "2K native")]):
        card = make_card(sizes, {"min": 900_000, "max": 4_200_000})
        assert [(o["w"], o["h"]) for o in clamp.size_options(card, None)] == [(w, h) for w, h, _ in sizes]


def test_clamp_size_snaps_to_the_closest_option():
    options = clamp.size_options(make_card([(896, 1152, "base"), (1152, 896, "landscape"), (1024, 1024, "square")]), None)
    assert clamp.clamp_size(896, 1152, options) == (896, 1152, "")
    w, h, note = clamp.clamp_size(4000, 1000, options)
    assert (w, h) == (1152, 896) and "not allowed" in note
    assert clamp.clamp_size(512, 512, []) == (512, 512, "")


def test_settings_helpers():
    assert allowed(["a", "b"], ["b", "c"]) == ["b"] and allowed(["a"], ["x"]) == ["x"] and allowed([], ["x"]) == ["x"] and allowed(["a"], []) == ["a"]
    assert parse_size("896x1152") == (896, 1152) and parse_size("896×1152") == (896, 1152) and parse_size("big") is None and parse_size(None) is None


# ---------- locks ----------

def test_locks_never_overwrite_and_replans_start_from_locked_only():
    fields = {"steps": make_field(22, "locked", "by hand"), "cfg": make_field(5, "auto", "x"), "prompts": make_field(["a"], "locked")}
    assert set(locks.locked(fields)) == {"steps", "prompts"} and locks.locked_values(fields) == {"steps": 22, "prompts": ["a"]}
    assert set(locks.start_replan(fields)) == {"steps", "prompts"}                       # the auto cfg is dropped
    merged = locks.merge(locks.start_replan(fields), {"steps": locks.auto(30, "llm"), "cfg": locks.auto(6, "llm")})
    assert merged["steps"]["value"] == 22 and merged["steps"]["source"] == "locked" and merged["cfg"]["value"] == 6


# ---------- schema shaping ----------

SCHEMA = {"type": "object", "required": ["steps", "prompts", "reasons"], "properties": {
    "steps": {"type": "integer"}, "sampler": {"type": "string"},
    "prompts": {"type": "array", "items": {"type": "object", "properties": {"prompt": {"type": "string"}}}},
    "reasons": {"type": "object", "required": ["why_steps"], "properties": {"why_steps": {"type": "string"}}}}}


def test_shape_schema_fills_enums_limits_and_drops():
    out = shape_schema(SCHEMA, {"sampler": ["euler", "er_sde"]}, {"prompts": {"minItems": 3, "maxItems": 3}}, ["steps", "why_steps"])
    assert out["properties"]["sampler"]["enum"] == ["euler", "er_sde"]
    assert out["properties"]["prompts"]["minItems"] == out["properties"]["prompts"]["maxItems"] == 3
    assert "steps" not in out["properties"] and out["required"] == ["prompts", "reasons"]       # dropped, and no longer required
    assert out["properties"]["reasons"]["required"] == [] and out["properties"]["reasons"]["properties"] == {}
    assert "steps" in SCHEMA["properties"] and "minItems" not in SCHEMA["properties"]["prompts"]     # the original is untouched


def test_real_planning_schemas_and_templates_load_and_validate():
    """Every planning task has a prompt, a schema and a reply model, and the model accepts a reply shaped like the schema."""
    from app.llm.client import TASK_MODELS
    import app.planning.planner  # noqa: F401  (registers the reply models)
    client = LlmClient.__new__(LlmClient)
    for task in ("plan_request", "pick_loras", "plan_scene", "write_prompts", "pick_settings", "apply_tweak", "pick_wildcards", "repair_tags"):
        schema = client._schema(task)
        assert task in TASK_MODELS and set(schema["required"]) <= set(schema["properties"])
        system, user = client._prompt(task, {k: "x" for k in ("request", "user_request", "user_words", "min_rating", "concepts", "scene", "max_images", "input_images", "workflow", "job", "locked", "candidates", "max_loras",
                                                                "count", "card", "loras", "liked", "notes", "retry", "instruction", "kept", "ideas", "keep_tokens", "weights", "brief", "catalog", "max_lists", "tags", "prompts", "options", "planned", "slots", "rating", "look")})
        assert system and user and "{{" not in user


# ---------- prompt checks ----------

def tag_card(prefix="masterpiece, best quality", kind="tags") -> WorkflowCard:
    return WorkflowCard(prompt_style=PromptStyle(kind=kind, quality_prefix=prefix))


def test_quality_prefix_is_added_for_tag_prompts_in_order_and_not_duplicated():
    assert ensure_prefix("1girl, rain", ["masterpiece", "best quality"]) == "masterpiece, best quality, 1girl, rain"
    assert ensure_prefix("masterpiece, best quality, 1girl", ["masterpiece", "best quality"]) == "masterpiece, best quality, 1girl"
    assert ensure_prefix("1girl, Masterpiece, rain", ["masterpiece", "best quality"]) == "masterpiece, best quality, 1girl, rain"   # moved to the front
    prompts, notes = finalize_prompts(["1girl, rain", "masterpiece, best quality, 1boy"], tag_card(), [], [])
    assert prompts == ["masterpiece, best quality, 1girl, rain", "masterpiece, best quality, 1boy"] and notes == ["prompt 1: quality prefix"]
    sentences, _ = finalize_prompts(["A cabin in snow."], tag_card(kind="sentences"), [], [])
    assert sentences == ["A cabin in snow."]                                                         # no prefix for prose prompts


def test_trigger_words_and_style_tags_are_added_when_missing():
    card = tag_card()
    prompts, notes = finalize_prompts(["masterpiece, best quality, 1girl, rain", "masterpiece, best quality, @lensfox, 1girl, glow"], card,
                                      ["inkstyle"], ["@bluejay", "@lensfox"], "a girl in rain")
    assert prompts[0].startswith("masterpiece, best quality, inkstyle, @") and "@lensfox" in prompts[1] and "inkstyle" in prompts[1]
    assert any("prompt 1: added inkstyle" in n for n in notes)
    assert len([n for n in notes if n.startswith("prompt 2")]) == 1                                     # the style tag was already there
    assert all(p.index("inkstyle") > p.index("best quality") for p in prompts)                       # after the prefix
    prose, _ = finalize_prompts(["A quiet street."], tag_card(kind="sentences"), ["monochrome ink wash style"], [])
    assert prose == ["monochrome ink wash style, A quiet street."]
    case, notes = finalize_prompts(["InkStyle, a cat"], tag_card(prefix=""), ["inkstyle"], [])
    assert notes == [] and case == ["InkStyle, a cat"]                                                  # matching ignores case


def test_lora_requirements_treat_at_tags_on_style_loras_as_selectors():
    style = LoraCard(file="s", trigger_words=["@style_name", "inkstyle"], style_tags=["@a", "@b"])
    plain = LoraCard(file="p", trigger_words=["monochrome ink wash style"])
    assert lora_requirements({"s": style, "p": plain}) == (["inkstyle", "monochrome ink wash style"], ["@a", "@b"])
    assert lora_requirements({}) == ([], [])


def test_near_duplicates_are_found_ignoring_the_shared_parts():
    shared = ["masterpiece, best quality", "@bluejay"]
    a = "masterpiece, best quality, @bluejay, 1girl, yellow raincoat, neon city, night, rain, smile, looking at viewer"
    b = "masterpiece, best quality, @bluejay, 1girl, yellow raincoat, neon city, night, rain, smile, looking at viewer, wet hair"
    c = "masterpiece, best quality, @bluejay, 1girl, umbrella, alley, glowing signs, from above, serious, walking"
    assert near_duplicates([a, b, c], shared) == [(0, 1)]
    assert near_duplicates([a, a], shared) == [(0, 1)]
    assert near_duplicates([a, c], shared) == []
    assert near_duplicates(["masterpiece, best quality", "masterpiece, best quality"], shared) == [(0, 1)]       # nothing left after the shared parts
    assert duplicate_note([(0, 1), (2, 3)]) == "prompts 1 and 2; prompts 3 and 4"


# ---------- the content rules ----------

@pytest.mark.parametrize("texts,blocked", [
    ([f"1girl, {CONTENT[0]}, {SUBJ[0]}"], True), ([f"a {CONTENT[1]} {SUBJ[1]} on a beach"], True), ([f"{CONTENT[2]} {NUM[2]} year old girl"], True),
    ([f"{CONTENT[3]} scene, {SUBJ[2]}"], True), ([f"a {SUBJ[3]}. {CONTENT[4]}"], True), ([CONTENT[5], SUBJ[4]], True),
    ([f"1girl, {CONTENT[0]}, 25 years old"], False),
    ([f"a {SUBJ[1]} playing in a park, sunny"], False),
    ([f"{CONTENT[0]} woman, studio lighting"], False),
    ([f"{SUBJ[6]} blue {CONTENT[0]} figure on a chair"], False), ([f"a {SUBJ[2]} at school, backpack"], False), ([""], False), ([], False)])
def test_content_rule(texts, blocked):
    issue = check_content(texts)
    assert (issue is not None) == blocked
    if blocked:
        assert issue["severity"] == "blocking" and issue["field"] == "prompts" and issue["code"] == "cse"


@pytest.mark.parametrize("texts", [
    [f"{CONTENT[0]}, ({NUM[4]}:1.3) years old"], [f"{CONTENT[0]}, (({NUM[4]})) years old"], [f"{CONTENT[0]}, ({NUM[4]}:1.3 years old:1.2)"],
    [f"{CONTENT[0]}, \\({NUM[4]}\\) years old"],
    [f"{CONTENT[0]}, ({SUBJ[0]}:1.4)"], [f"({CONTENT[0]}:1.5), {SUBJ[5]}"], [f"{CONTENT[0][:2]}({CONTENT[0][2:]}:1.2), {SUBJ[1]}"],
    [f"{CONTENT[0]}, 1girl, (age:{NUM[3]})"], [f"{CONTENT[0]}, (age {NUM[0]}:1.2)"],
    [f"{CONTENT[0]}, (({NUM[0]})) yo, ({NUM[1]}:1.1) y old"], [f"{CONTENT[6][:2]}({CONTENT[6][2:5]}){CONTENT[6][5:]}, ({NUM[2]}:1.2)-year-old"],
    [CONTENT[2], f"({NUM[3]}:1.3) years old"], [f"{CONTENT[0]}, (1({NUM[4][1]}):1.2) years old"]])
def test_weights_hide_nothing_from_the_content_rules(texts):
    issue = check_content(texts)
    assert issue is not None and issue["code"] == "cse"


@pytest.mark.parametrize("texts", [[f"{CONTENT[0]}, (25:1.3) years old"],
                                   [f"({CONTENT[0]}:1.5), (adult:1.2) woman"],
                                   [f"{CONTENT[0]}, ((30)) years old"],
                                   [f"a ({SUBJ[1]}:1.3) in a park"],
                                   [f"({CONTENT[1]}:1.2) dress"]])
def test_weights_do_not_make_the_content_rules_stricter_than_they_were(texts):
    assert check_content(texts) is None


# ---------- database migration ----------

def test_old_database_is_migrated_with_jobs_images_and_ids_kept(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE workflows (id TEXT PRIMARY KEY, title TEXT NOT NULL, source_path TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, runnable INTEGER NOT NULL,
            missing TEXT NOT NULL, warnings TEXT NOT NULL, notes TEXT NOT NULL, slots TEXT NOT NULL, slots_confirmed INTEGER NOT NULL DEFAULT 0,
            cover_image_id INTEGER, api_path TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL REFERENCES workflows(id), description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, count INTEGER NOT NULL, fields TEXT NOT NULL, position REAL NOT NULL DEFAULT 0, error TEXT,
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
        CREATE TABLE images (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, idx INTEGER NOT NULL,
            status TEXT NOT NULL, seed INTEGER NOT NULL, prompt_id TEXT, file_path TEXT, recipe TEXT NOT NULL, error TEXT, rating INTEGER,
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, UNIQUE (job_id, idx));
        INSERT INTO workflows VALUES ('wf', 'WF', '/x', 't2i', 1, '[]', '[]', '[]', '{}', 0, NULL, '/y', 'now', 'now');
        INSERT INTO jobs (id, workflow_id, description, status, count, fields, created_at) VALUES (7, 'wf', 'old job', 'done', 1, '{}', 'now');
        INSERT INTO images (job_id, idx, status, seed, recipe, created_at) VALUES (7, 1, 'done', 5, '{}', 'now');
    """)
    conn.commit()
    conn.close()
    db = Database(path)
    job = db.row("SELECT * FROM jobs WHERE id = 7")
    assert job["description"] == "old job" and job["issues"] == [] and job["meta"] == {} and job["plan_id"] is None
    db.execute("INSERT INTO jobs (workflow_id, description, status, count, fields, created_at) VALUES (NULL, 'planned', 'planning', 2, '{}', 'now')")
    assert db.row("SELECT id FROM jobs WHERE description = 'planned'")["id"] == 8                  # numbering continues
    assert db.row("SELECT COUNT(*) AS n FROM images WHERE job_id = 7")["n"] == 1
    db.execute("DELETE FROM jobs WHERE id = 7")
    assert db.row("SELECT COUNT(*) AS n FROM images")["n"] == 0                                      # the cascade still works
    assert db.rows("PRAGMA foreign_key_check") == []
    db.close()
    Database(path).close()                                                                           # a second open changes nothing


def test_a_card_with_many_sizes_is_offered_exactly_as_listed():
    sizes = [(896, 1152, "base"), (1024, 1024, "square"), (1344, 768, "wide")]
    assert [o["size"] for o in clamp.size_options(make_card(sizes, {"min": 900_000, "max": 1_200_000}), None)] == ["896x1152", "1024x1024", "1344x768"]
