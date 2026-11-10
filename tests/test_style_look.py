"""A LoRA that sets the picture's look: no colour-lighting idea, no generic look tags, a setting on the LoRA card.

No LLM, ComfyUI or GPU: units on the look rules, the offers, the scene plan and the tag check, plus planning runs against the fake LLM."""
import json
import random
from types import SimpleNamespace

import pytest

from app.library.card_models import LoraCard
from app.library.cards import CardStore
from app.planning import offers as O
from app.planning.concepts import load_concepts
from app.planning.context import PlanContext
from app.planning.look import NOTE, OPEN, Look, colour_words, look_for, look_note, look_rules, sets_look
from app.planning.scene import plan_scene
from app.planning.tagstep import TagStep
from tests.test_card_defaults import OLD_CARD, put
from app.planning.taglib import TagLib
from tests.test_cards import STYLE_LORA, lora_dir, make_builder, wait_for  # noqa: F401  (lora_dir is a fixture)
from tests.test_offers import wlist
from tests.test_planning_flow import app  # noqa: F401  (the app fixture)
from tests.test_group_expansion import planned  # noqa: F401  (the fixture)
from tests.test_scene_plan import FakeLlm, sheet
from tests.test_tagcheck import cc, lib, run
from tests.test_tagstep import with_library
from tests.test_weights import prompt_reply
from app.wildcards.store import WildcardStore

COLOURS = ["blue violet and light orange", "navy blue and magenta", "light red and cyan", "dark green and gold", "forest green"]
CONCEPT_LORA = "torn clothes_anima_V1.0.safetensors"


def look(colours=COLOURS) -> Look:
    """The look rules as the real config and a colour list give them."""
    book = load_concepts()
    return Look(tuple(book.look_endings), frozenset(w for c in colours for w in c.split() if w != "and"))


# ---------- the card ----------

def test_a_style_card_sets_the_look_by_default_and_other_kinds_do_not():
    assert LoraCard(file="a", kind="style").sets_look is True
    assert all(LoraCard(file="a", kind=k).sets_look is False for k in ("concept", "clothing", "character", "pose", "other"))
    assert LoraCard(file="a", kind="style", sets_look=False).sets_look is False          # a style LoRA that works with lighting
    assert LoraCard(file="a", kind="concept", sets_look=True).sets_look is True
    assert LoraCard.model_validate(LoraCard(file="a", kind="style", sets_look=False).model_dump()).sets_look is False      # the choice survives a save


def test_a_card_stored_before_the_setting_follows_its_kind_without_a_rebuild(db, config):
    put(db, "old.safetensors", OLD_CARD)             # kind style, no sets_look in the stored JSON
    put(db, "old-concept.safetensors", {**OLD_CARD, "file": "old-concept.safetensors", "kind": "concept"})
    cards = CardStore(db, config)
    assert "sets_look" not in OLD_CARD
    assert cards.get("lora", "old.safetensors")["card"]["sets_look"] is True
    assert cards.get("lora", "old-concept.safetensors")["card"]["sets_look"] is False


def test_the_card_editor_saves_the_setting_through_the_api(app):
    card = app.client.get(f"/api/cards/loras/{STYLE_LORA}").json()["card"]
    assert card["sets_look"] is True
    assert app.client.put(f"/api/cards/loras/{STYLE_LORA}", json={**card, "sets_look": False}).status_code == 200
    assert app.client.get(f"/api/cards/loras/{STYLE_LORA}").json()["card"]["sets_look"] is False


async def test_a_regenerated_card_keeps_your_choice(db, config, library, comfy, lora_dir):
    builder, *_ = make_builder(db, config, library, comfy, token=None)
    builder.request("lora", STYLE_LORA)
    await wait_for(builder, "lora", STYLE_LORA)
    card = builder.cards.get("lora", STYLE_LORA)["card"]
    builder.cards.save_edit("lora", STYLE_LORA, {**card, "sets_look": not card["sets_look"]})
    builder.request("lora", STYLE_LORA)
    await wait_for(builder, "lora", STYLE_LORA)
    row = builder.cards.get("lora", STYLE_LORA)
    assert (row["pending"] or row["card"])["sets_look"] is (not card["sets_look"])


