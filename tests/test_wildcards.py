"""Wildcards: parsing, nested and variant resolution, seeded draws, roles and offer rules, the size of the ideas
block, tokens in requests and prompts, the import, and the API."""
import hashlib
import random
from pathlib import Path

import pytest

from app.config import Config
from app.db import Database
from app.queue.jobs import JobError, build_recipes
from app.wildcards import ideas as wi
from app.wildcards.expand import expand, expand_for_image, find_refs, find_tokens, unknown_refs
from app.wildcards.parse import MAX_ENTRY_CHARS, parse_entries, parse_simple_yaml
from app.wildcards.store import Catalog, WildcardError, WildcardStore
from tests.content_probes import CSE_MESSAGE, CONTENT_TERMS as CONTENT, MODIFIERS as MOD, NUMBERS as NUM, SUBJECT_TERMS as SUBJ
from tests.conftest import TAGWF, import_fixture
from tests.test_planning_flow import app, REQUEST  # noqa: F401  (the planning app fixture)
from tests.test_settings import client as settings_client, toml_path  # noqa: F401  (an app that writes to a temporary settings file)

LOCATIONS = ["banff national park", "arches national park", "a rainy tokyo alley", "a quiet harbor", "mount fuji at dawn", "a neon arcade",
             "a mossy forest", "a desert canyon", "a snowy village", "a rooftop garden", "an old library", "a night market"]
SHOTS = ["dutch angle", "wide shot", "close-up", "from above", "low angle shot", "cowboy shot"]
OUTFITS = ["red kimono", "leather jacket", "sailor dress", "trench coat", "silk robe", "denim overalls"]
POSES = ["leaning on a railing", "arms crossed", "mid-stride", "looking over the shoulder", "hands in pockets"]
COLORS = ["teal and orange", "pastel pink", "monochrome blue", "warm sepia"]
ARTISTS = ["by alpha artist", "by beta artist", "by gamma artist", "by delta artist"]
STEAM = ["brass gears", "steam pipes", "copper goggles", "airship", "clockwork arm"]


def make_source(root: Path) -> Path:
    """A small Forge-style wildcard tree."""
    files = {"background/background-location.txt": LOCATIONS, "camera/camera-shot.txt": SHOTS, "clothing/clothing.txt": OUTFITS,
             "pose/pose.txt": POSES, "color/color-palette.txt": COLORS, "artist-group/artist-painting.txt": ARTISTS,
             "style/style-steampunk.txt": STEAM, "random/rng-background.txt": ["__background/background-location__", "{a quiet|a busy} street"]}
    for name, lines in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def source(tmp_path) -> Path:
    return make_source(tmp_path / "forge-wildcards")


@pytest.fixture
def wc_config(tmp_path) -> Config:
    return Config(data_dir=tmp_path / "data")


@pytest.fixture
def wc(wc_config, source) -> WildcardStore:
    store = WildcardStore(Database(":memory:"), wc_config)
    store.import_folder(str(source))
    return store


# ---------- parsing ----------

def test_parse_entries_drops_blanks_comments_duplicates_and_control_characters():
    text = "# a comment\n\nred apple\nRed Apple\n  green  pear \n\x00\x07bad\x1bchars\n" + "x" * (MAX_ENTRY_CHARS + 1) + "\n{a|b} c\n"
    assert parse_entries(text) == ["red apple", "green pear", "badchars", "{a|b} c"]


def test_simple_yaml_lists():
    assert parse_simple_yaml("warm:\n  - red\n  - orange\ncool:\n  - 'blue'\nnested:\n  deep:\n    - skip\n") == {"warm": ["red", "orange"], "cool": ["blue"]}
    assert parse_simple_yaml("- one\n- two\n") == {"": ["one", "two"]}


# ---------- expansion ----------

