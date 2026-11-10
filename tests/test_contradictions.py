"""Contradicting tags: the evidence is computed from co-occurrence counts, and the tag check drops the less wanted tag of a
contradicting pair. The counts here come from a small synthetic corpus (no download): a made-up set of 20 000 pictures where `day` and
`night` never share a picture, a person is either `sitting` or `lying` or `standing`, and so on. The limits are scaled down to its size."""
import collections
import functools
import importlib.util
import os
import sys
import itertools
import json
import random
import time

import pytest

from app.planning import contradictions as con_mod
from app.planning.contradictions import (Contradictions, Derived, Pair, derive, estimate_images, get_contradictions, load_contradictions,
                                         read_pair_counts, read_tag_counts, write_derived)
from app.planning.tagcheck import FAMILY_RULES, CheckContext, check_prompt, contradicts_present, drop_contradictions, one_person_tags, top_up
from app.planning.tagstep import TagStep, records
from app.planning.taglib import TagLib
from tests.test_planning_flow import anima_card, app  # noqa: F401  (the fixture: a running app with a fake LLM)

NL = chr(10)
PREFIX = ["masterpiece", "best quality", "score_9", "newest"]
SMALL_LIMITS = dict(MIN_TAG_COUNT=300, CONTEXT_MIN_COUNT=300, PAIR_FLOOR=10, MIN_EXPECTED=20, GROUP_MIN_EXPECTED=40,
                    STORE_MIN_EXPECTED_SOLO=10, STORE_MIN_EXPECTED_ALL=20, MODIFIER_MIN_COUNT=200, ALT_MIN_EXPECTED=20)


@pytest.fixture(autouse=True)
def small_limits(monkeypatch):
    """The module's limits are for millions of pictures; these tests have 20 000. Most tests below judge every pair (OUTSIDE_COMPETITION), as the
    first version of the check did, because they test the statistics and the tag check; the section "competition" switches that off, which is
    what the app ships."""
    for name, value in SMALL_LIMITS.items():
        monkeypatch.setattr(con_mod, name, value)
    monkeypatch.setattr(con_mod, "OUTSIDE_COMPETITION", True)


@pytest.fixture
def competition_only(monkeypatch):
    """The shipped setting: only pairs that compete for the same thing (the same noun, alike words in front of it) are judged."""
    monkeypatch.setattr(con_mod, "OUTSIDE_COMPETITION", False)


# ---------- the synthetic corpus ----------

@functools.cache
def corpus(n=20000, seed=7) -> tuple[tuple[frozenset, ...], ]:
    """Pictures as tag sets. A third are groups (two people). Time of day (`day`/`night`/none) and place (`indoors`/`outdoors`/none) are
    shared and exclusive. Each person has one pose (`sitting`, `lying`, `standing`, `lying_on_side`) and one hair length, so in a
    group both poses can appear. `sky`/`lighting` follow the time of day, `legs`/`hands` follow a pose: that is what makes `day` and
    `night`, or two poses, look alike. `kimono` and `armor` never meet but live in other surroundings. `smile` and `blush` are
    independent; `knight` and `cafe` are rare and independent; `dusk` is a copy of `night` that is too rare to judge. Eyes and hair have
    one colour per person (`blue_eyes`/`brown_eyes`, `blue_hair`/`brown_hair`; both eye colours with `heterochromia`), dresses are independent
    (layers), `closed_eyes` is a state."""
    rng = random.Random(seed)
    other = random.Random(seed + 1)             # a second stream for the attributes added later, so the pictures above do not change
    images = []
    for _ in range(n):
        t = set()
        group = rng.random() < 0.3
        t |= {"1girl", "multiple_girls"} if group else {"1girl", "solo"}
        time_of_day = rng.choices(["day", "night", None], [.35, .25, .40])[0]
        if time_of_day:
            t.add(time_of_day)
            t.update(x for x in ("sky", "lighting") if rng.random() < .85)
        else:
            t.update(x for x in ("sky", "lighting") if rng.random() < .1)
        place = rng.choices(["indoors", "outdoors", None], [.4, .4, .2])[0]
        if place:
            t.add(place)
        for _person in range(2 if group else 1):
            pose = rng.choices(["sitting", "lying", "standing", "lying_on_side", None], [.30, .15, .30, .05, .20])[0]
            if pose:
                t.add(pose)
                t.update(x for x in ("legs", "hands") if rng.random() < .85)
            else:
                t.update(x for x in ("legs", "hands") if rng.random() < .1)
            t.add(rng.choice(["long_hair", "short_hair"]))
        outfit = rng.choices(["kimono", "armor", None], [.08, .08, .84])[0]
        if outfit:
            t.add(outfit)
            if rng.random() < .7:
                t.add("temple" if outfit == "kimono" else "beach")
        if rng.random() < .5:
            t.add("smile")
        if rng.random() < .4:
            t.add("blush")
        if rng.random() < .015:
            t.add("knight")
        if rng.random() < .015:
            t.add("cafe")
        if time_of_day is None and rng.random() < .008:
            t.update(("dusk", "sky"))
        for _person in range(2 if group else 1):
            if other.random() < .01:                                           # a heterochromia picture has both eye colours
                t.update(("heterochromia", "blue_eyes", "brown_eyes"))
            else:
                t.add(other.choice(["blue_eyes", "brown_eyes"]))
            t.add(other.choice(["blue_hair", "brown_hair"]))
        t.update(x for x in ("blue_dress", "brown_dress") if other.random() < .2)           # clothes are not exclusive: layers
        if other.random() < .15:
            t.add("closed_eyes")                                              # a state, independent of the eye colour
        images.append(frozenset(t))
    return tuple(images)


