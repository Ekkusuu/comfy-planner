"""Removing a LoRA from a job also removes its trigger words and style tags from the prompts (planning/lora_words.py)."""
from app.library.card_models import LoraCard, Range
from app.planning.lora_words import clean_prompts, remove_words, words_to_remove
from tests.test_cards import STYLE_LORA
from tests.test_planning_flow import app  # noqa: F401  (the fixture: a running app with a fake LLM)

OTHER_LORA = "other_style.safetensors"
STYLE_CARD = LoraCard(file=STYLE_LORA, family="Anima", kind="style", trigger_words=["@style_name", "pine-mix"], style_tags=["@pine-owl", "@lensfox"],
                      weight=Range(rec=1.0, min=0.5, max=1.2))
OTHER_CARD = LoraCard(file=OTHER_LORA, family="Anima", kind="style", trigger_words=["@lensfox"], weight=Range(rec=1.0, min=0.5, max=1.2))


# ---------- the words ----------

def test_whole_tags_are_removed_from_a_tag_prompt_ignoring_case_and_weights():
    prompt = "masterpiece, best quality, @Pine-Owl, 1girl, (@lensfox:1.2), @pine-owl-2, forest"
    assert remove_words(prompt, ["@pine-owl", "@lensfox"], tags=True) == "masterpiece, best quality, 1girl, @pine-owl-2, forest"
    assert remove_words("1girl, forest", ["@pine-owl"], tags=True) == "1girl, forest"        # nothing to remove: untouched


def test_whole_phrases_are_removed_from_a_sentence_prompt_and_the_leftovers_tidied():
    assert remove_words("monochrome ink wash style, a cabin in the snow", ["Monochrome Ink Wash style"], tags=False) == "a cabin in the snow"
    assert remove_words("A cabin, in the style of pine-mix, at dawn.", ["pine-mix"], tags=False) == "A cabin, in the style of, at dawn."
    assert remove_words("A cabin with pine-mix.", ["pine-mix"], tags=False) == "A cabin with."
    assert remove_words("pine-mixed cabin", ["pine-mix"], tags=False) == "pine-mixed cabin"          # only whole phrases
    assert remove_words("a cat, @pine-owl, a hat", ["@pine-owl"], tags=False) == "a cat, a hat"


def test_words_another_lora_still_needs_stay():
    other = {OTHER_LORA: OTHER_CARD}
    assert words_to_remove({STYLE_LORA: STYLE_CARD}, other) == {STYLE_LORA: ["@style_name", "pine-mix", "@pine-owl"]}
    assert words_to_remove({STYLE_LORA: STYLE_CARD}, {}) == {STYLE_LORA: ["@style_name", "pine-mix", "@pine-owl", "@lensfox"]}
    assert words_to_remove({STYLE_LORA: LoraCard(file=STYLE_LORA)}, {}) == {}                    # a card without words removes nothing


def test_clean_prompts_notes_the_change_in_the_reason_and_only_touches_the_chosen_prompts():
    field = {"value": ["q, @pine-owl, a", "q, @pine-owl, b", "q, c"], "source": "auto", "locks": [True, False, False],
             "items": [{"reason": "Locked by you"}, {"reason": "second"}, {"reason": "third"}]}
    out, changed = clean_prompts(field, {STYLE_LORA: STYLE_CARD}, {}, True)
    assert changed == [0, 1] and out["value"] == ["q, a", "q, b", "q, c"] and out["locks"] == [True, False, False]
    assert out["items"][0]["reason"] == "Locked by you; removed trigger words of model_styles_anima-base1_v6" and out["items"][2]["reason"] == "third"
    again, changed = clean_prompts(field, {STYLE_LORA: STYLE_CARD}, {}, True, only={1})
    assert changed == [1] and again["value"] == ["q, @pine-owl, a", "q, b", "q, c"]
    assert clean_prompts(field, {STYLE_LORA: STYLE_CARD}, {STYLE_LORA: STYLE_CARD}, True) == (field, [])     # nothing is removed if it is still on


# ---------- through the API ----------

def make_job(app, loras, prompts, locks=None):
    cards, comfy = app.state.cards, app.state.comfy
    comfy.oi = {**comfy.oi, "LoraLoaderModelOnly": {"input": {"required": {"lora_name": [[STYLE_LORA, OTHER_LORA]]}}}}
    for name, card in ((STYLE_LORA, STYLE_CARD), (OTHER_LORA, OTHER_CARD)):          # the words each test needs (the fixture has its own card)
        if cards.get("lora", name) is None:
            cards.save_built("lora", name, card); cards.approve("lora", name)
        cards.save_edit("lora", name, card.model_dump())
    prompt_field = {"value": prompts, "source": "auto", "reason": "", **({"locks": locks} if locks else {})}
    fields = {"prompts": prompt_field, "loras": {"value": [{"name": n, "weight": 1.0} for n in loras], "source": "auto"}}
    return app.client.post("/api/jobs", json={"workflow_id": app.ids["anima"], "count": len(prompts), "fields": fields}).json()


