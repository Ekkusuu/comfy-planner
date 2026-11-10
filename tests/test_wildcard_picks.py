"""The LLM picks the wildcard lists (pick_wildcards) and exact phrase matches inside lists."""
import random

import pytest

from app.config import Config
from app.planning import pick_wildcards as pw
from app.wildcards import ideas as wi
from app.wildcards.store import WildList
from tests.test_planning_flow import app, REQUEST  # noqa: F401  (the planning app fixture)
from tests.test_wildcards import import_into, source, wc, wc_config  # noqa: F401  (fixtures and helpers)


def add_list(config: Config, name: str, entries: list[str]):
    path = config.wildcards_dir / f"{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")


def realistic(wc, config):
    """Lists that the plain keyword search would match against anything: a themes list whose name hides "steampunk", and
    body / nsfw lists with long entry lists full of ordinary words."""
    add_list(config, "concept/themes", ["steampunk", "cyberpunk", "noir detective", "vaporwave", "dieselpunk"])
    add_list(config, "body-parts/body-type", ["woman", "tall", "cat ears", "long hair", "in the garden", "a lot of", "girl", "wet", "large breasts"])
    add_list(config, "NSFW/nsfw-scene", ["on top of", "in the garden shed", "cat", "a girl", "glowing"])
    add_list(config, "body-parts/eyes", ["glowing", "wet"])
    add_list(config, "state/state-adjective", ["glowing", "moody"])
    add_list(config, "concept/descriptors", ["glowing", "handsome"])
    return wc.lists()


def names(lists) -> list[str]:
    return [l.name for l in lists]


# ---------- B: phrase matches ----------

def test_only_distinctive_entries_count_as_phrases():
    for entry in ("steampunk", "cat ears", "noir detective", "Mount Fuji"):
        assert wi.distinctive(entry), entry
    for entry in ("cat", "girl", "woman", "wet", "in the", "a lot of", "__background/x__", "{a|b}", "on top of", "1000", "dog"):
        assert wi.distinctive(entry) == "", entry
    assert wi.brief_phrases("A steampunks, in the-city")  >= {"steampunks", "steampunk", "in the city", "city"}


def test_a_list_is_found_by_an_entry_even_if_its_name_does_not_say_so(wc, wc_config):
    lists = realistic(wc, wc_config)
    for brief in ("a steampunk girl", "two steampunks at a harbor party"):
        chosen = wi.choose(lists, brief, brief, wc_config, random.Random(1))
        themes = next(c for c in chosen if c.wild.name == "concept/themes")
        assert themes.by == "phrase" and "steampunk" in themes.why
    first = wi.choose(lists, "a steampunk girl", "a steampunk girl", wc_config, random.Random(1))[0]
    assert first.wild.name == "concept/themes"                          # phrase matches come before keyword matches


def test_a_cat_in_a_garden_does_not_offer_body_or_nsfw_lists(wc, wc_config):
    lists = realistic(wc, wc_config)
    for brief in ("a cat in a garden", "a woman in a garden at night", "a girl with a cat"):
        for seed in range(6):
            chosen = wi.choose_lists(lists, brief, brief, wc_config, random.Random(seed))
            assert not {l.role for l in chosen} & {"body", "nsfw", "concept"}, (brief, names(chosen))
    # ... but an entry that is distinctive and in the brief does pull the list in, whatever its role
    chosen = wi.choose(lists, "a girl with cat ears", "a girl with cat ears", wc_config, random.Random(1))
    assert chosen[0].wild.name == "body-parts/body-type" and chosen[0].by == "phrase"


def test_a_phrase_that_many_lists_contain_is_too_general_to_pick_one(wc, wc_config):
    lists = realistic(wc, wc_config)                                                   # "glowing" is in four lists
    brief = "a mermaid with glowing jellyfish"
    assert wi.phrase_matches(lists, brief) == []
    chosen = wi.choose(lists, brief, brief, wc_config, random.Random(1))
    assert {"body", "nsfw", "concept", "state"}.isdisjoint(c.wild.role for c in chosen)
    assert [(l.name, h) for l, h in wi.phrase_matches(lists, "a moody scene")] == [("state/state-adjective", "moody")]


