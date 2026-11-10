"""Prompt weights: when they are allowed, the code that enforces the rules after the LLM, and the planning flow."""
import random

import pytest

from app.comfy.brackets import protect_literal_brackets
from app.fields import merge_negative
from app.library.card_models import PromptStyle, WorkflowCard
from app.planning.lora_words import remove_words
from app.planning.prompts import ensure_prefix, finalize_prompts, has_word, near_duplicates
from app.planning.weights import WeightRules, clean_negative, enforce, rule_text, strip_weights, typed_groups, weights_allowed
from app.wildcards.expand import expand
from app.wildcards.store import Catalog
from tests.content_probes import CONTENT_TERMS as CONTENT, MODIFIERS as MOD, NUMBERS as NUM, SUBJECT_TERMS as SUBJ
from tests.conftest import KREA, QWEN, TAGWF, TAGWF_LORA
from tests.test_planning_flow import KREA_LORA, STYLE_LORA, anima_card, app  # noqa: F401  (the fixture: a running app with a fake LLM)

ON, OFF = WeightRules(True), WeightRules(False)
PROTECTED = ["masterpiece", "best quality", "@pine-owl", "monochrome ink wash style"]


# ---------- when weights are allowed ----------

def card(kind: str) -> WorkflowCard:
    return anima_card().model_copy(update={"prompt_style": PromptStyle(kind=kind, quality_prefix="")})


@pytest.mark.parametrize("kind,workflow,allowed", [
    ("tags", TAGWF, True), ("tags", TAGWF_LORA, True), ("tags", KREA, False), ("tags", QWEN, False),       # tags, but an encoder that reads weights as text
    ("sentences", TAGWF, False), ("mixed", TAGWF, True), ("sentences", KREA, False), ("mixed", KREA, False)])    # a prose card never; a mixed card when the encoder reads weights
def test_weights_need_a_tag_card_and_an_encoder_that_reads_them(graphs, kind, workflow, allowed):
    assert weights_allowed(card(kind), graphs[workflow]) is allowed


def test_the_rule_texts_are_files_the_owner_can_edit():
    assert "(tag:1.4)" in rule_text(True) and "1.2 to 1.8" in rule_text(True) and "At most 3" in rule_text(True)
    assert "does not read" in rule_text(False) and "wording and word order" in rule_text(False)


# ---------- the enforcement table ----------

@pytest.mark.parametrize("text,expected", [
    ("1girl, (red scarf:1.5), forest", "1girl, (red scarf:1.5), forest"),                 # fine as it is
    ("1girl, (red scarf:3), forest", "1girl, (red scarf:1.8), forest"),                   # clamp down
    ("1girl, (red scarf:1.05), forest", "1girl, (red scarf:1.2), forest"),                # clamp up
    ("1girl, (red scarf:1.0), forest", "1girl, red scarf, forest"),                       # no emphasis at all
    ("1girl, (red scarf:0.6), forest", "1girl, red scarf, forest"),                       # below 1: unwrapped
    ("1girl, (red scarf:-0.5), forest", "1girl, red scarf, forest"),
    ("1girl, (red scarf), forest", "1girl, (red scarf:1.4), forest"),                     # bare emphasis: repaired to the default
    ("1girl, ((red scarf)), forest", "1girl, (red scarf:1.4), forest"),
    ("1girl, (red (silk) scarf:1.3), forest", "1girl, (red (silk) scarf:1.3), forest"),   # a literal bracket inside is not nesting
    ("1girl, (a (b:2):1.5), forest", "1girl, (a b:1.5), forest"),                         # nested: the inner one is unwrapped
    ("1girl, (:1.3), forest", "1girl, forest"),                                           # empty
    ("1girl, aria vale (starlight), forest", "1girl, aria vale (starlight), forest"), # literal brackets stay
    ("1girl, holding a (glowing staff:1.6)", "1girl, holding a (glowing staff:1.6)"),     # part of a tag is fine
    ("1girl, (a:1.3), (b:1.3), (c:1.3), (d:1.3), (e:1.3)", "1girl, (a:1.3), (b:1.3), (c:1.3), d, e"),    # the first three stay
    ("masterpiece, (best quality:1.3), 1girl", "masterpiece, best quality, 1girl"),       # the quality prefix is never weighted
    ("(masterpiece, best quality:1.2), 1girl", "masterpiece, best quality, 1girl"),
    ("1girl, (@pine-owl:1.5), forest", "1girl, @pine-owl, forest"),                     # neither a trigger word nor a style tag
    ("(monochrome ink wash:1.3) style, forest", "monochrome ink wash style, forest"),     # also not when a weight cuts through one
    ("1girl, (red scarf:1.5", "1girl, (red scarf:1.5"),                                   # unbalanced: only a character, left for the escape
    ("1girl, (red scarf:abc), forest", "1girl, red scarf:abc, forest"),                    # not a number: not a weight, unwrapped
    ("a girl in a red scarf", "a girl in a red scarf")])