def test_expansion_is_seeded_and_resolves_nested_references_and_variants(wc):
    catalog = wc.catalog()
    text = "__rng-background__, __camera/camera-shot__, {red|green|blue} coat"
    a = expand(text, catalog, random.Random(5))
    assert a == expand(text, catalog, random.Random(5))                      # same seed, same text
    assert len({expand(text, catalog, random.Random(s)) for s in range(30)}) > 5       # different seeds differ
    for s in range(30):
        out = expand(text, catalog, random.Random(s))
        assert "__" not in out and "{" not in out and "|" not in out
        assert any(loc in out for loc in LOCATIONS) or "street" in out


def test_variants_with_counts_separators_and_weights():
    empty = Catalog([])
    picks = {expand("{2$$a|b|c}", empty, random.Random(s)) for s in range(40)}
    assert all(len(p.split(", ")) == 2 and len(set(p.split(", "))) == 2 for p in picks)
    assert all(" and " in expand("{2$$ and $$a|b|c}", empty, random.Random(s)) for s in range(10))
    assert {expand("{1-3$$a|b|c}", empty, random.Random(s)).count(",") for s in range(60)} == {0, 1, 2}
    assert {expand("{100::heavy|0::never}", empty, random.Random(s)) for s in range(20)} == {"heavy"}
    assert expand("a [b::0.3] c [:d:0.5] e [f:g:0.2]", empty, random.Random(0)) == "a b c d e f"


def test_unknown_references_become_plain_words_and_loops_end(wc, tmp_path):
    catalog = wc.catalog()
    assert expand("a __no-such-list__ here", catalog, random.Random(0)) == "a no-such-list here"
    root = wc.root
    (root / "loop.txt").write_text("__loop__ and __loop__\n", encoding="utf-8")
    out = expand("__loop__", wc.catalog(), random.Random(0))
    assert "__" not in out and len(out) < 4000                                  # depth limit and size cap stop it


def test_reference_names_full_short_ambiguous_and_pattern(wc):
    c = wc.catalog()
    assert c.resolve("background/background-location")[0][0].name == "background/background-location"
    assert c.resolve("Background-Location")[0][0].name == "background/background-location"       # short name, any case
    assert c.resolve("camera-*")[0][0].name == "camera/camera-shot" and c.resolve("*-shot")[0]
    (wc.root / "other").mkdir()
    (wc.root / "other" / "pose.txt").write_text("handstand\n", encoding="utf-8")
    c = wc.catalog()
    assert c.resolve("pose")[0] == [] and "several" in c.resolve("pose")[1]                          # two lists called pose
    assert len(c.resolve("pose/pose")[0]) == 1 and "no wildcard list" in c.resolve("nope")[1]
    assert unknown_refs("__pose__ and __camera-shot__ and __zzz__", c) == [c.resolve("pose")[1], c.resolve("zzz")[1]]


def test_tokens_are_found_exactly_as_written():
    text = "a cat in __background/background-location__ with {red|blue} eyes, __camera-shot__, {x} and {1-2$$a|b}"
    assert find_tokens(text) == ["__background/background-location__", "{red|blue}", "__camera-shot__", "{1-2$$a|b}"]
    assert find_refs(text) == ["background/background-location", "camera-shot"]
    assert find_tokens("no tokens, (word:1.2), a_b, __") == []


def test_expand_for_image_uses_the_images_seed(wc):
    c = wc.catalog()
    assert expand_for_image("plain prompt", c, 1) == "plain prompt"
    prompt = "__background/background-location__, __camera-shot__, __clothing__"
    outs = {expand_for_image(prompt, c, seed) for seed in range(20)}
    assert len(outs) > 5 and expand_for_image(prompt, c, 7) == expand_for_image(prompt, c, 7)


# ---------- roles, offer rules and the ideas ----------

def test_default_roles_follow_the_folder_and_can_be_changed_per_list_and_folder(wc):
    roles = {l.name: l.role for l in wc.lists()}
    assert roles["background/background-location"] == "setting" and roles["camera/camera-shot"] == "shot"
    assert roles["artist-group/artist-painting"] == "artist" and roles["style/style-steampunk"] == "style"
    wc.update("camera/camera-shot", role="pose")
    assert wc.get("camera/camera-shot").role == "pose" and wc.get("camera/camera-shot").default_role == "shot"
    wc.update_folder("pose", enabled=False)
    assert wc.get("pose/pose").enabled is False
    wc.update("camera/camera-shot", role="")
    assert wc.get("camera/camera-shot").role == "shot"
    with pytest.raises(WildcardError):
        wc.update("pose/pose", role="nonsense")


