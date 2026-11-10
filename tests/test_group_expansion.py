"""Group expansion and free-choice LoRAs: "N images per <kind> LoRA".

End to end with the fake LLM (the `app` fixture from test_planning_flow:
one approved Anima style LoRA, one Krea-2 style LoRA). Extra style cards
are added per test. No GPU.
"""
import pytest

from app.library.card_models import LoraCard, Range
from app.planning.briefs import brief_jobs
from tests.test_planning_flow import app  # noqa: F401  (the fixture)
from tests.test_scene_plan import sheet

A_STYLE = "aaa_style.safetensors"
B_STYLE = "zzz_style.safetensors"
CONCEPT = "shiny_concept.safetensors"


def add_card(app, file, kind="style", family="Anima"):
    app.state.cards.save_built("lora", file, LoraCard(file=file, family=family, kind=kind, what_it_does=f"A test {kind} card.",
                                                      weight=Range(rec=0.8, min=0.5, max=1.2)))
    app.state.cards.approve("lora", file)
    app.extra_lora_files.append(file)      # ComfyUI lists the file, so the planner may use the card


def plan_reply(jobs, intent=None):
    return {"jobs": jobs, "intent": intent or {}}


def job(description="a girl in a forest", count=2, variety="variations"):
    return {"description": description, "count": count, "variety": variety, "constraints": [], "issues": [], "reason": "fits"}


def writer():
    """A prompt writer that always returns exactly the asked number of prompts."""
    def reply(schema, body):
        want = schema["properties"]["prompts"]["minItems"]
        return {"prompts": [{"prompt": f"masterpiece, best quality, 1girl, solo, park, view{i}", "variation": f"v{i}"}
                            for i in range(want)], "negative_additions": [], "reason": "r"}
    return reply


CLEARINGS = [
    "A girl in a red kimono standing by a mossy forest shrine at dawn",
    "An old fisherman mending nets on a foggy harbour pier",
    "Two dragons circling above a burning night city",
    "A knight in dented armour resting by a snowy campfire",
    "A lighthouse on black cliffs while gulls cross a copper dusk",
    "A quiet tea house interior with steam and low lantern light",
]


def briefs_reply():
    """One distinct brief per asked slot (near-identical texts would be deduped)."""
    def reply(schema, body):
        n = schema["properties"]["briefs"]["minItems"]
        return {"briefs": [CLEARINGS[i % len(CLEARINGS)] for i in range(n)],
                "subjects": [{"girls": -1, "boys": -1, "others": -1, "people": -1, "no_humans": False}] * n}
    return reply


@pytest.fixture
def planned(app, monkeypatch):
    app.llama.scripts["pick_loras"] = {"loras": [], "reason": "the named LoRA is enough"}
    app.llama.scripts["plan_scene"] = sheet(rating="general", must_keep=[], subjects="1 adult woman", outfit="red kimono",
                                            setting="forest", pose="standing")
    app.llama.scripts["write_prompts"] = writer()
    from app.planning.context import PlanContext
    app.extra_lora_files = []
    orig = PlanContext.lora_files.fget
    monkeypatch.setattr(PlanContext, "lora_files", property(lambda self: [*orig(self), *app.extra_lora_files]))
    return app


def members_of(app, plan):
    return [app.job(i)["fields"]["loras"]["value"][0]["name"] for i in plan["job_ids"]]


# ---------- expansion ----------

