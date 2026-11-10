"""@ LoRA mentions: the resolver, and what a mention does to a plan (scope, batches, other families, artist tags)."""
import copy
import json

from app.library.card_models import LoraCard, Range
from app.planning.loras import named_items, named_loras
from app.planning.mentions import mentioned, plain, readable, scan, stem, unnamed
from tests.test_cards import STYLE_LORA
from tests.test_planning_flow import KREA_LORA, app, anima_card, settings_reply  # noqa: F401  (app is a fixture)

STYLE = "model_styles_anima-base1_v6"
KEYS = [STYLE_LORA, KREA_LORA, "foo.safetensors", "foo bar.safetensors", "sub/Deep Name.safetensors"]


# ---------- the resolver ----------

def test_a_mention_is_an_at_sign_and_a_known_file_name_without_its_extension():
    assert stem("sub/Deep Name.safetensors") == "Deep Name" and stem(STYLE_LORA) == STYLE
    assert mentioned(f"a girl in a park @{STYLE}", KEYS) == [STYLE_LORA]
    assert mentioned(f"a girl @{STYLE.upper()} in a park", KEYS) == [STYLE_LORA]                  # not case sensitive
    assert mentioned("with @krea2_inkbrush, and @model_styles_anima-base1_v6.", KEYS) == [KREA_LORA, STYLE_LORA]       # in the order written
    assert mentioned(f"(@{STYLE}) @{STYLE}", KEYS) == [STYLE_LORA]                               # each file once


def test_the_longest_name_wins_and_a_name_may_contain_spaces():
    assert mentioned("a knight @foo bar in rain", KEYS) == ["foo bar.safetensors"]
    assert mentioned("a knight @foo barista", KEYS) == ["foo.safetensors"]                       # "foo bar" would end inside a word
    assert mentioned("a knight @foo  bar", KEYS) == ["foo bar.safetensors"]                      # any white space between the words
    assert mentioned("use @deep name now", KEYS) == ["sub/Deep Name.safetensors"]                # no folder needed


def test_a_name_must_end_where_a_name_ends():
    assert mentioned("@foobar", KEYS) == [] and mentioned("@foo-v2", KEYS) == [] and mentioned("@foo_2", KEYS) == []
    assert mentioned("@foo, @foo. @foo! (@foo) @foo\n", KEYS) == ["foo.safetensors"]
    assert mentioned("mail me@foo", KEYS) == [] and mentioned("@@foo", KEYS) == []            # the @ starts a word


def test_artist_and_style_tags_are_left_alone():
    text = "a girl, @velden mara, @brkt, @bluejay style, @foo2"
    assert scan(text, KEYS) == [] and plain(text, KEYS) == text
    assert scan("anything", []) == [] and plain("@foo", []) == "@foo"


def test_a_mention_is_written_as_plain_words_for_the_llm():
    assert plain("use @ink7cm-paint-anima-base-epoch-05 here", ["ink7cm-paint-anima-base-epoch-05.safetensors"]) == "use the chosen LoRA here"
    assert readable("ink7cm-paint-anima-base-epoch-05.safetensors") == "ink7cm paint" and readable(STYLE_LORA) == "model styles anima base1 v6"    # nothing left: all words
    assert plain(f"@{STYLE} and @foo", KEYS) == "the chosen LoRA and the chosen LoRA"
    assert "@" not in plain(f"x @{STYLE},@krea2_inkbrush", KEYS)
    assert "paint" not in plain("use @ink7cm-paint-anima-base-epoch-05", ["ink7cm-paint-anima-base-epoch-05.safetensors"]).lower()
    # a trailing "lora" after the mention must not read as the job asking for a LoRA
    said = plain("use @ink7cm-paint-anima-base-epoch-05 lora", ["ink7cm-paint-anima-base-epoch-05.safetensors"])
    assert said == "use the chosen LoRA" and unnamed(said, ["ink7cm-paint-anima-base-epoch-05.safetensors"]) == "use"
    assert unnamed("a girl, use the LoRA you named", []) == "a girl, use"
    assert unnamed("use the LoRA the user named", []) == "use"     # the planner's third-person wording too


