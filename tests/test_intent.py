"""Intent: the LLM's reading of the request, checked against the real LoRA cards.

Pure code, no LLM, no GPU. The fixture builds a small card catalogue:
three approved Anima style LoRAs (JADE, cedarv2, model_styles), one
approved Anima clothing LoRA, one draft Anima style LoRA and one approved
Krea-2 style LoRA.
"""
import pytest

from app.library.card_models import LoraCard, Range
from app.library.cards import CardStore
from app.planning import intent as I
from app.planning.context import PlanContext

JADE = "JADE.safetensors"
CEDAR = "cedarv2.safetensors"
MIXED = "model_styles_anima-base1_v6.safetensors"
TORN = "torn clothes_anima_V1.0.safetensors"
DRAFT = "draft_style.safetensors"
KREA = "krea2_inkbrush.safetensors"


def card(file, kind="style", family="Anima", **kw):
    return LoraCard(file=file, family=family, kind=kind, what_it_does=f"{file} test card.",
                    weight=Range(rec=0.8, min=0.5, max=1.2), **kw)


@pytest.fixture
def ctx(db, config):
    cards = CardStore(db, config)
    for file, kind, family in ((JADE, "style", "Anima"), (CEDAR, "style", "Anima"), (MIXED, "style", "Anima"),
                               (TORN, "clothing", "Anima"), (KREA, "style", "Krea-2")):
        cards.save_built("lora", file, card(file, kind, family))
        cards.approve("lora", file)
    cards.save_built("lora", DRAFT, card(DRAFT))       # a draft: not approved
    return PlanContext(config, None, None, cards, db)


def resolve(ctx, reply, mentions=(), family="Anima", **kw):
    return I.resolve(ctx, reply, list(mentions), family, **kw)


# ---------- exclusion ----------

def test_excluded_lora_is_left_out_of_the_group_with_a_reason(ctx):
    out = resolve(ctx, {"skip_loras": [MIXED], "per_lora_kind": "style"}, [], "Anima")
    assert MIXED not in out.members and MIXED in out.skip
    assert (MIXED, "you ruled it out") in out.left_out
    assert set(out.members) == {JADE, CEDAR}


def test_excluding_by_at_mention_wins_over_a_plain_name(ctx):
    out = resolve(ctx, {"skip_loras": ["cedarv2"], "mentions": [{"mention": 1, "wanted": False}]}, [CEDAR], "Anima")
    assert out.skip == [CEDAR] and out.use == []


def test_unclassified_mention_falls_back_to_use(ctx):
    out = resolve(ctx, {}, [JADE], "Anima")
    assert out.use == [JADE]


def test_mention_verdict_wins_over_the_name_lists(ctx):
    out = resolve(ctx, {"skip_loras": ["JADE"], "mentions": [{"mention": 1, "wanted": True}]}, [JADE], "Anima")
    assert out.use == [JADE] and out.skip == []


# ---------- typos, unknown and junk names ----------

def test_a_typo_that_fits_one_lora_still_resolves(ctx):
    file, why = I.resolve_name("cedarv", I.known_loras(ctx), [])
    assert (file, why) == (CEDAR, "")


def test_unknown_names_become_a_visible_issue_not_a_guess(ctx):
    out = resolve(ctx, {"use_loras": ["mystyle xyzqqq"]}, [], "Anima")
    assert out.use == []
    assert any("mystyle xyzqqq" in i["message"] and "no LoRA is called that" in i["message"] for i in out.issues)


def test_junk_that_names_no_lora_is_ignored_with_a_visible_issue(ctx):
    out = resolve(ctx, {"use_loras": ["lora"]}, [], "Anima")
    assert out.use == []
    assert any("lora" in i["message"] for i in out.issues)


def test_a_word_that_fits_several_loras_is_ambiguous_not_a_guess(ctx):
    file, why = I.resolve_name("anima", I.known_loras(ctx), [])
    assert file is None and "several LoRAs" in why