def test_a_phrase_match_in_a_slot_list_means_the_aspect_is_given_not_offered(wc, wc_config):
    lists = wc.lists()
    brief = "a girl at a quiet harbor"           # an entry of the setting list
    chosen = wi.choose(lists, brief, brief, wc_config, random.Random(1))
    assert "setting" not in {c.wild.role for c in chosen}
    assert wi.phrase_hits(next(l for l in lists if l.role == "setting"), wi.brief_phrases(brief)) == {"a quiet harbor"}
    # a non-slot list with a phrase match is offered, the longest phrase first
    add_list(wc_config, "style/other-style", ["brass gears", "steam pipes and brass gears"])
    two = wi.choose(wc.lists(), "brass gears and steam pipes and brass gears", "", wc_config, random.Random(1))
    assert [c.by for c in two][:1] == ["phrase"] and two[0].wild.name == "style/other-style" and len({c.wild.name for c in two}) == len(two)


def test_choose_takes_the_llm_picks_first_checks_them_and_fills_the_rest(wc, wc_config):
    lists = realistic(wc, wc_config)
    brief = "a girl in a city"                                          # the city fixes the setting
    picks = [("background/background-location", "nice places"), ("concept/themes", "adventurous theme"), ("style/style-steampunk", "brass look"),
             ("pose/pose", "more life"), ("does/not-exist", "made up"), ("camera/camera-shot", "framing")]
    chosen = wi.choose(lists, brief, brief, wc_config, random.Random(1), picks)
    assert [(c.wild.name, c.by) for c in chosen[:3]] == [("concept/themes", "llm"), ("style/style-steampunk", "llm"), ("pose/pose", "llm")]
    assert chosen[0].why == "adventurous theme" and len(chosen) == 4                      # setting dropped (given), made up dropped
    assert chosen[3].wild.name == "camera/camera-shot"                                   # the fourth place is the LLM's next valid pick
    wc.update("pose/pose", enabled=False)
    again = wi.choose(wc.lists(), brief, brief, wc_config, random.Random(1), picks)
    assert "pose/pose" not in names(c.wild for c in again)                              # a list switched off is never offered
    twice = wi.choose(lists, brief, brief, wc_config, random.Random(1), [("concept/themes", "a"), ("concept/themes", "b"), ("style/style-steampunk", "c")])
    assert names(c.wild for c in twice[:2]) == ["concept/themes", "style/style-steampunk"]    # no list twice
    fewer = wi.choose(lists, brief, brief, wc_config, random.Random(1), [("concept/themes", "x")])
    assert len(fewer) == 4 and fewer[0].by == "llm" and {c.by for c in fewer[1:]} <= {"phrase", "keyword", "slot"}      # the rest is filled in


# ---------- A: the catalog ----------

def synthetic(n_lists=300, n_entries=400) -> list[WildList]:
    roles = ["setting", "shot", "palette", "outfit", "pose", "style", "artist", "material", "concept", "state", "body", "nsfw"]
    lists = []
    for i in range(n_lists):
        entries = [f"{'wordy ' * 8}entry {i} {j} with some long extra text" for j in range(n_entries)]
        lists.append(WildList(f"{roles[i % len(roles)]}/folder-{i // len(roles)}/a-long-list-name-{i}", roles[i % len(roles)], roles[i % len(roles)], "other", True, entries))
    return lists


def test_catalog_has_every_list_once_and_stays_inside_its_size_cap():
    config = Config()
    small = [WildList("setting/places", "setting", "setting", "setting", True, ["banff", "__other__", "{a|b}", "a mossy forest", "a night market", "a rooftop", "an old library"])]
    text, shown = pw.build_catalog(small, config)
    assert shown == small and text.startswith("[setting]\nsetting/places (7): ") and "__other__" not in text and "{a|b}" not in text
    assert text == pw.build_catalog(small, config)[0]                                   # the same sample every time
    plain_first = pw.samples_of(small[0], 4, 40)
    assert len(plain_first) == 4 and plain_first[0] == "banff" and len(set(plain_first)) == 4
    assert pw.samples_of(WildList("a", "", "x", "x", True, ["__a__", "{b|c}"]), 4, 40) == []
    assert all(len(s) <= 6 for s in pw.samples_of(small[0], 4, 5))                      # cut to the width (plus the ellipsis)

    big = synthetic(150, 400)
    text, shown = pw.build_catalog(big, config)
    assert len(text) <= config.wildcards_catalog_chars and len(shown) == 150
    assert all(f"{l.name} (" in text for l in big)                                      # every list stays, whatever else is cut
    config.wildcards_catalog_chars = 10 ** 6
    assert pw.build_catalog(big[:20], config)[0] == pw.render_catalog(big[:20], 4, 40, set())      # room to spare: the fullest samples


