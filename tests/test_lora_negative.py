"""A LoRA card can list negative tags to remove from the negative prompt of every job that uses the LoRA."""
import json

from app.comfy.graph import build_graph, negative_text
from app.library.card_models import LoraCard
from app.planning.lora_negative import apply_to_field, asked_to_avoid, take_out, word_key
from app.planning.mentions import stem
from tests.test_cards import STYLE_LORA
from tests.test_planning_flow import KREA_LORA, app  # noqa: F401  (the fixture: a running app with a fake LLM)

OTHER_LORA = "other_style.safetensors"
# the Anima workflow's own negative contains lowres, monochrome, greyscale, watermark, simple background, text, logo, ...


def set_words(app, lora, words):
    """Save the card of `lora` with these words, the way the card editor does (the whole card through the API)."""
    cards = app.state.cards
    if cards.get("lora", lora) is None:
        cards.save_built("lora", lora, LoraCard(file=lora, family="Anima", kind="style")); cards.approve("lora", lora)
    card = cards.get("lora", lora)["card"]
    return app.client.put(f"/api/cards/loras/{lora}", json={**card, "remove_from_negative": words})


def make_job(app, loras, negative=None, request=None, on="anima"):
    app.state.comfy.oi = {**app.state.comfy.oi, "LoraLoaderModelOnly": {"input": {"required": {"lora_name": [[STYLE_LORA, OTHER_LORA, KREA_LORA]]}}}}
    fields = {"prompts": {"value": ["masterpiece, best quality, 1girl, forest"], "source": "auto", "reason": ""},
              "loras": {"value": [{"name": n, "weight": 1.0} for n in loras], "source": "auto"}}
    if negative is not None:
        fields["negative"] = negative
    return app.client.post("/api/jobs", json={"workflow_id": app.ids[on], "count": 1, "fields": fields}).json()


def patch_loras(app, job, names):
    return app.client.patch(f"/api/jobs/{job['id']}", json={"loras": {"value": [{"name": n, "weight": 1.0} for n in names], "source": "locked"}}).json()


def tags(job):
    return [t.strip() for t in job["fields"]["negative"]["value"].split(",")]


# ---------- the card ----------

def test_the_setting_is_empty_by_default_and_an_old_card_loads():
    assert LoraCard(file="a.safetensors").remove_from_negative == []
    old = LoraCard(file="a.safetensors", kind="style").model_dump()
    del old["remove_from_negative"]                                   # a card stored before the setting existed
    assert LoraCard.model_validate(old).remove_from_negative == []
    assert LoraCard(file="a", remove_from_negative=[" blur ", "", "blur", "haze"]).remove_from_negative == ["blur", "haze"]


def test_the_api_saves_and_reads_the_words_and_a_new_card_has_none(app):
    assert app.client.get(f"/api/cards/loras/{STYLE_LORA}").json()["card"]["remove_from_negative"] == []
    assert set_words(app, STYLE_LORA, ["watermark", "Simple Background"]).status_code == 200
    assert app.client.get(f"/api/cards/loras/{STYLE_LORA}").json()["card"]["remove_from_negative"] == ["watermark", "Simple Background"]


# ---------- matching ----------

def test_whole_tags_match_ignoring_case_weights_and_underscores_but_never_part_of_a_tag():
    negative = "lowres, (Watermark:1.2), simple_background, ((logo)), oil painting, painting style, text"
    out, removed = take_out(negative, {"x.safetensors": ["watermark", "Simple Background", "logo", "painting"]}, set())
    assert out == "lowres, oil painting, painting style, text"           # `painting` does not touch `oil painting`
    assert [t for t, _ in removed] == ["(Watermark:1.2)", "simple_background", "((logo))"]
    assert take_out("lowres, text", {"x": ["blur"]}, set()) == ("lowres, text", [])        # nothing to remove: the text is untouched
    assert word_key("(Oil_Painting:1.2)") == "oil painting"


def test_a_sentence_negative_loses_only_whole_comma_separated_items():
    out, _ = take_out("blurry, a painting of a wall, painting, flat colours", {"x": ["painting", "flat"]}, set())
    assert out == "blurry, a painting of a wall, flat colours"