def test_lora_number_mentions_resolve_by_position(ctx):
    known = I.known_loras(ctx)
    assert I.resolve_name("lora 2", known, [JADE, CEDAR]) == (CEDAR, "")
    assert I.resolve_name("the chosen lora [1]", known, [JADE, CEDAR]) == (JADE, "")
    file, why = I.resolve_name("lora 9", known, [JADE, CEDAR])
    assert file is None and "no mention number 9" in why


def test_a_number_the_user_never_wrote_is_quietly_ignored(ctx):
    out = resolve(ctx, {"use_loras": ["lora 3"]}, [JADE], "Anima")
    assert out.use == [JADE] and not [i for i in out.issues if "lora 3" in i["message"]]


# ---------- include + exclude conflict ----------

def test_asking_for_and_ruling_out_the_same_lora_leaves_it_out_with_an_issue(ctx):
    out = resolve(ctx, {"use_loras": ["JADE"], "skip_loras": ["JADE"]}, [], "Anima")
    assert out.use == [] and out.skip == [JADE]
    assert any("both asked for and ruled out" in i["message"] and "JADE" in i["message"] for i in out.issues)


# ---------- a mention inside a sentence about the image ----------

def test_a_mention_verdict_against_use_keeps_the_lora_off(ctx):
    out = resolve(ctx, {"mentions": [{"mention": 1, "wanted": False}]}, [JADE], "Anima")
    assert out.use == [] and out.skip == [JADE]


def test_an_out_of_range_mention_verdict_is_ignored(ctx):
    out = resolve(ctx, {"mentions": [{"mention": 5, "wanted": True}]}, [JADE], "Anima")
    assert out.use == [JADE] and out.skip == []       # the unclassified mention still falls back to use


# ---------- other families, drafts, malformed replies ----------

def test_a_lora_of_another_family_is_not_a_group_member(ctx):
    out = resolve(ctx, {"per_lora_kind": "style"}, [], "Anima")
    assert KREA not in out.members


def test_members_come_sorted_by_file_name(ctx):
    out = resolve(ctx, {"per_lora_kind": "style"}, [], "Anima")
    assert out.members == sorted(out.members, key=str.lower) == [CEDAR, JADE, MIXED]


def test_drafts_are_members_only_when_settings_allow_them(ctx, config):
    config.allow_draft_cards = False
    assert DRAFT not in resolve(ctx, {"per_lora_kind": "style"}, [], "Anima").members
    config.allow_draft_cards = True
    assert DRAFT in resolve(ctx, {"per_lora_kind": "style"}, [], "Anima").members


def test_empty_and_malformed_replies_fall_back_to_defaults(ctx):
    out = resolve(ctx, None, [], "Anima")
    assert (out.rating, out.use, out.skip, out.group_kind, out.members, out.extras) == ("general", [], [], "", [], "default")
    out = resolve(ctx, {"rating": "bogus", "extra_loras": "bogus", "vary": ["camera", "camera"]}, [], "Anima")
    assert out.rating == "general" and out.extras == "default" and out.vary == ["camera"]


def test_a_workflow_that_takes_no_loras_says_so(ctx):
    out = resolve(ctx, {"per_lora_kind": "style"}, [], "Anima", can_take_loras=False)
    assert out.members == [] and any("can't take LoRAs" in i["message"] for i in out.issues)


def test_zero_members_is_a_blocking_issue_not_a_crash(ctx):
    out = resolve(ctx, {"per_lora_kind": "character"}, [], "Anima")
    assert out.members == [] and any(i["severity"] == "blocking" and i["code"] == "lora_group" for i in out.issues)


def test_group_summary_names_members_and_left_out(ctx):
    out = resolve(ctx, {"skip_loras": [MIXED], "per_lora_kind": "style"}, [], "Anima")
    text = out.summary({m: 3 for m in out.members}, 3)
    assert "3 images for each style LoRA" in text and "model" in text.lower() and "you ruled it out" in text


def test_per_lora_tags_narrow_the_group_and_unknown_tags_are_visible(ctx, db):
    from app.library.lora_extras import LoraExtras
    LoraExtras(db).change_tags([JADE], add=["Ink Wash"])
    out = resolve(ctx, {"per_lora_kind": "style", "per_lora_tags": ["ink wash"]}, [], "Anima")
    assert out.members == [JADE]
    out = resolve(ctx, {"per_lora_kind": "style", "per_lora_tags": ["no such tag xyz"]}, [], "Anima")
    assert any("no such tag xyz" in i["message"] for i in out.issues)