def patch_loras(app, job, names, **extra):
    return app.client.patch(f"/api/jobs/{job['id']}", json={"loras": {"value": [{"name": n, "weight": 1.0} for n in names], "source": "locked"}, **extra}).json()


def test_removing_a_lora_takes_its_style_tag_out_of_the_prompts_and_the_images(app):
    job = make_job(app, [STYLE_LORA], ["masterpiece, best quality, @pine-owl, 1girl, forest", "masterpiece, best quality, pine-mix, 1girl, city"])
    assert job["status"] == "ready"
    out = patch_loras(app, job, [])
    assert out["fields"]["prompts"]["value"] == ["masterpiece, best quality, 1girl, forest", "masterpiece, best quality, 1girl, city"]
    assert [i["recipe"]["prompt"] for i in out["images"]] == out["fields"]["prompts"]["value"] and out["fields"]["loras"]["value"] == []
    assert out["removed_trigger_words"] == {"loras": [STYLE_LORA], "prompts": 2}
    assert "removed trigger words of model_styles_anima-base1_v6" in out["fields"]["prompts"]["items"][0]["reason"]
    assert app.job(job["id"])["fields"]["prompts"]["value"] == out["fields"]["prompts"]["value"]


def test_a_word_the_other_lora_still_needs_stays_and_a_locked_prompt_is_cleaned_too(app):
    job = make_job(app, [STYLE_LORA, OTHER_LORA], ["q, @pine-owl, @lensfox, a", "q, @pine-owl, @lensfox, b"], locks=[False, True])
    out = patch_loras(app, job, [OTHER_LORA])
    assert out["fields"]["prompts"]["value"] == ["q, @lensfox, a", "q, @lensfox, b"]            # @lensfox belongs to the LoRA that stays too
    assert out["fields"]["prompts"]["locks"] == [False, True]                                   # the lock stays; the words went anyway
    assert out["fields"]["prompts"]["items"][1]["reason"] == "Locked by you; removed trigger words of model_styles_anima-base1_v6" or \
        out["fields"]["prompts"]["items"][1]["reason"].endswith("removed trigger words of model_styles_anima-base1_v6")


def test_nothing_is_removed_without_a_card_or_when_the_patch_writes_the_prompts(app):
    job = make_job(app, [STYLE_LORA], ["q, @pine-owl, a"])
    app.state.db.execute("DELETE FROM cards WHERE kind = 'lora' AND key = ?", (STYLE_LORA,))
    out = patch_loras(app, job, [])
    assert out["fields"]["prompts"]["value"] == ["q, @pine-owl, a"] and "removed_trigger_words" not in out      # no card: nothing known to remove
    job = make_job(app, [STYLE_LORA], ["q, @pine-owl, a"])
    out = patch_loras(app, job, [], prompts={"value": ["q, @pine-owl, mine"], "source": "locked"})
    assert out["fields"]["prompts"]["value"] == ["q, @pine-owl, mine"]                           # you typed it, it stays as typed
    assert "removed_trigger_words" not in patch_loras(app, make_job(app, [OTHER_LORA], ["q, a"]), [])    # no words in the prompts: no report


def test_a_replan_that_drops_a_lora_cleans_the_locked_prompts_it_keeps(app):
    app.llama.scripts["pick_loras"] = {"loras": [], "reason": "none needed"}
    job = make_job(app, [STYLE_LORA], ["q, @pine-owl, my first", "q, @pine-owl, second"], locks=[True, False])      # one prompt locked
    done = app.wait(app.client.post(f"/api/jobs/{job['id']}/replan").json()["id"])
    out = app.job(done["job_ids"][0])
    assert out["fields"]["loras"]["value"] == [] and out["fields"]["prompts"]["value"][0] == "q, my first"
    assert "removed trigger words of" in out["fields"]["prompts"]["items"][0]["reason"] and "@pine-owl" not in out["fields"]["prompts"]["value"][1]
    whole = make_job(app, [STYLE_LORA], ["q, @pine-owl, only"], locks=[True])                                          # the whole field locked
    out = app.job(app.wait(app.client.post(f"/api/jobs/{whole['id']}/replan").json()["id"])["job_ids"][0])
    assert out["fields"]["prompts"]["value"] == ["q, only"] and out["fields"]["prompts"]["source"] == "locked"
