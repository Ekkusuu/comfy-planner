"""The artist rule: an artist list is offered only when one was asked for, and gives one by default."""
import random

from app.config import Config
from app.planning.artists import asked_artists, wants_artists
from app.wildcards.ideas import choose, draw
from app.wildcards.store import Catalog, WildList


def lists() -> list[WildList]:
    return [WildList("artist-group/artists", "", "artist", "artist", True, [f"artist {i}" for i in range(10)]),
            WildList("style/style", "", "style", "style", True, ["steampunk", "ink wash", "watercolor"]),
            WildList("pose/pose", "", "pose", "pose", True, ["sitting", "standing", "lying"])]


def test_artist_words_and_counts():
    assert not wants_artists("a girl in rain") and asked_artists("a girl in rain") == 0
    assert wants_artists("use artist wildcards") and asked_artists("use artist wildcards") == 1
    assert asked_artists("two artists I like") == 2 and asked_artists("3 artists") == 3
    assert asked_artists("an art style") == 1


def test_an_artist_list_is_not_chosen_unless_asked_for():
    config = Config()
    plain = [c.wild.name for c in choose(lists(), "a moody girl", "", config, random.Random(1))]
    assert "artist-group/artists" not in plain
    asked = [c.wild.name for c in choose(lists(), "a girl, use artist wildcards", "", config, random.Random(1))]
    assert "artist-group/artists" in asked


def test_an_artist_list_gives_one_entry_by_default():
    config = Config()
    chosen = [lists()[0]]
    one = draw(chosen, Catalog(chosen), 2, config, seed=7)
    assert all(len([i for i in prompt if i["role"] == "artist"]) == 1 for prompt in one.per_prompt)
    three = draw(chosen, Catalog(chosen), 2, config, seed=7, artist_limit=3)
    assert all(len([i for i in prompt if i["role"] == "artist"]) == 3 for prompt in three.per_prompt)
