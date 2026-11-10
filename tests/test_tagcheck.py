"""The tag library and the tag check, with a small fake library."""
from tests.content_probes import CONTENT_TERMS as CONTENT, MODIFIERS as MOD, NUMBERS as NUM, SUBJECT_TERMS as SUBJ
from app.planning.tagcheck import FAMILY_RULES, CheckContext, check_prompt, put_in_order, wanted_tags
from app.fields import split_tags, tag_key
from app.planning.taglib import TagLib, key, parse_tags, spaced

PREFIX = ["masterpiece", "best quality", "score_9", "newest"]
ROWS = """1girl,0,8000000,"sole_female,1girls"
solo,0,7000000,
multiple_girls,0,2000000,
2girls,0,1400000,"2girl"
3girls,0,330000,3girl
6+girls,0,108000,"6girls,7girls"
1boy,0,2000000,
2boys,0,300000,
multiple_boys,0,500000,
no_humans,0,237000,no_human
old_man,0,15000,
long_hair,0,6000000,"/lh,longhair"
looking_at_viewer,0,4900000,
park,0,60000,
tree,0,900000,"trees"
sitting,0,1300000,"sit"
blush,0,2000000,blushing
one_eye_closed,0,500000,wink
kimono,0,67846,
lace,0,49912,
hotel_room,0,824,
night,0,172854,
rain,0,49210,
red_dress,0,30000,
dress,0,40000,
black_kimono,0,10000,
velden_mara,1,47,
quillon,1,9000,
aria_vale,4,146000,
aria_vale_(starlight),4,10,
highres,5,8000000,
""" + f"{SUBJ[0]},0,100000,\n"
IMPLICATIONS = [("2girls", "multiple_girls"), ("3girls", "multiple_girls"), ("6+girls", "multiple_girls"), ("2boys", "multiple_boys")]
OPEN = {"girls": None, "boys": None, "others": None, "no_humans": None}


def lib() -> TagLib:
    tags, aliases = parse_tags(ROWS)
    return TagLib(tags, aliases, IMPLICATIONS)


def cc(family="Anima", kind="tags", subjects=None, typed="", protected=PREFIX, library=None, **kw) -> CheckContext:
    return CheckContext(library or lib(), FAMILY_RULES.get(family), kind, list(protected), typed, subjects, family=family, **kw)


def run(text, **kw):
    return check_prompt(text, cc(**kw))


def test_parse_and_lookup():
    L = lib()
    assert L.lookup("Long Hair").name == "long_hair"
    assert L.lookup("1girls").via == "alias" and L.lookup("1girls").name == "1girl"
    assert L.lookup("/lh") is None                     # shortcuts are not aliases
    assert L.artist("Mara Velden").name == "velden_mara"      # the name in either order
    assert L.parents("3girls") == ["multiple_girls"]
    assert key(" Long  hair_") == "long_hair" and spaced("long_hair") == "long hair"


def test_exact_tags_and_alias():
    r = run("masterpiece, best quality, score_9, newest, 1girls, long_hair, wink, sit, trees")
    assert r.text == "masterpiece, best quality, score_9, newest, 1girl, long hair, one eye closed, sitting, tree"
    froms = {c["from"] for c in r.changes}
    assert "wink" in froms
    assert not froms & {"long_hair", "trees", "sit", "1girls"}       # a format change and a plural are not news


def test_made_up_count_form_and_artist_on_anima():
    r = run("masterpiece, best quality, score_9, newest, 1girl, 1old man, by Mara Velden, 3 girls", typed="by Mara Velden")
    assert "old man" in r.text and "1old man" not in r.text
    assert "@velden mara" in r.text and "by Mara" not in r.text
    assert "3girls" in r.text and "multiple girls" in r.text
    assert any(c["from"] == "1old man" and c["to"] == "old man" for c in r.changes)


def test_artist_form_only_on_a_family_that_wants_at():
    r = run("1girl, by Mara Velden, quillon", family="Illustrious", typed="by Mara Velden")
    assert "by Mara Velden" in r.text and "@" not in r.text
    assert run("1girl, quillon, sea foam", family="Anima").text == "1girl, sea foam"       # unasked: an artist tag is removed
    assert run("1girl, quillon, sea foam", family="Anima", typed="quillon").text == "1girl, quillon, sea foam"   # typed by you: kept