def test_open_slot_roles_are_offered_and_given_ones_are_not(wc, wc_config):
    lists = wc.lists()
    chosen = wi.choose_lists(lists, "a girl", "a girl", wc_config, random.Random(1))
    assert len(chosen) == 4 and len({l.role for l in chosen}) == 4 and {l.role for l in chosen} <= {"setting", "shot", "palette", "outfit", "pose"}
    brief = "a girl in a city wearing a red kimono, close-up shot"
    assert wi.given_roles(brief, lists, wc_config) == {"setting", "outfit", "palette", "shot"}          # city / kimono, red / close-up
    left = wi.choose_lists(lists, brief, brief, wc_config, random.Random(1))
    assert {l.role for l in left} == {"pose"}
    assert wi.given_roles("a girl at a quiet harbor", lists, wc_config) >= {"setting"}              # an entry of a setting list is in the text


def test_match_roles_are_offered_only_when_the_brief_matches_the_list(wc, wc_config):
    lists = wc.lists()
    plain = wi.choose_lists(lists, "a girl in a cafe", "a girl in a cafe", wc_config, random.Random(1))
    assert not {l.role for l in plain} & {"style", "artist", "random"}
    steam = wi.choose_lists(lists, "a steampunk inventor", "a steampunk inventor", wc_config, random.Random(1))
    assert steam[0].name == "style/style-steampunk"                                          # matches first
    painting = wi.choose_lists(lists, "an artist painting", "", wc_config, random.Random(1))
    assert painting[0].name == "artist-group/artist-painting"
    wc.update("style/style-steampunk", enabled=False)
    assert "style/style-steampunk" not in [l.name for l in wi.choose_lists(wc.lists(), "a steampunk inventor", "", wc_config, random.Random(1))]


def test_ideas_differ_per_prompt_are_seeded_and_capped(wc, wc_config):
    lists = wc.lists()
    chosen = wi.choose_lists(lists, "a girl", "a girl", wc_config, random.Random(1))
    a = wi.draw(chosen, wc.catalog(), 3, wc_config, seed=11)
    assert a.per_prompt == wi.draw(chosen, wc.catalog(), 3, wc_config, seed=11).per_prompt             # repeatable
    assert a.per_prompt != wi.draw(chosen, wc.catalog(), 3, wc_config, seed=12).per_prompt
    assert len({tuple(i["text"] for i in p) for p in a.per_prompt}) == 3                              # every prompt has its own
    assert all(len(p) <= wc_config.wildcards_ideas_per_prompt * wc_config.wildcards_entries_per_list for p in a.per_prompt)
    text = a.block_text()
    assert text.startswith("Prompt 1 — ") and "setting (background/background-location):" in text

    wc_config.wildcards_max_idea_entries = 20                                                         # a cap shrinks the draw
    small = wi.draw(chosen, wc.catalog(), 16, wc_config, seed=1)
    assert sum(len(p) for p in small.per_prompt) <= 20 and len(small.per_prompt) == 16 and len(small.lists) <= 1
    wc_config.wildcards_max_idea_entries = 120
    big = wi.draw(chosen, wc.catalog(), 16, wc_config, seed=1)
    assert sum(len(p) for p in big.per_prompt) <= 120 and all(p for p in big.per_prompt)
    wc_config.wildcards_max_idea_entries = 0
    assert wi.draw(chosen, wc.catalog(), 3, wc_config, seed=1).per_prompt == []


def test_nested_entries_are_resolved_when_drawn(wc, wc_config):
    lists = [l for l in wc.lists() if l.name == "random/rng-background"]
    drawn = wi.draw(lists, wc.catalog(), 1, wc_config, seed=3)
    assert drawn.per_prompt[0] and all("__" not in i["text"] and "{" not in i["text"] for i in drawn.per_prompt[0])