def csv_lines(images, floor):
    """The two CSVs the way the dataset has them: tag counts, and pair counts without the pairs seen fewer than `floor` times."""
    counts = collections.Counter(t for im in images for t in im)
    pairs = collections.Counter(p for im in images for p in itertools.combinations(sorted(im), 2))
    tags = ["tag,category,count,alias"] + [f"{t},0,{n}," for t, n in counts.items()]
    rows = ["tag_a,tag_b,count"] + [f"{a},{b},{n}.0" for (a, b), n in pairs.items() if n >= floor]
    return tags, rows


@functools.cache
def evidence_tuple():
    images = corpus()
    tag_lines, pair_lines = csv_lines(images, con_mod.PAIR_FLOOR)
    counts, bad = read_tag_counts(tag_lines)
    adj, bad_pairs = read_pair_counts(pair_lines)
    assert bad == bad_pairs == 0
    return counts, adj, len(images)


def build(**over) -> Derived:
    counts, adj, n = evidence_tuple()
    return derive(counts, adj, images=n)


def evidence() -> Contradictions:
    return Contradictions.from_derived(build())


# ---------- the statistics ----------

def test_alternatives_are_flagged_for_one_person():
    c = evidence()
    for a, b in [("day", "night"), ("sitting", "lying"), ("sitting", "standing"), ("lying", "standing"), ("sitting", "lying_on_side"), ("long_hair", "short_hair")]:
        assert c.contradicts(a, b, single=True) and c.contradicts(b, a, single=True), (a, b)


def test_a_pair_that_is_merely_rare_is_not_flagged():
    c = evidence()
    pair = c.evidence("knight", "cafe")
    assert not c.contradicts("knight", "cafe", single=True) and not c.contradicts("knight", "cafe", single=False)
    assert pair is None or pair.solo_expected < con_mod.MIN_EXPECTED       # expected ~6 together in 20 000 pictures: nothing can be said


def test_exclusive_tags_that_do_not_play_the_same_role_are_not_flagged():
    c = evidence()
    assert not c.contradicts("kimono", "armor", single=True)               # never together, but they live in other surroundings
    assert not c.contradicts("indoors", "outdoors", single=True)            # never together, but nothing else is alike about them here
    pair = c.evidence("kimono", "armor")
    assert pair is None or pair.similarity < con_mod.MIN_SIMILARITY


def test_tags_that_go_together_are_not_flagged():
    c = evidence()
    assert not c.contradicts("smile", "blush", single=True) and not c.contradicts("smile", "blush", single=False)
    assert not c.contradicts("sky", "day", single=True)


def test_the_limits_are_plain_named_constants(monkeypatch):
    c = evidence()
    assert c.contradicts("day", "night", single=True)
    monkeypatch.setattr(con_mod, "MIN_SIMILARITY", 0.999)
    assert not c.contradicts("day", "night", single=True)
    monkeypatch.setattr(con_mod, "MIN_SIMILARITY", 0.4)
    monkeypatch.setattr(con_mod, "MIN_EXPECTED", 10 ** 9)
    assert not c.contradicts("day", "night", single=True)                   # not enough pairs expected to say anything
    monkeypatch.setattr(con_mod, "MIN_EXPECTED", 20)
    monkeypatch.setattr(con_mod, "MIN_TAG_COUNT", 10 ** 9)
    assert not c.contradicts("day", "night", single=True)                   # tags used too rarely give no verdict


def test_a_tag_too_rare_to_judge_never_conflicts():
    counts, adj, n = evidence_tuple()
    assert counts["dusk"] < con_mod.MIN_TAG_COUNT                           # `dusk` is a copy of `night` that appears 61 times
    c = evidence()
    assert "dusk" not in c.counts
    assert not c.contradicts("dusk", "day", single=True) and not c.contradicts("dusk", "day", single=False)
    strong = Pair(0.0, 5000.0, 0.001, 5000.0, 0.95)                         # even strong evidence is ignored when a tag is used too rarely
    rare = Contradictions({"dusk": 50, "day": 7000}, {("day", "dusk"): strong})
    assert not rare.contradicts("dusk", "day", single=True)
    assert Contradictions({"dusk": 5000, "day": 7000}, {("day", "dusk"): strong}).contradicts("dusk", "day", single=True)


def test_pairs_the_data_does_not_list_use_the_upper_bound():
    counts, adj, n = evidence_tuple()
    assert "night" not in adj["day"]                                        # the dataset prunes pairs seen fewer than PAIR_FLOOR times
    pair = evidence().evidence("day", "night")
    assert pair is not None
    assert pair.ratio == pytest.approx((con_mod.PAIR_FLOOR - 1) / pair.expected, rel=0.01)       # at most 9 seen: that bound, divided by the expected count