def test_a_library_too_big_even_for_bare_names_loses_lists_but_not_roles():
    config = Config()
    huge = synthetic(600, 5)
    text, shown = pw.build_catalog(huge, config)
    assert len(text) <= config.wildcards_catalog_chars and 0 < len(shown) < 600
    assert {l.role for l in shown} == {l.role for l in huge} and all(f"{l.name} (" in text for l in shown)


def test_catalog_shrinks_in_steps():
    lists = synthetic(60, 50)
    config = Config()
    sizes = [len(pw.render_catalog(lists, c, w, set())) for c, w in pw.SAMPLE_LEVELS]
    assert sizes == sorted(sizes, reverse=True)
    config.wildcards_catalog_chars = sizes[1]                                           # only the second level fits
    assert pw.build_catalog(lists, config)[0] == pw.render_catalog(lists, *pw.SAMPLE_LEVELS[1], set())
    config.wildcards_catalog_chars = sizes[-1] - 1                                      # even the smallest samples don't: biggest roles lose theirs
    text, shown = pw.build_catalog(lists, config)
    assert len(shown) == len(lists) and len(text) <= sizes[-1] - 1 and text.count(": ") < sizes[-1] and all(f"{l.name} (" in text for l in lists)


def test_the_real_sized_catalog_fits_the_budget(wc, wc_config):
    """About 140 real-looking lists: the whole pick call stays far inside the context of 8192 tokens (3 characters per token)."""
    for n in range(130):
        add_list(wc_config, f"{['concept', 'state', 'NSFW', 'body-parts', 'artist-group'][n % 5]}/list-{n}-with-a-long-descriptive-name",
                 [f"entry number {i} of list {n} with words" for i in range(80)])
    lists = [l for l in wc.lists() if l.enabled and l.entries]
    assert len(lists) > 130
    assert len(pw.build_catalog(lists, wc_config)[0]) <= wc_config.wildcards_catalog_chars <= 9000


# ---------- A: in planning ----------

LISTS = ["background/background-location", "camera/camera-shot", "clothing/clothing", "pose/pose", "color/color-palette",
         "artist-group/artist-painting", "style/style-steampunk", "random/rng-background"]


def script_picks(app, *picks, fail=False):
    reply = {"picks": [{"list_name": n, "reason": why} for n, why in picks]}
    app.llama.scripts["pick_wildcards"] = {"picks": "broken"} if fail else reply


def plan_girl(app, description="a girl", count=2, request=None, **kw):
    app.script_plan({"description": description, "count": count, "constraints": []})
    return app.plan(request or description, on="tagwf", **kw)


def pick_message(app, n=-1) -> str:
    return app.llama.of("pick_wildcards")[n]["messages"][1]["content"]


def offered_lists(app, job_id) -> list[dict]:
    return app.job(job_id)["fields"]["prompts"]["items"][0]["ideas"]["lists"]