# ---------- what a mention does ----------

def planned(app, request, on="anima", **job):
    app.script_plan({"description": "a girl in a park", "count": 1, **job})
    plan = app.plan(request, on=on)
    assert plan["status"] == "done", plan
    return [app.job(i) for i in plan["job_ids"]]


def lora_names(job):
    return [x["name"] for x in job["fields"]["loras"]["value"]]


def test_a_mentioned_lora_is_on_the_job_at_its_recommended_weight_with_the_reason(app):
    (job,) = planned(app, f"a girl in a park @{STYLE}")
    item = job["fields"]["loras"]["value"][0]
    assert item == {"name": STYLE_LORA, "weight": 1.0, "reason": "You named this LoRA", "named": True}
    assert job["fields"]["loras"]["source"] == "auto" and job["fields"]["loras"]["reason"].startswith("You named ")
    assert job["issues"] == [] and job["meta"]["loras_named"] == [STYLE_LORA]
    assert job["meta"]["request"] == f"a girl in a park @{STYLE}"                               # the request as typed
    assert all(p.startswith("masterpiece, best quality, ") and any(t in p for t in ("@bluejay", "@lensfox", "@quietmoss")) for p in job["fields"]["prompts"]["value"])   # the card's style tag, added by code


def test_the_token_never_reaches_the_llm_or_the_prompts(app):
    (job,) = planned(app, f"a girl in a park @{STYLE}")
    assert app.llama.requests
    for task, body in app.llama.requests:
        assert f"@{STYLE}" not in json.dumps(body), task
    told = app.llama.of("plan_request")[0]["messages"][1]["content"]
    assert "the chosen LoRA" in told and "model styles" not in told
    write = app.llama.of("write_prompts")[0]["messages"][1]["content"]
    assert "@model_styles" not in write
    assert not any("@model_styles" in p for p in job["fields"]["prompts"]["value"])


def test_the_trigger_words_of_a_mentioned_lora_are_added_to_every_prompt(app):
    (job,) = planned(app, "a cabin @krea2_inkbrush", on="krea", count=2)
    assert job["fields"]["loras"]["value"][0]["weight"] == 0.8                                   # the card's recommended weight
    prompts = job["fields"]["prompts"]["value"]
    assert len(prompts) == 2 and all("monochrome ink wash style" in p and "@krea2" not in p for p in prompts)


def test_the_recommended_weight_stays_inside_the_cards_range(app):
    cards = app.state.cards
    cards.save_built("lora", "light.safetensors", LoraCard(file="light.safetensors", family="Anima", kind="style", weight=Range(min=0.2, max=0.4)))       # no recommended weight
    cards.approve("lora", "light.safetensors")
    chosen, issues = named_loras(app.state.planner.ctx, "Anima", ["light.safetensors"], 3)
    assert named_items(chosen)[0]["weight"] == 0.4 and issues == []


def test_a_shared_mention_applies_to_every_job_and_one_in_a_part_to_that_part(app):
    shared = planned(app, f"@{STYLE} === a knight --- a cabin")
    assert [lora_names(j) for j in shared] == [[STYLE_LORA], [STYLE_LORA]]
    assert not any(f"@{STYLE}" in c for j in shared for c in j["meta"]["constraints"])           # the shared text is a constraint, in plain words
    assert all(j["meta"]["request"].endswith(f"(for all images: @{STYLE})") for j in shared)
    part = planned(app, f"a knight @{STYLE} --- a cabin")
    assert [lora_names(j) for j in part] == [[STYLE_LORA], []]
    assert "loras_named" not in part[1]["meta"]


def test_a_mention_in_a_request_without_separators_applies_to_all_its_jobs(app):
    app.two_jobs()
    plan = app.plan(f"3 images of a girl with the style @{STYLE}, and 2 of her in a cafe")
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert len(jobs) == 2 and [lora_names(j) for j in jobs] == [[STYLE_LORA], [STYLE_LORA]]


