"""Variety: the same vague request gives different prompts between runs while the user's own words stay locked.
Wildcard options reach the scene plan, tag offers are sampled (avoiding recent tags), vague-word lists rotate, and one seed per job
makes all of it repeatable. Sample words are neutral; nothing here calls an LLM, ComfyUI or the GPU."""
import asyncio
import json
import random
import re
from types import SimpleNamespace

from app.planning import prompts
from app.planning.concepts import CONCEPT_SAMPLE, subset
from app.planning.context import RECENT_JOBS, derive_rng, derive_seed, tag_name
from app.planning.taglib import TagLib
from app.planning.tagstep import MAX_EXACT_TAGS, MAX_SLOT_TAGS, SLOT_POOL, TagStep, sample_tags, tag_offer_record
from app.wildcards.ideas import IdeaSet
from tests.content_probes import CONTENT_TERMS as CONTENT, MODIFIERS as MOD, NUMBERS as NUM, SUBJECT_TERMS as SUBJ
from tests.test_planning_flow import app  # noqa: F401  (the fixture: a running app with a fake LLM)
from tests.test_scene_plan import sheet
from tests.test_tagcheck import PREFIX, cc, run
from tests.test_tagstep import NoMeaning, with_library
from tests.test_weights import prompt_reply
from tests.test_wildcards import OUTFITS, import_into, source  # noqa: F401  (the wildcard tree and the import helper)

def test_a_wildcard_entry_the_content_rules_avoid_is_never_offered(monkeypatch):
    drawn = IdeaSet([], [[{"list": "x", "role": "outfit", "text": f"school uniform for a {SUBJ[2]}"},
                          {"list": "x", "role": "outfit", "text": "red dress"}],
                         [{"list": "x", "role": "outfit", "text": f"{SUBJ[7]} clothes"}]])

    async def nothing_related(*_):
        return None
    monkeypatch.setattr(prompts, "idea_brief", lambda *_: ("", "", {"job": "", "constraints": []}))
    monkeypatch.setattr(prompts, "usable_lists", lambda _: [])
    monkeypatch.setattr(prompts, "related_lists", nothing_related)
    async def offered(*_, **__):
        return drawn
    monkeypatch.setattr(prompts.offering, "build", offered)
    ideas = asyncio.run(prompts.draw_ideas(SimpleNamespace(wildcards=None, config=None), {}, 2, 1))
    assert [[o["text"] for o in each] for each in ideas.per_prompt] == [["red dress"], []]


# ---------- sample_tags: the pick among good matches ----------

POOL = [f"tag{n:02d}" for n in range(60)]      # best match first


def test_the_pick_is_repeatable_for_a_seed_and_stays_inside_the_good_matches():
    one = sample_tags(POOL, 10, random.Random(5))
    assert one == sample_tags(POOL, 10, random.Random(5)) and len(one) == 10
    assert one == sorted(one)                                                  # returned in relevance order
    seen = set()
    for seed in range(60):
        seen.update(sample_tags(POOL, 10, random.Random(seed)))
    assert seen <= set(POOL[:SLOT_POOL])                                       # relevance is the gate: nothing beyond the pool
    assert len(seen) > 20                                                      # and over runs it is not always the same top ten


def test_different_seeds_give_different_picks():
    picks = {tuple(sample_tags(POOL, 10, random.Random(seed))) for seed in range(8)}
    assert len(picks) >= 6


def test_better_matches_are_somewhat_likelier():
    firsts = lasts = 0
    for seed in range(300):
        picked = sample_tags(POOL, 10, random.Random(seed))
        firsts += POOL[0] in picked
        lasts += POOL[SLOT_POOL - 1] in picked
    assert firsts > lasts


def test_recent_tags_are_left_out_while_enough_others_exist():
    recent = set(POOL[:20])
    for seed in range(20):
        assert not set(sample_tags(POOL, 10, random.Random(seed), recent)) & recent        # 10 fresh ones remain in the pool of 30