def test_words_you_asked_to_avoid_are_found_in_your_request():
    assert asked_to_avoid("a girl in a park, no painting, cartoon or blur. A red scarf") == {"painting", "cartoon", "blur"}
    assert asked_to_avoid("a girl without a hat") == {"hat"}
    assert asked_to_avoid("negative: Oil_Painting, crayon") == {"oil painting", "crayon"}
    assert asked_to_avoid("a girl in a painting studio") == set()
    assert asked_to_avoid("no blurry please") == {"blurry"}


def test_apply_to_field_gives_the_words_back_from_the_base():
    field, issues = apply_to_field({"value": "a, b, c", "source": "auto", "reason": "r"}, "", {"lora": ["b"]}, set(), set())
    assert (field["value"], field["base"], field["note"], issues) == ("a, c", "a, b, c", "removed `b`: lora asks for it", [])
    back, _ = apply_to_field(field, "", {}, set(), set())
    assert back == {"value": "a, b, c", "source": "auto", "reason": "r"}


# ---------- through the app ----------

def test_a_job_without_a_negative_field_gets_the_workflow_default_minus_the_words(app):
    set_words(app, STYLE_LORA, ["Monochrome", "simple_background"])
    job = make_job(app, [STYLE_LORA])
    assert "monochrome" not in tags(job) and "simple background" not in tags(job) and "greyscale" in tags(job)
    assert job["fields"]["negative"]["source"] == "auto"
    assert job["fields"]["negative"]["note"] == "removed `monochrome`, `simple background`: model styles anima base1 v6 asks for it"
    plain = make_job(app, [])
    assert "negative" not in plain["fields"] and plain["issues"] == []                          # no LoRA words: nothing is stored


def test_two_loras_remove_the_union_of_their_words(app):
    set_words(app, STYLE_LORA, ["watermark"]); set_words(app, OTHER_LORA, ["logo", "watermark"])
    job = make_job(app, [STYLE_LORA, OTHER_LORA])
    assert not {"watermark", "logo"} & set(tags(job)) and "signature" in tags(job)
    assert job["fields"]["negative"]["note"].count("`watermark`") == 1


def test_it_applies_for_any_lora_strength(app):
    set_words(app, STYLE_LORA, ["watermark"])
    app.state.comfy.oi = {**app.state.comfy.oi, "LoraLoaderModelOnly": {"input": {"required": {"lora_name": [[STYLE_LORA]]}}}}
    fields = {"prompts": ["1girl"], "loras": {"value": [{"name": STYLE_LORA, "weight": 0.1}], "source": "auto"}}
    job = app.client.post("/api/jobs", json={"workflow_id": app.ids["anima"], "count": 1, "fields": fields}).json()
    assert "watermark" not in tags(job)


def test_a_locked_negative_is_kept_with_a_warning(app):
    set_words(app, STYLE_LORA, ["watermark"])
    job = make_job(app, [STYLE_LORA], negative="lowres, watermark, text")                       # a plain value counts as set by hand: locked
    assert job["fields"]["negative"]["value"] == "lowres, watermark, text" and job["fields"]["negative"]["source"] == "locked"
    [issue] = [i for i in job["issues"] if i["code"] == "lora_negative_locked"]
    assert issue["severity"] == "warning" and issue["field"] == "negative" and "locked" in issue["message"] and "`watermark`" in issue["message"]
    assert job["status"] == "ready"
    quiet = make_job(app, [STYLE_LORA], negative="lowres, text")                                # nothing of the LoRA's in it: no warning
    assert quiet["issues"] == []
    after = patch_loras(app, job, [])                                                           # the warning goes with the LoRA
    assert after["issues"] == [] and after["fields"]["negative"]["value"] == "lowres, watermark, text"


def test_words_the_safety_tags_need_are_never_removed(app):
    app.state.config.safety_negative = ["nsfw", "nude", "nudity"]
    set_words(app, STYLE_LORA, ["nsfw", "watermark"])
    job = make_job(app, [STYLE_LORA], negative={"value": "lowres, nsfw, watermark", "source": "auto", "reason": "r"})
    assert job["fields"]["negative"]["value"] == "lowres, nsfw"
    cfg = app.state.config
    image = job["images"][0]
    assert negative_text(app.state.library.get(app.ids["anima"])["slots"], image["recipe"], cfg.safety_negative) == "lowres, nsfw, nude, nudity"


