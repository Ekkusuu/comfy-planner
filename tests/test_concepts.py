"""config/concepts.toml: the few word lists the planner reads. The tags for vague words are found in the tag library, not listed here."""
import tomllib
from pathlib import Path

from app.planning.concepts import CONCEPTS_PATH, load_concepts
from app.planning.content_level import ADULT_ANCHORS, ORDINARY_ANCHORS
from app.planning.content_safety import matches_term
from app.planning.scene import clean_must_keep
from app.planning.tagcheck import VAGUE_WORDS


def test_the_file_holds_word_lists_and_no_tag_lists():
    raw = tomllib.loads(CONCEPTS_PATH.read_text(encoding="utf-8"))
    assert set(raw) == {"ratings", "skipped_at_high_rating", "look"}
    assert set(raw["look"]) == {"endings"}
    assert "tags" not in str(raw.keys()) and not any("tags" in table for table in raw.values())
    book = load_concepts(CONCEPTS_PATH)
    assert book.ratings and book.intensifiers and book.skipped_at_high_rating and book.look_endings == ("lighting",)


def test_no_word_in_the_file_matches_the_content_rules(real_rules):
    book = load_concepts(CONCEPTS_PATH)
    assert book.every_tag(), "concepts.toml is empty"
    for group, tag in book.every_tag():
        assert not matches_term(tag), f"{group}: an entry matches the content rules"


def test_the_adult_anchors_are_plain_phrases_and_none_is_avoided(real_rules):
    assert set(ADULT_ANCHORS["explicit"]) < set(ADULT_ANCHORS["nsfw"])                  # explicit asks for the narrower set only
    assert set(ADULT_ANCHORS["nsfw"]).isdisjoint(ORDINARY_ANCHORS)
    assert not any(matches_term(a) for a in (*ADULT_ANCHORS["nsfw"], *ORDINARY_ANCHORS))


def test_the_rating_minimum_follows_the_words():
    book = load_concepts(CONCEPTS_PATH)
    assert book.rating_min("a knight by a campfire") == "general"
    assert book.rating_min("a teasing look") == "sensitive"
    assert book.rating_min("a very lewd picture") == "nsfw"
    assert book.rating_min("teasing, lewd and explicit") == "explicit"
    assert book.rating_min("a very complex castle") == "general"


def test_cute_words_are_left_out_of_the_tag_search_at_nsfw_and_explicit():
    book = load_concepts(CONCEPTS_PATH)
    assert book.for_rating("a cute and lewd girl", "nsfw") == "a and lewd girl"
    assert book.for_rating("adorable kawaii, sweet pictures", "explicit") == ", pictures"
    assert book.for_rating("a cute girl", "general") == "a cute girl" and book.for_rating("a cute girl", "sensitive") == "a cute girl"
    assert book.for_rating("a sweater and a pretty dress", "nsfw") == "a sweater and a dress"      # whole words only


def test_the_vague_words_are_the_rating_words_and_the_keep_check_does_not_gain_any():
    book = load_concepts(CONCEPTS_PATH)
    assert book.vague_words() == {w for words in book.ratings.values() for w in words}
    # a cozy or detailed word the model copied is not a concrete thing to keep; the concrete words around it stay
    kept = clean_must_keep(["cozy cabin", "intricate lace", "lewd kimono"], "a cozy cabin, intricate lace, lewd kimono", book.vague_words() | VAGUE_WORDS)
    assert kept == ["cabin", "lace", "kimono"]


def test_the_file_is_read_on_every_call(tmp_path: Path):
    path = tmp_path / "concepts.toml"
    path.write_text('[ratings]\nnsfw = ["spicy"]\n\n[skipped_at_high_rating]\nwords = ["cute"]\n', encoding="utf-8")
    book = load_concepts(path)
    assert book.rating_min("a spicy picture") == "nsfw" and book.for_rating("a cute cup", "nsfw") == "a cup"
    path.write_text('[ratings]\nexplicit = ["spicy"]\n\n[skipped_at_high_rating]\nwords = ["tiny"]\n', encoding="utf-8")
    book = load_concepts(path)
    assert book.rating_min("a spicy picture") == "explicit" and book.for_rating("a cute tiny cup", "nsfw") == "a cute cup"
    assert load_concepts(tmp_path / "missing.toml").rating_min("a spicy picture") == "general"        # a missing file is an empty book


def test_intensifiers_raise_a_rating_word_by_one_level():
    book = load_concepts(CONCEPTS_PATH)
    assert book.rating_min("a very nsfw picture") == "nsfw"
    assert book.intensified("generate a very nsfw image", "nsfw") == "explicit"
    assert book.intensified("extremely lewd pictures", "nsfw") == "explicit"
    assert book.intensified("a spicy picture", "nsfw") == "explicit"                     # a rating word that intensifies by itself
    assert book.intensified("a very lewd portrait", "sensitive") == "nsfw"
    assert book.intensified("a very very nsfw picture", "explicit") == "explicit"        # nothing above explicit
    assert book.intensified("4 very unique nsfw images", "nsfw") == "nsfw"               # "very" is not next to the rating word
    assert book.intensified("a very cute girl", "general") == "general"                  # nothing adult was asked for
    assert book.intensified("a nsfw picture", "nsfw") == "nsfw"