def test_used_ideas_are_the_offered_entries_found_in_the_prompt():
    offered = [{"list": "a", "role": "shot", "text": "dutch angle"}, {"list": "b", "role": "setting", "text": "banff national park"}, {"list": "c", "role": "palette", "text": "red"}]
    assert wi.used_ideas(offered, "1girl, Dutch Angle, hiking in Banff National Park, tired") == ["dutch angle", "banff national park"]


def test_make_ideas_is_none_when_off_or_empty(wc, wc_config):
    assert wi.make_ideas(wc, wc_config, "a girl", "a girl", 2, 1) is not None
    wc_config.wildcards_enabled = False
    assert wi.make_ideas(wc, wc_config, "a girl", "a girl", 2, 1) is None
    wc_config.wildcards_enabled = True
    assert wi.make_ideas(WildcardStore(Database(":memory:"), Config(data_dir=wc_config.data_dir / "x")), wc_config, "a girl", "a girl", 2, 1) is None
    assert wi.make_ideas(None, wc_config, "a girl", "a girl", 2, 1) is None


# ---------- import ----------

def test_import_copies_the_tree_and_never_touches_the_source(wc_config, source):
    (source / "notes.md").write_text("skip me", encoding="utf-8")
    (source / "colors.yaml").write_text("warm:\n  - red\n  - orange\n", encoding="utf-8")
    before = tree_hash(source)
    store = WildcardStore(Database(":memory:"), wc_config)
    result = store.import_folder(str(source))
    assert tree_hash(source) == before                                                         # the source is only read
    assert result["lists"] == 9 and result["skipped"] == 1 and result["entries"] > 30
    assert (wc_config.wildcards_dir / "background" / "background-location.txt").read_text(encoding="utf-8").startswith("banff national park")
    assert {l.name for l in store.lists()} >= {"background/background-location", "colors/warm"} and not any(l.name == "notes" for l in store.lists())


def test_reimport_replaces_the_copy_and_keeps_your_settings(wc_config, source):
    store = WildcardStore(Database(":memory:"), wc_config)
    store.import_folder(str(source))
    store.update("pose/pose", enabled=False, role="shot")
    (source / "pose" / "pose.txt").write_text("handstand\ncartwheel\n", encoding="utf-8")
    (source / "color" / "color-palette.txt").unlink()
    result = store.import_folder(str(source))
    pose = store.get("pose/pose")
    assert pose.entries == ["handstand", "cartwheel"] and pose.enabled is False and pose.role == "shot"      # new text, same settings
    assert result["removed"] == 1 and not (wc_config.wildcards_dir / "color" / "color-palette.txt").exists()


def test_import_refuses_missing_empty_and_its_own_copy(wc_config, source, tmp_path):
    store = WildcardStore(Database(":memory:"), wc_config)
    for bad, text in ((str(tmp_path / "nope"), "doesn't exist"), ("", "Enter the folder")):
        with pytest.raises(WildcardError, match=text):
            store.import_folder(bad)
    (tmp_path / "empty").mkdir()
    with pytest.raises(WildcardError, match="No wildcard lists"):
        store.import_folder(str(tmp_path / "empty"))
    store.import_folder(str(source))
    with pytest.raises(WildcardError, match="app's own copy"):
        store.import_folder(str(wc_config.wildcards_dir))


# ---------- API ----------