def test_enforcement_on_a_workflow_that_reads_weights(text, expected):
    assert enforce(text, PROTECTED, ON)[0] == expected
    assert enforce(expected, PROTECTED, ON)[0] == expected                               # running it again changes nothing


@pytest.mark.parametrize("text,expected", [
    ("a (forest:1.5), 1girl", "a forest, 1girl"), ("(red scarf), ((forest)), x", "red scarf, forest, x"), ("a (b:2) in (c (d:1.2):1.3)", "a b in c d"),
    ("aria vale (starlight), (red scarf:1.4)", "aria vale (starlight), red scarf"), ("a girl (with a scarf) walking", "a girl (with a scarf) walking"),
    ("smile :(, (forest:1.5)", "smile :(, forest"), ("plain text", "plain text")])
def test_on_a_workflow_that_reads_weights_as_text_every_weight_is_unwrapped(text, expected):
    assert enforce(text, PROTECTED, OFF)[0] == expected
    assert strip_weights(text) == expected


def test_notes_say_what_was_taken_out_but_not_routine_clamping():
    assert enforce("a, (b:3), (c:1.4)", [], ON)[1] == []
    assert enforce("a, (b:0.5), (c:1.4)", [], ON)[1] == ["weight removed from b"]
    assert enforce("a, (b:1.3), (c:1.3), (d:1.3), (e:1.3)", [], ON)[1] == ["weight removed from e"]
    assert enforce("a, (masterpiece:1.3)", PROTECTED, ON)[1] == ["weight removed from masterpiece"]


# ---------- what the person typed ----------

def test_typed_weights_are_found_as_typed():
    typed = typed_groups("a girl in a (forest:2.5) with ((fog)) and aria vale (starlight)", "(red scarf:0.3)")
    assert typed == {"forest": "(forest:2.5)", "fog": "((fog))", "red scarf": "(red scarf:0.3)"}


def test_a_typed_weight_is_kept_exactly_outside_every_limit():
    rules = WeightRules(True, typed_groups("(forest:2.5), (fog:0.5), ((rain)), (a:3)"))
    text = "1girl, (forest:1.4), (fog:1.4), (rain:1.4), (a:1.4), (b:1.3), (c:1.3), (d:1.3), (e:1.3)"
    out, notes = enforce(text, [], rules)
    assert out == "1girl, (forest:2.5), (fog:0.5), ((rain)), (a:3), (b:1.3), (c:1.3), (d:1.3), e"         # the LLM's own count still stops at 3


def test_a_typed_weight_the_llm_wrote_plain_or_left_out_is_put_back():
    rules = WeightRules(True, typed_groups("(forest:2.5), (fog:1.3)"))
    out, notes = enforce("1girl, forest, red scarf", [], rules)
    assert out == "1girl, (forest:2.5), red scarf, (fog:1.3)" and notes == ["put back (forest:2.5)", "put back (fog:1.3)"]
    assert enforce("1girl, forest, red scarf", [], WeightRules(True, rules.typed, put_back=False))[0] == "1girl, forest, red scarf"      # a tweak


def test_a_typed_weight_is_removed_where_weights_are_text():
    assert enforce("1girl, (forest:2.5), x", [], WeightRules(False, typed_groups("(forest:2.5)")))[0] == "1girl, forest, x"


def test_the_negative_gets_no_weights_from_the_llm():
    rules = WeightRules(True, typed_groups("(bad hands:1.6)"))
    assert clean_negative(["(blurry:1.4)", "((text))", "(bad hands:1.2)", "ugly"], rules) == ["blurry", "text", "(bad hands:1.6)", "ugly"]
    assert clean_negative(["(bad hands:1.2)"], WeightRules(False, rules.typed)) == ["bad hands"]
    assert clean_negative(["(bad)", "(x:1.3)"], ON, existing=["(bad)"]) == ["(bad)", "x"]             # the workflow's own tags stay as they are


# ---------- finalize_prompts: prefix, triggers, near-duplicates ----------