# ---------- what a look tag is ----------

def test_light_and_colour_schemes_are_look_tags_and_things_in_the_picture_are_not():
    rules = look()
    for tag in ("cinematic lighting", "soft lighting", "sidelighting", "backlighting", "neon lighting", "volumetric lighting", "rim lighting",
                "blue violet", "light orange light", "light red and cyan", "navy blue and magenta"):
        assert rules.is_look(tag), tag
    assert not any(rules.is_look(tag) for tag in ("bokeh", "lens flare", "film grain"))        # lens effects are no longer in the config: only the writers are told to leave them out
    library = TagLib({"forest": (0, 50000), "gold": (0, 17000)})
    for tag in ("blue hair", "red dress", "light smile", "moonlight", "city lights", "forest", "gold", "night", "rain", "window", "long hair", "from below"):
        assert not rules.is_look(tag, library), tag          # light as a thing or a feature, colour of a thing, a real tag that is also a colour word


def test_a_real_tag_that_is_also_a_colour_word_is_not_a_colour_scheme():
    rules = look(["forest green and gold", "gold"])
    assert rules.is_look("forest green") and rules.is_look("forest")          # without a library `forest` is just a colour word
    library = TagLib({"forest": (0, 50000), "gold": (0, 17000)})
    assert not rules.is_look("forest", library) and not rules.is_look("gold", library)      # with one it is the place and the metal


def test_without_colour_lists_only_the_config_decides():
    rules = look([])
    assert rules.is_look("cinematic lighting") and not rules.is_look("blue violet")


def test_the_config_has_a_short_documented_look_table():
    book = load_concepts()
    assert book.look_endings == ("lighting",)


def test_colours_come_from_the_lists_whose_role_fills_the_lighting_slot(db, config):
    lists = [wlist("color/color-set", "palette", COLOURS), wlist("clothing/clothing", "outfit", ["red kimono", "silk robe"])]
    store = WildcardStore(db, config)
    store.lists = lambda: lists
    ctx = PlanContext(config, None, None, None, db, wildcards=store)
    words = colour_words(ctx)
    assert {"violet", "magenta", "cyan", "gold"} <= words and "kimono" not in words and "and" not in words
    assert look_rules(ctx).is_look("light red and cyan") and not look_rules(ctx).is_look("red kimono")


def test_an_entry_is_a_look_entry_when_any_of_its_pieces_is():
    rules = look()
    assert rules.in_entry("crepuscular ray,purple lighting,dark forest with waterfall") and rules.in_entry("blue violet and light orange")
    assert not rules.in_entry("dark forest with waterfall, mossy stones")


# ---------- offers ----------

SETTINGS = ["rainy alley", "rooftop at night", "library", "flower garden"]
LIGHTS = ["blue violet and light orange", "light red and cyan", "navy blue and magenta"]


@pytest.fixture
def ctx(db, config):
    lists = [wlist("camera/camera-shot", "shot", ["from above", "wide shot", "dutch angle"]),
             wlist("background/background-location", "setting", SETTINGS),
             wlist("color/color-set", "palette", LIGHTS),
             wlist("style/style-oriart", "style", ["purple lighting, dark forest", "sketchy linework", "rim lighting, soft focus"])]
    store = WildcardStore(db, config)
    store.lists = lambda: lists
    return PlanContext(config, None, None, None, db, wildcards=store)


PICKS = [("color/color-set", "colours", "lighting"), ("background/background-location", "places", "setting"), ("style/style-oriart", "style", "")]


async def offered(ctx, **kw):
    got = await O.build(ctx, "a woman in a city", "", PICKS, [], None, random.Random(1), always=True, **kw)
    return got.options(0) if got else []