def test_wildcard_api_import_list_read_and_switch(app, source):
    c = app.client
    assert c.get("/api/wildcards").json()["lists"] == []
    done = c.post("/api/wildcards/import", json={"path": str(source)}).json()
    assert done["imported"]["lists"] == 8 and done["imported"]["entries"] > 30 and len(done["lists"]) == 8
    listing = c.get("/api/wildcards").json()
    row = next(l for l in listing["lists"] if l["name"] == "background/background-location")
    assert (row["folder"], row["role"], row["enabled"], row["count"]) == ("background", "setting", True, len(LOCATIONS))
    assert row["samples"] == LOCATIONS[:5] and row["source"] == str(source.resolve()) and "setting" in listing["roles"]
    assert c.get("/api/wildcards/background/background-location").json()["entries"] == LOCATIONS
    assert c.patch("/api/wildcards/camera/camera-shot", json={"enabled": False, "role": "pose"}).json()["enabled"] is False
    assert c.patch("/api/wildcard-folders/clothing", json={"enabled": False}).json()["lists"]
    after = {l["name"]: l for l in c.get("/api/wildcards").json()["lists"]}
    assert after["camera/camera-shot"]["role"] == "pose" and after["clothing/clothing"]["enabled"] is False
    assert c.patch("/api/wildcards/pose/pose", json={"role": "bogus"}).status_code == 400
    assert c.get("/api/wildcards/zzz/none").status_code == 404 and c.post("/api/wildcards/import", json={"path": "C:/nowhere-at-all"}).status_code == 400


def test_wildcard_settings_are_editable_and_validated(settings_client, toml_path):
    c = settings_client
    res = c.put("/api/config", json={"wildcards_enabled": False, "wildcards_ideas_per_prompt": 2, "wildcards_entries_per_list": 3})
    assert res.status_code == 200 and (res.json()["wildcards_enabled"], res.json()["wildcards_ideas_per_prompt"]) == (False, 2)
    assert c.get("/api/config").json()["wildcards_llm_picks_lists"] is True                      # the Settings page shows the switch
    assert c.app.state.config.wildcards_entries_per_list == 3 and "ideas_per_prompt = 2" in toml_path.read_text(encoding="utf-8")
    bad = c.put("/api/config", json={"wildcards_ideas_per_prompt": 99, "wildcards_enabled": "yes"})
    assert bad.status_code == 422 and set(bad.json()["errors"]) == {"wildcards_ideas_per_prompt", "wildcards_enabled"}


# ---------- in planning ----------

def import_into(app, source):
    app.client.post("/api/wildcards/import", json={"path": str(source)})
    app.state.planner.ctx.rng = random.Random(4)


def writer_message(app, n=0) -> str:
    return app.llama.of("write_prompts")[n]["messages"][1]["content"]


def test_ideas_reach_the_prompt_writer_and_are_recorded_per_prompt(app, source):
    import_into(app, source)
    app.script_plan({"description": "a girl in a city at night", "count": 3, "constraints": []})
    app.llama.scripts["write_prompts"] = lambda schema, body: {"prompts": [
        {"prompt": "1girl, city street, wearing a trench coat, dutch angle", "variation": "angle"},
        {"prompt": "1girl, neon sign, red kimono, leaning on a railing", "variation": "kimono"},
        {"prompt": "1girl, rain, hands in pockets", "variation": "rain"}], "negative_additions": [], "reason": "ok"}
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    message = writer_message(app)
    assert "## Wildcard ideas" in message and "Prompt 1 — " in message and "Prompt 3 — " in message and "<<<BEGIN DATA: wildcard ideas>>>" in message
    items = job["fields"]["prompts"]["items"]
    assert all(i["ideas"]["offered"] for i in items) and len({tuple(o["text"] for o in i["ideas"]["offered"]) for i in items}) == 3
    offered = {o["text"] for i in items for o in i["ideas"]["offered"]}
    assert all(o["role"] in ("shot", "outfit", "pose", "palette") for i in items for o in i["ideas"]["offered"])        # the city fixes the setting
    for item, text in zip(items, job["fields"]["prompts"]["value"]):
        assert set(item["ideas"]["used"]) == {o["text"] for o in item["ideas"]["offered"] if o["text"].lower() in text.lower()} <= offered


def test_nothing_changes_when_wildcards_are_off_or_not_imported(app, source):
    app.script_plan({"description": "a girl in a city", "count": 2})
    app.plan(on="tagwf")                                                               # nothing imported
    assert "## Wildcard ideas (optional ingredients)\n(none)" in writer_message(app, 0)
    assert all("ideas" not in i for i in app.job(1)["fields"]["prompts"]["items"])
    import_into(app, source)
    app.state.config.wildcards_enabled = False
    app.plan(on="tagwf")
    assert "## Wildcard ideas (optional ingredients)\n(none)" in writer_message(app, 1)


