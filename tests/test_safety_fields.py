"""Safety of the new fields, the tag check's reworked rules, and the rating chain.

No LLM, no GPU, except two prompt-level flow tests with the fake LLM.
The content-rule tests build their inputs from the placeholder words of
tests/content_probes.py (the synthetic rules conftest.py loads); no descriptive text is written here.
"""
import pytest

from app.planning.concepts import load_concepts, rating_max, rating_rank
from app.planning.content_safety import check_content
from app.planning.scene import plan_scene, scene_issue
from app.planning.tagcheck import headcount, said
from app.queue.jobs import planner_texts
from tests.content_probes import CSE_MESSAGE, CONTENT_TERMS as CONTENT, MODIFIERS as MOD, NUMBERS as NUM, SUBJECT_TERMS as SUBJ
from tests.test_planning_flow import app  # noqa: F401  (the fixture)
from tests.test_scene_plan import sheet
from tests.test_tagcheck import cc, lib, run
from tests.test_weights import prompt_reply

# Block-test vectors, joined from the single words of tests/content_probes.py.
BLOCKED = [f"1girl, {CONTENT[0]}, {SUBJ[0]}", f"a {CONTENT[1]} {SUBJ[1]} on a beach", f"{CONTENT[2]} {NUM[2]} year old girl",
           f"{CONTENT[3]} scene, {SUBJ[2]}", f"a {SUBJ[3]}. {CONTENT[4]}", f"{CONTENT[5]}, {SUBJ[4]}"]
CLEAN = [f"1girl, {CONTENT[0]}, 25 years old",
         f"a {SUBJ[1]} playing in a park, sunny",
         f"{CONTENT[0]} woman, studio lighting",
         "a red kimono in a forest",
         ""]


def meta_with(**kw):
    meta = {"intent": {"asked": {}}, "assigned": {}, "scene_options": {}}
    meta.update(kw)
    return meta


# ---------- the content rules read every new field ----------

def test_planner_texts_covers_intent_assigned_and_offered():
    meta = meta_with(intent={"asked": {"use_loras": ["u"], "skip_loras": ["s"], "per_lora_tags": ["t"]}},
                     assigned={"camera": "a"},
                     scene_options={"offered": [{"text": "o", "slot": "camera"}]})
    assert planner_texts(meta) == ["u", "s", "t", "a", "o"]
    assert planner_texts({}) == [] and planner_texts(None) == []


@pytest.mark.parametrize("field", ["use_loras", "skip_loras", "per_lora_tags"])
@pytest.mark.parametrize("text", BLOCKED)
def test_block_fires_on_intent_names(field, text):
    meta = meta_with(intent={"asked": {field: [text]}})
    assert check_content([*planner_texts(meta), "a quiet street"]) is not None


@pytest.mark.parametrize("text", BLOCKED)
def test_block_fires_on_assigned_and_offered_entries(text):
    assert check_content(planner_texts(meta_with(assigned={"camera": text}))) is not None
    offered = meta_with(scene_options={"offered": [{"text": text, "slot": "camera"}]})
    assert check_content(planner_texts(offered)) is not None


@pytest.mark.parametrize("text", BLOCKED)
def test_block_fires_on_briefs_scenes_and_final_prompts(text):
    assert check_content([text, "a quiet street"]) is not None          # a brief (job description)
    assert scene_issue(text) != []                                          # a scene plan the rules block
    assert check_content(["a quiet street", text]) is not None          # a final prompt


@pytest.mark.parametrize("text", CLEAN)
def test_adult_and_ordinary_texts_are_not_blocked_in_any_field(text):
    meta = meta_with(intent={"asked": {"use_loras": [text], "skip_loras": [text], "per_lora_tags": [text]}},
                     assigned={"camera": text}, scene_options={"offered": [{"text": text, "slot": "pose"}]})
    assert check_content([*planner_texts(meta), text or "a quiet street"]) is None
    assert scene_issue("a woman waiting in a room, cowboy shot") == []


def test_issue_shape():
    issue = check_content([BLOCKED[0]])
    assert issue["severity"] == "blocking" and issue["field"] == "prompts" and issue["code"] == "cse" and issue["message"] == CSE_MESSAGE


# ---------- the tag check: meta-words are never added ----------

META_WORDS = ["different", "viewing", "angles", "time", "shown", "captured"]


def test_meta_words_are_not_demanded_back():
    r = run("masterpiece, best quality, 1girl, solo, park",
            keep_text="a different image from a different viewing angle each time, shown as captured")
    for word in META_WORDS:
        assert word not in r.text.split(", ") and word not in r.text


def test_only_tags_used_often_enough_are_demanded():
    from app.planning.taglib import parse_tags, TagLib
    tags, aliases = parse_tags("frequent,0,5000,\nrareword,0,500,\n")
    small = TagLib(tags, aliases, [])
    r = run("masterpiece, best quality, 1girl, park", library=small, keep_text="a frequent rareword hut")
    assert "frequent" in r.text and "rareword" not in r.text


# ---------- solo follows the headcount ----------

def test_headcount_folds_people_into_named_kinds():
    assert headcount({"girls": 1, "boys": None, "others": None, "people": 1, "no_humans": None}) == 1
    assert headcount({"girls": 1, "boys": None, "others": None, "people": 3, "no_humans": None}) == 3
    assert headcount({"girls": 2, "boys": 1, "others": None, "people": None, "no_humans": None}) == 3