def test_the_quality_prefix_is_not_added_twice_next_to_nested_or_negative_weights():
    prefix = ["masterpiece", "best quality"]
    assert ensure_prefix("((masterpiece)), best quality, 1girl", prefix) == "((masterpiece)), best quality, 1girl"
    assert ensure_prefix("1girl, (masterpiece:-0.5), (best quality:1.2)", prefix) == "masterpiece, best quality, 1girl"
    assert ensure_prefix("(best quality:1.1), (masterpiece:1.1), 1girl", prefix) == "masterpiece, best quality, 1girl"


def test_finalize_enforces_first_so_the_prefix_and_triggers_come_out_clean():
    anima = anima_card()
    prompts, notes = finalize_prompts(["(masterpiece:1.4), (best quality:1.3), 1girl, (@pine-owl:1.5), (red scarf:1.5)"], anima, [], ["@pine-owl"], rules=ON)
    assert prompts == ["masterpiece, best quality, 1girl, @pine-owl, (red scarf:1.5)"]
    assert notes == ["prompt 1: weight removed from masterpiece", "prompt 1: weight removed from best quality", "prompt 1: weight removed from @pine-owl"]
    untouched, _ = finalize_prompts(["masterpiece, best quality, (red scarf:3), (a:0.5)"], anima, [], [])         # no rules: left alone
    assert untouched == ["masterpiece, best quality, (red scarf:3), (a:0.5)"]


def test_a_trigger_cut_by_a_weight_is_not_added_a_second_time():
    assert has_word("(monochrome ink wash:1.3) style, forest", "monochrome ink wash style")
    assert has_word("1girl, (@pine-owl:1.5)", "@pine-owl") and not has_word("1girl, (@gs:1.5)", "@pine-owl")
    prompts, notes = finalize_prompts(["(monochrome ink wash:1.3) style, forest"], card("sentences"), ["monochrome ink wash style"], [])
    assert prompts == ["(monochrome ink wash:1.3) style, forest"] and notes == []      # a hand-made prompt is not touched, and gets no second copy


def test_weight_numbers_are_not_words_for_the_near_duplicate_check():
    a, b = "1girl, (red scarf:1.5), forest, night", "1girl, red scarf, forest, night"
    assert near_duplicates([a, b], []) == [(0, 1)]
    c = "1girl, (red scarf:1.5), forest, night, (smile:1.2)"
    assert near_duplicates([a, c], [], 0.8) == [(0, 1)]


def test_removing_a_trigger_leaves_no_empty_weight():
    assert remove_words("a, (@x:1.2), b", ["@x"], True) == "a, b"
    assert remove_words("a (@x:1.2) b", ["@x"], False) == "a b"


# ---------- brackets, wildcards ----------

@pytest.mark.parametrize("text,escaped", [
    ("1girl, (red scarf:1.5), (forest:1.2)", "1girl, (red scarf:1.5), (forest:1.2)"),
    ("1girl, (smile:1.3), name (starlight), (aria vale (starlight):1.4)", r"1girl, (smile:1.3), name \(starlight\), (aria vale \(starlight\):1.4)"),
    ("(__color__:1.3) scarf, ({red|blue} coat:1.4)", "(__color__:1.3) scarf, ({red|blue} coat:1.4)")])
def test_the_escape_leaves_valid_weights_alone(text, escaped):
    assert protect_literal_brackets(enforce(text, [], ON)[0]) == escaped


def test_wildcards_inside_a_weight_expand_and_keep_the_weight():
    out = enforce("1girl, ({red|blue} scarf:1.4), (__color__:1.3)", [], ON)[0]
    assert out == "1girl, ({red|blue} scarf:1.4), (__color__:1.3)"
    for seed in range(8):
        text = expand(out, Catalog([]), random.Random(seed))
        assert text in ("1girl, (red scarf:1.4), (color:1.3)", "1girl, (blue scarf:1.4), (color:1.3)")      # no list called color: the plain word


# ---------- safety, merge ----------

def test_a_weighted_negative_survives_the_merge():
    assert merge_negative("bad hands, blurry", ["(bad hands:1.5)"]) == "(bad hands:1.5), blurry"


# ---------- the planning flow (fake LLM) ----------

def prompt_reply(*prompts, negative=()):
    return {"prompts": [{"prompt": p, "variation": f"v{i}"} for i, p in enumerate(prompts)], "negative_additions": list(negative), "reason": "ok"}


def writer_text(app, n=-1):
    return app.llama.of("write_prompts")[n]["messages"][-1]["content"]