def test_full_prompt_with_16_prompts_stays_well_inside_the_context(app, source):
    """Worst case: 16 prompts (written in chunks of 6), a long request, the limits at their defaults. Every write_prompts message,
    also the last chunk that sees the 12 prompts before it, has to fit 8192 tokens with room for the reply (3000), so the input
    must stay far below ~5000 tokens (about 3 characters each)."""
    import_into(app, source)
    for n in range(4):      # a big library, so every draw is full
        (app.state.config.wildcards_dir / "background" / f"big{n}.txt").write_text("\n".join(f"{w} place number {i} with details" for i, w in enumerate(["a"] * 300)) + "\n", encoding="utf-8")
    request = "16 images of " + "a girl walking through a city at night with neon signs and rain, " * 6
    app.script_plan({"description": "a girl in a city at night with neon signs and rain " * 3, "count": 16, "constraints": ["neon", "rain", "night", "35mm"]})
    app.plan(request, on="tagwf")
    assert len(app.llama.of("write_prompts")) == 3                                     # 6 + 6 + 4
    for n, body in enumerate(app.llama.of("write_prompts")):
        sent = sum(len(m["content"]) for m in body["messages"])
        ideas_part = writer_message(app, n).split("## Wildcard ideas (optional ingredients)")[1].split("## Tokens to keep")[0]
        assert ideas_part.count("Prompt ") == (6, 6, 4)[n] and sent / 3 < 4500, (n, sent, len(ideas_part))
        assert len(ideas_part) < 4500                                                 # cut first: the ideas never grow past their cap


def test_explicit_tokens_survive_in_the_prompts(app, source):
    import_into(app, source)
    token = "__background/background-location__"
    app.script_plan({"description": "a cat sitting", "count": 2, "constraints": []})           # the planner dropped the token
    app.llama.scripts["write_prompts"] = {"prompts": [{"prompt": "a cat sitting in a park", "variation": "a"}, {"prompt": f"a cat on a wall, {token}", "variation": "b"}],
                                          "negative_additions": [], "reason": "ok"}
    job = app.job(app.plan(f"a cat sitting in {token}", on="tagwf")["job_ids"][0])
    assert token in job["meta"]["constraints"][0]                                    # kept as a constraint of the job
    assert "Tokens to keep word for word\n" + token in writer_message(app)
    prompts = job["fields"]["prompts"]["value"]
    assert all(token in p for p in prompts) and prompts[0].endswith(token)           # the dropped one was put back at the end
    assert "put back" in job["fields"]["prompts"]["note"]
    offered = {o["role"] for i in job["fields"]["prompts"]["items"] for o in i.get("ideas", {}).get("offered", [])}
    assert "setting" not in offered and "random" not in offered                      # the token fills the setting, its words pull nothing in
    texts = [i["recipe"]["prompt"] for i in job["images"]]
    assert all("__" not in t and any(loc in t for loc in LOCATIONS) for t in texts)
    assert all(i["recipe"]["prompt_template"] == p for i, p in zip(job["images"], prompts))


def test_tokens_expand_per_image_from_the_images_seed(wc):
    wf = {"id": "w", "title": "W", "slots": {"defaults": {}}}
    fields = {"prompts": {"value": ["a cat in __background/background-location__, {red|blue} collar"]}, "seed": {"value": 100}, "seed_strategy": {"value": "increment"}}
    again = build_recipes(wf, fields, 12, False, wc.catalog())
    recipes = build_recipes(wf, fields, 12, False, wc.catalog())
    assert [r["prompt"] for r in recipes] == [r["prompt"] for r in again]              # the seed decides
    assert len({r["prompt"] for r in recipes}) > 4                                      # each image its own pick
    for r in recipes:
        assert r["prompt_template"] == fields["prompts"]["value"][0] and r["prompt"] != r["prompt_template"] and "__" not in r["prompt"]
        assert r["prompt"] == expand_for_image(r["prompt_template"], wc.catalog(), r["seed"])
    plain = build_recipes(wf, {**fields, "prompts": {"value": ["a plain prompt"]}}, 2, False, wc.catalog())
    assert all("prompt_template" not in r for r in plain)


