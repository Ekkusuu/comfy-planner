"""The size floor restates the plan, and appearance options make the look vary. Sample words are neutral; nothing here
calls an LLM, ComfyUI or the GPU."""
import asyncio
import json
import random
import re

from types import SimpleNamespace

from app.planning.appearance import APPEARANCE_MIN_COUNT, EYE_COLOURS, HAIR_COLOUR_PICKS, HAIR_COLOURS, HAIR_STYLES, appearance_options, has_character_lora
from app.planning.context import derive_rng
from app.planning.tagcheck import FEMALE_WORDS, MALE_WORDS, absent_sex_words, marks_absent_sex, phrases_of, restates
from app.planning.taglib import TagLib
from app.planning.tagstep import MAX_SLOT_TAGS, TagStep
from tests.test_planning_flow import app  # noqa: F401  (the fixture: a running app with a fake LLM)
from tests.test_scene_plan import sheet
from tests.test_tagcheck import cc, run
from tests.test_tagstep import NoMeaning
from tests.test_variety import NAMES, SUBJECT_BOYS, SUBJECT_GIRL, SUBJECT_MIXED
from tests.test_weights import prompt_reply

def test_only_a_character_lora_counts_as_having_its_own_look():
    kinds = {"a": "character", "b": "style"}
    ctx = SimpleNamespace(lora_card=lambda name: SimpleNamespace(kind=kinds[name]) if name in kinds else None)
    assert has_character_lora(ctx, [{"name": "b"}, {"name": "a"}])
    assert not has_character_lora(ctx, [{"name": "b"}, {"name": "gone"}])
    assert not has_character_lora(ctx, [])


# ---------- the size floor only restates the plan ----------

FLOOR_BASE = "masterpiece, best quality, 1girl, park"


def floor_added(plan, offered, **kw):
    r = run(FLOOR_BASE, detail_level="high", offered=offered, slots=plan, **kw)
    return [c["to"] for c in r.changes if "detail level" in c["why"]], r


def test_the_floor_adds_a_tag_only_when_all_its_words_are_inside_one_phrase_of_one_slot():
    plan = {"body": "blue eyes, fair skin, long wavy brown hair", "pose": "sitting, legs extended"}
    offered = ["blue skin", "blue hair", "sitting on person", "brown hair", "long hair", "wavy hair", "fair skin", "legs"]
    added, _ = floor_added(plan, offered)
    assert added == ["brown hair", "long hair", "wavy hair", "fair skin"]          # `blue skin` has its words in two phrases; `sitting on person` needs `person`


def test_the_phrase_rule_folds_plurals_only_and_keeps_the_head_word():
    one = lambda tag, text: restates(tag, phrases_of(text))
    assert one("blue eye", "blue eyes, fair skin") and one("long hair", "long wavy brown hair") and one("sitting on bed", "sitting on a bed")
    assert not one("short shorts", "short brown hair") and not one("building", "slender build")      # `shorts` and `-ing` are not plurals
    assert not one("closed eyes", "half-closed eyes") and one("half-closed eyes", "half-closed eyes")
    assert not one("cowboy", "cowboy shot") and not one("back", "leaning back on hands") and one("cowboy shot", "cowboy shot")      # the tag ends on the phrase's own head
    assert not one("hand up", "hand on own chest")                                  # `up` is not a stop word: it changes the tag


def test_a_request_that_cannot_reach_the_floor_stays_below_it_without_wrong_tags():
    added, r = floor_added({"setting": "forest"}, [f"neighbour{chr(97 + n)}x" for n in range(40)])
    assert added == [] and r.text == FLOOR_BASE and not r.changes                    # shorter, not padded


def test_a_tag_that_needs_a_second_person_is_not_added_to_a_picture_of_one():
    plan = {"pose": "sitting on lap, leaning on person, hands on own lap, standing"}
    offered = ["sitting on lap", "leaning on person", "hands on own lap", "standing"]
    assert floor_added(plan, offered, subjects=SUBJECT_GIRL)[0] == ["hands on own lap", "standing"]
    group = {"girls": 2, "boys": None, "others": None, "no_humans": None}
    assert {"sitting on lap", "leaning on person", "standing"} <= set(floor_added(plan, offered, subjects=group)[0])      # two people: nothing is held back
    solo_tag = run("masterpiece, best quality, 1girl, solo, park", detail_level="high", offered=offered, slots=plan)
    assert "sitting on lap" not in solo_tag.text and "standing" in solo_tag.text      # the prompt's own `solo` counts too