def test_a_slot_is_never_starved_when_recent_tags_leave_too_few():
    recent = set(POOL[:SLOT_POOL - 4])                                          # only 4 fresh tags left in the pool
    picked = sample_tags(POOL, 10, random.Random(1), recent)
    assert len(picked) == 10 and set(POOL[SLOT_POOL - 4:SLOT_POOL]) <= set(picked)       # all 4 fresh ones, topped up from the recent
    assert len(sample_tags(POOL, 10, random.Random(1), set(POOL))) == 10                 # even when every tag is recent
    assert sample_tags(POOL[:6], 10, random.Random(1), set(POOL)) == POOL[:6]            # a small pool is offered whole


# ---------- slot_suggestions ----------

def word(n: int) -> str:
    return "".join(chr(97 + int(d)) for d in f"{n:03d}") + "x"        # 'aaax', 'aabx', ... : plain words, equal after stemming


NAMES = [word(n) for n in range(50)]
BIG = TagLib({name: (0, 10_000 - n) for n, name in enumerate(NAMES)} | {"fillerx": (0, 99), "elsewherex": (0, 5000)})       # use count falls with n


def offers(rng=None, avoid=None, text=None) -> list[str]:
    context = cc(family="Anima", library=BIG)
    import asyncio
    got = asyncio.run(TagStep(NoMeaning(), context.lib, context, "Anima").slot_suggestions({"outfit": text or ", ".join(NAMES)}, None, rng, avoid))
    return got.get("outfit", [])


def test_without_a_generator_the_offer_is_still_the_best_ten_by_use():
    assert offers() == NAMES[:MAX_SLOT_TAGS]


def test_the_offer_is_a_repeatable_random_pick_among_the_matches_only():
    first = offers(random.Random(3))
    assert first == offers(random.Random(3)) and len(first) == MAX_SLOT_TAGS
    assert set(first) <= set(NAMES[:SLOT_POOL]) and "elsewherex" not in first       # a tag the slot's words do not name is never offered
    assert len({tuple(offers(random.Random(s))) for s in range(6)}) >= 5
    assert first != NAMES[:MAX_SLOT_TAGS]


def test_recent_tags_are_avoided_and_the_fallback_keeps_the_slot_full():
    recent = set(NAMES[:20])
    assert not (set(offers(random.Random(2), recent)) - set(NAMES[:MAX_EXACT_TAGS])) & recent        # the slot's own phrases are offered whatever is recent
    everything = offers(random.Random(2), set(NAMES))                              # every match is recent: still a full offer
    assert len(everything) == MAX_SLOT_TAGS and set(everything) <= set(NAMES[:SLOT_POOL])
    assert len(offers(random.Random(2), set(NAMES[:SLOT_POOL - 3]))) == MAX_SLOT_TAGS


def test_tag_offers_are_recorded_with_what_the_prompt_used():
    groups = {"outfit": ["red dress", "scarf"], "setting": ["forest"]}
    record = tag_offer_record(groups, "masterpiece, (red dress:1.2), forest, rain")
    assert record == {"offered": groups, "used": ["red dress", "forest"]}


# ---------- the tags found for a vague word rotate ----------

FOUND = [f"found tag {n}" for n in range(12)]          # the better matches the tag search found for a request's words, best first


def test_a_vague_word_gives_a_different_subset_each_run_but_the_whole_list_without_a_generator():
    runs = [subset(FOUND, random.Random(s)) for s in range(8)]
    assert all(len(r) == CONCEPT_SAMPLE and set(r) <= set(FOUND) for r in runs)
    assert [t for t in FOUND if t in runs[0]] == runs[0]                                      # the order kept
    assert len({tuple(r) for r in runs}) >= 6
    assert runs[0] == subset(FOUND, random.Random(0))                                         # repeatable
    assert subset(FOUND, None) == FOUND                                                       # no generator: whole and unchanged


def test_a_concept_tag_the_user_wrote_is_always_kept():
    tags = [f"item{n}x" for n in range(20)] + ["plant"]
    for seed in range(15):
        got = subset(tags, random.Random(seed), named="a cozy room with a plant")
        assert "plant" in got and len(got) == CONCEPT_SAMPLE + 1
    assert len(subset(tags, random.Random(1), named="a cozy room")) == CONCEPT_SAMPLE


def test_short_lists_stay_whole():
    assert subset(["a", "b"], random.Random(1)) == ["a", "b"] and subset(list("abcdefghijkl"), None) == list("abcdefghijkl")


# ---------- derived seeds ----------