async def test_a_picture_is_offered_a_colour_lighting_idea_unless_a_lora_sets_its_look(ctx):
    plain = await offered(ctx)
    assert {o["text"] for o in plain if o.get("slot") == "lighting"} >= {"blue violet and light orange"}
    styled = await offered(ctx, look=look())
    assert not [o for o in styled if o.get("slot") == "lighting"] and any(o.get("slot") == "setting" for o in styled)


async def test_entries_with_a_look_tag_are_left_out_of_every_list_not_only_the_lighting_slot(ctx):
    texts = {o["text"] for o in await offered(ctx, look=look())}
    assert "sketchy linework" in texts                                              # the plain entry of the same list stays
    assert not texts & {"purple lighting, dark forest", "rim lighting, soft focus", *LIGHTS}
    assert {"purple lighting, dark forest", "rim lighting, soft focus"} & {o["text"] for o in await offered(ctx)}        # without a look they are offered


async def test_a_request_that_wants_different_lighting_every_time_is_not_a_job_whose_look_is_set(ctx, db, config):
    store = ctx.wildcards
    store.lists()
    card_ctx = SimpleNamespace(lora_card=lambda name: LoraCard(file=name, kind="style"), config=config, wildcards=store)
    assert look_for(card_ctx, ["x.safetensors"]) is not None
    assert look_for(card_ctx, ["x.safetensors"], ["camera", "lighting"]) is None


# ---------- the cards decide ----------

def test_any_lora_of_the_job_that_sets_the_look_makes_it_a_look_job(db, config):
    cards = {"style.safetensors": LoraCard(file="style.safetensors", kind="style"), "torn.safetensors": LoraCard(file="torn.safetensors", kind="clothing"),
             "light.safetensors": LoraCard(file="light.safetensors", kind="style", sets_look=False)}
    ctx = SimpleNamespace(lora_card=lambda name: cards.get(name))
    assert sets_look(ctx, ["style.safetensors"]) and sets_look(ctx, ["torn.safetensors", "style.safetensors"])
    assert not sets_look(ctx, ["torn.safetensors"]) and not sets_look(ctx, ["light.safetensors"]) and not sets_look(ctx, ["gone.safetensors"]) and not sets_look(ctx, [])
    assert look_note(look()) == NOTE and look_note(None) == OPEN


# ---------- the scene plan ----------

def scene_job(text, rules, **kw):
    return {"description": text, "request": text, "constraints": [], "subjects": {"girls": 1}, "look": rules, **kw}


async def plan(text, rules, **over):
    llm = FakeLlm(sheet(**{"lighting": "cinematic lighting, blue violet, light orange light", "setting": "cafe, volumetric lighting, rim lighting, wooden tables",
                           "camera": "cowboy shot, volumetric lighting", "mood": "cozy", "rating": "general", "must_keep": [], **over}))
    scene, issues = await plan_scene(SimpleNamespace(llm=llm, taglib=lambda: lib(), embedder=None), scene_job(text, rules))
    return scene, llm


async def test_a_scene_for_a_look_lora_has_no_lighting_or_colour_words():
    scene, llm = await plan("a woman in a cafe", look())
    assert scene["lighting"] == "" and scene["setting"] == "cafe, wooden tables" and scene["camera"] == "cowboy shot" and scene["mood"] == "cozy"
    assert llm.calls[0][1]["look"] == NOTE


async def test_light_the_user_wrote_stays_in_the_scene():
    scene, _ = await plan("a woman in a cafe with soft candle lighting", look(), lighting="soft candle lighting, cinematic lighting")
    assert scene["lighting"] == "soft candle lighting"          # what the request says stays; what the planner added goes
    scene, _ = await plan("a woman in a cafe, volumetric lighting", look(), camera="cowboy shot, volumetric lighting")
    assert scene["camera"] == "cowboy shot, volumetric lighting"


