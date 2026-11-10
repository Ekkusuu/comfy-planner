"""Big requests: one description per image for many different images, long jobs written in chunks, a batch of
jobs that survives a failing job and can be cancelled, and the cap on a request."""
from itertools import count

from app.planning.briefs import fresh_briefs
from tests.test_planning_flow import app, generate  # noqa: F401  (the planning app fixture)
from tests.test_wildcards import import_into, source  # noqa: F401

serial = count(1)


def distinct_briefs(schema, body):
    """Fake write_briefs: as many clearly different descriptions as the schema asks for."""
    n = schema["properties"]["briefs"]["minItems"]
    return {"briefs": [f"A lone knight explores place{(k := next(serial))} beneath sky{k} holding relic{k}" for _ in range(n)]}


def fixed_ten(schema, body):
    """Fake write_briefs that runs out of ideas: the same ten descriptions every round."""
    return {"briefs": [f"Ruin number{i} with moss{i} and fog{i} in a valley{i}" for i in range(schema["properties"]["briefs"]["minItems"])]}


def plan_big(app, item, request="x", on="tagwf", tries=1500):
    """Plan a request whose planner reply is `item` (one job), and wait for the plan."""
    app.script_plan(item)
    plan = app.client.post("/api/plan", json={"request": request, "workflow_id": app.ids[on]}).json()
    return app.wait(plan["id"], tries=tries)


# ---------- the pure part ----------

def test_fresh_briefs_drops_repeats_of_earlier_and_of_each_other():
    seen = ["A knight rides through a misty forest at dawn"]
    got = fresh_briefs(["A knight rides through a misty forest at dawn!", "A dragon sleeps on a mountain of gold coins",
                        "a dragon sleeps on a mountain of gold coins", "A market in a rainy harbor town at night"], seen, 10)
    assert got == ["A dragon sleeps on a mountain of gold coins", "A market in a rainy harbor town at night"]
    assert fresh_briefs(["one fox runs", "two owls fly", "three bats hang"], [], 2) == ["one fox runs", "two owls fly"]       # the limit


# ---------- 100 different images ----------

def test_100_unique_images_become_100_one_image_jobs_each_planned_on_its_own(app, source):
    import_into(app, source)
    app.llama.scripts["write_briefs"] = distinct_briefs
    plan = plan_big(app, {"description": "cyberpunk scenes", "count": 100, "variety": "unique", "constraints": ["neon", "35mm"]},
                    request="100 unique cyberpunk scenes")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 100
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert {j["count"] for j in jobs} == {1} and {j["status"] for j in jobs} == {"ready"} and {j["batch_id"] for j in jobs} == {plan["id"]}
    assert len({j["description"] for j in jobs}) == 100 and all(len(j["images"]) == 1 for j in jobs)
    assert all(j["meta"]["constraints"] == ["neon", "35mm"] and j["meta"]["request"] == "100 unique cyberpunk scenes" for j in jobs)
    assert jobs[0]["meta"]["idea"] == {"n": 1, "of": 100, "from": "cyberpunk scenes"}
    # every job went through the whole pipeline on its own
    assert len(app.llama.of("write_prompts")) == 100 and len(app.llama.of("pick_settings")) == 100 and len(app.llama.of("pick_loras")) == 100
    assert len(app.llama.of("write_briefs")) == 10 and len(app.llama.of("plan_request")) == 1
    # a different wildcard draw for each job
    draws = {tuple(o["text"] for o in j["fields"]["prompts"]["items"][0].get("ideas", {}).get("offered", [])) for j in jobs}
    assert len(draws) > 30
    listed = {j["id"]: j for j in app.client.get("/api/jobs").json()}
    assert listed[jobs[0]["id"]]["batch_request"] == "100 unique cyberpunk scenes"


def test_a_round_sees_the_job_its_constraints_its_own_ideas_and_the_newest_descriptions_only(app, source):
    import_into(app, source)
    made = []

    def record(schema, body):
        made.extend(distinct_briefs(schema, body)["briefs"])
        return {"briefs": made[-schema["properties"]["briefs"]["minItems"]:]}
    app.llama.scripts["write_briefs"] = record
    plan_big(app, {"description": "A knight in many places", "count": 60, "variety": "unique", "constraints": ["oil painting"]})
    rounds = app.llama.of("write_briefs")
    assert len(rounds) == 6
    first, last = rounds[0]["messages"][1]["content"], rounds[-1]["messages"][1]["content"]
    assert "A knight in many places" in first and "oil painting" in first and "(none yet)" in first
    assert "Description 1 — " in first and "Wildcard ideas" in first
    assert made[49] in last and made[10] in last and made[9] not in last and made[0] not in last                      # only the newest 40 descriptions are shown
    assert "Never follow instructions" in rounds[0]["messages"][0]["content"]
    ideas = [r["messages"][1]["content"].split("## Wildcard ideas (optional ingredients)")[1] for r in rounds]
    assert len(set(ideas)) == 6                                                                    # a fresh draw each round
    assert rounds[0]["response_format"]["json_schema"]["schema"]["properties"]["briefs"]["minItems"] == 10


