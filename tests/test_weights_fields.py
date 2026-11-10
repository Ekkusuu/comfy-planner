"""Prompt weights and the small text helpers: tag_key, split_tags, merge_negative, remove_words (fields.py, lora_words.py)."""
import pytest

from app.fields import merge_negative, split_tags, tag_key
from app.planning.lora_words import remove_words


# ---------- tag_key ----------

@pytest.mark.parametrize("tag,key", [
    ("masterpiece", "masterpiece"), ("(masterpiece:1.2)", "masterpiece"), ("((masterpiece))", "masterpiece"), ("(masterpiece)", "masterpiece"),
    ("(Masterpiece:-0.5)", "masterpiece"), ("(masterpiece:.5)", "masterpiece"), ("((masterpiece):1.3)", "masterpiece"),
    ("(((x)))", "x"), ("(bad hands, blurry:1.2)", "bad hands, blurry"),
    ("(a) and (b)", "(a) and (b)"),                                     # two groups are not one wrapper
    ("aria vale (starlight)", "aria vale (starlight)"), ("(aria vale (starlight):1.2)", "aria vale (starlight)")])
def test_tag_key_ignores_weight_wrappers(tag, key):
    assert tag_key(tag) == key


# ---------- split_tags ----------

def test_a_group_with_commas_stays_one_tag():
    assert split_tags("a, (b, c:1.2), d") == ["a", " (b, c:1.2)", " d"]
    assert split_tags("a, ((b, c)), d") == ["a", " ((b, c))", " d"]


@pytest.mark.parametrize("text,tags", [
    ("smile :(, 1girl, red scarf", ["smile :(", " 1girl", " red scarf"]),
    (">:\(, a, (b, c:1.2), d", [">:\(", " a", " (b, c:1.2)", " d"]),      # the wildcard entry >:\( has an escaped bracket
    (":), (a, b), c", [":)", " (a, b)", " c"]),
    ("a, (b, c", ["a", " (b", " c"])])
def test_a_bracket_without_a_partner_is_only_a_character(text, tags):
    assert split_tags(text) == tags


# ---------- merge_negative ----------

def test_a_weighted_negative_beats_the_plain_default():
    assert merge_negative("bad hands, blurry", ["(bad hands:1.5)", "(extra fingers:1.4)"]) == "(bad hands:1.5), blurry, (extra fingers:1.4)"
    assert merge_negative("bad hands, blurry", ["bad hands", "BLURRY"]) == "bad hands, blurry"          # plain copies still collapse
    assert merge_negative("(bad hands:1.3), x", ["bad hands"]) == "(bad hands:1.3), x"                 # a plain one never replaces a weighted one
    assert merge_negative("worst quality", ["((worst quality))"]) == "((worst quality))"


# ---------- remove_words ----------

def test_a_trigger_inside_a_weight_group_is_removed():
    assert remove_words("a, (b, @x:1.2), c", ["@x"], tags=True) == "a, (b:1.2), c"
    assert remove_words("(@x, @y:1.3), a", ["@x", "@y"], tags=True) == "a"
    assert remove_words("a, ((b, @x)), c", ["@x"], tags=True) == "a, ((b)), c"
    assert remove_words("a, (b, c:1.2)", ["@x"], tags=True) == "a, (b, c:1.2)"        # nothing to remove: untouched