def test_on_a_tag_workflow_the_llm_sees_the_weights_rule_and_its_weights_are_enforced(app):
    app.script_plan({"description": "a girl with a red scarf", "count": 2, "constraints": ["the scarf must stand out"]})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, (red scarf:3), (a:1.3), (b:1.3), (c:1.3), (d:1.3), forest",
                                                      "masterpiece, best quality, 1girl, (red scarf:0.4), forest", negative=["(blurry:1.5)", "lowres"])
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert "(tag:1.4)" in writer_text(app) and "does not read prompt weights" not in writer_text(app)
    one, two = job["fields"]["prompts"]["value"]
    assert one == "masterpiece, best quality, 1girl, (red scarf:1.8), (a:1.3), (b:1.3), c, d, forest"
    assert two == "masterpiece, best quality, 1girl, red scarf, forest"
    assert "prompt 1: weight removed from c" in job["fields"]["prompts"]["note"]
    assert "(blurry" not in job["fields"]["negative"]["value"] and "blurry" in job["fields"]["negative"]["value"]


def test_on_a_prose_workflow_the_llm_is_told_no_weights_and_any_it_writes_are_unwrapped(app):
    app.script_plan({"description": "a cabin in a forest", "count": 1, "constraints": []})
    app.llama.scripts["write_prompts"] = prompt_reply("A cabin in a (forest:1.5), with aria vale (starlight) outside")
    job = app.job(app.plan(on="krea")["job_ids"][0])
    assert "does not read prompt weights" in writer_text(app) and "wording and word order" in writer_text(app)
    assert job["fields"]["prompts"]["value"] == ["A cabin in a forest, with aria vale (starlight) outside"]
    assert not any(i["code"] == "weights_removed" for i in job["issues"])        # the LLM's own weights are fixed quietly, with a note
    assert "weight removed from forest" in job["fields"]["prompts"]["note"]


def test_a_weight_the_user_typed_is_kept_on_a_tag_workflow(app):
    app.script_plan({"description": "a girl in a (forest:2.5)", "count": 2, "constraints": ["(fog:0.7) over the lake"]})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, forest, fog over the lake", "masterpiece, best quality, 1girl, (forest:1.4), (fog:1.4)")
    job = app.job(app.plan("a girl in a (forest:2.5), (fog:0.7) over the lake", on="tagwf")["job_ids"][0])
    assert "wrote in the job themselves" in writer_text(app) and "(forest:2.5)" in writer_text(app)
    assert job["fields"]["prompts"]["value"] == ["masterpiece, best quality, 1girl, (forest:2.5), (fog:0.7) over the lake",
                                                 "masterpiece, best quality, 1girl, (forest:2.5), (fog:0.7)"]


def test_a_typed_weight_the_planner_dropped_is_kept_as_a_constraint(app):
    app.script_plan({"description": "a girl in a forest", "count": 1, "constraints": []})
    job = app.job(app.plan("a girl in a (forest:2.5)", on="tagwf")["job_ids"][0])
    assert job["meta"]["constraints"] == ["Use (forest:2.5) exactly as written in every prompt"]
    assert "(forest:2.5)" in job["fields"]["prompts"]["value"][0]
    krea = app.job(app.plan("a girl in a (forest:2.5)", on="krea")["job_ids"][0])
    assert krea["meta"]["constraints"] == []                                     # nothing to keep where weights are text


def test_a_typed_weight_on_a_prose_workflow_is_removed_with_a_warning(app):
    app.script_plan({"description": "a girl in a (forest:2.5)", "count": 1, "constraints": []})
    app.llama.scripts["write_prompts"] = prompt_reply("A girl in a (forest:2.5)")
    job = app.job(app.plan("a girl in a (forest:2.5)", on="krea")["job_ids"][0])
    assert job["fields"]["prompts"]["value"] == ["A girl in a forest"]
    warning = next(i for i in job["issues"] if i["code"] == "weights_removed")
    assert warning["severity"] == "warning" and "(forest:2.5)" in warning["message"] and "reads weights as text" in warning["message"]
    assert job["status"] == "ready"                                              # only a warning: the job can run


def test_prompts_a_person_wrote_are_never_changed(app):
    hand = ["masterpiece, (red scarf:3), (a:0.5), ((b))", "(forest:9), (c:1), (d:2), (e:2), (f:2)"]
    for key in ("tagwf", "krea"):
        manual = app.job(app.client.post("/api/jobs", json={"workflow_id": app.ids[key], "count": 2, "fields": {"prompts": hand}}).json()["id"])
        assert manual["fields"]["prompts"]["value"] == hand
        edited = app.client.patch(f"/api/jobs/{manual['id']}", json={"prompts": hand[::-1]}).json()
        assert edited["fields"]["prompts"]["value"] == hand[::-1] and edited["fields"]["prompts"]["source"] == "locked"
        app.script_plan({"description": "a girl", "count": 2, "constraints": []})
        app.llama.scripts["write_prompts"] = prompt_reply("never used 1", "never used 2")
        assert app.wait(app.client.post(f"/api/jobs/{manual['id']}/replan").json()["id"])["status"] == "done"
        assert app.job(manual["id"])["fields"]["prompts"]["value"] == hand[::-1]            # a locked prompt stays word for word