def test_a_tag_for_a_second_person_is_not_offered_to_a_picture_of_one_unless_typed():
    lib = TagLib({name: (0, 1000) for name in ("lap_pillow", "sitting", "other_hand")})

    def offer(subjects, typed=""):
        context = cc(family="Anima", library=lib, subjects=subjects, typed=typed)
        got = asyncio.run(TagStep(NoMeaning(), lib, context, "Anima").slot_suggestions({"pose": "lap pillow, sitting, other hand"}, None, None))
        return set(got.get("pose", []))
    assert offer(SUBJECT_GIRL) == {"sitting"}
    assert offer({"girls": 2, "boys": None, "others": None, "no_humans": None}) == offer(None) == {"lap pillow", "sitting", "other hand"}
    assert "lap pillow" in offer(SUBJECT_GIRL, typed="a girl on a lap pillow")


def test_body_words_of_one_sex_mark_that_sex_but_never_a_typed_tag():
    girl, boys = absent_sex_words(SUBJECT_GIRL), absent_sex_words(SUBJECT_BOYS)
    assert marks_absent_sex("beard", girl) and not marks_absent_sex("beard", boys) and not marks_absent_sex("beard", absent_sex_words(SUBJECT_MIXED))
    assert len(MALE_WORDS) > 7 and len(FEMALE_WORDS) > 7 and not MALE_WORDS & FEMALE_WORDS
    lib = TagLib({"beard": (0, 1000), "red_dress": (0, 1000)})
    context = cc(family="Anima", library=lib, subjects=SUBJECT_GIRL)
    got = asyncio.run(TagStep(NoMeaning(), lib, context, "Anima").slot_suggestions({"body": "beard, red dress"}, None, None))
    assert got == {"body": ["red dress"]}
    typed = cc(family="Anima", library=lib, subjects=SUBJECT_GIRL, typed="a girl with a beard")
    assert asyncio.run(TagStep(NoMeaning(), lib, typed, "Anima").slot_suggestions({"body": "beard, red dress"}, None, None)) == {"body": ["red dress", "beard"]}      # the user typed it: kept


def test_the_tags_that_restate_a_slot_are_always_offered_even_when_recent():
    hair = ["long_hair", "brown_hair", "wavy_hair", "blue_eyes"]
    lib = TagLib({**{name: (0, 10_000 - n) for n, name in enumerate(NAMES)}, **{name: (0, 1500) for name in hair}})
    context = cc(family="Anima", library=lib)
    text = "long wavy brown hair, blue eyes, " + " ".join(NAMES)          # the long phrase's filler words are neighbours, not restatements
    for seed in range(8):
        got = asyncio.run(TagStep(NoMeaning(), lib, context, "Anima").slot_suggestions({"body": text}, None, random.Random(seed), {"long hair", "brown hair"}))["body"]
        assert {"long hair", "brown hair", "wavy hair", "blue eyes"} <= set(got) and len(got) == MAX_SLOT_TAGS


# ---------- appearance options ----------

LOOKS = {f"{c}_hair": (0, 300_000) for c in HAIR_COLOURS} | {f"{c}_eyes": (0, 300_000) for c in EYE_COLOURS}
LOOKS |= {s.replace(" ", "_"): (0, 400_000) for s in HAIR_STYLES} | {"pink_eyes": (0, 50_000), "aria_vale": (4, 146_000), "forest": (0, 400_000)}


def looks_library() -> TagLib:
    return TagLib(dict(LOOKS))


def draw_looks(text="a woman in a cozy room", seed=1, lib=None, subjects=None):
    return appearance_options(lib or looks_library(), text, subjects, derive_rng(seed, "appearance"))