def test_one_job_seed_gives_each_purpose_its_own_repeatable_numbers():
    assert derive_seed(7, "tags") == derive_seed(7, "tags") != derive_seed(7, "ideas:0") and derive_seed(7, "tags") != derive_seed(8, "tags")
    assert derive_rng(7, "x").random() == derive_rng(7, "x").random()
    assert tag_name("(Red_Dress:1.2)") == "red dress"


# ---------- the user's words stay locked ----------

def test_a_wildcard_pick_never_removes_what_the_user_named():
    result = run("masterpiece, best quality, silk robe, forest", must_keep=["red kimono"], typed="a red kimono in a forest")
    assert "red kimono" in result.text and "silk robe" in result.text                        # the draw is added to, never swapped for the user's words


# ---------- in planning ----------

CITY = "a girl in a city at night"


def option_block(app, n=-1) -> str:
    message = app.llama.of("plan_scene")[n]["messages"][1]["content"]
    return message.split("## Wildcard options")[1].split("\n", 1)[1].strip()


def option_lines(block: str) -> dict[str, list[str]]:
    lines = re.findall(r"^(\w+) \(([^)]+)\): (.+)$", block, flags=re.M)
    return {f"{role} ({name})": text.split(" | ") for role, name, text in lines}


def test_the_scene_plan_is_offered_several_options_and_the_record_says_what_it_took(app, source):
    import_into(app, source)
    app.script_plan({"description": "a girl in a city at night", "count": 1, "constraints": []})

    def scene(schema, body):
        lines = option_lines(body["messages"][1]["content"].split("## Wildcard options")[1])
        outfit = next(options for label, options in lines.items() if label.startswith("outfit"))
        return sheet(rating="general", outfit=outfit[0], must_keep=[], subjects="1 adult woman", setting="city street")
    app.llama.scripts["plan_scene"] = scene
    app.llama.scripts["write_prompts"] = lambda schema, body: prompt_reply("1girl, city street, night, " + re.search(r"outfit \([^)]+\): ([^|\n]+)", body["messages"][1]["content"]).group(1).strip())
    job = app.job(app.plan(CITY, on="tagwf")["job_ids"][0])
    block = app.llama.of("plan_scene")[0]["messages"][1]["content"]
    assert "<<<BEGIN DATA: wildcard options>>>" in block and "never let one replace" in app.llama.of("plan_scene")[0]["messages"][0]["content"]
    options = option_lines(option_block(app, 0).replace("<<<BEGIN DATA: wildcard options>>>", "").replace("<<<END DATA: wildcard options>>>", ""))
    assert len(options) >= 2 and all(len(texts) >= 3 for texts in options.values())          # several options per list, not one
    assert all(label.split(" ")[0] in ("camera", "outfit", "pose", "lighting") for label in options)      # the city already fixes the setting
    record = job["meta"]["scene_options"]
    assert {o["slot"] for o in record["offered"]} == {label.split(" ")[0] for label in options}
    outfit = job["meta"]["scene"]["outfit"]
    assert outfit in OUTFITS and outfit in record["used"] and outfit in record["in_prompts"]     # taken into the scene and the prompt
    assert any(entry["by"] for entry in record["lists"])


def test_options_are_never_offered_for_what_the_user_named_and_their_words_stay_in_the_plan(app, source):
    import_into(app, source)
    app.script_plan({"description": "a girl in a red kimono in a city at night", "count": 1, "constraints": []})
    app.llama.scripts["plan_scene"] = sheet(rating="general", outfit="silk robe", must_keep=["red kimono"], subjects="1 adult woman")      # the model drifted
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, silk robe, city street, night")
    with_library(app)
    job = app.job(app.plan("a girl in a red kimono in a city at night", on="anima")["job_ids"][0])
    labels = option_lines(option_block(app, 0))
    assert not any(label.startswith(("outfit", "palette", "setting")) for label in labels)              # kimono, red and city fix three roles
    assert not set(OUTFITS) & {text for texts in labels.values() for text in texts}
    assert job["meta"]["scene"]["must_keep"] == ["red kimono"]
    assert "red kimono" in job["fields"]["prompts"]["value"][0]                                         # the keep check put it back