def test_a_prompt_locked_on_its_own_keeps_its_weights_when_the_others_are_written(app):
    app.script_plan({"description": "a girl", "count": 2, "constraints": []})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, (red scarf:1.4), forest", "masterpiece, best quality, city")
    job_id = app.plan(on="tagwf")["job_ids"][0]
    app.client.post(f"/api/jobs/{job_id}/locks", json={"prompts": [0]})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, (red scarf:9), (rain:0.3)")
    assert app.wait(app.client.post(f"/api/jobs/{job_id}/replan").json()["id"])["status"] == "done"
    assert app.job(job_id)["fields"]["prompts"]["value"] == ["masterpiece, best quality, (red scarf:1.4), forest",
                                                             "masterpiece, best quality, (red scarf:1.8), rain"]


def test_weights_cannot_hide_blocked_content_from_the_check(app):
    manual = app.client.post("/api/jobs", json={"workflow_id": app.ids["tagwf"], "fields": {"prompts": [f"{CONTENT[5]}, {CONTENT[0]} girl, ({NUM[4]}:1.3) years old"]}}).json()
    assert app.client.post(f"/api/jobs/{manual['id']}/queue").status_code == 400
    app.script_plan({"description": "a nude woman", "count": 1, "constraints": []})
    app.llama.scripts["write_prompts"] = prompt_reply(f"masterpiece, best quality, {CONTENT[0]}, (({NUM[2]})) years old")
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert job["status"] == "draft" and job["issues"][0]["code"] == "cse"


# ---------- tweak ----------

def tweak(app, job_id, instruction, **reply):
    app.llama.scripts["apply_tweak"] = {"reason": "changed", **reply}
    return app.wait(app.client.post(f"/api/jobs/{job_id}/tweak", json={"instruction": instruction}).json()["id"])


def test_a_tweak_keeps_the_weights_it_was_not_asked_to_change_and_follows_the_same_limits(app):
    app.script_plan({"description": "a girl", "count": 2, "constraints": []})
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, (red scarf:1.4), forest", "masterpiece, best quality, 1girl, (red scarf:1.4), city")
    job_id = app.plan(on="tagwf")["job_ids"][0]
    assert "(red scarf:1.4)" in app.job(job_id)["fields"]["prompts"]["value"][0]
    tweak(app, job_id, "make the scarf stand out more", prompts=["masterpiece, best quality, 1girl, (red scarf:2.6), (a:1.3), (b:1.3), (c:1.3), forest",
                                                                  "masterpiece, best quality, 1girl, (red scarf:1.6), city"])
    sent = app.llama.of("apply_tweak")[0]["messages"][-1]["content"]
    assert "(red scarf:1.4)" in sent and "(tag:1.4)" in sent                     # the LLM sees the current weights and the rule
    assert app.job(job_id)["fields"]["prompts"]["value"] == ["masterpiece, best quality, 1girl, (red scarf:1.8), (a:1.3), (b:1.3), c, forest",
                                                              "masterpiece, best quality, 1girl, (red scarf:1.6), city"]


def test_a_tweak_on_a_prose_workflow_adds_no_weights_and_a_typed_one_is_said_to_be_removed(app):
    app.script_plan({"description": "a cabin", "count": 1, "constraints": []})
    job_id = app.plan(on="krea")["job_ids"][0]
    tweak(app, job_id, "make the (roof:1.5) stand out", prompts=["A cabin with a (roof:1.5) in red"])
    fields = app.job(job_id)["fields"]["prompts"]
    assert "does not read prompt weights" in app.llama.of("apply_tweak")[0]["messages"][-1]["content"]
    assert fields["value"] == ["A cabin with a roof in red"] and "reads weights as text" in fields["note"]


def test_a_tweak_cannot_put_weights_into_the_negative_and_keeps_the_workflows_own(app):
    app.script_plan({"description": "a girl", "count": 1, "constraints": []})
    job_id = app.plan(on="tagwf")["job_ids"][0]
    negative = app.job(job_id)["fields"]["negative"]["value"]
    tweak(app, job_id, "no blur", negative=f"{negative}, (blurry:1.5), (extra fingers:1.3)")
    after = app.job(job_id)["fields"]["negative"]["value"]
    assert after.startswith(negative) and "(blurry" not in after and after.endswith("blurry, extra fingers")