async def test_an_unknown_token_needs_attention_and_never_reaches_comfyui(store, library, wc, comfy):
    wf = await import_fixture(library, TAGWF)
    store.wildcards = wc
    job = store.create_job(wf["id"], 2, {"prompts": ["a cat, __background/background-location__"]})
    assert all("__" not in i["recipe"]["prompt"] for i in job["images"]) and job["images"][0]["recipe"]["prompt_template"]
    with pytest.raises(JobError, match="__no_such_list__|no_such_list") as e:
        store.create_job(wf["id"], 1, {"prompts": ["a cat in __no_such_list__"]})
    assert "Import your wildcard lists" in str(e.value) and e.value.field == "prompts"
    ok = store.update_fields(job["id"], {"prompts": {"value": ["a cat in __no_such_list__"], "source": "locked"}})
    assert ok["status"] == "draft" and ok["images"] == []
    issue = next(i for i in ok["issues"] if i["field"] == "prompts")
    assert issue["severity"] == "blocking" and "no_such_list" in issue["message"]
    with pytest.raises(JobError):
        store.queue_job(job["id"])
    fixed = store.update_fields(job["id"], {"prompts": {"value": ["a cat in __camera-shot__"], "source": "locked"}})
    assert fixed["status"] == "ready" and fixed["images"][0]["recipe"]["prompt"].startswith("a cat in ")


async def test_a_token_that_fills_in_blocked_content_is_blocked(store, library, wc):
    wf = await import_fixture(library, TAGWF)
    store.wildcards = wc
    (wc.root / "bad.txt").write_text(f"a {MOD[1]} girl, {CONTENT[0]}\n", encoding="utf-8")
    assert store.create_job(wf["id"], 1, {"prompts": ["portrait, __bad__"]})["id"]
    job = store.get_job(1)
    with pytest.raises(JobError, match=CSE_MESSAGE):
        store.queue_job(job["id"])


def test_hand_edited_prompts_keep_their_ideas_and_recount_the_used_ones():
    from app.queue.jobs import _prompts_edit
    old = {"value": ["one", "two"], "items": [{"reason": "a", "ideas": {"offered": [{"list": "x", "role": "shot", "text": "dutch angle"}], "used": []}}, {"reason": "b"}]}
    new = _prompts_edit(old, {"value": ["one, dutch angle", "two"]})
    assert new["items"][0]["reason"] == "Edited by you" and new["items"][0]["ideas"]["used"] == ["dutch angle"]


def test_tweak_gets_ideas_only_when_asked_for_something_different(app, source):
    import_into(app, source)
    app.script_plan({"description": "a girl in rain", "count": 2})
    job_id = app.plan(on="tagwf")["job_ids"][0]
    app.llama.scripts["apply_tweak"] = {"prompts": ["masterpiece, best quality, 1girl, mount fuji at dawn", "masterpiece, best quality, 1girl, a quiet harbor"], "reason": "new place"}
    app.wait(app.client.post(f"/api/jobs/{job_id}/tweak", json={"instruction": "darker"}).json()["id"])
    assert "(none)" in app.llama.of("apply_tweak")[-1]["messages"][1]["content"].split("## Wildcard ideas (optional ingredients)")[1].split("## What the card allows")[0]
    app.wait(app.client.post(f"/api/jobs/{job_id}/tweak", json={"instruction": "a different background"}).json()["id"])
    message = app.llama.of("apply_tweak")[-1]["messages"][1]["content"]
    assert "Prompt 1 — " in message and "<<<BEGIN DATA: wildcard ideas>>>" in message
    items = app.job(job_id)["fields"]["prompts"]["items"]
    assert all(i["ideas"]["offered"] and set(i["ideas"]["used"]) <= {o["text"] for o in i["ideas"]["offered"]} for i in items)