def test_the_same_seed_repeats_the_options_and_another_seed_changes_them(app, source):
    import_into(app, source)
    app.script_plan({"description": "a girl in a city at night", "count": 1, "constraints": []})
    app.llama.scripts["plan_scene"] = sheet(rating="general", must_keep=[], subjects="1 adult woman")
    blocks = []
    for seed in (4, 4, 5, 6, 7):
        app.state.planner.ctx.rng = random.Random(seed)
        app.plan(CITY, on="tagwf")
        blocks.append(option_block(app))
    assert blocks[0] == blocks[1] and len(set(blocks)) >= 3


def test_every_job_of_a_batch_gets_its_own_draw(app, source):
    import_into(app, source)
    app.script_plan({"description": "6 unique ruins", "count": 6, "variety": "unique"})
    app.llama.scripts["write_briefs"] = lambda schema, body: {
        "briefs": [f"Ruin number{i} with moss{i} and fog{i} in a valley{i}" for i in range(schema["properties"]["briefs"]["minItems"])]}
    app.llama.scripts["plan_scene"] = sheet(rating="general", must_keep=[], subjects="no humans, scenery")
    plan = app.plan("6 unique ruins", on="tagwf")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 6
    blocks = [option_block(app, n) for n in range(6)]
    assert len(set(blocks)) >= 5                                                                      # one seed per child job
    drawn = [app.job(i)["meta"].get("scene_options", {}).get("offered") for i in plan["job_ids"]]
    assert all(drawn) and len({tuple(o["text"] for o in each) for each in drawn}) >= 5


def test_the_scene_options_are_none_when_wildcards_are_not_imported(app):
    app.script_plan({"description": "a girl in a city", "count": 1})
    app.llama.scripts["plan_scene"] = sheet(rating="general", must_keep=[], subjects="1 adult woman")
    job = app.job(app.plan(CITY, on="tagwf")["job_ids"][0])
    assert option_block(app, 0) == "(none)" and "scene_options" not in job["meta"]


def test_tag_offers_are_recorded_per_prompt_and_recent_prompts_are_remembered(app):
    with_library(app)
    app.script_plan({"description": "a girl in a forest", "count": 1})
    app.llama.scripts["plan_scene"] = sheet(rating="general", must_keep=[], subjects="1 adult woman", outfit="school uniform", setting="forest",
                                            pose="standing", expression="", camera="", lighting="", mood="", body="")
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, forest, school uniform, standing")
    job = app.job(app.plan(on="anima")["job_ids"][0])
    item = job["fields"]["prompts"]["items"][0]["tag_offers"]
    assert "forest" in item["offered"]["setting"] and "forest" in item["used"]
    assert set(item["used"]) <= {t for tags in item["offered"].values() for t in tags}
    ctx = app.state.planner.ctx
    assert {"forest", "school uniform", "standing"} <= ctx.recent_tags()                              # read back from the stored job
    assert RECENT_JOBS >= 1


# ---------- style tags that every prompt shares (the habit rule) ----------

FILLER = [f"detail{chr(97 + n)}x" for n in range(20)]        # twenty plain content tags, enough to stay above the floor
HABITS = {"bokeh": 0.83, "cinematic_lighting": 0.8, "solo": 0.9}


def habit_run(extra, **kw):
    return run(", ".join(["masterpiece, best quality", "1girl", "solo", *FILLER, *extra]), **{"habits": HABITS, **kw})


def test_a_tag_most_recent_prompts_share_is_dropped_when_nothing_asks_for_it():
    r = habit_run(["bokeh", "cinematic lighting"])
    assert "bokeh" not in r.text and "cinematic lighting" not in r.text
    assert "solo" in r.text and "1girl" in r.text and r.text.startswith("masterpiece, best quality")       # people tags and the prefix are never touched
    assert {c["from"] for c in r.changes} == {"bokeh", "cinematic lighting"} and all("recent prompts" in c["why"] for c in r.changes)