async def test_a_scene_without_a_look_lora_is_left_alone():
    scene, llm = await plan("a woman in a cafe", None)
    assert scene["lighting"] == "cinematic lighting, blue violet, light orange light" and "volumetric lighting" in scene["setting"]
    assert llm.calls[0][1]["look"] == OPEN


# ---------- the tag check ----------

PREFIX_TAGS = "masterpiece, best quality, "


def test_the_tag_check_takes_look_tags_out_and_keeps_the_rest():
    text = PREFIX_TAGS + "1girl, solo, park, cinematic lighting, volumetric lighting, sidelighting, blue violet, light orange light, long hair, sitting"
    r = run(text, look=look())
    assert r.text == PREFIX_TAGS + "1girl, solo, park, long hair, sitting"
    gone = {c["from"] for c in r.changes if c["to"] is None and "sets the look" in c["why"]}
    assert gone == {"cinematic lighting", "volumetric lighting", "sidelighting", "blue violet", "light orange light"}


def test_without_a_look_lora_the_check_keeps_them():
    text = PREFIX_TAGS + "1girl, solo, park, cinematic lighting, volumetric lighting"
    assert run(text).text == text


def test_light_the_user_typed_or_the_workflow_prefix_holds_is_kept():
    r = run(PREFIX_TAGS + "1girl, park, cinematic lighting, rim lighting, volumetric lighting", look=look(), typed="a girl in a park with cinematic lighting")
    assert "cinematic lighting" in r.text and "rim lighting" not in r.text and "volumetric lighting" not in r.text
    r = run("masterpiece, best quality, rim lighting, 1girl, park", look=look(), protected=["masterpiece", "best quality", "rim lighting"])
    assert "rim lighting" in r.text                                                    # a protected tag (a prefix or trigger word) is never touched
    r = run(PREFIX_TAGS + "1girl, long hair, red dress, night, rain", look=look())
    assert r.text == PREFIX_TAGS + "1girl, long hair, red dress, night, rain"      # things in the picture are not look tags


def test_look_tags_are_not_offered_by_the_tag_step_and_the_lighting_slot_is_skipped(app):
    import asyncio
    from tests.test_tagstep import NoMeaning, library
    context = cc(look=look(), library=library())
    step = TagStep(NoMeaning(), context.lib, context, "Anima")
    slots = {"setting": "park", "lighting": "night, rain"}
    assert "lighting" not in asyncio.run(step.slot_suggestions(slots))
    plain = cc(library=library())
    assert asyncio.run(TagStep(NoMeaning(), plain.lib, plain, "Anima").slot_suggestions(slots)).get("lighting") == ["night", "rain"]


# ---------- a planning run ----------

def writer(app, n=0) -> str:
    return app.llama.of("write_prompts")[n]["messages"][1]["content"]


def scene_message(app) -> str:
    return app.llama.of("plan_scene")[-1]["messages"][1]["content"]


def run_job(app, lora, ask="use the style LoRA", prompt="masterpiece, best quality, 1girl, solo, cafe, cinematic lighting, volumetric lighting, sidelighting, coffee cup"):
    with_library(app)
    app.script_plan({"description": "a woman in a cafe", "count": 1, "constraints": [ask]})
    app.llama.scripts["pick_loras"] = {"loras": [{"lora": lora, "weight": 1.0, "reason": "asked"}], "reason": "asked"}
    app.llama.scripts["plan_scene"] = sheet(lighting="cinematic lighting, neon lighting", setting="cafe, rim lighting", rating="general", must_keep=[])
    app.llama.scripts["write_prompts"] = prompt_reply(prompt)
    return app.job(app.plan("make an image of a woman in a cafe", on="anima")["job_ids"][0])


