"""The scene plan: planned once per job, stored on the job, reused by a re-plan and a tweak, and checked."""
import json
from types import SimpleNamespace

from app.planning.scene import forced_detail, plan_scene, scene_text
from tests.content_probes import CSE_MESSAGE, CONTENT_TERMS as CONTENT, MODIFIERS as MOD, NUMBERS as NUM, SUBJECT_TERMS as SUBJ
from tests.test_planning_flow import app  # noqa: F401  (the app fixture)
from tests.test_tagstep import library, with_library


class FakeLlm:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def call(self, task, inputs, enums=None, limits=None, drop=None):
        self.calls.append((task, inputs))
        return dict(self.reply)


def sheet(**over) -> dict:
    base = {"subjects": "1 adult woman", "body": "slender", "outfit": "black kimono", "pose": "lying on bed",
            "action": "waiting", "expression": "calm smile", "camera": "cowboy shot", "setting": "hotel room",
            "lighting": "warm lamp light", "mood": "serene", "detail_level": "high", "rating": "nsfw", "must_keep": ["kimono"]}
    return {**base, **over}


def job(text="a woman in a kimono in a rainy hotel room at night", **over) -> dict:
    return {"description": text, "request": text, "constraints": [], "subjects": {"girls": 1, **over}}


def scene_ctx(llm, lib=None):
    """What plan_scene needs of a context: the LLM, and the tag library (none: no tags are found for the request's words) without search by meaning."""
    return SimpleNamespace(llm=llm, taglib=lambda: lib, embedder=None)


async def run_scene(reply, text=None, lib=None, **kw):
    llm = FakeLlm(reply)
    scene, issues = await plan_scene(scene_ctx(llm, lib), job(text or "a woman in a kimono"), **kw)
    return scene, issues, llm


async def test_the_request_sets_the_least_rating_and_complex_forces_very_high():
    scene, issues, llm = await run_scene(sheet(rating="general", detail_level="normal", must_keep=["kimono", "invented phrase"]),
                                   "make a very lewd image of a woman in a kimono, as complex as possible", library())
    assert issues == [] and scene["rating"] == "nsfw" and scene["detail_level"] == "very_high"
    assert scene["must_keep"] == ["kimono"]                         # invented words are dropped, the user's own word stays
    assert "adult" in scene["subjects"].lower()                        # a scene at a high content level always says adult
    inputs = llm.calls[0][1]
    assert inputs["min_rating"] == "nsfw" and "kimono" in inputs["concepts"]            # the tags found in the library for the request's words (no list of tags in the config)
    assert forced_detail("lots of detail") == "very_high" and forced_detail("a cat") == "normal"


async def test_a_long_must_keep_phrase_is_cut_to_its_concrete_words():
    scene, _, _ = await run_scene(sheet(must_keep=["an adult woman in a kimono", "multiple possible lots"]),
                                  "an adult woman in a kimono, multiple options")
    assert scene["must_keep"] == ["kimono"]


async def test_a_batch_child_plans_from_its_own_description_not_the_count():
    text = "6 unique cozy cabins in a snowy forest"
    llm = FakeLlm(sheet(subjects="1 cozy cabin", rating="general", must_keep=[]))
    scene, issues = await plan_scene(scene_ctx(llm), {"description": "A cozy cabin in a snowy forest at dawn", "request": text,
                                                                 "constraints": [], "batch": True})
    assert issues == [] and scene["subjects"] == "1 cozy cabin"
    told = llm.calls[0][1]
    assert told["request"] == "A cozy cabin in a snowy forest at dawn" and "6 unique" not in told["user_words"]


async def test_a_failed_scene_call_is_a_warning_and_planning_goes_on():
    scene, issues, _ = await run_scene({"needs_attention": True, "error": "boom"})
    assert scene is None and len(issues) == 1 and issues[0]["code"] == "llm_failed" and issues[0]["severity"] == "warning"


async def test_a_scene_the_content_rules_block_is_blocked():
    scene, issues, _ = await run_scene(sheet(subjects=f"1 {MOD[0]} girl", rating="general"))
    assert scene is None and issues[0]["code"] == "scene_cse" and issues[0]["severity"] == "blocking"
    assert issues[0]["message"] == CSE_MESSAGE


async def test_a_no_human_plan_drops_the_body_and_outfit_slots():
    scene, _, _ = await run_scene(sheet(subjects="no humans, scenery", body="no humans", outfit="no outfit", pose="no humans",
                                        action="no humans", expression="no humans", mood="no mood", rating="general", must_keep=[]),
                                  "a cabin in a snowy forest")
    assert scene["subjects"] == "no humans, scenery"
    assert all(scene[key] == "" for key in ("body", "outfit", "pose", "action", "expression", "mood"))
    assert scene["setting"]


def test_the_scene_is_stored_and_the_writer_and_tag_check_use_it(app):
    with_library(app)
    app.script_plan({"description": "a woman in black kimono in a rainy hotel room at night", "count": 1})
    app.llama.scripts["plan_scene"] = sheet()
    app.llama.scripts["write_prompts"] = {"prompts": [{"prompt": "masterpiece, best quality, 1girl, hotel room, night", "variation": "v"}],
                                          "negative_additions": [], "reason": "r"}
    job = app.job(app.plan(on="anima")["job_ids"][0])
    assert job["status"] == "ready" and job["meta"]["scene"]["rating"] == "nsfw"
    assert job["meta"]["scene"]["outfit"] == "black kimono"
    told = app.llama.of("write_prompts")[0]["messages"][1]["content"]
    assert "Scene plan" in told and "black kimono" in told and "kimono" in told
    prompt = job["fields"]["prompts"]["value"][0]
    assert "nsfw" in prompt.split(", ")                               # the rating tag is written into the lead group
    assert "rain" in prompt and "kimono" in prompt                   # the concrete request words survived