def test_underscores_become_spaces_on_anima_only():
    assert run("1girl, long_hair, looking_at_viewer, score_9, o_o").text == "1girl, long hair, looking at viewer, score_9, o_o"
    kept = run("1girl, long_hair, looking at viewer", family="Illustrious").text
    assert kept == "1girl, long_hair, looking at viewer"           # Illustrious: the LLM's form is kept


def test_families_not_in_the_table_get_only_universal_checks():
    r = run("1girls, long_hair, long_hair, 1old man, by Mara Velden", family="Pony")
    assert r.text == "1girls, long_hair, 1old man, by Mara Velden"      # no lookup, no Anima rules; the duplicate is gone
    assert run("1girls, long_hair", family="other").text == "1girls, long_hair"
    assert run("1girl, solo", family="Pony", subjects={**OPEN, "girls": 3}).text == "1girl, solo"       # no count tags either


def test_sentences_are_not_checked():
    text = "A girl sitting under a tree, 1girls, long_hair"
    assert run(text, kind="sentences").text == text


def test_mixed_checks_only_short_pieces():
    text = "long_hair, a girl with long hair sitting on the grass at the park and smiling, 1girls"
    r = run(text, kind="mixed")
    assert r.text == "long hair, a girl with long hair sitting on the grass at the park and smiling, 1girl"


def test_typed_protected_and_tokens_and_weights():
    r = run("1girl, 1old man, __pose__, (trees:1.4), (long_hair:1.6)", typed="please 1old man")
    assert "1old man" in r.text and "__pose__" in r.text
    assert "(tree:1.4)" in r.text and "(long hair:1.6)" in r.text      # the fix goes inside the weight


def test_protected_prefix_triggers_and_styles_untouched():
    p = PREFIX + ["ink7cm_Paint", "@pine-owl"]
    r = run("masterpiece, best quality, score_9, newest, @pine-owl, ink7cm_Paint, ink7cm-Paint, 1girl, ink7cm Paint", protected=p)
    assert r.text.count("ink7cm_Paint") == 1 and "ink7cm-Paint" not in r.text and "ink7cm Paint" not in r.text
    assert "@pine-owl" in r.text and r.text.startswith("masterpiece, best quality, score_9, newest")


def test_duplicates_removed_weighted_wins():
    r = run("1girl, long hair, (long_hair:1.3), 1girl")
    assert r.text == "1girl, (long hair:1.3)"


def test_subjects_give_count_tags():
    r = run("masterpiece, best quality, score_9, newest, solo, 1girl, park", subjects={**OPEN, "girls": 3})
    assert r.text == "masterpiece, best quality, score_9, newest, 3girls, multiple girls, park"
    r = run("masterpiece, best quality, score_9, newest, park", subjects={**OPEN, "girls": 8, "boys": 0})
    assert "6+girls, multiple girls" in r.text and "multiple boys" not in r.text
    r = run("masterpiece, best quality, score_9, newest, 2girls, 1boy, solo", subjects={**OPEN, "girls": 1, "boys": 2})
    assert "1girl" in r.text and "2boys, multiple boys" in r.text and "solo" not in r.text and "2girls" not in r.text
    r = run("masterpiece, best quality, score_9, newest, 1girl, forest", subjects={"girls": 0, "boys": 0, "others": 0, "no_humans": True})
    assert "no humans" in r.text and "1girl" not in r.text


def test_open_subjects_force_nothing():
    assert run("1girl, solo, park", subjects=OPEN).text == "1girl, solo, park"
    assert run("1girl, solo, park", subjects=None).text == "1girl, solo, park"
    assert run("1girl, park", subjects={**OPEN, "boys": 1}).text.count("1girl") == 1       # girls are open: the 1girl stays


def test_typed_tag_survives_subjects():
    r = run("score_9, solo, 3girls", subjects={**OPEN, "girls": 3}, typed="solo", protected=["score_9"])
    assert "solo" in r.text


def test_count_tag_gets_its_implied_parent():
    assert run("3girls, park").text == "3girls, multiple girls, park"
    assert run("3girls, multiple girls").text == "3girls, multiple girls"
    assert run("1girl, park").text == "1girl, park"


def test_conflicts_are_reported_when_subjects_are_open():
    r = run("1girl, 3girls, solo, park")
    assert r.problems and any("solo" in p or "counts" in p for p in r.problems)
    assert run("2girls, no humans").problems
    assert run("1girl, solo").problems == []


def test_broken_count_form_is_a_problem():
    r = run("1girl, 2 old woman, park")
    assert any("2 old woman" in p for p in r.problems)