def test_a_pruned_pair_of_unrelated_tags_gives_no_verdict_however_large_the_expected_count():
    counts, adj, n = evidence_tuple()
    assert "cafe" not in adj["knight"]                                      # pruned: together fewer than PAIR_FLOOR times
    counts = dict(counts, knight=3000, cafe=3000)                           # pretend both are common: 450 pairs expected, fewer than 10 seen
    c = Contradictions.from_derived(derive(counts, adj, images=n))
    pair = c.evidence("knight", "cafe")
    assert pair is None or pair.similarity < con_mod.MIN_SIMILARITY         # ... which would be strong evidence if the roles were alike; they are not
    assert not c.contradicts("knight", "cafe", single=True) and not c.contradicts("knight", "cafe", single=False)


def test_one_person_and_group_pictures_are_counted_differently():
    c = evidence()
    for a, b in [("sitting", "lying"), ("sitting", "standing"), ("long_hair", "short_hair")]:
        assert c.contradicts(a, b, single=True) and not c.contradicts(a, b, single=False), (a, b)   # in a group the two people may differ
    assert c.contradicts("day", "night", single=False)                       # one time of day for everybody: judged by the plain ratio (OUTSIDE_COMPETITION)
    pair = c.evidence("sitting", "lying")
    assert pair.solo_ratio <= con_mod.MAX_RATIO < pair.ratio                 # almost never together among solo pictures, yet often over all


def test_the_picture_count_is_estimated_from_the_people_tags():
    counts = {"1girl": 800, "1boy": 300, "multiple_girls": 200, "solo": 700, "no_humans": 50}
    adj = {"1girl": {"1boy": 100}, "1boy": {"1girl": 100}}
    assert estimate_images(counts, adj) == 800 + 300 + 200 + 50 - 100
    assert estimate_images({"x": 5}, {}) == 5


def test_malformed_rows_are_skipped_and_counted():
    tags, bad = read_tag_counts(["tag,category,count,alias", "day,0,100,", "night,0,notanumber,", "x", "bad name,0,-5,", ",0,3,", "ok,0,7", "inf,0,inf,"])
    assert tags == {"day": 100, "ok": 7} and bad == 5
    adj, bad = read_pair_counts(["tag_a,tag_b,count", "day,night,5.0", "day,night,x", "day", "a,a,3", "day,sky,12.0", "long," + "z" * 300 + ",4", "u,v,nan"])
    assert adj["day"] == {"night": 5, "sky": 12} and adj["night"] == {"day": 5} and bad == 4      # the row of a tag with itself is dropped, not counted
    adj, bad = read_pair_counts(["day,night,5", "day,sky,9"], wanted={"day", "night"})
    assert adj == {"day": {"night": 5}, "night": {"day": 5}} and bad == 0


def test_tag_names_are_read_as_text_only():
    tags, bad = read_tag_counts(["__import__('os'),0,50,", "a b,0,5,", "ok,0,3,x\ny", "fine,0,9,"])
    assert tags == {"import_('os')": 50, "a_b": 5, "fine": 9} and bad == 1  # plain names, nothing is evaluated or run; the row with a stray line break is skipped
    adj, bad = read_pair_counts(["a,b,3", "c,d,4\re,f,5", "g,h,6"])
    assert adj["g"] == {"h": 6} and bad == 1


# ---------- the derived file ----------

def test_the_derived_file_round_trips(tmp_path):
    derived = build()
    path = tmp_path / "x" / "contradictions.json"
    write_derived(derived, path)
    loaded = load_contradictions(path)
    assert loaded is not None and len(loaded) == len(derived.pairs) and loaded.images == derived.images
    original = Contradictions.from_derived(derived)
    for a, b in [("day", "night"), ("sitting", "lying"), ("smile", "blush"), ("kimono", "armor")]:
        assert loaded.contradicts(a, b, True) == original.contradicts(a, b, True)
        assert loaded.contradicts(a, b, False) == original.contradicts(a, b, False)


def test_a_missing_or_unusable_file_means_no_check(tmp_path, caplog):
    assert load_contradictions(tmp_path / "none.json") is None and get_contradictions(tmp_path) is None       # quiet: a fresh install
    for text in ("not json", "[]", json.dumps({"format": 99, "tags": [], "counts": [], "pairs": []}), json.dumps({"format": 1, "tags": ["a"], "counts": [1], "pairs": []}),
                 json.dumps({"format": 2, "tags": ["a"], "counts": [], "pairs": []})):
        (tmp_path / "bad.json").write_text(text, encoding="utf-8")
        assert load_contradictions(tmp_path / "bad.json") is None
    assert (tmp_path / "bad.json").exists()


def test_malformed_pairs_in_the_file_are_skipped(tmp_path):
    path = tmp_path / "f.json"
    good = [0, 1, 0.0, 800.0, 0.01, 900.0, 0.9, None]
    path.write_text(json.dumps({"format": 2, "images": 5000, "tags": ["day", "night", "sky"], "counts": [900, 800, 1000],
                                "pairs": [good, [0, 9, 0, 1, 1, 1, 1, None], "junk", [0, 0, 0, 1, 1, 1, 1, None], [0, 2, "x", 1, 1, 1, 1, None],
                                          [1, 2, None, 5.0, 0.2, 300.0, 0.7, 0.4], [0, 2, 0, 1, 1, 1, 1]]}), encoding="utf-8")
    loaded = load_contradictions(path)
    assert len(loaded) == 2 and loaded.evidence("day", "night") == Pair(0.0, 800.0, 0.01, 900.0, 0.9) and loaded.evidence("night", "sky").solo_ratio is None
    assert loaded.evidence("night", "sky").kind == 0.4                       # an old-format row (one number short) is skipped like any malformed row