def test_per_lora_limit_cuts_with_a_reason(ctx):
    out = resolve(ctx, {"per_lora_kind": "style", "per_lora_limit": 2}, [], "Anima")
    assert out.members == [CEDAR, JADE]
    assert (MIXED, "only 2 asked for") in out.left_out


def test_skip_kinds_does_not_remove_the_asked_group_kind(ctx):
    out = resolve(ctx, {"per_lora_kind": "style", "skip_kinds": ["style"]}, [], "Anima")
    assert out.group_kind == "style" and out.skip_kinds == [] and len(out.members) == 3


# ---------- what the LLM writes that is no LoRA name ----------

def test_a_kind_of_lora_or_junk_in_the_lists_is_dropped_without_an_issue(ctx):
    said = "every style lora except model styles"
    out = resolve(ctx, {"use_loras": ["style", "true"], "skip_loras": ["styles", "model styles"]}, request=said)
    assert out.use == [] and out.skip == [MIXED] and out.issues == []


def test_a_name_the_request_never_says_is_made_up_and_dropped(ctx):
    out = resolve(ctx, {"use_loras": ["JADE"]}, request="a girl in a garden")
    assert out.use == [] and out.issues == []
    assert resolve(ctx, {"use_loras": ["JADE"]}, request="a girl, use the jade lora").use == [JADE]


def test_a_lora_whose_whole_name_is_a_kind_word_is_still_found(ctx, db, config):
    file = "style.safetensors"
    store = CardStore(db, config)
    store.save_built("lora", file, card(file))
    store.approve("lora", file)
    assert I.resolve_name("style", I.known_loras(ctx), [])[0] == file


def test_naming_a_lora_of_the_kind_is_a_choice_not_a_group(ctx):
    out = resolve(ctx, {"use_loras": ["JADE"], "per_lora_kind": "style", "images_per_lora": 3}, request="a portrait, use the JADE lora")
    assert out.use == [JADE] and out.group_kind == "" and out.members == []
    both = resolve(ctx, {"per_lora_kind": "style", "images_per_lora": 3}, request="3 images per style lora")
    assert both.group_kind == "style" and both.per == 3 and JADE in both.members


def test_a_missing_images_per_lora_is_zero_so_the_job_count_is_used(ctx):
    assert resolve(ctx, {"per_lora_kind": "style"}).per == 0


def test_a_group_the_request_does_not_say_in_words_is_not_a_group(ctx):
    said = "a portrait of a woman in the rain, use the JADE lora"
    reply = {"per_lora_kind": "style", "images_per_lora": 1}
    assert resolve(ctx, {**reply, "per_lora_words": "use the JADE lora"}, request="3 images per style lora").group_kind == ""      # not in that request
    assert resolve(ctx, {**reply, "per_lora_words": ""}, request=said).group_kind == ""
    assert resolve(ctx, {**reply, "per_lora_words": "3 images per style lora"}, request="3 images per style lora").group_kind == "style"
    assert resolve(ctx, reply, request=said).group_kind == "style"       # a reply without the words (an older one) is taken as it is


# ---------- content level ----------

def test_an_intensifier_raises_the_content_level_one_step(ctx):
    assert resolve(ctx, {"rating": "nsfw"}, request="generate a very nsfw image").rating == "explicit"
    assert resolve(ctx, {"rating": "nsfw"}, request="make 3 nsfw images of a woman at the beach").rating == "nsfw"
    assert resolve(ctx, {"rating": "nsfw"}, request="extremely lewd, spicy").rating == "explicit"
    assert resolve(ctx, {"rating": "general"}, request="a very cute girl").rating == "general"


def test_a_limit_in_the_request_stops_the_intensifier(ctx):
    out = resolve(ctx, {"rating": "sensitive", "rating_limit": True}, request="very sexy, nothing explicit")
    assert out.rating == "sensitive"
    assert resolve(ctx, {"rating": "nsfw"}, request=None).rating == "nsfw"          # no request text: nothing to read