def test_the_mention_survives_the_big_batch_path(app):
    app.llama.scripts["write_briefs"] = lambda schema, body: {"briefs": [f"Ruin number{i} with moss{i} and fog{i} in a valley{i}" for i in range(schema["properties"]["briefs"]["minItems"])]}
    jobs = planned(app, f"@{STYLE} === 4 unique ruins", count=4, variety="unique")
    assert len(jobs) == 4 and all(j["count"] == 1 and lora_names(j) == [STYLE_LORA] and j["meta"]["loras_named"] == [STYLE_LORA] for j in jobs)
    assert all("@model_styles" not in json.dumps(b) for task, b in app.llama.requests if task == "write_briefs")


def test_a_lora_of_another_family_is_not_added_and_the_job_says_why(app):
    (job,) = planned(app, "a cabin @krea2_inkbrush")
    assert lora_names(job) == []
    (issue,) = job["issues"]
    assert (issue["field"], issue["severity"], issue["code"]) == ("loras", "warning", "lora_mention")
    assert "krea2_inkbrush" in issue["message"] and "Krea-2" in issue["message"] and "Anima" in issue["message"]
    (job,) = planned(app, f"a cabin @krea2_inkbrush @{STYLE}")                                 # the right one is still added
    assert lora_names(job) == [STYLE_LORA] and len(job["issues"]) == 1


def test_a_card_that_is_not_approved_is_not_used_and_says_so(app):
    cards = app.state.cards
    cards.save_built("lora", "draft-only.safetensors", LoraCard(file="draft-only.safetensors", family="Anima", kind="style"))      # a draft card
    comfy_oi = app.state.planner.ctx.oi
    ctx = app.state.planner.ctx
    ctx.oi = copy.deepcopy(comfy_oi) if comfy_oi else {"LoraLoaderModelOnly": {"input": {"required": {"lora_name": [[STYLE_LORA, "draft-only.safetensors"]]}}}}
    chosen, issues = named_loras(ctx, "Anima", ["draft-only.safetensors", "nothing-here.safetensors"], 3)
    assert chosen == [] and "not approved" in issues[0]["message"] and "no card" in issues[1]["message"]
    ctx.config.allow_draft_cards = True                                                         # Settings allow drafts: the planner may use it, so a mention may too
    assert [n for n, _ in named_loras(ctx, "Anima", ["draft-only.safetensors"], 3)[0]] == ["draft-only.safetensors"]


def test_artist_tags_are_left_as_typed_and_force_no_lora(app):
    (job,) = planned(app, "a girl in @velden mara style")
    assert lora_names(job) == [] and job["issues"] == [] and "loras_named" not in job["meta"]
    assert "@velden mara" in app.llama.of("plan_request")[0]["messages"][1]["content"]
    assert app.llama.of("pick_loras")                                                            # the LLM chose as always