def test_the_file_is_small_and_loads_once_and_quickly(tmp_path, monkeypatch):
    path = tmp_path / con_mod.DERIVED_FILE
    write_derived(build(), path)
    assert path.stat().st_size < 200_000
    reads = []
    real = json.loads
    monkeypatch.setattr(con_mod.json, "loads", lambda text, *a, **k: reads.append(1) or real(text, *a, **k))
    first = get_contradictions(tmp_path)
    started = time.perf_counter()
    for _ in range(200):
        assert get_contradictions(tmp_path) is first
    assert len(reads) == 1 and time.perf_counter() - started < 0.5          # repeated use does not read the file again
    time.sleep(0.01)
    write_derived(build(), path)
    path.touch()
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert get_contradictions(tmp_path) is not first and len(reads) == 2    # a rebuild is picked up without a restart


def test_loading_is_lazy_and_the_switch_works(app, monkeypatch):
    ctx = app.state.planner.ctx
    folder = ctx.config.data_dir / con_mod.DATA_FOLDER
    con_mod._loaded.clear()
    (folder / con_mod.DERIVED_FILE).unlink(missing_ok=True)
    assert con_mod.CONTRADICTIONS_ENABLED is True            # shipped on
    assert ctx.contradictions() is None                      # ... but never built (a fresh install): skipped quietly, nothing raised
    write_derived(build(), folder / con_mod.DERIVED_FILE)
    assert ctx.contradictions() is not None and ctx.contradictions() is ctx.contradictions()       # loaded on first use, then kept
    monkeypatch.setattr(con_mod, "CONTRADICTIONS_ENABLED", False)
    assert ctx.contradictions() is None                      # the switch turns it off even with the data there
    monkeypatch.setattr(con_mod, "CONTRADICTIONS_ENABLED", True)
    (folder / con_mod.DERIVED_FILE).unlink()
    con_mod._loaded.clear()
    assert ctx.contradictions() is None
    ctx.contradiction_data = evidence()
    assert ctx.contradictions() is ctx.contradiction_data    # tests (and callers) can hand the evidence over


def test_a_prompt_is_checked_unchanged_when_the_data_was_never_built(app):
    ctx = app.state.planner.ctx
    ctx.tag_library = library()
    ctx.contradiction_data = None
    con_mod._loaded.clear()
    (ctx.config.data_dir / con_mod.DATA_FOLDER / con_mod.DERIVED_FILE).unlink(missing_ok=True)
    step = TagStep.build(ctx, anima_card(), {"girls": 1, "boys": None, "others": None, "no_humans": None}, list(PREFIX), "")
    assert step.check_context.contradictions is None
    text = f"{SOLO}, blue_eyes, brown_eyes, sitting, lying"
    assert step.check(text).text == run(text, con=None).text and not [c for c in step.check(text).changes if "contradicts" in c["why"]]


# ---------- the tag check ----------

def library() -> TagLib:
    names = ["day", "night", "sitting", "lying", "standing", "lying_on_side", "long_hair", "short_hair", "indoors", "outdoors", "kimono", "armor",
             "smile", "blush", "knight", "cafe", "sky", "legs", "hands", "lighting", "1girl", "solo", "multiple_girls", "2girls", "dusk", "temple", "beach",
             "blue_eyes", "brown_eyes", "blue_hair", "brown_hair", "blue_dress", "brown_dress", "closed_eyes", "heterochromia", "multicolored_hair"]
    tags = {n: (0, 100_000) for n in names}
    tags["nsfw"] = (5, 100_000)
    tags["sensitive"] = (0, 100_000)
    tags["aria_vale"] = (4, 100_000)
    return TagLib(tags, {"daytime": "day", "sit": "sitting"}, [("2girls", "multiple_girls")])


def cc(kind="tags", subjects=None, typed="", protected=PREFIX, con="default", **kw) -> CheckContext:
    return CheckContext(library(), FAMILY_RULES["Anima"], kind, list(protected), typed, subjects, family="Anima",
                        contradictions=evidence() if con == "default" else con, **kw)


def run(text, **kw):
    return check_prompt(text, cc(**kw))


def tags_of(result) -> list[str]:
    return [t.strip() for t in result.text.split(",")]


SOLO = "masterpiece, best quality, score_9, newest, 1girl, solo"


def test_the_less_wanted_tag_of_a_contradicting_pair_is_removed_with_a_reason():
    r = run(f"{SOLO}, sitting, lying, sky")
    assert "sitting" in tags_of(r) and "lying" not in tags_of(r)              # equal rank: the earlier one stays
    [change] = [c for c in r.changes if "contradicts" in c["why"]]
    assert change["from"] == "lying" and change["to"] is None and "sitting" in change["why"] and set(change) == {"from", "to", "why"}
    assert records(r) == [{"from": c["from"], "to": c["to"], "why": c["why"]} for c in r.changes]      # the job card's form


def test_nothing_is_removed_without_evidence_and_the_rest_keeps_its_order():
    text = f"{SOLO}, smile, day, sitting, blush, night, lying, sky"
    plain = run(text, con=None)
    assert tags_of(plain) == tags_of(run(text, con=None))
    with_evidence = run(text)
    assert [c["from"] for c in with_evidence.changes if "contradicts" in c["why"]] == ["night", "lying"]
    assert tags_of(with_evidence) == [t for t in tags_of(plain) if t not in ("night", "lying")]            # only those two are gone, the order is the same