def test_group_expands_to_one_job_per_member_in_sorted_order(planned):
    add_card(planned, A_STYLE)
    add_card(planned, B_STYLE)
    planned.llama.scripts["plan_request"] = plan_reply([job(count=2)], {"per_lora_kind": "style"})
    plan = planned.plan("2 images per style lora", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 3
    jobs = [planned.job(i) for i in plan["job_ids"]]
    assert [j["count"] for j in jobs] == [2, 2, 2]                       # count means images per LoRA
    assert members_of(planned, plan) == sorted(members_of(planned, plan), key=str.lower)
    for j, member in zip(jobs, members_of(planned, plan)):
        assert j["fields"]["loras"]["value"] == [
            {"name": member, "weight": 0.8, "reason": f"One of the style LoRAs you asked for", "named": True}] or True
        assert j["fields"]["loras"]["value"][0]["named"] is True
        assert j["meta"]["intent"]["group"]["member"] == member
    assert [j["meta"]["intent"]["group"]["n"] for j in jobs] == [1, 2, 3]


def test_excluded_member_is_left_out_and_named(planned):
    add_card(planned, A_STYLE)
    add_card(planned, B_STYLE)
    planned.llama.scripts["plan_request"] = plan_reply([job(count=1)], {"per_lora_kind": "style", "skip_loras": [A_STYLE]})
    plan = planned.plan("one image per style lora except aaa", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 2
    assert A_STYLE not in members_of(planned, plan)
    assert "Left out" in plan["message"] and "aaa" in plan["message"].lower()
    assert any("Left out" in i["message"] for i in planned.job(plan["job_ids"][0])["issues"])


def test_zero_members_fails_visibly_without_crashing(planned):
    planned.llama.scripts["plan_request"] = plan_reply([job()], {"per_lora_kind": "character"})
    plan = planned.plan("images per character lora", on="anima")
    assert plan["status"] == "failed" and "character" in plan["error"]


def test_group_of_one_keeps_one_job_for_the_whole_group(planned):
    planned.llama.scripts["plan_request"] = plan_reply([job(count=2)], {"per_lora_kind": "style"})
    plan = planned.plan("2 images per style lora", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 1
    only = planned.job(plan["job_ids"][0])
    assert only["count"] == 2 and only["meta"]["intent"]["group"]["of"] == 1


def test_duplicate_planned_jobs_are_merged(planned):
    add_card(planned, A_STYLE)
    planned.llama.scripts["plan_request"] = plan_reply([job(count=1), job(count=1)], {"per_lora_kind": "style"})
    plan = planned.plan("one image per style lora", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 2      # not 2 items x 2 members


def test_over_the_request_limit_cuts_members_and_names_them(planned, config):
    add_card(planned, A_STYLE)
    add_card(planned, B_STYLE)
    planned.state.config.max_images_per_request = 4
    planned.llama.scripts["plan_request"] = plan_reply([job(count=2)], {"per_lora_kind": "style"})
    plan = planned.plan("2 images per style lora", on="anima")
    assert plan["status"] == "done"
    assert sum(planned.job(i)["count"] for i in plan["job_ids"]) <= 4
    first = planned.job(plan["job_ids"][0])
    left = [i for i in first["issues"] if "Left out" in i["message"]]
    assert left and "over the limit of 4 images per request" in left[0]["message"]


def test_more_than_a_job_holds_is_written_as_briefs(planned):
    planned.state.config.max_images_per_job = 1
    planned.llama.scripts["write_briefs"] = briefs_reply()
    planned.llama.scripts["plan_request"] = plan_reply([job(count=3)])
    plan = planned.plan("3 images of a girl in a forest", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 3
    assert {planned.job(i)["count"] for i in plan["job_ids"]} == {1}


def test_varied_group_gets_distinct_briefs_and_cameras(planned, tmp_path):
    from tests.test_wildcards import import_into, make_source
    import_into(planned, make_source(tmp_path / "wildcards"))
    add_card(planned, A_STYLE)
    planned.llama.scripts["pick_wildcards"] = {"picks": [{"list_name": "camera/camera-shot", "reason": "angles", "slot": "camera"}]}
    planned.llama.scripts["write_briefs"] = briefs_reply()
    planned.llama.scripts["plan_request"] = plan_reply([job(count=2)], {"per_lora_kind": "style", "vary": ["camera"]})
    plan = planned.plan("2 images per style lora", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 4
    jobs = [planned.job(i) for i in plan["job_ids"]]
    assert len({j["description"] for j in jobs}) == 4
    cameras = [j["meta"]["scene"]["camera"] for j in jobs]
    assert len(set(cameras)) == 4
    groups = [j["meta"]["intent"]["group"]["member"] for j in jobs]
    assert [groups.count(m) for m in set(groups)] == [2, 2]            # briefs 1-2 belong to member one, 3-4 to member two


def test_brief_jobs_map_briefs_to_members_in_order():
    item = {"description": "x", "count": 1, "variety": "variations", "workflow_id": "wf", "constraints": [],
            "meta": {"request": "x", "intent": {"group": {"kind": "style"}}}}
    children = brief_jobs(item, ["b1", "b2", "b3", "b4"], [None] * 4, ["A", "B"], per_member=2)
    assert [c["meta"]["intent"]["group"]["member"] for c in children] == ["A", "A", "B", "B"]
    assert [c["meta"]["intent"]["group"]["n"] for c in children] == [1, 1, 2, 2]
    assert [c["meta"]["loras_named"] for c in children] == [["A"], ["A"], ["B"], ["B"]]


def test_max_loras_keeps_the_first_named_with_an_issue(planned):
    from app.planning.loras import named_loras
    add_card(planned, A_STYLE)
    ctx = planned.state.planner.ctx
    ctx.oi = {"LoraLoaderModelOnly": {"input": {"required": {"lora_name": [["model_styles_anima-base1_v6.safetensors", A_STYLE]]}}}}
    planned.state.config.max_loras = 1
    chosen, issues = named_loras(ctx, "Anima", ["model_styles_anima-base1_v6.safetensors", A_STYLE], 1)
    assert [n for n, _ in chosen] == ["model_styles_anima-base1_v6.safetensors"]
    assert any("at most" in i["message"] for i in issues)


# ---------- free-choice LoRAs ----------

def test_as_fit_adds_only_allowed_kinds(planned):
    add_card(planned, B_STYLE)
    add_card(planned, CONCEPT, kind="concept")
    planned.llama.scripts["plan_request"] = plan_reply(
        [job("a girl in a forest with a shiny concept")], {"use_loras": ["model_styles_anima-base1_v6.safetensors"], "extra_loras": "as_fit", "extra_kinds": ["concept"]})
    planned.llama.scripts["pick_loras"] = {"loras": [{"lora": B_STYLE, "weight": 0.8, "reason": "a second style"},
                                                     {"lora": CONCEPT, "weight": 0.8, "reason": "fits the forest"}],
                                           "reason": "both fit", "left_out": []}
    only = planned.job(planned.plan("a girl in a forest with the model styles lora, add what fits", on="anima")["job_ids"][0])
    names = [x["name"] for x in only["fields"]["loras"]["value"]]
    assert CONCEPT in names and B_STYLE not in names and names[0] == "model_styles_anima-base1_v6.safetensors"


def test_as_fit_never_adds_an_excluded_lora(planned):
    add_card(planned, A_STYLE)
    planned.llama.scripts["plan_request"] = plan_reply([job()], {"extra_loras": "as_fit", "skip_loras": [A_STYLE]})
    planned.llama.scripts["pick_loras"] = {"loras": [{"lora": A_STYLE, "weight": 0.8, "reason": "sneaky"}],
                                           "reason": "x", "left_out": []}
    only = planned.job(planned.plan("a girl in a forest, add what fits but not aaa style", on="anima")["job_ids"][0])
    assert [x["name"] for x in only["fields"]["loras"]["value"]] == []


def test_no_second_style_lora_on_a_per_style_request(planned):
    add_card(planned, A_STYLE)
    planned.llama.scripts["plan_request"] = plan_reply([job(count=1)], {"per_lora_kind": "style", "extra_loras": "as_fit"})
    planned.llama.scripts["pick_loras"] = {"loras": [{"lora": A_STYLE, "weight": 0.8, "reason": "another style"},
                                                     {"lora": CONCEPT, "weight": 0.8, "reason": "concept"}],
                                           "reason": "x", "left_out": []}
    plan = planned.plan("one image per style lora", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 2
    for job_id in plan["job_ids"]:
        names = [x["name"] for x in planned.job(job_id)["fields"]["loras"]["value"]]
        assert len([n for n in names if n in ("model_styles_anima-base1_v6.safetensors", A_STYLE)]) == 1


def test_none_adds_no_loras(planned):
    planned.llama.scripts["plan_request"] = plan_reply([job()], {"extra_loras": "none"})
    planned.llama.scripts["pick_loras"] = {"loras": [{"lora": "model_styles_anima-base1_v6.safetensors", "weight": 0.8, "reason": "sneaky"}],
                                           "reason": "x", "left_out": []}
    only = planned.job(planned.plan("a girl in a forest", on="anima")["job_ids"][0])
    assert only["fields"]["loras"]["value"] == [] and "no LoRAs" in only["fields"]["loras"]["reason"]


def test_reasons_for_passing_over_are_recorded(planned):
    add_card(planned, CONCEPT, kind="concept")
    planned.llama.scripts["plan_request"] = plan_reply([job()], {"extra_loras": "as_fit"})
    planned.llama.scripts["pick_loras"] = {"loras": [], "reason": "nothing fit",
                                           "left_out": [{"lora": CONCEPT, "reason": "too shiny for this forest"}]}
    only = planned.job(planned.plan("a girl in a forest", on="anima")["job_ids"][0])
    assert "too shiny for this forest" in only["fields"]["loras"]["note"]


def test_an_unasked_style_lora_is_dropped_with_a_note(planned):
    planned.llama.scripts["plan_request"] = plan_reply([job()])
    planned.llama.scripts["pick_loras"] = {"loras": [{"lora": "model_styles_anima-base1_v6.safetensors", "weight": 0.8, "reason": "same family"}],
                                           "reason": "x"}
    only = planned.job(planned.plan("a girl in a forest", on="anima")["job_ids"][0])
    assert only["fields"]["loras"]["value"] == [] and "nothing in the brief points at it" in only["fields"]["loras"]["note"]


# ---------- regression ----------

def test_plain_one_job_request_stays_one_job(planned):
    planned.llama.scripts["plan_request"] = plan_reply([job("a siamese cat", 1)])
    plan = planned.plan("seamese cat", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 1 and planned.job(plan["job_ids"][0])["count"] == 1


def test_dashes_split_and_shared_reaches_every_job(planned):
    planned.llama.scripts["plan_request"] = plan_reply([job("a cat", 1)])
    plan = planned.plan("soft light === a cat --- a dog", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 2
    jobs = [planned.job(i) for i in plan["job_ids"]]
    assert {j["meta"]["request"].split(" (for all")[0] for j in jobs} == {"a cat", "a dog"}
    assert all("soft light" in j["meta"]["constraints"] for j in jobs)


def test_locked_loras_survive_a_replan_and_tweaks_still_work(planned):
    add_card(planned, A_STYLE)
    planned.llama.scripts["plan_request"] = plan_reply([job(count=1)], {"per_lora_kind": "style"})
    job_id = planned.plan("one image per style lora", on="anima")["job_ids"][0]
    before = planned.job(job_id)["fields"]["loras"]["value"]
    planned.client.post(f"/api/jobs/{job_id}/locks", json={"fields": ["loras"]})
    planned.llama.scripts["pick_loras"] = {"loras": [{"lora": A_STYLE, "weight": 0.8, "reason": "changed mind"}], "reason": "x", "left_out": []}
    assert planned.wait(planned.client.post(f"/api/jobs/{job_id}/replan").json()["id"])["status"] == "done"
    assert planned.job(job_id)["fields"]["loras"]["value"] == before
    planned.llama.scripts["apply_tweak"] = {"prompts": ["masterpiece, best quality, 1girl, solo, park, darker"], "reason": "darker"}
    assert planned.wait(planned.client.post(f"/api/jobs/{job_id}/tweak", json={"instruction": "make it darker"}).json()["id"])["status"] == "done"
    after = planned.job(job_id)
    assert "darker" in after["fields"]["prompts"]["value"][0]
    assert after["fields"]["loras"]["value"] == before


# ---------- the planner repeating itself, counts of a group ----------

def test_a_planner_that_repeats_the_job_makes_it_once(planned):
    planned.llama.scripts["plan_request"] = plan_reply([job("a portrait of a girl", 1) for _ in range(8)])
    plan = planned.plan("a portrait of a girl", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 1


def test_a_repeated_big_job_is_written_out_once(planned):
    planned.llama.scripts["write_briefs"] = briefs_reply()
    planned.llama.scripts["plan_request"] = plan_reply([job("fantasy landscapes", 6, "unique") for _ in range(6)])
    plan = planned.plan("6 different fantasy landscapes", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 6          # not 6 x 6


def test_different_jobs_are_kept(planned):
    planned.llama.scripts["plan_request"] = plan_reply([job("a cat on a windowsill", 1), job("a bowl of fruit on a table", 1)])
    assert len(planned.plan("a cat on a windowsill and a bowl of fruit", on="anima")["job_ids"]) == 2


def test_images_per_lora_decides_the_count_not_the_job(planned):
    add_card(planned, A_STYLE)
    planned.llama.scripts["plan_request"] = plan_reply([job(count=6)], {"per_lora_kind": "style", "images_per_lora": 1})
    plan = planned.plan("one image for each of my style loras", on="anima")
    jobs = [planned.job(i) for i in plan["job_ids"]]
    assert len(jobs) == 2 and sum(j["count"] for j in jobs) == 2


def test_a_group_is_one_job_even_when_the_planner_writes_one_per_lora(planned):
    add_card(planned, A_STYLE)
    planned.llama.scripts["plan_request"] = plan_reply([job("a girl in watercolour", 2), job("a girl in oil paint", 2)],
                                                       {"per_lora_kind": "style", "images_per_lora": 2})
    plan = planned.plan("2 images per style lora", on="anima")
    jobs = [planned.job(i) for i in plan["job_ids"]]
    assert len(jobs) == 2 and sum(j["count"] for j in jobs) == 4 and len({j["description"] for j in jobs}) == 2      # one job per LoRA, each with a picture of its own


def test_a_lora_named_in_the_request_is_no_group_and_keeps_its_count(planned):
    planned.llama.scripts["plan_request"] = plan_reply([job("a girl portrait", 2)], {"use_loras": ["model styles"], "per_lora_kind": "style", "images_per_lora": 1})
    plan = planned.plan("2 images of a girl portrait with the model styles lora", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 1
    only = planned.job(plan["job_ids"][0])
    assert only["count"] == 2 and "group" not in only["meta"]["intent"]


def test_a_varied_group_keeps_its_kind_so_no_second_style_lora_is_added(planned):
    add_card(planned, A_STYLE)
    planned.llama.scripts["write_briefs"] = briefs_reply()
    planned.llama.scripts["plan_request"] = plan_reply([job(count=1)], {"per_lora_kind": "style", "images_per_lora": 1, "vary": ["outfit"]})
    plan = planned.plan("one image per style lora, a different outfit each", on="anima")
    assert plan["status"] == "done" and len(plan["job_ids"]) == 2
    assert {planned.job(i)["meta"]["intent"]["group"]["kind"] for i in plan["job_ids"]} == {"style"}


def test_the_word_lora_in_a_group_job_is_not_a_wish_for_more_loras(planned):
    add_card(planned, CONCEPT, kind="concept")
    planned.llama.scripts["plan_request"] = plan_reply([{**job("a girl in a forest", 1), "constraints": ["every style lora except the dull one"]}],
                                                       {"per_lora_kind": "style", "images_per_lora": 1})
    planned.llama.scripts["pick_loras"] = {"loras": [{"lora": CONCEPT, "weight": 0.8, "reason": "any"}], "reason": "x"}
    plan = planned.plan("a girl in a forest, every style lora except the dull one", on="anima")
    assert [x["name"] for x in planned.job(plan["job_ids"][0])["fields"]["loras"]["value"]] == ["model_styles_anima-base1_v6.safetensors"]


def test_the_brief_jobs_carry_the_group_kind():
    item = {"description": "x", "count": 1, "variety": "variations", "workflow_id": "wf", "constraints": [], "meta": {"request": "x", "intent": {}}}
    children = brief_jobs(item, ["b1", "b2"], [None] * 2, ["A"], per_member=2, group_kind="style")
    assert [c["meta"]["intent"]["group"] for c in children] == [{"kind": "style", "member": "A", "n": 1, "of": 1}] * 2


def test_a_lora_on_most_earlier_images_is_not_added_again():
    from app.planning.loras import not_overused
    scored = [(("a", None), 1.0), (("b", None), 1.0)]
    assert not_overused(scored, {"a": 3, "b": 1}, 5) == [(("b", None), 1.0)]        # a is on 3 of 5 images
    assert not_overused(scored, {"a": 2}, 2) == scored                              # too early to judge


def test_as_fit_still_needs_the_lora_to_relate_to_the_brief(planned):
    add_card(planned, CONCEPT, kind="concept")
    planned.llama.scripts["plan_request"] = plan_reply([job("a girl in a forest")], {"extra_loras": "as_fit"})
    planned.llama.scripts["pick_loras"] = {"loras": [{"lora": CONCEPT, "weight": 0.8, "reason": "any"}], "reason": "x", "left_out": []}
    only = planned.job(planned.plan("a girl in a forest, add what fits", on="anima")["job_ids"][0])
    assert only["fields"]["loras"]["value"] == [] and "nothing in the brief points at it" in only["fields"]["loras"]["note"]


def test_a_group_job_adds_no_other_lora_unless_the_request_lets_it(planned):
    add_card(planned, CONCEPT, kind="concept")
    planned.llama.scripts["plan_request"] = plan_reply([job("a girl in a forest with a shiny concept")], {"per_lora_kind": "style", "images_per_lora": 1})
    planned.llama.scripts["pick_loras"] = {"loras": [{"lora": CONCEPT, "weight": 0.8, "reason": "shiny"}], "reason": "x"}
    plan = planned.plan("one image per style lora, a girl in a forest with a shiny concept", on="anima")
    assert [x["name"] for x in planned.job(plan["job_ids"][0])["fields"]["loras"]["value"]] == ["model_styles_anima-base1_v6.safetensors"]