def test_appearance_options_are_three_hair_and_three_eye_tags_from_the_library():
    got = draw_looks()
    assert len(got["hair"]) == 3 and len(got["eyes"]) == 3 and len(set(got["hair"])) == 3 and len(set(got["eyes"])) == 3
    assert all(LOOKS[o.replace(" ", "_")][1] >= APPEARANCE_MIN_COUNT for o in [*got["hair"], *got["eyes"]])
    assert sum(o.endswith(" hair") and o.split()[0] in HAIR_COLOURS for o in got["hair"]) >= HAIR_COLOUR_PICKS
    assert all("pink eyes" not in draw_looks(seed=s)["eyes"] for s in range(40))     # used too rarely in the library


def test_appearance_options_repeat_for_a_seed_and_change_with_it():
    assert draw_looks(seed=3) == draw_looks(seed=3)
    assert len({json.dumps(draw_looks(seed=s)) for s in range(8)}) >= 6


def test_appearance_is_left_alone_when_the_user_gave_a_look_or_named_a_character():
    assert draw_looks("a woman with red hair") is None and draw_looks("a green-eyed knight") is None and draw_looks("a blonde girl") is None
    assert draw_looks("aria vale on a stage") is None                              # a named character has their own look
    assert draw_looks("a woman in a forest") is not None
    assert draw_looks("a lone cabin", subjects={"no_humans": True}) is None
    assert appearance_options(None, "a woman", None, random.Random(1)) is None        # no tag library


def test_a_group_gets_one_set_of_options():
    got = draw_looks("three friends at a picnic", subjects={"girls": 3})
    assert len(got["hair"]) == 3 and len(got["eyes"]) == 3


LINE = re.compile(r"^appearance \(hair and eyes\): (.+)$", re.M)


def plan_with_looks(app, request, seed=None, scene=None):
    app.state.planner.ctx.tag_library = looks_library()
    if seed is not None:
        app.state.planner.ctx.rng = random.Random(seed)
    app.script_plan({"description": request, "count": 1, "constraints": []})
    app.llama.scripts["plan_scene"] = scene or sheet(rating="general", must_keep=[], subjects="1 adult woman")
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, solo, standing")
    return app.job(app.plan(request, on="anima")["job_ids"][0])


def scene_message(app, n=-1) -> str:
    return app.llama.of("plan_scene")[n]["messages"][1]["content"]


def test_the_scene_plan_is_offered_appearance_options_and_the_job_records_what_it_took(app):
    def scene(schema, body):
        options = LINE.search(body["messages"][1]["content"]).group(1).split(" | ")
        return sheet(rating="general", must_keep=[], subjects="1 adult woman", body=f"slender build, {options[0]}, {options[-1]}")
    job = plan_with_looks(app, "a woman in a cozy room", seed=5, scene=scene)
    options = LINE.search(scene_message(app)).group(1).split(" | ")
    record = job["meta"]["appearance_options"]
    assert len(options) == 6 and [*record["offered"]["hair"], *record["offered"]["eyes"]] == options
    assert options[0] in record["used"] and options[-1] in record["used"]
    assert "appearance" in app.llama.of("plan_scene")[-1]["messages"][0]["content"]
    assert "scene_options" not in job["meta"]                                         # no wildcard lists here: the appearance line stands alone


def test_the_same_seed_repeats_the_appearance_line_and_another_seed_changes_it(app):
    lines = []
    for seed in (4, 4, 5, 6, 7):
        plan_with_looks(app, "a woman in a cozy room", seed)
        lines.append(LINE.search(scene_message(app)).group(1))
    assert lines[0] == lines[1] and len(set(lines)) >= 3


def test_no_appearance_line_when_the_user_gave_a_look_or_named_a_character(app):
    for request in ("a woman with long red hair in a cozy room", "aria vale in a cozy room"):
        job = plan_with_looks(app, request)
        assert not LINE.search(scene_message(app)) and "appearance_options" not in job["meta"]


def test_no_appearance_line_without_a_tag_library(app):
    app.script_plan({"description": "a woman in a cozy room", "count": 1, "constraints": []})
    app.llama.scripts["plan_scene"] = sheet(rating="general", must_keep=[], subjects="1 adult woman")
    job = app.job(app.plan("a woman in a cozy room", on="anima")["job_ids"][0])
    assert not LINE.search(scene_message(app)) and "appearance_options" not in job["meta"]