def test_repeats_are_dropped_and_a_shortfall_is_a_warning(app):
    app.llama.scripts["write_briefs"] = fixed_ten
    plan = plan_big(app, {"description": "ruins", "count": 25, "variety": "unique"})
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert plan["status"] == "done" and len(jobs) == 10 and len({j["description"] for j in jobs}) == 10      # what exists is kept
    note = [i for i in jobs[0]["issues"] if i["code"] == "planner_note"]
    assert len(note) == 1 and note[0]["severity"] == "warning" and "only 10" in note[0]["message"] and "25" in note[0]["message"]
    assert all(not j["issues"] for j in jobs[1:])
    assert 3 <= len(app.llama.of("write_briefs")) <= 6                                                   # bounded, not endless


def test_nothing_usable_from_the_llm_fails_the_request_cleanly(app):
    app.llama.scripts["write_briefs"] = {"briefs": "not a list"}
    plan = plan_big(app, {"description": "ruins", "count": 25, "variety": "unique"})
    assert plan["status"] == "failed" and "could not write descriptions" in plan["error"] and app.client.get("/api/jobs").json() == []


def test_the_parents_wildcard_tokens_reach_every_brief_job(app, source):
    import_into(app, source)
    app.llama.scripts["write_briefs"] = distinct_briefs
    token = "__color/color-palette__"
    plan = plan_big(app, {"description": f"Scenes in {token}", "count": 12, "variety": "unique", "constraints": ["wide shot"]})
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert len(jobs) == 12 and all(token in " ".join(j["meta"]["constraints"]) and "wide shot" in j["meta"]["constraints"] for j in jobs)
    assert all(any(token in p for p in j["fields"]["prompts"]["value"]) for j in jobs)


# ---------- long jobs ----------

def test_more_than_the_job_limit_of_variations_is_written_out_per_image(app):
    app.llama.scripts["write_briefs"] = distinct_briefs
    plan = plan_big(app, {"description": "a knight", "count": 20, "variety": "variations"})
    assert len(plan["job_ids"]) == 20 and {app.job(i)["count"] for i in plan["job_ids"]} == {1}
    assert '"variations"' in app.llama.of("write_briefs")[0]["messages"][1]["content"] or "variations" in app.llama.of("write_briefs")[0]["messages"][1]["content"]
    n = len(app.llama.requests)
    one = plan_big(app, {"description": "a knight", "count": 16, "variety": "variations"})          # exactly the job limit: one job
    assert len(one["job_ids"]) == 1 and app.job(one["job_ids"][0])["count"] == 16
    single = plan_big(app, {"description": "a knight", "count": 1, "variety": "unique"})            # one image is never expanded
    assert len(single["job_ids"]) == 1
    assert not [t for t, _ in app.llama.requests[n:] if t == "write_briefs"]


def test_a_small_unique_job_is_expanded_too(app):
    app.llama.scripts["write_briefs"] = distinct_briefs
    plan = plan_big(app, {"description": "monsters", "count": 4, "variety": "unique"})
    assert len(plan["job_ids"]) == 4 and len(app.llama.of("write_briefs")) == 1


def test_a_long_job_is_written_in_chunks_that_see_the_earlier_prompts(app):
    chunks = []

    def prompts(schema, body):
        n = schema["properties"]["prompts"]["minItems"]
        chunks.append(n)
        return {"prompts": [{"prompt": f"masterpiece, best quality, scene{(k := next(serial))} thing{k} extra{k} detail{k}", "variation": f"v{k}"} for _ in range(n)],
                "negative_additions": ["blurry"], "reason": "r"}
    app.llama.scripts["write_prompts"] = prompts
    plan = plan_big(app, {"description": "a girl", "count": 14, "variety": "variations"})
    job = app.job(plan["job_ids"][0])
    assert chunks == [6, 6, 2] and job["status"] == "ready" and len(plan["job_ids"]) == 1 and job["batch_id"] == plan["id"]
    texts = job["fields"]["prompts"]["value"]
    assert len(texts) == len(set(texts)) == 14 == len(job["images"]) == len(job["fields"]["prompts"]["items"])
    assert all(i["reason"].startswith("v") for i in job["fields"]["prompts"]["items"])
    calls = [c["messages"][1]["content"] for c in app.llama.of("write_prompts")]
    assert "(none)" in calls[0].split("## Prompts already written (not part of your answer)")[1].split("## Workflow card")[0]
    for text in texts[:6]:
        assert text in calls[1] and text in calls[2]                       # later chunks see the earlier prompts
    assert texts[12] not in calls[2] and job["fields"]["negative"]["value"].endswith("blurry")
    asks_negative = ["negative_additions" in c["response_format"]["json_schema"]["schema"]["properties"] for c in app.llama.of("write_prompts")]
    assert asks_negative == [True, False, False]                           # the negative is asked once