def test_a_failed_scene_call_leaves_a_ready_job_with_a_warning(app):
    app.script_plan({"description": "a woman in a park", "count": 1})
    app.llama.scripts["plan_scene"] = {"rating": "not-a-rating"}
    job = app.job(app.plan(on="anima")["job_ids"][0])
    assert job["status"] == "ready" and job["fields"]["prompts"]["value"]
    assert any(i["field"] == "job" and i["code"] == "llm_failed" for i in job["issues"])


def test_a_blocked_scene_stops_before_prompts_are_written(app):
    app.script_plan({"description": "a woman in a park", "count": 1})
    app.llama.scripts["plan_scene"] = sheet(subjects=f"1 {MOD[0]} girl", rating="nsfw")
    job = app.job(app.plan(on="anima")["job_ids"][0])
    assert job["status"] == "draft" and job["images"] == []
    assert any(i["code"] == "scene_cse" and i["severity"] == "blocking" for i in job["issues"])
    assert app.llama.of("write_prompts") == []                        # the unsafe plan is never handed to the writer


def test_every_job_of_a_big_batch_gets_its_own_scene(app):
    app.script_plan({"description": "4 unique ruins", "count": 4, "variety": "unique"})
    app.llama.scripts["write_briefs"] = lambda schema, body: {
        "briefs": [f"Ruin number{i} with moss{i} and fog{i} in a valley{i}" for i in range(schema["properties"]["briefs"]["minItems"])]}
    app.llama.scripts["plan_scene"] = sheet()
    plan = app.plan(on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 4
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert all(j["meta"]["scene"]["outfit"] == "black kimono" for j in jobs)
    assert len(app.llama.of("plan_scene")) == 4
    assert all("4 unique" not in b["messages"][1]["content"] for b in app.llama.of("plan_scene"))


def test_a_tweak_reuses_the_stored_scene_until_the_change_is_about_it(app):
    app.script_plan({"description": "a woman in black kimono in a hotel room", "count": 1})
    app.llama.scripts["plan_scene"] = [sheet(), sheet(outfit="red dress")]
    job_id = app.plan(on="anima")["job_ids"][0]
    before = app.job(job_id)["meta"]["scene"]
    app.llama.scripts["apply_tweak"] = {"steps": 20, "reason": "quicker"}
    plan = app.client.post(f"/api/jobs/{job_id}/tweak", json={"instruction": "fewer steps"}).json()
    assert app.wait(plan["id"])["status"] == "done"
    assert len(app.llama.of("plan_scene")) == 1                       # not about the scene: reused
    assert app.job(job_id)["meta"]["scene"] == before
    assert "black kimono" in app.llama.of("apply_tweak")[0]["messages"][1]["content"]

    app.llama.scripts["apply_tweak"] = {"prompts": ["masterpiece, best quality, red dress, hotel room"], "reason": "red dress"}
    plan = app.client.post(f"/api/jobs/{job_id}/tweak", json={"instruction": "make her wear a red dress"}).json()
    assert app.wait(plan["id"])["status"] == "done"
    assert len(app.llama.of("plan_scene")) == 2                       # about the outfit: planned again
    assert app.job(job_id)["meta"]["scene"]["outfit"] == "red dress"


def test_the_final_prompt_is_still_checked_after_the_scene_step(app):
    app.script_plan({"description": "a woman in a park", "count": 1})
    app.llama.scripts["plan_scene"] = sheet(rating="general", must_keep=[], subjects="1 adult woman")
    app.llama.scripts["write_prompts"] = {"prompts": [{"prompt": f"masterpiece, best quality, {CONTENT[0]} {SUBJ[0]}, park", "variation": "v"}],
                                          "negative_additions": [], "reason": "r"}
    job = app.job(app.plan(on="anima")["job_ids"][0])
    assert job["status"] == "draft" and any(i["code"] == "cse" for i in job["issues"])


def test_the_scene_text_holds_every_slot():
    text = scene_text(sheet())
    assert "black kimono" in text and "hotel room" in text and "kimono" in text
    assert json.dumps(sheet())                                                # it is plain JSON for meta


async def test_a_rating_read_from_the_whole_request_is_the_minimum_as_it_stands():
    """"nothing explicit" holds the word "explicit": the keyword floor would raise it to explicit, the planner's reading of the sentence says sensitive."""
    text = "a glamorous adult woman portrait, nothing explicit"
    words, _, llm = await run_scene(sheet(rating="general", must_keep=[]), text)
    assert llm.calls[0][1]["min_rating"] == "explicit" and words["rating"] == "explicit"       # no intent: the words decide, as before
    llm = FakeLlm(sheet(rating="general", must_keep=[]))
    scene, _ = await plan_scene(scene_ctx(llm), {**job(text), "rating_read": True}, "sensitive")
    assert llm.calls[0][1]["min_rating"] == "sensitive" and scene["rating"] == "sensitive"