def test_the_llm_picks_the_lists_and_the_choice_is_recorded_with_its_reason(app, source):
    import_into(app, source)
    script_picks(app, ("style/style-steampunk", "brass and gears suit it"), ("artist-group/artist-painting", "painterly variety"), ("pose/pose", "more life"))
    plan = plan_girl(app, "a moody girl, surprise me", request="2 images of a moody girl, surprise me")
    assert plan["status"] == "done"
    lists = offered_lists(app, plan["job_ids"][0])
    assert [(l["list"], l["by"]) for l in lists][:2] == [("style/style-steampunk", "llm"), ("pose/pose", "llm")]
    assert lists[0]["why"] == "brass and gears suit it" and lists[0]["role"] == "style" and len(lists) == 4 and lists[2]["by"] in ("slot", "keyword", "phrase")
    assert all(l["role"] != "artist" for l in lists)                                    # no artist list unless one was asked for
    call = app.llama.of("pick_wildcards")
    assert len(call) == 1                                                                # once per job, not per prompt
    schema = app.llama.schema_of("pick_wildcards")
    assert schema["properties"]["picks"]["maxItems"] == 4
    enum = schema["properties"]["picks"]["items"]["properties"]["list_name"]["enum"]
    assert set(enum) == set(LISTS) - {"artist-group/artist-painting"}
    message = pick_message(app)
    assert "<<<BEGIN DATA: wildcard lists>>>" in message and "style/style-steampunk (5): " in message and "brass gears" in message
    assert "moody girl, surprise me" in message and "Anima" in message and "Choose up to 4 lists" in message
    assert "Prompt 1 — " in writer(app) and "style (style/style-steampunk)" in writer(app)       # the picked list's ideas reach the writer
    assert [t["step"] for t in plan["timings"]] == ["loras", "wildcards", "scene", "prompts", "settings"]
    assert all(t["seconds"] >= 0 for t in plan["timings"])


def writer(app, n=0) -> str:
    return app.llama.of("write_prompts")[n]["messages"][1]["content"]


def test_picks_for_an_aspect_the_request_decides_are_dropped_and_the_catalog_leaves_them_out(app, source):
    import_into(app, source)
    script_picks(app, ("background/background-location", "nice places"), ("style/style-steampunk", "brass look"), ("random/rng-background", "same role as the first"))
    plan = plan_girl(app, "a girl in a city at night")                                  # "city" fixes the setting
    lists = offered_lists(app, plan["job_ids"][0])
    assert "background/background-location" not in [l["list"] for l in lists] and lists[0]["list"] == "style/style-steampunk"
    assert "background/background-location" not in pick_message(app) and "[setting]" not in pick_message(app)
    enum = app.llama.schema_of("pick_wildcards")["properties"]["picks"]["items"]["properties"]["list_name"]["enum"]
    assert "background/background-location" not in enum and "style/style-steampunk" in enum


def test_one_list_per_role_and_made_up_names_are_ignored(app, source):
    import_into(app, source)
    script_picks(app, ("style/style-steampunk", "a"), ("style/style-steampunk", "again"), ("pose/pose", "b"))
    roles = [l["role"] for l in offered_lists(app, plan_girl(app)["job_ids"][0])]
    assert len(roles) == len(set(roles)) and roles[:2] == ["style", "pose"]
    # a name outside the enum can't come from the model; if one did get through, code ignores it
    assert pw.picking_lists(app.state.planner.ctx)


def test_the_llm_failing_falls_back_to_phrase_keyword_and_slot_rules(app, source):
    import_into(app, source)
    script_picks(app, fail=True)
    plan = plan_girl(app, "a steampunk girl")
    assert plan["status"] == "done" and not plan.get("error")
    lists = offered_lists(app, plan["job_ids"][0])
    assert lists[0]["list"] == "style/style-steampunk" and lists[0]["by"] in ("phrase", "keyword") and len(lists) == 4
    assert {l["by"] for l in lists} <= {"phrase", "keyword", "slot"}
    assert len(app.llama.of("pick_wildcards")) == 2                                      # the client's one retry, then the fallback
    assert not plan["job_ids"] or app.job(plan["job_ids"][0])["status"] != "failed"


def test_without_a_pick_the_old_rules_decide(app, source):
    import_into(app, source)
    script_picks(app)                                                                    # the LLM found nothing
    lists = offered_lists(app, plan_girl(app, "a girl")["job_ids"][0])
    assert {l["by"] for l in lists} == {"slot"} and len(lists) == 4