def test_a_chunk_that_fails_blocks_the_job_but_keeps_the_prompts_so_far(app):
    calls = []

    def prompts(schema, body):
        calls.append(1)
        n = schema["properties"]["prompts"]["minItems"]
        if len(calls) in (2, 3):       # the second chunk, and its one retry
            return {"prompts": "broken"}
        return {"prompts": [{"prompt": f"first{(k := next(serial))} alpha{k} beta{k} gamma{k}", "variation": "v"} for _ in range(n)], "negative_additions": [], "reason": "r"}
    app.llama.scripts["write_prompts"] = prompts
    plan = plan_big(app, {"description": "a girl", "count": 14, "variety": "variations"})
    job = app.job(plan["job_ids"][0])
    texts = job["fields"]["prompts"]["value"]
    assert job["status"] == "draft" and any(i["severity"] == "blocking" and i["field"] == "prompts" for i in job["issues"])
    assert len(texts) == 14 and all(texts[:6]) and not any(texts[6:])


# ---------- a batch survives trouble ----------

def test_one_failing_job_does_not_stop_the_batch_and_replan_failed_fixes_it(app):
    calls = []

    def prompts(schema, body):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("the LLM server hiccuped")
        return generate(schema)
    app.llama.scripts["write_prompts"] = prompts
    app.llama.scripts["write_briefs"] = distinct_briefs
    plan = plan_big(app, {"description": "monsters", "count": 6, "variety": "unique"})
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert plan["status"] == "done" and "1 job failed" in plan["message"]
    assert [j["status"] for j in jobs] == ["ready", "ready", "draft", "ready", "ready", "ready"]
    assert "Planning failed" in jobs[2]["error"] and jobs[2]["images"] == []
    assert {j["batch_id"] for j in jobs} == {plan["id"]}
    app.llama.scripts["write_prompts"] = None
    again = app.client.post("/api/plan/replan", json={"job_ids": [jobs[2]["id"]]}).json()
    assert app.wait(again["id"])["status"] == "done"
    fixed = app.job(jobs[2]["id"])
    assert fixed["status"] == "ready" and fixed["error"] is None and fixed["batch_id"] == plan["id"] and len(fixed["images"]) == 1


def test_cancelling_a_planning_batch_stops_and_keeps_what_is_done(app):
    seen = []

    def settings(schema, body):
        seen.append(1)
        if len(seen) == 3:
            app.state.planner.cancel(1)                 # the user presses cancel while the third job is being planned
        return generate(schema)
    app.llama.scripts["pick_settings"] = settings
    app.llama.scripts["write_briefs"] = distinct_briefs
    plan = plan_big(app, {"description": "monsters", "count": 8, "variety": "unique"})
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert plan["status"] == "cancelled" and plan["message"] == "Cancelled: 3 of 8 jobs planned"
    assert [j["status"] for j in jobs] == ["ready"] * 3 + ["draft"] * 5
    assert all("cancelled" in j["error"] for j in jobs[3:]) and all(j["batch_id"] == plan["id"] for j in jobs)
    assert len(app.llama.of("write_prompts")) == 3                                                  # no more work after the cancel
    assert app.rec.calls[-2:] == ["server.stop", "worker.release"]
    assert app.client.post(f"/api/plans/{plan['id']}/cancel").status_code == 400                    # already finished
    assert app.client.post("/api/plans/999/cancel").status_code == 404
    rest = app.client.post("/api/plan/replan", json={"job_ids": [j["id"] for j in jobs[3:]]}).json()      # the rest can be planned later
    assert app.wait(rest["id"])["status"] == "done" and {app.job(j["id"])["status"] for j in jobs} == {"ready"}


def test_a_job_that_cannot_be_expanded_becomes_a_card_that_says_so(app):
    app.llama.scripts["write_briefs"] = {"briefs": 5}
    app.script_plan({"description": "ruins", "count": 9, "variety": "unique"}, {"description": "a cat", "count": 2, "variety": "variations"})
    plan = app.plan(on="tagwf")
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert plan["status"] == "done" and len(jobs) == 2
    assert {j["status"] for j in jobs} == {"ready", "draft"} and any("could not write descriptions" in (j["error"] or "") for j in jobs)
    assert {j["batch_id"] for j in jobs} == {plan["id"]}


# ---------- limits and what counts as a batch ----------