def test_anima_order():
    r = run("masterpiece, best quality, score_9, newest, park, aria vale, @quillon, 1girl, highres, long hair, aria vale (starlight)", typed="quillon")
    parts = r.text.split(", ")
    assert parts[:4] == PREFIX
    assert parts.index("highres") < parts.index("1girl") < parts.index("aria vale (starlight)") < parts.index("@quillon") < parts.index("park")


def test_order_keeps_protected_tags_in_place():
    p = PREFIX + ["@pine-owl", "ink7cm_Paint"]
    out = put_in_order("masterpiece, best quality, score_9, newest, @pine-owl, park, ink7cm_Paint, 1girl".split(", "), cc(protected=p))
    assert out[:5] == [*PREFIX, "@pine-owl"] and out[6] == "ink7cm_Paint"
    assert out.index("1girl") < out.index("park")


def test_avoided_words_are_not_introduced_by_an_alias():
    L = lib()
    L.aliases["my_pal"] = SUBJ[0]                   # an alias into a tag the content rules avoid: not followed
    assert L.lookup("my pal").name == SUBJ[0]       # (the alias does lead there)
    assert run("my pal, park", library=L).text == "my pal, park"


def test_wanted_tags():
    need, drop, group = wanted_tags({**OPEN, "girls": 2})
    assert need == ["2girls", "multiple_girls"] and group and "solo" not in need and "1girl" in drop
    assert wanted_tags(OPEN) == ([], set(), False)

# ---------- snapping, keep check, size floor, rating, artists, LoRA description ----------

def test_a_colour_word_does_not_snap_to_a_tag_about_something_else():
    from app.planning.taglib import TagLib, parse_tags
    tags, aliases = parse_tags(ROWS + "orange_(fruit),0,50000,orange\nglow,0,40000,\n")
    library = TagLib(tags, aliases, IMPLICATIONS)
    r = run("masterpiece, best quality, orange glow, park", library=library)
    assert "fruit" not in r.text and "glow" in r.text


def test_snapping_a_phrase_to_real_tags():
    from app.planning.taglib import TagLib, parse_tags
    tags, aliases = parse_tags(ROWS.replace("black_kimono,0,10000,\n", ""))
    without_black = TagLib(tags, aliases, IMPLICATIONS)
    r = run("masterpiece, best quality, black lace kimono, park", library=without_black)
    assert "kimono, lace" in r.text and "black lace kimono" not in r.text
    assert any(c["from"] == "black lace kimono" and c["to"] == "kimono, lace" for c in r.changes)

    with_black = run("masterpiece, best quality, black lace kimono, park")
    assert "black kimono, lace" in with_black.text                       # a real black kimono tag is kept

    assert "silky black lace kimono" in run("masterpiece, best quality, silky black lace kimono").text  # not fully covered: stays
    assert run("masterpiece, best quality, black lace kimono", typed="black lace kimono").text == \
        "masterpiece, best quality, black lace kimono"                  # what you typed is never snapped


def test_keep_check_adds_the_user_words_back_and_ignores_meta_words():
    r = run("masterpiece, best quality, park", typed="kimono in the rain", keep_text="kimono in the rain")
    assert "kimono" in r.text and "rain" in r.text
    assert any(c["to"] in ("kimono", "rain") and "your request" in c["why"] for c in r.changes)
    told = run("masterpiece, best quality, park", typed="make a very complex image, prompt", keep_text="make a very complex image, prompt")
    assert told.text == "masterpiece, best quality, park"                 # vague and meta words are not demanded
    assert "rain" in run("masterpiece, best quality, rain, park", typed="rainy", keep_text="rainy").text   # rainy is covered by rain
    junk = run("masterpiece, best quality, park", typed="tags from the wildcards, her dress, as possible",
               keep_text="tags from the wildcards, her dress, as possible")
    assert junk.text == "masterpiece, best quality, park, dress"   # tag talk and pronouns are not demanded


def test_the_size_floor_tops_up_from_the_offered_slots():
    offered = [f"made up tag {i}" for i in range(60)]
    plan = {"setting": ", ".join(offered)}                 # the floor only adds tags that restate the plan
    r = run("masterpiece, best quality, 1girl, park", detail_level="very_high", offered=offered, slots=plan)
    content = [t for t in split_tags(r.text) if key(tag_key(t)) not in {key(p) for p in PREFIX}]
    assert len(content) >= 35 and any("detail level" in c["why"] for c in r.changes)
    small = run("masterpiece, best quality, 1girl, park", detail_level="high", offered=offered[:5], slots=plan)
    assert len([t for t in split_tags(small.text) if key(tag_key(t)) not in {key(p) for p in PREFIX}]) < 25    # nothing more to offer: stays short


