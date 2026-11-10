"""Content-based wildcard serving: per-entry slot attribution (planning/serving.py).

Stubbed, no LLM, no GPU, no embedding server. A tiny stub embedder returns
hand-set vectors, so every closeness score is exact. Lists are built
directly; the store is a stub whose lists() returns them. Wildcard text is
test data, never instructions.
"""
import random

import pytest

from app.planning import offers as O
from app.planning import serving as S
from app.planning.context import PlanContext
from app.wildcards.ideas import MAX_IDEA_ENTRY
from app.wildcards.store import WildcardStore, WildList


def wlist(name, entries, role="other", default=None, enabled=True, role_set=False):
    folder = name.split("/")[0] if "/" in name else ""
    default = default if default is not None else role
    return WildList(name, folder, role, default, enabled, list(entries), role_set=role_set)


class StubEmbedder:
    """Hand-set vectors per text; about texts get axis directions by default."""

    enabled = True

    def __init__(self, vecs=None, about=None):
        self.vecs = dict(vecs or {})
        self.about = about or {}

    def vec(self, text):
        if text in self.vecs:
            return self.vecs[text]
        return self.about.get(text)

    def cached(self, texts):
        return [self.vec(t) for t in texts]

    async def embed(self, texts, query=False):
        return [self.vec(t) for t in texts]


@pytest.fixture
def three_slots(db, config):
    """Config with three short slot descriptions and a matching stub embedder."""
    config.wildcards_slot_about = {"camera": "cam about", "gaze": "gaze about", "pose": "pose about"}
    config.wildcards_role_slots = {"shot": "camera", "gaze": "gaze", "pose": "pose"}
    cam, gaze, pose = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]
    emb = StubEmbedder(about={"cam about": cam, "gaze about": gaze, "pose about": pose})
    return config, emb, {"camera": cam, "gaze": gaze, "pose": pose}


def serve_entries(config, emb, entries, name="misc/views", role="other", **kw):
    vecs = [emb.vec(e) for e in entries]
    assert all(v is not None for v in vecs), "stub has no vector for an entry"
    rows = S.closeness(emb, config, entries, vecs)
    return S.by_meaning(wlist(name, entries, role), entries, rows, config)


# ---------- per-entry attribution ----------

def test_each_entry_goes_to_its_closest_slot(three_slots):
    config, emb, d = three_slots
    emb.vecs.update({"cam one": d["camera"], "gaze one": d["gaze"], "pose one": d["pose"]})
    got = serve_entries(config, emb, ["cam one", "gaze one", "pose one"])
    assert got.by == "meaning" and got.sure is True
    assert got.slots == {"camera": ["cam one"], "gaze": ["gaze one"], "pose": ["pose one"]}


def test_an_entry_between_two_slots_feeds_neither(three_slots):
    config, emb, d = three_slots
    emb.vecs["mid"] = [0.5, 0.5, 0.0]           # equally close to camera and gaze
    emb.vecs["cam one"] = d["camera"]
    got = serve_entries(config, emb, ["mid", "cam one"], name="misc/a")
    assert got.slots.get("camera") == ["cam one"]
    assert "mid" not in [e for es in got.slots.values() for e in es]


def test_an_entry_below_the_floor_feeds_nothing(three_slots):
    config, emb, d = three_slots
    emb.vecs.update({"low": [0.1, 0.05, 0.0], "cam one": d["camera"]})
    config.wildcards_fit_floor = 0.30
    got = serve_entries(config, emb, ["low", "cam one"], name="misc/a")
    assert "low" not in [e for es in got.slots.values() for e in es]


def test_floor_and_lead_are_honest_thresholds(three_slots):
    config, emb, d = three_slots
    emb.vecs.update({"edge": [0.25, 0.24, 0.0], "cam one": d["camera"]})
    config.wildcards_fit_floor = 0.30
    assert "edge" not in [e for es in serve_entries(config, emb, ["edge", "cam one"], name="misc/a").slots.values() for e in es]
    config.wildcards_fit_floor = 0.20
    config.wildcards_fit_lead = 0.05           # lead 0.01 < 0.05: still nowhere
    assert "edge" not in [e for es in serve_entries(config, emb, ["edge", "cam one"], name="misc/b").slots.values() for e in es]
    config.wildcards_fit_lead = 0.0
    got = serve_entries(config, emb, ["edge", "cam one"], name="misc/c")
    assert "edge" in got.slots.get("camera", [])