def test_solo_is_kept_for_one_person_including_the_people_shape():
    one = {"girls": 1, "boys": None, "others": None, "people": None, "no_humans": None}
    assert "solo" in run("masterpiece, best quality, 1girl, solo, park", subjects=one).text
    same = {"girls": 1, "boys": None, "others": None, "people": 1, "no_humans": None}
    assert "solo" in run("masterpiece, best quality, 1girl, solo, park", subjects=same).text


def test_solo_is_removed_for_real_groups():
    two = {"girls": 2, "boys": None, "others": None, "people": None, "no_humans": None}
    r = run("masterpiece, best quality, 2girls, solo, park", subjects=two)
    assert "solo" not in r.text.split(", ") and "2girls" in r.text


def test_named_thing_protection_still_holds():
    r = run("masterpiece, best quality, silk robe, forest", must_keep=["red kimono"], typed="a red kimono in a forest")
    assert "red kimono" in r.text and "silk robe" in r.text


def test_singular_and_plural_count_as_said():
    assert said("brown eyes", "one brown eye") and said("tree", "trees")
    r = run("masterpiece, best quality, 1girl, park", keep_text="trees on a hill")
    assert "tree" in r.text


# ---------- rating: intent to scene to prompt writer ----------

def test_rating_chain_never_goes_below_what_was_asked():
    assert rating_rank("general") < rating_rank("sensitive") < rating_rank("nsfw") < rating_rank("explicit")
    assert rating_max("nsfw", "general") == "nsfw" and rating_max("general", "explicit") == "explicit"
    assert rating_max("sensitive", "sensitive") == "sensitive"


def test_concepts_still_count_through_max():
    book = load_concepts()
    assert book.rating_min("a cat in a garden") == "general"          # plain requests stay general
    assert rating_max("general", book.rating_min("a cat in a garden")) == "general"
    hot = book.rating_min("an explicit erotic scene with adults")
    assert rating_rank(hot) >= rating_rank("explicit")
    assert rating_max("sensitive", hot) == hot                        # concepts only raise, never lower


def test_rating_tag_written_for_nsfw_and_not_for_general():
    assert "nsfw" in run("masterpiece, best quality, 1girl, park", rating="nsfw").text
    plain = run("masterpiece, best quality, 1girl, park", rating="general").text
    assert not {"sensitive", "nsfw", "explicit"} & set(plain.split(", "))
    assert "nsfw" not in run("masterpiece, best quality, 1girl, park", rating="nsfw", family="Pony").text


def test_intent_rating_reaches_the_final_prompt(app):
    from tests.test_tagcheck import lib as make_lib
    app.state.planner.ctx.tag_library = make_lib()      # the tag check (and its rating tag) only runs with a library
    app.llama.scripts["plan_request"] = {"jobs": [{"description": "an adult woman in a hotel room", "count": 1,
                                                   "variety": "variations", "constraints": [], "issues": [], "reason": "fits"}],
                                         "intent": {"rating": "nsfw"}}
    app.llama.scripts["pick_loras"] = {"loras": [], "reason": "none fit"}
    app.llama.scripts["plan_scene"] = sheet(rating="general", must_keep=[], subjects="1 adult woman")
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, solo, hotel room")
    job = app.job(app.plan("an adult woman in a hotel room, nsfw", on="anima")["job_ids"][0])
    assert job["meta"]["intent"]["rating"] == "nsfw"
    assert job["meta"]["scene"]["rating"] == "nsfw"                   # the scene never goes below the intent
    assert "nsfw" in job["fields"]["prompts"]["value"][0]


def test_plain_request_stays_general(app):
    from tests.test_tagcheck import lib as make_lib
    app.state.planner.ctx.tag_library = make_lib()
    app.llama.scripts["plan_request"] = {"jobs": [{"description": "a girl in a yellow raincoat", "count": 1,
                                                   "variety": "variations", "constraints": [], "issues": [], "reason": "fits"}]}
    app.llama.scripts["pick_loras"] = {"loras": [], "reason": "none fit"}
    app.llama.scripts["plan_scene"] = sheet(rating="general", must_keep=[], subjects="1 adult woman")
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, solo, raincoat, street")
    job = app.job(app.plan("a girl in a yellow raincoat", on="anima")["job_ids"][0])
    assert job["meta"]["scene"]["rating"] == "general"
    assert not {"sensitive", "nsfw", "explicit"} & set(job["fields"]["prompts"]["value"][0].split(", "))


async def test_scene_floor_is_the_max_of_asked_and_concepts():
    from tests.test_scene_plan import FakeLlm, job as scene_job
    llm = FakeLlm(sheet(rating="general", detail_level="normal", must_keep=[]))
    from app.planning import scene as S
    from unittest.mock import patch
    with patch.object(S, "load_concepts", lambda: load_concepts()):
        scene, _ = await plan_scene(__import__("types").SimpleNamespace(llm=llm, taglib=lambda: None, embedder=None),
                                    {**scene_job("an explicit erotic scene with adults"), "min_rating": "sensitive"},
                                    "sensitive")
    assert rating_rank(scene["rating"]) >= rating_rank("explicit")