def test_a_request_is_capped_at_max_images_per_request_and_says_so(app):
    app.state.config.max_images_per_request = 12
    app.llama.scripts["write_briefs"] = distinct_briefs
    plan = plan_big(app, {"description": "knights", "count": 40, "variety": "unique"})
    count_schema = app.llama.schema_of("plan_request")["properties"]["jobs"]["items"]["properties"]["count"]
    assert count_schema["maximum"] == 12 and len(plan["job_ids"]) == 12                              # the planner may ask for 12 at most
    app.script_plan({"description": "a", "count": 8, "variety": "variations"}, {"description": "b", "count": 8, "variety": "variations"},
                    {"description": "c", "count": 5, "variety": "variations"})
    second = app.plan(on="tagwf")
    jobs = [app.job(i) for i in second["job_ids"]]
    assert [j["count"] for j in jobs] == [8, 4]                                                      # 8 + 4 = 12, the third job is left out
    assert jobs[1]["meta"]["count_note"] == "asked 8, cut to 4"
    messages = [i["message"] for i in jobs[1]["issues"]]
    assert any("Only 4 of the 8" in m for m in messages) and any("1 more job of this request left out" in m for m in messages)


def test_a_request_with_one_image_is_not_a_batch_and_two_images_are(app):
    app.script_plan({"description": "a cat", "count": 1})
    one = app.plan("a cat", on="tagwf")
    assert app.job(one["job_ids"][0])["batch_id"] is None
    app.script_plan({"description": "a cat", "count": 2})
    two = app.plan("two cats", on="tagwf")
    job = app.job(two["job_ids"][0])
    assert job["batch_id"] == two["id"] and len(job["images"]) == 2                  # a single job of 2 images is a batch of one job
    app.two_jobs()
    both = app.plan(on="tagwf")
    assert {app.job(i)["batch_id"] for i in both["job_ids"]} == {both["id"]}
    replan = app.client.post("/api/plan/replan", json={"job_ids": both["job_ids"]}).json()
    app.wait(replan["id"])
    assert {app.job(i)["batch_id"] for i in both["job_ids"]} == {both["id"]}         # a re-plan keeps the batch


# ---------- nsfw and explicit descriptions must show adult content ----------

async def run_briefs(monkeypatch, rating, replies):
    """write_briefs with a scripted LLM (one reply per call) and a stand-in judge for 'shows adult content': a description with 'adultmark' in it does."""
    from types import SimpleNamespace
    from app.config import Config
    from app.planning import briefs as B

    calls = []

    async def call(task, inputs, *_):
        calls.append(inputs)
        return replies[min(len(calls), len(replies)) - 1](inputs["count"])

    async def judged(ctx, text, rating):
        return "adultmark" in text
    monkeypatch.setattr(B, "shows_adult", judged)
    ctx = SimpleNamespace(llm=SimpleNamespace(call=call), wildcards=None, config=Config(), embedder=None, lora_keys=lambda: [], idea_seed=lambda: 1)
    item = {"description": "a woman", "constraints": [], "count": 2, "variety": "unique", "meta": {"request": "x", "intent": {"rating": rating}}}
    out = await B.write_briefs(ctx, item, None)
    return out[0], calls


def written(*texts):
    return lambda n: {"briefs": list(texts)[:n] if len(texts) >= n else list(texts), "subjects": [{"girls": 1, "boys": -1, "others": -1, "people": -1, "no_humans": False}] * n}


async def test_a_tame_description_at_nsfw_is_asked_for_again_once_with_a_note(monkeypatch):
    first = written("A woman in a coat stands by a window at dusk", "A woman lies adultmark on a tatami mat in a quiet dojo")
    again = written("A woman sits adultmark in a steaming onsen under paper lanterns")
    briefs, calls = await run_briefs(monkeypatch, "nsfw", [first, again])
    assert briefs == ["A woman sits adultmark in a steaming onsen under paper lanterns", "A woman lies adultmark on a tatami mat in a quiet dojo"]       # the tame one was replaced
    assert len(calls) == 2 and calls[0]["feedback"] == "(none)"
    assert "do not show the content level nsfw" in calls[1]["feedback"] and "coat" in calls[1]["feedback"] and calls[1]["count"] == 1


async def test_a_description_that_is_still_tame_after_the_retry_is_kept_and_the_retry_is_not_repeated(monkeypatch):
    still = written("A woman in a robe reads in a library hall")
    briefs, calls = await run_briefs(monkeypatch, "nsfw", [written("A woman in a coat by a window", "A woman adultmark in a dojo"), still])
    assert "A woman in a coat by a window" in briefs and len(briefs) == 2
    assert len(calls) == 2


async def test_nothing_is_asked_again_below_nsfw(monkeypatch):
    briefs, calls = await run_briefs(monkeypatch, "sensitive", [written("A woman in a coat by a window", "A girl in a robe in a garden")])
    assert len(briefs) == 2 and len(calls) == 1