def test_a_habit_tag_stays_when_the_user_the_plan_or_the_offers_ask_for_it():
    assert "bokeh" in habit_run(["bokeh"], typed="a portrait with bokeh").text                # the user's words
    assert "bokeh" in habit_run(["bokeh"], keep_text="a portrait with bokeh").text
    assert "bokeh" in habit_run(["bokeh"], must_keep=["bokeh"]).text
    assert "bokeh" in habit_run(["bokeh"], plan_text="lighting: soft, bokeh lights").text    # the scene plan says it
    assert "bokeh" in habit_run(["bokeh"], offered=["bokeh", "rain"]).text                    # this job's own tag offers include it
    assert "bokeh" in habit_run(["bokeh"], protected=[*PREFIX, "bokeh"]).text                 # a style tag or trigger word
    assert "bokeh" in habit_run(["bokeh"], habits={}).text                                    # no habit known (a fresh install)


def test_the_habit_rule_never_starves_a_prompt_and_drops_at_most_two_tags():
    assert "bokeh" in run("masterpiece, best quality, 1girl, solo, rain, bokeh", habits={"bokeh": 0.9}).text      # a short prompt keeps its habits
    many = habit_run(["bokeh", "cinematic lighting", "depth of field"], habits={"bokeh": 0.9, "cinematic_lighting": 0.8, "depth_of_field": 0.7})
    assert "bokeh" not in many.text and "cinematic lighting" not in many.text and "depth of field" in many.text      # the two most common


def card_with(examples):
    from tests.test_planning_flow import anima_card
    return anima_card().model_copy(update={"examples": examples})


def test_the_writer_sees_card_examples_without_the_tags_everyone_copies_and_the_card_is_unchanged():
    example = "masterpiece, best quality, 1girl, solo, night city, bokeh, reflections, cinematic lighting"
    card, wf = card_with([example]), {"title": "t", "kind": "t2i"}
    habits = {"bokeh": 0.83, "cinematic_lighting": 0.8, "solo": 0.9, "masterpiece": 1.0, "best_quality": 1.0}
    assert prompts.card_view(card, wf, habits)["examples"] == ["masterpiece, best quality, 1girl, solo, night city, reflections"]     # prefix and people tags stay
    assert card.examples == [example]                                                         # the user's card is not edited
    assert prompts.card_view(card, wf)["examples"] == [example]                               # no habits: as written