def test_a_job_with_a_style_lora_gets_no_lighting_or_look_tags(app):
    job = run_job(app, STYLE_LORA)
    text = job["fields"]["prompts"]["value"][0]
    assert "cafe" in text and "coffee cup" in text
    assert not any(w in text for w in ("cinematic lighting", "volumetric lighting", "sidelighting"))
    assert job["meta"]["scene"]["lighting"] == "" and "rim lighting" not in job["meta"]["scene"]["setting"]
    assert NOTE in scene_message(app) and NOTE in writer(app)
    record = job["fields"]["prompts"]["items"][0]["tagcheck"]
    assert any(c["from"] == "cinematic lighting" and "sets the look" in c["why"] for c in record)


def test_a_job_with_a_concept_lora_keeps_its_lighting(app):
    from tests.test_planning_flow import anima_card  # noqa: F401
    app.state.cards.save_built("lora", CONCEPT_LORA, LoraCard(file=CONCEPT_LORA, family="Anima", kind="clothing", what_it_does="Torn clothes.",
                                                              trigger_words=["torn clothes"]))
    app.state.cards.approve("lora", CONCEPT_LORA)
    job = run_job(app, CONCEPT_LORA, "use the torn clothes LoRA")
    text = job["fields"]["prompts"]["value"][0]
    assert "cinematic lighting" in text and "volumetric lighting" in text and "sidelighting" in text
    assert job["meta"]["scene"]["lighting"] == "cinematic lighting, neon lighting"
    assert NOTE not in scene_message(app) and NOTE not in writer(app) and "## Look\n(open)" in writer(app)


def test_a_style_lora_with_the_setting_turned_off_keeps_the_lighting(app):
    card = app.client.get(f"/api/cards/loras/{STYLE_LORA}").json()["card"]
    app.client.put(f"/api/cards/loras/{STYLE_LORA}", json={**card, "sets_look": False})
    text = run_job(app, STYLE_LORA)["fields"]["prompts"]["value"][0]
    assert "cinematic lighting" in text and "volumetric lighting" in text


def test_the_users_own_lighting_survives_a_look_lora(app):
    with_library(app)
    app.script_plan({"description": "a woman in a cafe with cinematic lighting", "count": 1, "constraints": ["use the style LoRA"]})
    app.llama.scripts["pick_loras"] = {"loras": [{"lora": STYLE_LORA, "weight": 1.0, "reason": "asked"}], "reason": "asked"}
    app.llama.scripts["plan_scene"] = sheet(lighting="cinematic lighting, neon lighting", setting="cafe", rating="general", must_keep=[])
    app.llama.scripts["write_prompts"] = prompt_reply("masterpiece, best quality, 1girl, solo, cafe, cinematic lighting, neon lighting, volumetric lighting")
    job = app.job(app.plan("a woman in a cafe with cinematic lighting", on="anima")["job_ids"][0])
    text = job["fields"]["prompts"]["value"][0]
    assert "cinematic lighting" in text and "neon lighting" not in text and "volumetric lighting" not in text
    assert job["meta"]["scene"]["lighting"] == "cinematic lighting"


def group_request(planned, kind, member):
    from tests.test_group_expansion import add_card, briefs_reply, plan_reply, job
    add_card(planned, member, kind=kind)
    planned.llama.scripts["write_briefs"] = briefs_reply()
    planned.llama.scripts["plan_request"] = plan_reply([job(count=1)], {"per_lora_kind": kind, "vary": ["camera"]})
    assert planned.plan(f"1 image per {kind} lora", on="anima")["status"] == "done"
    return planned.llama.of("write_briefs")[0]["messages"][1]["content"]


def test_the_briefs_of_a_group_of_style_loras_are_told_to_leave_light_and_colour_out(planned):
    assert NOTE in group_request(planned, "style", "aaa_style.safetensors")


def test_the_briefs_of_a_group_of_concept_loras_keep_the_lighting_open(planned):
    message = group_request(planned, "concept", "shiny_concept.safetensors")
    assert NOTE not in message and "## Look\n(open)" in message