def test_removing_the_lora_from_the_job_brings_the_words_back_and_adding_it_removes_them_again(app):
    set_words(app, STYLE_LORA, ["watermark", "logo"])
    job = make_job(app, [STYLE_LORA], negative={"value": "lowres, watermark, text, logo", "source": "auto", "reason": "planned"})
    assert tags(job) == ["lowres", "text"]
    gone = patch_loras(app, job, [])
    assert gone["fields"]["negative"]["value"] == "lowres, watermark, text, logo" and "note" not in gone["fields"]["negative"] and "base" not in gone["fields"]["negative"]
    assert json.loads(json.dumps(gone["images"][0]["recipe"]))["negative"] == "lowres, watermark, text, logo"
    again = patch_loras(app, job, [STYLE_LORA])
    assert again["fields"]["negative"]["value"] == "lowres, text"
    assert again["images"][0]["recipe"]["negative"] == "lowres, text"


def test_a_negative_you_edit_by_hand_is_locked_and_stays_after_the_lora_leaves(app):
    set_words(app, STYLE_LORA, ["watermark"])
    job = make_job(app, [STYLE_LORA], negative={"value": "lowres, watermark, text", "source": "auto", "reason": "r"})
    edited = app.client.patch(f"/api/jobs/{job['id']}", json={"negative": {"value": "lowres, blur", "source": "locked", "reason": "set by hand"}}).json()
    assert edited["fields"]["negative"]["value"] == "lowres, blur" and edited["issues"] == []
    assert patch_loras(app, job, [])["fields"]["negative"]["value"] == "lowres, blur"


def test_the_graph_gets_the_negative_the_job_card_shows(app):
    app.state.config.safety_negative = ["nsfw", "nude", "nudity"]
    set_words(app, STYLE_LORA, ["Monochrome", "greyscale"])
    job = make_job(app, [STYLE_LORA])
    wf = app.state.library.get(app.ids["anima"])
    recipe = job["images"][0]["recipe"]
    graph = build_graph(app.state.library.get_graph(app.ids["anima"]), wf["slots"], recipe, app.state.config.safety_negative)
    slot = wf["slots"]["negative"]
    sent = graph[slot["node"]]["inputs"][slot["input"]]
    shown = job["fields"]["negative"]["value"]
    assert sent == negative_text(wf["slots"], recipe, app.state.config.safety_negative) and "monochrome" not in sent and "greyscale" not in sent
    assert set(t.strip() for t in sent.split(",")) == set(t.strip() for t in shown.split(",")) | {"nsfw", "nude", "nudity"}   # the job card plus the safety tags


def test_a_workflow_without_a_negative_is_left_alone(app):
    set_words(app, KREA_LORA, ["blur"])
    job = make_job(app, [KREA_LORA], on="krea")
    assert "negative" not in job["fields"] and job["issues"] == []


# ---------- planning, re-planning and tweaks ----------

def plan_with(app, request="a girl in a park with the style LoRA", pick=STYLE_LORA, **kw):
    app.script_plan({"description": "a girl in a park", "count": 1, "constraints": ["use the style LoRA"]})
    app.llama.scripts["pick_loras"] = {"loras": [{"lora": pick, "weight": 1.0, "reason": "the style"}] if pick else [], "reason": "ok"}
    app.llama.scripts["write_prompts"] = {"prompts": [{"prompt": "1girl, park", "variation": "v"}], "negative_additions": kw.get("additions", []), "reason": "r"}
    return app.job(app.plan(request=request)["job_ids"][0])


def test_a_lora_the_planner_picked_removes_its_words_and_the_job_card_says_so(app):
    set_words(app, STYLE_LORA, ["watermark", "Greyscale"])
    job = plan_with(app, additions=["blurry"])
    assert not {"watermark", "greyscale"} & set(tags(job)) and "blurry" in tags(job)
    field = job["fields"]["negative"]
    assert field["note"].startswith("removed `greyscale`, `watermark`:") and "asks for it" in field["note"] and field["source"] == "auto"
    assert job["images"][0]["recipe"]["negative"] == field["value"]
    assert any(m["role"] == "user" and "watermark" not in m["content"] for m in app.llama.of("write_prompts")[0]["messages"])    # the writer is not told about it