def test_the_lora_limit_never_removes_a_mentioned_lora(app, comfy, oi):
    ctx = app.state.planner.ctx
    ctx.config.max_loras = 1
    app.llama.scripts["pick_loras"] = {"loras": [{"lora": "park.safetensors", "weight": 1, "reason": "x"}], "reason": "x"}
    (job,) = planned(app, f"a girl in a park @{STYLE}")
    assert lora_names(job) == [STYLE_LORA]
    assert app.llama.of("pick_loras") == []                                                      # no place is left, so the LLM is not asked

    cards = app.state.cards                                                                      # a second Anima LoRA that fits the job
    cards.save_built("lora", "park.safetensors", LoraCard(file="park.safetensors", family="Anima", kind="concept", what_it_does="Park scenes.", weight=Range(rec=0.7, min=0.5, max=1.0)))
    cards.approve("lora", "park.safetensors")
    comfy.oi = copy.deepcopy(oi)
    comfy.oi["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0].append("park.safetensors")
    ctx.config.max_loras = 2
    (job,) = planned(app, f"a girl in a park @{STYLE}")
    assert [(x["name"], x.get("named")) for x in job["fields"]["loras"]["value"]] == [(STYLE_LORA, True), ("park.safetensors", None)]     # the named one first, the LLM's after
    assert app.llama.schema_of("pick_loras")["properties"]["loras"]["maxItems"] == 1                                                     # one place was left
    assert "loras_already_on_the_job" in app.llama.of("pick_loras")[0]["messages"][1]["content"]
    ctx.config.max_loras = 1
    (job,) = planned(app, f"a girl in a park @{STYLE}")
    assert lora_names(job) == [STYLE_LORA]


def test_two_mentions_over_the_limit_keep_the_first_and_report_the_second(app):
    ctx = app.state.planner.ctx
    cards = app.state.cards
    cards.save_built("lora", "second.safetensors", LoraCard(file="second.safetensors", family="Anima", kind="style"))
    cards.approve("lora", "second.safetensors")
    ctx.oi = {"LoraLoaderModelOnly": {"input": {"required": {"lora_name": [[STYLE_LORA, "second.safetensors"]]}}}}
    chosen, issues = named_loras(ctx, "Anima", [STYLE_LORA, "second.safetensors"], 1)
    assert [n for n, _ in chosen] == [STYLE_LORA] and "1 LoRA at most" in issues[0]["message"] and "second" in issues[0]["message"]


def test_a_replan_and_a_tweak_keep_the_mentioned_lora(app):
    (job,) = planned(app, f"a girl in a park @{STYLE}", on="tagwf")
    assert lora_names(job) == [STYLE_LORA]
    app.llama.scripts["pick_loras"] = {"loras": [], "reason": "none"}
    plan = app.client.post(f"/api/jobs/{job['id']}/replan").json()
    assert app.wait(plan["id"])["status"] == "done"
    again = app.job(job["id"])
    assert lora_names(again) == [STYLE_LORA] and again["fields"]["loras"]["value"][0]["reason"] == "You named this LoRA"
    app.llama.scripts["apply_tweak"] = {"prompts": ["darker, moody"] * again["count"], "lora_weights": [{"lora": STYLE_LORA, "weight": 0.9}], "reason": "darker"}
    plan = app.client.post(f"/api/jobs/{job['id']}/tweak", json={"instruction": "make it darker, with @krea2_inkbrush"}).json()
    assert app.wait(plan["id"])["status"] == "done"
    tweaked = app.job(job["id"])
    assert lora_names(tweaked) == [STYLE_LORA] and tweaked["fields"]["loras"]["value"][0]["named"] is True
    assert "@krea2_inkbrush" not in json.dumps(app.llama.of("apply_tweak")) and "@model_styles" not in json.dumps(app.llama.of("apply_tweak"))


def test_a_lora_you_locked_on_the_card_is_not_overruled_by_a_mention(app):
    (job,) = planned(app, f"a girl in a park @{STYLE}")
    mine = app.client.patch(f"/api/jobs/{job['id']}", json={"loras": []}).json()
    assert mine["fields"]["loras"]["source"] == "locked"
    plan = app.client.post(f"/api/jobs/{job['id']}/replan").json()
    assert app.wait(plan["id"])["status"] == "done"
    assert lora_names(app.job(job["id"])) == []


def test_a_mention_does_not_count_as_asking_for_another_lora(app, comfy, oi):
    cards = app.state.cards
    cards.save_built("lora", "rockets.safetensors", LoraCard(file="rockets.safetensors", family="Anima", kind="concept", what_it_does="Rocket ships.", weight=Range(rec=0.7, min=0.5, max=1.0)))
    cards.approve("lora", "rockets.safetensors")
    comfy.oi = copy.deepcopy(oi)
    comfy.oi["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0].append("rockets.safetensors")
    app.state.planner.ctx.config.max_loras = 3
    app.llama.scripts["pick_loras"] = {"loras": [{"lora": "rockets.safetensors", "weight": 0.7, "reason": "it is for this model"}], "reason": "x"}
    (job,) = planned(app, f"a girl in a park @{STYLE}", description=f"a girl in a park, with the {readable(STYLE_LORA)} LoRA")
    assert '"user_asked_for_a_lora": false' in app.llama.of("pick_loras")[0]["messages"][1]["content"]
    assert lora_names(job) == [STYLE_LORA] and "nothing in the brief points at it" in job["fields"]["loras"]["note"]
    assert unnamed(f"a girl in a park, with the {readable(STYLE_LORA)} LoRA and a real lora", [STYLE_LORA]) == "a girl in a park, with and a real lora"