def test_a_slot_counts_from_a_quarter_of_the_list_up(three_slots):
    config, emb, d = three_slots
    emb.vecs.update({f"c{i}": d["camera"] for i in range(2)} | {f"p{i}": d["pose"] for i in range(6)})
    entries = [f"c{i}" for i in range(2)] + [f"p{i}" for i in range(6)]
    got = serve_entries(config, emb, entries, name="misc/mixed")
    assert set(got.slots) == {"camera", "pose"}          # 2/8 each is above MIN_SHARE
    emb.vecs.update({f"x{i}": d["pose"] for i in range(7)})
    lonely = ["c0"] + [f"x{i}" for i in range(7)]
    got = serve_entries(config, emb, lonely, name="misc/lonely")
    assert "camera" not in got.slots                     # 1/8 is below MIN_SHARE


def test_a_mixed_list_feeds_both_slots(three_slots):
    config, emb, d = three_slots
    emb.vecs.update({"c1": d["camera"], "c2": d["camera"], "g1": d["gaze"], "g2": d["gaze"]})
    got = serve_entries(config, emb, ["c1", "c2", "g1", "g2"], name="misc/mixed")
    assert got.slots == {"camera": ["c1", "c2"], "gaze": ["g1", "g2"]}


# ---------- names and folders do not decide ----------

def test_odd_names_and_a_flat_folder_still_read_by_meaning(three_slots):
    config, emb, d = three_slots
    emb.vecs.update({"cam one": d["camera"], "gaze one": d["gaze"]})
    odd = serve_entries(config, emb, ["cam one", "gaze one"], name="views/mixed-angles", role="other")
    assert odd.slots == {"camera": ["cam one"], "gaze": ["gaze one"]}
    flat = serve_entries(config, emb, ["cam one", "gaze one"], name="topfile", role="other")
    assert flat.slots == {"camera": ["cam one"], "gaze": ["gaze one"]}


def test_the_folder_hint_tips_but_never_makes_a_fit(three_slots):
    config, emb, d = three_slots
    emb.vecs.update({"low": [0.1, 0.05, 0.0], "cam one": d["camera"]})
    config.wildcards_fit_floor = 0.30
    hinted = wlist("camera/views", ["low"], role="shot", default="shot")
    rows = S.closeness(emb, config, ["low"], [emb.vec("low")])
    assert S.by_meaning(hinted, ["low"], rows, config).slots == {}


# ---------- the user's role ----------

def test_the_user_role_wins_and_survives_reimport(three_slots):
    config, emb, d = three_slots
    emb.vecs["cam one"] = d["camera"]
    mine = wlist("misc/mine", ["cam one"], role="pose", role_set=True)
    got = S.serve_now([mine], config, emb, O.plain_entries)[mine.name]
    assert got.by == "you" and got.slots == {"pose": ["cam one"]}
    again = wlist("misc/mine", ["cam one"], role="pose", role_set=True)   # rebuilt after a re-import, settings kept
    assert S.serve_now([again], config, None, O.plain_entries)[again.name].slots == {"pose": ["cam one"]}
    assert S.by_you(mine, ["cam one"], config).slots == {"pose": ["cam one"]}


def test_a_role_with_no_slot_serves_nothing_but_is_kept(three_slots):
    config, emb, _ = three_slots
    mine = wlist("style/mine", ["brass gears"], role="style", role_set=True)
    got = S.serve_now([mine], config, None, O.plain_entries)[mine.name]
    assert got.by == "you" and got.slots == {}


# ---------- words fallback ----------

def test_words_fallback_when_embeddings_are_missing(config):
    l = wlist("camera/views", ["wide shot", "canon camera"], role="shot", default="shot")
    got = S.serve_now([l], config, None, O.plain_entries)[l.name]
    assert got.by == "words" and got.sure is False
    assert "wide shot" in got.slots.get("camera", [])


def test_words_fallback_when_entries_are_not_embedded_yet(three_slots):
    config, emb, _ = three_slots

    class HalfEmbedder(StubEmbedder):
        def cached(self, texts):
            return [None] * len(texts)          # nothing stored yet: read by words this time

    l = wlist("pose/poses", ["standing pose"], role="pose", default="pose")
    got = S.serve_now([l], config, HalfEmbedder(), O.plain_entries)[l.name]
    assert got.by == "words"


