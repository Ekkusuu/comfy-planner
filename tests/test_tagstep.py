"""The tag step of planning: real tags offered before writing, the check after, one repair call; subjects as data.
A small fake library and the fake LLM; nothing is sent anywhere."""
import asyncio

from app.planning.tagcheck import describe_subjects, is_group, open_subjects
from app.planning.taglib import TagLib, parse_tags
from app.planning.tagstep import TagStep, keyword_hits
from tests.content_probes import CONTENT_TERMS as CONTENT, MODIFIERS as MOD, NUMBERS as NUM, SUBJECT_TERMS as SUBJ
from tests.test_planning_flow import app  # noqa: F401  (the fixture: a running app with a fake LLM)
from tests.test_tagcheck import IMPLICATIONS, ROWS, cc
from tests.test_weights import prompt_reply, writer_text

MORE = """picnic,0,20000,
cherry_blossoms,0,120000,
old_woman,0,30000,
probem1_girl,0,2000,
school_uniform,0,300000,
forest,0,400000,
"""
GROUP = {"girls": 3, "boys": -1, "others": -1, "no_humans": False}


def library() -> TagLib:
    tags, aliases = parse_tags(ROWS + MORE)
    return TagLib(tags, aliases, IMPLICATIONS)


class NoMeaning:       # search by meaning is off: keywords only
    embedder = None


def suggest(text, family="Anima", lib=None) -> list[str]:
    context = cc(family=family, library=lib or library())
    return asyncio.run(TagStep(NoMeaning(), context.lib, context, family).suggestions(text))


# ---------- offering real tags ----------

def test_keyword_hits_find_tags_whose_words_are_all_in_the_request():
    hits = keyword_hits(library(), "an old man under cherry blossoms in the forest")
    assert {"old_man", "cherry_blossoms", "forest"} <= set(hits)
    assert "old_woman" not in hits and "long_hair" not in hits


def test_suggestions_are_written_the_way_the_family_writes_them():
    assert "old man" in suggest("an old man in a park") and "cherry blossoms" in suggest("cherry blossom trees")


def test_tags_the_content_rules_avoid_are_never_offered_nothing_else_is_held_back():
    out = suggest(f"a {MOD[0]} girl in a school uniform, a {SUBJ[0]}, sitting in a park, 1girl")
    assert f"{MOD[0]} girl" not in out and SUBJ[0] not in out         # the entry-filter group of the content rules
    assert "sitting" in out and "park" in out and "1girl" in out
    assert "school uniform" in suggest("a school uniform")        # not one of its words: offered


def test_at_most_forty_and_none_for_a_family_without_danbooru_rules():
    big = TagLib({f"tag{n}": (0, 5000) for n in range(1500)} | {"word": (0, 5000)})
    assert len(suggest(" ".join(["word"] + [f"tag{n}" for n in range(100)]), lib=big)) <= 40
    assert suggest("an old man", family="Krea-2") == []


# ---------- subjects ----------

def test_subjects_become_numbers_and_open_ones_stay_open():
    assert open_subjects(GROUP) == {"girls": 3, "boys": None, "others": None, "people": None, "no_humans": None}
    assert open_subjects({"girls": -1, "boys": -1, "others": -1, "people": -1, "no_humans": False}) is None and open_subjects(None) is None
    assert is_group(open_subjects({"girls": -1, "boys": -1, "others": -1, "people": 3, "no_humans": False}))
    assert is_group({"girls": 3, "boys": None}) and not is_group({"girls": 1, "boys": None}) and not is_group({"girls": 2, "no_humans": True})
    assert "3 girls" in describe_subjects({"girls": 3, "boys": 1}) and "1 boy" in describe_subjects({"girls": 3, "boys": 1})


# ---------- the flow ----------

def with_library(app):
    app.state.planner.ctx.tag_library = library()


def test_a_group_request_gets_count_tags_and_a_visible_record(app):
    with_library(app)
    app.script_plan({"description": "friends at a park", "count": 2, "subjects": GROUP})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, solo, 1girl, 1old man, park, sit, long_hair",
                                                      "masterpiece, best quality, 3girls, park, sitting")
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    one, two = job["fields"]["prompts"]["value"]
    assert one == "masterpiece, best quality, 3girls, multiple girls, old man, park, sitting, long hair"
    assert two == "masterpiece, best quality, 3girls, multiple girls, park, sitting"
    record = job["fields"]["prompts"]["items"][0]["tagcheck"]
    assert any(c["from"] == "1old man" and c["to"] == "old man" for c in record)
    assert any(c["from"] == "solo" and c["to"] is None for c in record)
    assert not any("long_hair" in str(c) for c in record)                       # a format change is silent
    assert "3 girls" in writer_text(app) and "Real tags that match this job" in writer_text(app)
    assert app.llama.of("repair_tags") == []                                    # a clean prompt costs no extra call


def test_a_prompt_the_check_left_alone_has_no_record(app):
    with_library(app)
    app.script_plan({"description": "a park", "count": 1})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, park, sitting")
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert all("tagcheck" not in i for i in job["fields"]["prompts"]["items"])