@pytest.mark.parametrize("how", ["disabled", "switch_off", "no_lists", "none_per_prompt", "not_imported"])
def test_no_llm_call_when_there_is_nothing_to_choose(app, source, how):
    if how != "not_imported":
        import_into(app, source)
    config = app.state.config
    if how == "disabled":
        config.wildcards_enabled = False
    elif how == "switch_off":
        config.wildcards_llm_picks_lists = False
    elif how == "none_per_prompt":
        config.wildcards_ideas_per_prompt = 0
    elif how == "no_lists":
        for l in app.state.planner.ctx.wildcards.lists():
            app.state.planner.ctx.wildcards.update(l.name, enabled=False)
    plan = plan_girl(app)
    assert app.llama.of("pick_wildcards") == [] and "wildcards" not in [t["step"] for t in plan["timings"]]
    assert [t["step"] for t in plan["timings"]] == ["loras", "scene", "prompts", "settings"]
    if how == "switch_off":                                                              # ideas still come, by the keyword rules alone
        assert "Prompt 1 — " in writer(app) and {l["by"] for l in offered_lists(app, plan["job_ids"][0])} == {"slot"}


def test_locked_prompts_skip_the_pick_step():
    from app.planning.prompts import writes_prompts
    assert not writes_prompts({"count": 2}, {"prompts": {}}, None)            # all prompts locked
    assert not writes_prompts({"count": 2}, {}, {0: {}, 1: {}})                 # every prompt kept one by one
    assert writes_prompts({"count": 2}, {}, {0: {}})


def test_a_tweak_for_something_new_also_lets_the_llm_choose_lists(app, source):
    import_into(app, source)
    job_id = plan_girl(app, "a girl in rain")["job_ids"][0]
    before = len(app.llama.of("pick_wildcards"))
    app.llama.scripts["apply_tweak"] = {"prompts": ["masterpiece, best quality, 1girl, dark rain", "masterpiece, best quality, 1girl, dark street"], "reason": "darker"}
    script_picks(app, ("style/style-steampunk", "a new look"))
    app.wait(app.client.post(f"/api/jobs/{job_id}/tweak", json={"instruction": "darker"}).json()["id"])
    assert len(app.llama.of("pick_wildcards")) == before                                 # no ideas offered, no extra call
    app.llama.scripts["apply_tweak"] = {"prompts": ["masterpiece, best quality, 1girl, brass gears", "masterpiece, best quality, 1girl, steam pipes"], "reason": "new look"}
    plan = app.wait(app.client.post(f"/api/jobs/{job_id}/tweak", json={"instruction": "something new, surprise me"}).json()["id"])
    assert len(app.llama.of("pick_wildcards")) == before + 1
    message = pick_message(app)
    assert "something new, surprise me" in message and "a girl in rain" in message
    lists = app.job(job_id)["fields"]["prompts"]["items"][0]["ideas"]["lists"]
    assert lists[0] == {"list": "style/style-steampunk", "role": "style", "by": "llm", "why": "a new look"}
    assert [t["step"] for t in plan["timings"]] == ["wildcards", "tweak"]


def test_the_switch_is_a_setting(app):
    from app.settings import validate
    assert validate({"wildcards_llm_picks_lists": False}, app.state.config) == {"wildcards_llm_picks_lists": False}
    with pytest.raises(Exception):
        validate({"wildcards_llm_picks_lists": "no"}, app.state.config)
    assert Config().wildcards_llm_picks_lists is True


def test_hand_edits_keep_which_lists_the_ideas_came_from():
    from app.queue.jobs import _prompts_edit
    lists = [{"list": "style/style-steampunk", "role": "style", "by": "llm", "why": "brass look"}]
    old = {"value": ["one"], "items": [{"reason": "a", "ideas": {"offered": [{"list": "style/style-steampunk", "role": "style", "text": "brass gears"}], "used": [], "lists": lists}}]}
    new = _prompts_edit(old, {"value": ["one, brass gears"]})
    assert new["items"][0]["ideas"] == {"offered": old["items"][0]["ideas"]["offered"], "used": ["brass gears"], "lists": lists}


def test_lists_with_an_entry_from_the_request_are_marked_in_the_catalog(app, source):
    import_into(app, source)
    add_list(app.state.config, "concept/themes", ["steampunk", "cyberpunk", "vaporwave"])
    plan_girl(app, "a steampunk girl")
    message = pick_message(app)
    assert "concept/themes (3): steampunk | cyberpunk | vaporwave  ← has “steampunk”" in message
    assert message.count("← has “") == 1                                                # only that list