def test_words_fallback_marks_low_confidence(config):
    l = wlist("misc/stuff", ["red kimono"], role="other", default="other")
    got = S.serve_now([l], config, None, O.plain_entries)[l.name]
    assert got.by == "words" and got.sure is False


# ---------- other languages, long entries, avoided entries ----------

def test_non_english_entries_do_not_crash(three_slots):
    config, emb, d = three_slots
    entries = ["Froschperspektive, dramatisch", "contre-plong\u00e9e du personnage",
               "\u898b\u4e0a\u3052\u69cb\u56f3\u306e\u7167\u7247", "looking away"]
    emb.vecs.update({e: d["camera"] for e in entries})
    got = serve_entries(config, emb, entries, name="views/fremd")
    assert got.by == "meaning" and got.seen == 4
    words = S.serve_now([wlist("views/fremd", entries, role="other")], config, None, O.plain_entries)
    assert words["views/fremd"].by == "words"


def test_long_entries_are_reported_and_never_offered(config):
    long = "x" * (MAX_IDEA_ENTRY + 1)
    l = wlist("style/dumps", [long, "brass gears"], role="style", default="style")
    assert O.plain_entries(l) == ["brass gears"]
    got = S.serve_now([l], config, None, O.plain_entries)[l.name]
    assert got.long == 1
    assert all(len(e) <= MAX_IDEA_ENTRY for es in got.slots.values() for e in es)


def test_no_avoided_entry_is_offered(config):
    young = "a 10 year old girl"                # matches the age check: never offerable
    l = wlist("pose/poses", [young, "arms crossed"], role="pose", default="pose")
    assert O.plain_entries(l) == ["arms crossed"]
    got = S.serve_now([l], config, None, O.plain_entries)[l.name]
    assert young not in [e for es in got.slots.values() for e in es]


# ---------- caps, gaps, digest, empty library ----------

def slot_ctx(db, config, lists):
    store = WildcardStore(db, config)
    store.lists = lambda: lists                 # noqa: E731
    return PlanContext(config, None, None, None, db, wildcards=store)


async def test_per_slot_and_per_list_caps(db, config):
    mine = [wlist(f"camera/c{i}", [f"angle {i}-{j}" for j in range(160)], role="shot", default="shot") for i in range(8)]
    sv = {l.name: S.Serving({"camera": list(l.entries)}, "meaning", True, 0, len(l.entries)) for l in mine}
    ctx = slot_ctx(db, config, mine)
    out = await O.slot_lists(ctx, [], mine, set(), {"camera"}, "a portrait of an adult woman", sv)
    assert len(out["camera"]) <= O.MAX_LISTS_PER_SLOT
    assert all(len(l.entries) <= O.PER_LIST_POOL for l in out["camera"])


async def test_gaps_recorded_when_no_list_covers_a_slot(db, config):
    style = [wlist("style/steampunk", ["brass gears", "copper pipes"], role="style", default="style")]
    ctx = slot_ctx(db, config, style)
    gaps: list[str] = []
    got = await O.build(ctx, "a portrait of an adult woman", "", [], [], None, random.Random(1),
                        images=1, rating="general", gaps=gaps, always=True)
    assert "camera" in gaps and "gaze" in gaps


async def test_a_covering_list_closes_its_gap(db, config):
    lists = [wlist("camera/views", ["from above", "wide shot"], role="shot", default="shot"),
             wlist("style/steampunk", ["brass gears"], role="style", default="style")]
    ctx = slot_ctx(db, config, lists)
    gaps: list[str] = []
    await O.build(ctx, "a portrait of an adult woman", "", [("camera/views", "why", "camera")], [], None,
                  random.Random(1), images=1, rating="general", gaps=gaps, always=True)
    assert "camera" not in gaps and "gaze" in gaps


async def test_a_library_with_no_wildcards(db, config):
    ctx = slot_ctx(db, config, [])
    gaps: list[str] = []
    assert await O.build(ctx, "a portrait", "", [], [], None, random.Random(1), gaps=gaps) is None
    assert gaps == []


def test_digest_counts_slots_mixed_none_and_low(config):
    served = {"a": S.Serving({"camera": ["x"], "gaze": ["y"]}, "meaning", True, 0, 4),
              "b": S.Serving({}, "meaning", True, 0, 2),
              "c": S.Serving({"pose": ["z"]}, "words", False, 0, 1)}
    assert S.digest(served) == {"slots": {"camera": 1, "gaze": 1, "pose": 1}, "mixed": 1, "none": 1, "low": 1, "lists": 3}