def store_prompts(app, texts: list[str]):
    for text in texts:
        app.state.planner.ctx.db.execute(
            "INSERT INTO jobs (workflow_id, description, status, count, fields, issues, meta, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (app.ids["anima"], "x", "ready", 1, json.dumps({"prompts": {"value": [text]}}), "[]", "{}", "2026-01-01T00:00:00+00:00"))


def test_habit_tags_are_counted_from_the_stored_jobs_and_need_enough_prompts(app):
    ctx = app.state.planner.ctx
    store_prompts(app, ["bokeh, rain"] * 5)
    assert ctx.habit_tags() == {}                                                             # five prompts are too few to judge by
    store_prompts(app, ["bokeh, sun", "bokeh, sun", "rain"])
    assert ctx.habit_tags() == {"bokeh": 7 / 8, "rain": 6 / 8}
    assert ctx.recent_tags() == {"bokeh", "rain", "sun"}


def test_a_planned_prompt_loses_the_shared_tag_but_keeps_it_when_the_user_asked(app):
    with_library(app)
    store_prompts(app, ["masterpiece, best quality, 1girl, rain, bokeh"] * 10)
    app.llama.scripts["plan_scene"] = sheet(rating="general", must_keep=[], subjects="1 adult woman")
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, solo, " + ", ".join(FILLER) + ", bokeh")
    app.script_plan({"description": "a girl in a forest", "count": 1})
    job = app.job(app.plan("a girl in a forest", on="anima")["job_ids"][0])
    assert "bokeh" not in job["fields"]["prompts"]["value"][0]
    assert any(c["from"] == "bokeh" and "recent prompts" in c["why"] for c in job["fields"]["prompts"]["items"][0]["tagcheck"])
    app.script_plan({"description": "a girl in a forest with bokeh", "count": 1})
    job = app.job(app.plan("a girl in a forest with bokeh", on="anima")["job_ids"][0])
    assert "bokeh" in job["fields"]["prompts"]["value"][0]                                    # the user asked for it


# ---------- the size floor adds no wrong or second garments ----------

SUBJECT_GIRL = {"girls": 1, "boys": None, "others": None, "no_humans": None}      # the planner leaves boys open for a request about a girl
SUBJECT_BOYS = {"girls": 0, "boys": 2, "others": None, "no_humans": None}
SUBJECT_MIXED = {"girls": 1, "boys": 1, "others": None, "no_humans": None}
OPEN_SUBJECTS = {"girls": None, "boys": None, "others": None, "no_humans": None}


def test_a_tag_that_marks_the_other_sex_is_not_offered_and_not_added():
    lib = TagLib({name: (0, 1000) for name in ("male_swimwear", "red_dress", "female_swimwear")})

    def offer(subjects, typed=""):
        context = cc(family="Anima", library=lib, subjects=subjects, typed=typed)
        slots = {"outfit": "male swimwear, red dress, female swimwear"}
        return set(asyncio.run(TagStep(NoMeaning(), lib, context, "Anima").slot_suggestions(slots, None, None)).get("outfit", []))
    assert offer(SUBJECT_GIRL) == {"red dress", "female swimwear"}                             # an all-female scene: nothing male
    assert offer(SUBJECT_BOYS) == {"male swimwear", "red dress"}                               # the reverse
    everything = {"male swimwear", "red dress", "female swimwear"}
    assert offer(SUBJECT_MIXED) == offer(None) == offer(OPEN_SUBJECTS) == everything           # mixed or unknown: no filter
    assert "male swimwear" in offer(SUBJECT_GIRL, typed="a girl in male swimwear")            # something the user asked for is never filtered
    offered = ["male swimwear", "rain", *[f"extra{chr(97 + n)}x" for n in range(30)]]
    r = run("masterpiece, best quality, 1girl, park", detail_level="high", subjects=SUBJECT_GIRL, offered=offered, slots=plan_of(offered))
    assert "male swimwear" not in r.text and "rain" in r.text                                  # the size floor skips it too


def plan_of(offered):
    """A scene plan that says every offered tag as a phrase of its own: the size floor adds only tags that restate the plan."""
    return {"setting": ", ".join(offered)}


def floor_run(offered, clothes=(), **kw):
    return run("masterpiece, best quality, 1girl, solo, park", detail_level="high", offered=offered, outfit_offers=set(clothes), slots=plan_of(offered), **kw)


def test_the_floor_uses_scene_slots_first_and_clothes_last():
    from tests.test_tagcheck import lib
    step = TagStep(NoMeaning(), lib(), cc(family="Anima"), "Anima")
    step.offer({"outfit": ["bodysuit"], "pose": ["sitting"], "body": ["slender"], "setting": ["forest"], "lighting": ["rim light"]})
    assert step.check_context.offered == ["sitting", "forest", "rim light", "slender", "bodysuit"]       # body, then the clothes, last
    assert step.check_context.outfit_offers == {"bodysuit"}
    r = floor_run([*[f"scene{chr(97 + n)}x" for n in range(30)], "bodysuit"], ["bodysuit"])
    assert "bodysuit" not in r.text and any("detail level" in c["why"] for c in r.changes)     # the scene slots reached the floor alone


def test_a_named_outfit_gets_no_clothes_from_the_floor_and_an_open_one_at_most_two():
    clothes = ["red bodysuit", "blue skirt", "white scarf", "green coat"]
    named = floor_run(["rain", "night", *clothes], clothes, must_keep=["red dress"], outfit="red dress")
    assert not set(clothes) & set(named.text.split(", ")) and "rain" in named.text            # the floor stays unmet rather than dress a second outfit
    assert len(named.text.split(", ")) < 25
    other = floor_run(["rain", "night", *clothes], clothes, must_keep=["forest"], outfit="red dress")      # the user named something else
    assert len(set(clothes) & set(other.text.split(", "))) == 2                                # an open outfit: at most two clothes
    assert len(set(clothes) & set(floor_run(["rain", "night", *clothes], clothes).text.split(", "))) == 2


def test_a_near_duplicate_of_a_tag_already_there_is_not_added_but_a_different_colour_is():
    r = run("masterpiece, best quality, 1girl, lace thighhighs, red dress, hair ribbon", detail_level="high",
            offered=(near := ["lace-trimmed thighhighs", "lace thighhighs", "long red dress", "black dress", "red hair ribbon", "hairband"]), slots=plan_of(near))
    assert {c["to"] for c in r.changes if "detail level" in c["why"]} == {"black dress", "hairband"}       # the other variants repeat what is there