def test_without_a_library_the_prompts_are_what_the_llm_wrote(app):
    app.state.planner.ctx.tag_library = None        # and the test data folder has no taglib: skipped quietly
    app.script_plan({"description": "friends", "count": 1, "subjects": GROUP})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, solo, 1old man, park")
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert job["fields"]["prompts"]["value"] == ["masterpiece, best quality, solo, 1old man, park"]
    assert app.llama.of("repair_tags") == []


def test_a_sentence_workflow_gets_no_tag_work(app):
    with_library(app)
    app.script_plan({"description": "an old man", "count": 1, "subjects": GROUP})
    app.llama.scripts["write_prompts"] = prompt_reply("An old man, solo, 1old man, sits in a park.")
    job = app.job(app.plan(on="krea")["job_ids"][0])
    assert job["fields"]["prompts"]["value"] == ["An old man, solo, 1old man, sits in a park."]
    assert all("tagcheck" not in i for i in job["fields"]["prompts"]["items"])


def test_something_still_wrong_goes_to_one_repair_call_and_a_clean_prompt_does_not(app):
    with_library(app)
    app.script_plan({"description": "a park", "count": 2})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, 2girls, park", "masterpiece, best quality, 1girl, park")
    app.llama.scripts["repair_tags"] = {"prompts": ["masterpiece, best quality, 2girls, park"]}
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert len(app.llama.of("repair_tags")) == 1
    assert job["fields"]["prompts"]["value"] == ["masterpiece, best quality, 2girls, multiple girls, park", "masterpiece, best quality, 1girl, park"]
    assert any("repaired" in c["why"] for c in job["fields"]["prompts"]["items"][0]["tagcheck"])
    sent = app.llama.of("repair_tags")[0]["messages"][-1]["content"]
    assert "1girl, 2girls" in sent and "different girls counts" in sent


def test_a_failed_repair_leaves_the_prompt_as_the_check_left_it(app):
    with_library(app)
    app.script_plan({"description": "a park", "count": 1})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, 2girls, park")
    app.llama.scripts["repair_tags"] = {"prompts": ["a", "b"]}          # the wrong number of prompts
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert "1girl, 2girls" in job["fields"]["prompts"]["value"][0] and "tag check could not settle" in job["fields"]["prompts"]["note"]


def test_a_tag_the_person_typed_stays_whatever_the_subjects_say(app):
    with_library(app)
    app.script_plan({"description": "a girl in a park", "count": 1, "subjects": GROUP})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, solo, park")
    job = app.job(app.plan("solo, a girl in a park", on="tagwf")["job_ids"][0])
    assert "solo" in job["fields"]["prompts"]["value"][0].split(", ")


def test_tweak_gets_the_same_check(app):
    with_library(app)
    app.script_plan({"description": "a park", "count": 1})
    job_id = app.plan(on="tagwf")["job_ids"][0]
    app.llama.scripts["apply_tweak"] = {"prompts": ["masterpiece, best quality, 1old man, sit, park"], "reason": "an old man"}
    plan = app.client.post(f"/api/jobs/{job_id}/tweak", json={"instruction": "add someone"}).json()
    assert app.wait(plan["id"])["status"] == "done"
    prompts = app.job(job_id)["fields"]["prompts"]
    assert prompts["value"] == ["masterpiece, best quality, old man, sitting, park"]
    assert prompts["items"][0]["tagcheck"][0]["from"] == "1old man"
    assert app.llama.of("repair_tags") == []


def test_the_safety_check_still_runs_after_the_tag_check(app):
    with_library(app)
    app.script_plan({"description": "a nude girl in a park", "count": 1})
    app.llama.scripts["write_prompts"] = prompt_reply(f"masterpiece, best quality, {CONTENT[0]}, {SUBJ[0]}, park")
    issues = app.job(app.plan(on="tagwf")["job_ids"][0])["issues"]
    assert any(i["code"] == "cse" for i in issues)


def test_people_of_unsaid_kind_force_no_gender_but_make_a_group(app):
    """"3 friends": the LLM gives people 3 and no girls or boys: no count tag is forced, `solo` and single counts go, a group pose is asked for."""
    with_library(app)
    app.script_plan({"description": "3 friends at a picnic", "count": 1, "subjects": {"girls": -1, "boys": -1, "others": -1, "people": 3, "no_humans": False}})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, solo, 1girl, 3girls, multiple girls, park")
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert job["fields"]["prompts"]["value"] == ["masterpiece, best quality, 3girls, multiple girls, park, picnic"]
    assert "3 people" in writer_text(app)


def test_the_people_tags_come_in_a_fixed_order():
    from tests.test_tagcheck import run
    r = run("masterpiece, best quality, score_9, newest, 4boys, multiple boys, 1girl, aria vale, park", subjects={"girls": 1, "boys": 4, "others": None, "people": None, "no_humans": None})
    assert r.text == "masterpiece, best quality, score_9, newest, 1girl, 4boys, multiple boys, aria vale, park"