def test_a_prompt_without_contradictions_is_unchanged():
    text = f"{SOLO}, smile, blush, standing, day, sky, indoors"
    assert run(text).text == run(text, con=None).text and not run(text).notes


def test_user_typed_beats_the_rest():
    r = run(f"{SOLO}, sitting, lying", typed="a girl lying down")
    assert "lying" in tags_of(r) and "sitting" not in tags_of(r)


def test_must_keep_beats_plan_text():
    r = run(f"{SOLO}, day, night", must_keep=["night"], plan_text="a bright day by the window")
    assert "night" in tags_of(r) and "day" not in tags_of(r)


def test_the_scene_plan_beats_offered_tags():
    r = run(f"{SOLO}, day, night", plan_text="warm light, night", offered=["day"])
    assert "night" in tags_of(r) and "day" not in tags_of(r)
    r = run(f"{SOLO}, day, night", slots={"setting": "night"}, offered=["day"])        # a slot phrase counts as the plan too
    assert "night" in tags_of(r) and "day" not in tags_of(r)


def test_offered_tags_beat_the_rest():
    r = run(f"{SOLO}, standing, sitting", offered=["sitting"])
    assert "sitting" in tags_of(r) and "standing" not in tags_of(r)


def test_inside_a_tier_the_more_specific_tag_then_the_earlier_one_stays():
    r = run(f"{SOLO}, sitting, lying_on_side")
    assert "lying on side" in tags_of(r) and "sitting" not in tags_of(r)       # three words against one
    r = run(f"{SOLO}, standing, sitting")
    assert "standing" in tags_of(r) and "sitting" not in tags_of(r)


def test_two_tags_the_person_wrote_both_stay_and_it_is_noted(caplog):
    with caplog.at_level("INFO", logger="planning.tags"):
        r = run(f"{SOLO}, sitting, lying", typed="she is sitting, then lying down")
    assert {"sitting", "lying"} <= set(tags_of(r)) and not [c for c in r.changes if "contradicts" in c["why"]]
    assert len(r.notes) == 1 and "sitting" in r.notes[0] and "lying" in r.notes[0] and "both stay" in r.notes[0]
    assert any("both stay" in m for m in caplog.messages)


def test_two_must_keep_words_both_stay():
    r = run(f"{SOLO}, day, night", must_keep=["day", "night"])
    assert {"day", "night"} <= set(tags_of(r)) and r.notes


def test_protected_count_and_rating_tags_are_never_removed():
    pairs = {("1girl", "sitting"): Pair(0.0, 5000.0, 0.01, 5000.0, 0.9), ("sensitive", "day"): Pair(0.0, 5000.0, 0.01, 5000.0, 0.9),
             ("night", "smile"): Pair(0.0, 5000.0, 0.01, 5000.0, 0.9), ("blush", "day"): Pair(0.0, 5000.0, 0.01, 5000.0, 0.9)}
    counts = {t: 50_000 for pair in pairs for t in pair}
    c = Contradictions(counts, {tuple(sorted(k)): v for k, v in pairs.items()})
    r = check_prompt(f"{SOLO}, sensitive, sitting, day", cc(con=c, rating="sensitive"))
    assert {"1girl", "solo", "sensitive"} <= set(tags_of(r)) and "sitting" not in tags_of(r) and "day" not in tags_of(r)     # the loser is the ordinary tag
    r = check_prompt(f"{SOLO}, night, smile", cc(con=c, protected=[*PREFIX, "night"]))
    assert "night" in tags_of(r) and "smile" not in tags_of(r)               # a trigger word or style tag always wins
    r = check_prompt(f"{SOLO}, night, smile", cc(con=c, protected=[*PREFIX, "night", "smile"]))
    assert {"night", "smile"} <= set(tags_of(r)) and not r.notes             # two protected tags: nothing to decide, nothing to say


def test_what_the_keep_check_demands_is_not_removed():
    r = run(f"{SOLO}, sitting, lying", keep_text="a figure lying down")       # the job's description says lying; the person did not type it
    assert {"sitting", "lying"} <= set(tags_of(r))
    assert any("lying" in n and "asks for it" in n for n in r.notes)
    assert not [c for c in r.changes if "contradicts" in c["why"]]


def test_a_word_the_check_put_back_does_not_outrank_what_the_writer_wrote():
    # "time of day" makes the keep check add `day`; the writer wrote `night` for it: `night` must not be sacrificed
    r = run(f"{SOLO}, night, sky", typed="six pictures, each at a different time of day", keep_text="each at a different time of day")
    assert {"night", "day"} <= set(tags_of(r)) and not [c for c in r.changes if "contradicts" in c["why"]]