def test_the_rating_tag_follows_the_request_and_only_anima_gets_one():
    assert run("masterpiece, best quality, 1girl, park", rating="sensitive").text.startswith("masterpiece, best quality, sensitive, 1girl")
    assert "nsfw" in run("masterpiece, best quality, 1girl, park", rating="nsfw").text.split(", ") 
    assert "explicit" in run("masterpiece, best quality, 1girl, park", rating="explicit").text.split(", ")
    assert run("masterpiece, best quality, 1girl, park", rating="general").text == "masterpiece, best quality, 1girl, park"
    assert "nsfw" not in run("masterpiece, best quality, 1girl, park", family="Illustrious", rating="nsfw").text


def test_an_unasked_artist_is_removed_and_an_asked_one_is_capped():
    assert run("1girl, quillon, park").text == "1girl, park"
    assert "quillon" in run("1girl, quillon, park", typed="quillon").text
    many = "1girl, " + ", ".join(f"@artist{i}" for i in range(14))
    r = run(many, typed="3 artists", artist_allowed=True, artist_limit=3)
    assert len([t for t in split_tags(r.text) if t.strip().startswith("@")]) == 3
    assert len([t for t in split_tags(run(many, typed="artist", artist_allowed=True).text) if t.strip().startswith("@")]) == 1


def test_a_tag_copied_from_a_lora_description_is_removed():
    phrases = ["Applies flat saturated colours and thick dark outlines."]
    r = run("masterpiece, best quality, flat saturated colours, thick dark outlines, park", lora_phrases=phrases)
    assert "flat saturated colours" not in r.text and "thick dark outlines" not in r.text and "park" in r.text
    kept = run("masterpiece, best quality, flat saturated colours, park", typed="flat saturated colours", lora_phrases=phrases)
    assert "flat saturated colours" in kept.text


def test_words_from_the_plan_or_the_mention_wording_are_not_tags():
    r = run("masterpiece, best quality, chosen, very_high detail, park")
    assert "chosen" not in r.text and "very_high" not in r.text and "park" in r.text


# ---------- file names, nameless @ tokens, prose words ----------

def test_a_lora_file_name_is_never_a_tag():
    r = run("masterpiece, best quality, score_9, newest, 1girl, lark1 epoch14.safetensors, lark1_epoch14, lark1 epoch14, some_other.safetensors, long_hair",
            lora_files=["lark1_epoch14.safetensors"])
    assert r.text == "masterpiece, best quality, score_9, newest, 1girl, long hair"
    assert any(c["to"] is None and c["from"] == "lark1 epoch14.safetensors" and "file name" in c["why"] for c in r.changes)


def test_a_trigger_word_that_is_a_file_stem_stays():
    r = run("masterpiece, best quality, score_9, newest, lark1_epoch14, 1girl", lora_files=["lark1_epoch14.safetensors"], protected=[*PREFIX, "lark1_epoch14"])
    assert "lark1_epoch14" in r.text


def test_an_at_token_without_a_name_is_dropped_but_a_style_tag_of_the_job_stays():
    r = run("masterpiece, best quality, score_9, newest, @ @, 1girl, @l7m3n", protected=[*PREFIX, "@l7m3n"], typed="")
    assert r.text == "masterpiece, best quality, score_9, newest, 1girl, @l7m3n"
    r = run("masterpiece, best quality, score_9, newest, @ @, @_@, 1girl", protected=PREFIX, typed="by someone")
    assert "@ @" not in r.text and "@_@" not in r.text


def test_a_real_library_tag_that_starts_with_at_is_not_an_artist():
    from app.planning.taglib import TagLib, parse_tags
    tags, aliases = parse_tags("@_@,0,57000,\n1girl,0,8000000,\n")
    r = check_prompt("masterpiece, best quality, score_9, newest, 1girl, @_@", cc(library=TagLib(tags, aliases)))
    assert r.text.endswith("@_@")


def test_a_word_of_prose_between_the_tags_is_dropped_but_real_tags_stay():
    r = run("masterpiece, best quality, score_9, newest, 1girl, herself, long_hair, the, with")
    assert r.text == "masterpiece, best quality, score_9, newest, 1girl, long hair"
    assert run("masterpiece, best quality, score_9, newest, 1girl, herself", typed="herself").text.endswith("herself")      # what the person typed stays