def test_a_lora_named_with_at_removes_its_words_too(app):
    set_words(app, STYLE_LORA, ["watermark"])
    job = plan_with(app, request=f"a girl in a park @{stem(STYLE_LORA)}", pick=None)
    assert job["meta"]["loras_named"] == [STYLE_LORA] and [l["name"] for l in job["fields"]["loras"]["value"]] == [STYLE_LORA]
    assert "watermark" not in tags(job)


def test_a_word_you_asked_to_avoid_in_your_request_stays_but_the_card_default_goes(app):
    set_words(app, STYLE_LORA, ["watermark", "greyscale"])
    job = plan_with(app, request="a girl in a park with the style LoRA, no watermark", additions=["greyscale"])
    assert "watermark" in tags(job) and "greyscale" not in tags(job)


def test_a_locked_negative_survives_a_replan_with_the_warning(app):
    set_words(app, STYLE_LORA, ["watermark"])
    job = plan_with(app)
    assert "watermark" not in tags(job)
    app.client.post(f"/api/jobs/{job['id']}/locks", json={"fields": ["negative"]})
    locked = app.job(job["id"])
    assert locked["fields"]["negative"]["source"] == "locked" and "watermark" not in tags(locked)      # locking keeps what the card showed
    app.client.patch(f"/api/jobs/{job['id']}", json={"negative": {"value": "lowres, watermark", "source": "locked"}})
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/replan").json()["id"])["status"] == "done"
    after = app.job(job["id"])
    assert after["fields"]["negative"]["value"] == "lowres, watermark" and any(i["code"] == "lora_negative_locked" for i in after["issues"])


def test_replan_uses_the_words_the_card_has_now(app):
    set_words(app, STYLE_LORA, ["watermark"])
    job = plan_with(app)
    assert "watermark" not in tags(job) and "logo" in tags(job)
    set_words(app, STYLE_LORA, ["logo"])
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/replan").json()["id"])["status"] == "done"
    after = app.job(job["id"])
    assert "watermark" in tags(after) and "logo" not in tags(after)
    set_words(app, STYLE_LORA, [])
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/replan").json()["id"])["status"] == "done"
    assert {"watermark", "logo"} <= set(tags(app.job(job["id"]))) and "note" not in app.job(job["id"])["fields"]["negative"]


def test_a_tweak_keeps_the_words_out_and_a_new_negative_is_cleaned_again(app):
    set_words(app, STYLE_LORA, ["watermark"])
    job = plan_with(app)
    app.llama.scripts["apply_tweak"] = {"steps": 25, "reason": "quicker"}
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/tweak", json={"instruction": "fewer steps"}).json()["id"])["status"] == "done"
    assert "watermark" not in tags(app.job(job["id"]))
    seen = app.llama.of("apply_tweak")[-1]["messages"][1]["content"]
    assert "watermark" in seen                                                                       # the writer is shown the whole negative
    base = app.job(job["id"])["fields"]["negative"]["base"]
    app.llama.scripts["apply_tweak"] = {"negative": base + ", blurry", "reason": "more"}              # the writer works from the whole negative
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/tweak", json={"instruction": "avoid blur"}).json()["id"])["status"] == "done"
    now = app.job(job["id"])
    assert "blurry" in tags(now) and "watermark" not in tags(now) and "watermark" in now["fields"]["negative"]["base"]
    gone = patch_loras(app, now, [])
    assert "watermark" in tags(gone) and "blurry" in tags(gone)                                        # the LoRA leaves: the words come back


def test_a_word_you_ask_for_in_a_tweak_stays(app):
    set_words(app, STYLE_LORA, ["blurry"])
    job = plan_with(app)
    base = app.job(job["id"])["fields"]["negative"]["value"]
    app.llama.scripts["apply_tweak"] = {"negative": base + ", blurry", "reason": "as asked"}
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/tweak", json={"instruction": "no blurry please"}).json()["id"])["status"] == "done"
    assert "blurry" in tags(app.job(job["id"]))