def test_weights_spaces_underscores_and_aliases():
    r = run(f"{SOLO}, (night:1.3), DAY")
    assert [c["from"] for c in r.changes if "contradicts" in c["why"]] == ["day"]       # `DAY` was written `day` by the check first
    assert "(night:1.3)" in tags_of(r)                                         # the survivor keeps its weight
    r = run(f"{SOLO}, day, (night:0.8)")
    assert [c["from"] for c in r.changes if "contradicts" in c["why"]] == ["(night:0.8)"]       # a weighted loser goes whole
    r = run(f"{SOLO}, lying_on_side, sitting")
    assert "lying on side" in tags_of(r) and "sitting" not in tags_of(r)         # `lying_on_side` and `lying on side` are one tag
    tags = ["daytime", "night"]                                                  # an alias is judged as the tag it stands for
    c = cc()
    changes, notes = [], []
    assert drop_contradictions(tags, c, changes, notes) == ["daytime"] and changes[0]["from"] == "night"


def test_it_is_repeatable_and_idempotent():
    text = f"{SOLO}, day, sitting, night, lying, standing, smile"
    first = run(text)
    assert run(text).text == first.text and run(text).changes == first.changes
    again = run(first.text)
    assert again.text == first.text and not [c for c in again.changes if "contradicts" in c["why"]]
    typed_both = run(f"{SOLO}, sitting, lying", typed="sitting and lying")
    assert run(typed_both.text, typed="sitting and lying").text == typed_both.text and run(typed_both.text, typed="sitting and lying").notes == typed_both.notes


def test_a_chain_of_contradictions_is_settled_in_one_pass():
    r = run(f"{SOLO}, sitting, lying, standing")
    assert [t for t in tags_of(r) if t in ("sitting", "lying", "standing")] == ["sitting"]
    assert len([c for c in r.changes if "contradicts" in c["why"]]) == 2


def test_one_person_or_a_group_picks_the_limits():
    assert one_person_tags(["1girl", "solo"], cc()) and one_person_tags(["1girl"], cc()) and one_person_tags(["sitting"], cc(subjects={"girls": 1}))
    assert not one_person_tags(["2girls", "multiple_girls"], cc()) and not one_person_tags(["sitting"], cc())       # nobody known: the strict limits
    assert not one_person_tags(["1girl", "solo"], cc(subjects={"girls": 3}))
    group = run("masterpiece, best quality, score_9, newest, 2girls, multiple girls, sitting, lying, smile")
    assert {"sitting", "lying"} <= set(tags_of(group)) and not group.notes     # two people may differ: judged by the strict plain ratio, which they pass
    group = run("masterpiece, best quality, score_9, newest, 2girls, multiple girls, day, night")
    assert "night" not in tags_of(group)                                       # but one sky for everybody
    unknown = run("masterpiece, best quality, score_9, newest, sitting, lying")
    assert {"sitting", "lying"} <= set(tags_of(unknown))
    known = run("masterpiece, best quality, score_9, newest, sitting, lying", subjects={"girls": 1, "boys": None})
    assert "lying" not in tags_of(known)


def test_sentence_cards_are_skipped_and_mixed_cards_judge_only_the_tags():
    text = f"{SOLO}, sitting, lying"
    assert run(text, kind="sentences").text == text and not run(text, kind="sentences").changes
    mixed = run(f"{SOLO}, a day in the life of a night watchman, sitting, lying", kind="mixed")
    assert "a day in the life of a night watchman" in mixed.text and "lying" not in tags_of(mixed)        # the sentence is not a tag; the tags are judged
    mixed = run(f"{SOLO}, a quiet scene by day, day, night", kind="mixed")
    assert "a quiet scene by day" in mixed.text and "night" not in tags_of(mixed)


def test_a_family_without_danbooru_rules_is_left_alone():
    context = cc()
    context.rules = None
    assert check_prompt(f"{SOLO}, sitting, lying", context).text.endswith("sitting, lying")


def test_unknown_characters_and_wildcard_tokens_are_never_judged():
    r = run(f"{SOLO}, aria vale, __wild/card__, {{day|night}}, sitting")
    assert "aria vale" in tags_of(r) and "__wild/card__" in r.text and "{day|night}" in r.text


# ---------- the size floor ----------

def test_the_size_floor_does_not_add_a_tag_that_contradicts():
    tags = [*PREFIX, "1girl", "solo", "sitting"]
    context = cc(detail_level="high", slots={"pose": "lying, smiling", "expression": "smile"}, offered=["lying", "smile", "blush"])
    changes = []
    out = top_up(tags, context, changes)
    assert "smile" in out and "lying" not in out
    assert [c["to"] for c in changes] == ["smile"]
    assert contradicts_present("lying", tags, context) and not contradicts_present("smile", tags, context)
    # without evidence the candidate is allowed (nothing else contradicts it)
    free = cc(con=None, detail_level="high", slots={"pose": "lying"}, offered=["lying"])
    assert "lying" in top_up(tags, free, [])


# ---------- the planner ----------

def test_the_planner_builds_the_check_with_the_evidence_for_new_plans_and_tweaks(app):
    ctx = app.state.planner.ctx
    ctx.tag_library = library()
    ctx.contradiction_data = evidence()
    card = anima_card()
    step = TagStep.build(ctx, card, {"girls": 1, "boys": None, "others": None, "no_humans": None}, list(PREFIX), "", scene={"mood": "calm"})
    assert step is not None and step.check_context.contradictions is ctx.contradictions()
    result = step.check(f"{SOLO}, sitting, lying")                              # tweak.py uses the same TagStep.build and .check
    assert "lying" not in tags_of(result) and records(result)[-1]["to"] is None
    ctx.contradiction_data = None
    assert TagStep.build(ctx, card, None, list(PREFIX), "").check_context.contradictions is None        # switched off by default: nothing changes


# ---------- the build script ----------

def test_the_build_script_makes_a_file_the_app_can_use(tmp_path, monkeypatch, capsys):
    tag_lines, pair_lines = csv_lines(corpus(), con_mod.PAIR_FLOOR)
    (tmp_path / "danbooru_tags.csv").write_text(NL.join(tag_lines) + NL, encoding="utf-8")
    (tmp_path / "danbooru_tags_cooccurrence.csv").write_text(NL.join([*pair_lines, "broken row"]) + NL, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("build_contradictions", con_mod.Path(__file__).resolve().parent.parent / "scripts" / "build_contradictions.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    out = tmp_path / "out" / "contradictions.json"
    monkeypatch.setattr(sys, "argv", ["build", str(tmp_path / "danbooru_tags_cooccurrence.csv"), "--taglib", str(tmp_path / "no-library"), "--out", str(out)])
    script.main()
    text = capsys.readouterr().out
    assert "1 malformed" in text and out.is_file()
    built = load_contradictions(out)
    assert built.contradicts("day", "night", True) and not built.contradicts("smile", "blush", True)
    monkeypatch.setattr(sys, "argv", ["build", str(tmp_path / "missing.csv")])
    with pytest.raises(SystemExit):
        script.main()


# ---------- competition: the statistics only judge pairs that compete for the same thing ----------

def test_the_noun_and_the_words_in_front_of_it():
    noun = con_mod.noun_and_front
    assert noun("blue_eyes") == ("eye", ["blue"]) and noun("Very Long Hair") == ("hair", ["very", "long"]) and noun("crossed legs") == ("leg", ["crossed"])
    assert noun("sitting") is None and noun("hair_over_one_eye") is None and noun("hand_on_hip") is None      # one word, or a phrase with a link word
    nouns = {"blue": {"eye", "hair", "dress"}, "brown": {"eye", "hair", "dress"}, "closed": {"eye"}, "long": {"hair", "sleeve", "skirt"},
             "short": {"hair", "sleeve", "skirt"}, "red": {"eye", "dress", "skirt"}}
    kind = lambda a, b: con_mod.kind_of(a, b, nouns)
    assert kind("blue_eyes", "brown_eyes") == 1.0 and kind("long_hair", "short_hair") == 1.0
    assert kind("blue_eyes", "blue_hair") is None and kind("sitting", "lying") is None and kind("blue_eyes", "blue_eyes") is None      # another noun, or no noun, or the same value
    assert kind("blue_eyes", "closed_eyes") == 0.0                          # `closed` stands in front of too few nouns to say anything
    assert kind("blue_eyes", "red_eyes") == pytest.approx(2 / 4)            # two nouns in common out of four


def test_the_vocabulary_says_which_words_are_alike():
    names = ["blue_eyes", "brown_eyes", "blue_hair", "brown_hair", "blue_dress", "closed_eyes", "rare_dress", "hair_over_eyes"]
    counts = {n: 900 for n in names} | {"rare_dress": 5}
    assert con_mod.front_nouns(names, counts) == {"blue": {"eye", "hair", "dress"}, "brown": {"eye", "hair"}, "closed": {"eye"}}       # a rare tag and a phrase do not count


def test_same_noun_alternatives_are_flagged_with_evidence_only(competition_only):
    c = evidence()
    for a, b in [("blue_eyes", "brown_eyes"), ("blue_hair", "brown_hair")]:
        pair = c.evidence(a, b)
        assert pair.kind == 1.0 and pair.solo_ratio <= con_mod.ALT_MAX_RATIO, (a, b)
        assert c.contradicts(a, b, single=True) and c.contradicts(b, a, single=True)
    # the same noun and alike words, but the data says they go together (dresses are layers): they stay
    pair = c.evidence("blue_dress", "brown_dress")
    assert pair is None or pair.kind == 1.0 and (pair.solo_ratio or 0) > con_mod.ALT_MAX_RATIO
    assert not c.contradicts("blue_dress", "brown_dress", single=True)
    # a colour and a state are not alternatives, and the data says they go together
    assert not c.contradicts("blue_eyes", "closed_eyes", single=True) and not c.contradicts("brown_eyes", "closed_eyes", single=True)
    # a colour of one thing and a colour of another thing are not alternatives either
    assert not c.contradicts("blue_eyes", "blue_hair", single=True) and not c.contradicts("blue_eyes", "brown_hair", single=True)


def test_the_profile_test_is_not_applied_to_alternatives_of_one_noun(competition_only, monkeypatch):
    c = evidence()
    monkeypatch.setattr(con_mod, "MIN_SIMILARITY", 0.999)            # the old test would reject everything
    assert c.contradicts("blue_eyes", "brown_eyes", single=True)
    monkeypatch.setattr(con_mod, "ALT_MIN_SIMILARITY", 0.999)
    assert not c.contradicts("blue_eyes", "brown_eyes", single=True)


def test_only_competing_pairs_are_judged_when_competition_is_required(competition_only):
    c = evidence()
    for a, b in [("day", "night"), ("sitting", "lying"), ("sitting", "standing"), ("long_hair", "short_hair")]:
        assert c.evidence(a, b) is not None and not c.contradicts(a, b, single=True), (a, b)        # strong evidence, but not the same noun with alike words
    # a framing tag against a pose: improbable together, and a perfect profile match cannot make them competitors
    strong = Pair(0.0, 5000.0, 0.01, 5000.0, 0.99)
    framing = Contradictions({"cowboy_shot": 50_000, "crossed_legs": 50_000}, {("cowboy_shot", "crossed_legs"): strong})
    assert not framing.contradicts("cowboy_shot", "crossed_legs", single=True) and not framing.contradicts("cowboy_shot", "crossed_legs", single=False)
    # the same numbers for two tags with the same noun and alike words do count
    same_noun = Contradictions({"spread_legs": 50_000, "crossed_legs": 50_000}, {("crossed_legs", "spread_legs"): strong._replace(kind=0.4)})
    assert same_noun.contradicts("spread_legs", "crossed_legs", single=True)
    # ... but not when the words in front are not alike (a colour and a state)
    unlike = Contradictions({"blue_eyes": 50_000, "closed_eyes": 50_000}, {("blue_eyes", "closed_eyes"): strong._replace(kind=0.02)})
    assert not unlike.contradicts("blue_eyes", "closed_eyes", single=True)


def test_competing_pairs_are_judged_for_one_person_only(competition_only):
    c = evidence()
    assert c.contradicts("blue_eyes", "brown_eyes", single=True) and not c.contradicts("blue_eyes", "brown_eyes", single=False)      # two people may differ
    assert not c.contradicts("day", "night", single=False)                   # no group rule without OUTSIDE_COMPETITION: shared things are not told from per-person ones
    group = run("masterpiece, best quality, score_9, newest, 2girls, multiple girls, blue_eyes, brown_eyes, day, night")
    assert {"blue eyes", "brown eyes", "day", "night"} <= set(tags_of(group)) and not group.notes


def test_the_limits_of_competing_pairs_are_named_constants(competition_only, monkeypatch):
    c = evidence()
    assert c.contradicts("blue_eyes", "brown_eyes", single=True)
    monkeypatch.setattr(con_mod, "MIN_KIND", 1.01)
    assert not c.contradicts("blue_eyes", "brown_eyes", single=True)         # not alike enough to compete
    monkeypatch.setattr(con_mod, "MIN_KIND", 0.18)
    monkeypatch.setattr(con_mod, "ALT_MAX_RATIO", -1.0)
    assert not c.contradicts("blue_eyes", "brown_eyes", single=True)         # more often together than that
    monkeypatch.setattr(con_mod, "ALT_MAX_RATIO", 0.10)
    monkeypatch.setattr(con_mod, "ALT_MIN_EXPECTED", 10 ** 9)
    assert not c.contradicts("blue_eyes", "brown_eyes", single=True)         # too few pairs expected to say anything
    monkeypatch.setattr(con_mod, "ALT_MIN_EXPECTED", 20)
    monkeypatch.setattr(con_mod, "ALT_MAX_PLAIN_RATIO", 0.0)
    assert not c.contradicts("blue_eyes", "brown_eyes", single=True)
    monkeypatch.setattr(con_mod, "ALT_MAX_PLAIN_RATIO", 0.5)
    assert c.contradicts("blue_eyes", "brown_eyes", single=True)
    monkeypatch.setattr(con_mod, "OUTSIDE_COMPETITION", True)                # the strict limits come back for everything else
    assert c.contradicts("day", "night", single=True) and c.contradicts("day", "night", single=False)


def test_the_check_removes_the_less_wanted_value_in_a_prompt(competition_only):
    r = run(f"{SOLO}, blue_eyes, closed_eyes, brown_eyes, sitting, lying, brown_dress, blue_dress")
    gone = [c["from"] for c in r.changes if "contradicts" in c["why"]]
    assert gone == ["brown_eyes"] or gone == ["brown eyes"]                  # sitting/lying are not judged here; the dresses and the state stay
    assert {"blue eyes", "closed eyes", "sitting", "lying", "brown dress", "blue dress"} <= set(tags_of(r))
    # what the person wrote stays, even the second value
    r = run(f"{SOLO}, blue_eyes, brown_eyes", typed="one blue eye and one brown eye")
    assert not [c for c in r.changes if "contradicts" in c["why"]]


def test_a_prompt_that_asks_for_several_values_is_left_alone(competition_only):
    for extra in ("heterochromia", "multicolored hair", "Two-tone_hair"):
        r = run(f"{SOLO}, blue_eyes, brown_eyes, {extra}")
        assert {"blue eyes", "brown eyes"} <= set(tags_of(r)), extra
    assert con_mod.says_several_values(["blue_hair", "multicolored_hair"]) and not con_mod.says_several_values(["blue_hair", "hair_ornament"])
    tags = [*PREFIX, "1girl", "solo", "blue_eyes", "heterochromia"]
    assert not contradicts_present("brown_eyes", tags, cc())                  # the size floor does not add a second value either


def test_the_size_floor_does_not_add_a_second_value(competition_only):
    tags = [*PREFIX, "1girl", "solo", "blue_eyes"]
    context = cc(detail_level="high", slots={"body": "brown eyes, a smile", "expression": "smile"}, offered=["brown_eyes", "smile", "blush"])
    out = top_up(tags, context, [])
    assert "smile" in out and "brown eyes" not in out
    assert contradicts_present("brown_eyes", tags, context) and not contradicts_present("smile", tags, context)
