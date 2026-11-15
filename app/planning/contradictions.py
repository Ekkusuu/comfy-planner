"""Contradicting tags: two tags that are alternatives for the same thing and almost never appear together in real
pictures, found from how often Danbooru tags occur together. There is no list of pairs in the app: the evidence is computed from the
public co-occurrence counts and kept in one small derived file, built once by `scripts/build_contradictions.py`.

The counts alone cannot tell "cannot be together" from "improbable together" (a cowboy-shot framing and crossed legs almost never appear
together, yet they are not alternatives). So the statistics only judge pairs that COMPETE for the same thing, and that is read from the
tag names and the vocabulary, not from a list: both tags end in the same noun and the words in front of it are the same kind of word
(`blue eyes`/`brown eyes`, `long hair`/`short hair`, `white background`/`black background`). Words are "the same kind" when they stand in
front of the same nouns across the tag vocabulary (`blue` and `brown`: eyes, hair, dress, ...; `blue` and `closed`: hardly any), so a
colour and a state, `blue eyes`/`closed eyes`, are not alternatives. Such a pair is contradicting when the ratio of observed pairs to
the pairs expected if the two were unrelated, among `solo` pictures (so a group with two different people does not count), is at most
ALT_MAX_RATIO, with enough expected pairs (ALT_MIN_EXPECTED) and a tag used often enough (MIN_TAG_COUNT) to mean something. A pair the
data does not list was seen fewer than PAIR_FLOOR times, so its observed count is at most PAIR_FLOOR - 1: that bound is used.
Only one-person pictures are judged: a group has per-person attributes, and the shared-thing conflicts (`day`/`night`) cannot be told
from per-person ones or from framing tags by these statistics. Pairs that do not compete are not judged at all, unless
OUTSIDE_COMPETITION: the first version judged them by the plain ratio plus a test of alike neighbours (`similarity`, the cosine of the PPMI
neighbour profiles, MIN_SIMILARITY), which let framing against pose through.

Pictures with several people are counted differently. A drawing of a group has `sitting` and `lying` side by side, so for one person
the ratio is estimated among `solo` pictures only (the share of pairs that groups would explain is taken out).

The data is untrusted text: it is only parsed and compared. Without the derived file (a fresh install) every function here is
skipped quietly. The limits below are the knobs; changing them needs no rebuild as long as the file holds the pair (the file keeps
a looser set, STORE_*)."""
import csv
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, NamedTuple

from app.planning.taglib import key

log = logging.getLogger("planning.tags")

# The check is ON, with low recall: it judges only pairs that compete for the same thing (above). Without the first version's
# "everything improbable" rule the measured wrong removals are zero. Set False to turn it off;
# it also does nothing without data/cooccurrence/contradictions.json.
CONTRADICTIONS_ENABLED = True

DATA_FOLDER = "cooccurrence"                  # under the data directory; holds the downloaded CSVs and the derived file
DERIVED_FILE = "contradictions.json"
MAX_FILE = 30_000_000                         # bytes; a bigger derived file is not read
FORMAT = 2

# ---------- the strict limits (kept for OUTSIDE_COMPETITION: pairs that do not compete; one person, then a group or nobody known) ----------
MIN_TAG_COUNT = 15_000        # a tag used fewer times than this in the data gives no verdict
MIN_EXPECTED = 500            # pairs expected among solo pictures if the tags were unrelated; fewer is too little to judge
MAX_RATIO = 0.03              # observed / expected among solo pictures at most this ("never together" once the groups are taken out)
MAX_PLAIN_RATIO = 0.50        # ... and the plain ratio over all pictures must also be below chance this much (the group correction alone can overshoot)
MIN_SIMILARITY = 0.40         # cosine of the neighbour profiles at least this
# ... a group, or nobody known: the plain ratio over all pictures
GROUP_MIN_EXPECTED = 1_000
GROUP_MAX_RATIO = 0.15
# ---------- competition: the statistics only judge pairs that compete for the same thing ----------
# Two tags compete when they are values of one attribute: they end in the same noun (`long hair`/`short hair`, `blue eyes`/`brown eyes`,
# `spread legs`/`crossed legs`) and the words in front of it are the same kind of word. "Same kind" is read from the tag vocabulary itself:
# the words `blue` and `brown` stand in front of the same nouns (eyes, hair, dress, ...), while `blue` and `closed` hardly do. Such a pair
# gets the ALT_ limits and no profile test; a pair that does not compete is not judged (with OUTSIDE_COMPETITION: judged by the strict limits above).
MIN_KIND = 0.18               # least overlap (Jaccard) of the nouns that the two differing words stand in front of
MODIFIER_MIN_COUNT = 2_000    # a vocabulary tag counts for that overlap when it is used at least this often
MIN_MODIFIER_NOUNS = 3        # a word that stands in front of fewer nouns than this says nothing about its kind
LINK_WORDS = frozenset("a an and at between by for from in of on over past the through to under with without".split())     # a tag with one of these is a phrase, not a value
ALT_MIN_EXPECTED = 200        # pairs expected among solo pictures (competing pairs)
ALT_MAX_RATIO = 0.10          # observed / expected among solo pictures at most this (competing pairs)
ALT_MAX_PLAIN_RATIO = 0.50    # ... and the plain ratio at most this
ALT_MIN_SIMILARITY = 0.0      # no profile test: `long hair`/`short hair` (0.11) is an alternative with unlike neighbours
OUTSIDE_COMPETITION = False   # True: pairs that do not compete are judged too, with the strict limits above (the first version); False: they are not judged
# A prompt that says it wants several values at once (`heterochromia`, `multicolored hair`, `two-tone eyes`) is not judged at all: `blue eyes` with `red eyes`
# is what it asks for. These are words of such tags, not pairs.
SEVERAL_VALUES_WORDS = frozenset("heterochromia multicolored multi-colored two-tone gradient streaked split-color mismatched rainbow patchwork".split())
# ---------- reading the data ----------
PAIR_FLOOR = 100              # the co-occurrence file only lists pairs seen at least this often
CONTEXT_MIN_COUNT = 20_000    # a tag must be this common to count as a neighbour in a profile
SOLO_TAG = "solo"
PERSON_TAGS = ("1girl", "1boy", "1other", "multiple_girls", "multiple_boys", "multiple_others", "no_humans")      # to count the pictures
# ---------- what the derived file keeps (looser than the limits above, so they can move without a rebuild) ----------
STORE_MAX_RATIO = 0.30
STORE_MIN_EXPECTED_SOLO = 200
STORE_MIN_EXPECTED_ALL = 400
STORE_MIN_SIMILARITY = 0.20
STORE_MIN_KIND = 0.10


class Pair(NamedTuple):
    solo_ratio: float | None      # observed / expected among solo pictures (an upper bound when the pair is not listed); None: no solo data
    solo_expected: float          # pairs expected among solo pictures
    ratio: float                  # observed / expected over all pictures (an upper bound when the pair is not listed)
    expected: float
    similarity: float             # cosine of the two neighbour profiles
    kind: float | None = None     # the two tags end in the same noun: how alike the words in front of it are (None: they do not share a noun)


@dataclass
class Derived:
    images: int                                       # estimated number of pictures behind the counts
    solo: int
    counts: dict[str, int]                            # tag -> times used, for the tags that appear in a pair
    pairs: dict[tuple[str, str], Pair] = field(default_factory=dict)      # (a, b) with a < b, both in `key` form


# ---------- reading the downloaded CSVs ----------

def _count(text: str) -> int:
    """A count such as `3816210.0` or `42`; ValueError for anything else (text, negative, infinite)."""
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError(text)
    return int(value)


def _name(text: str) -> str:
    """A tag name from the data: plain text of sensible length, in `key` form; ValueError otherwise."""
    name = key(text)
    if not name or len(name) > 120 or not name.isprintable():
        raise ValueError(text)
    return name


def _rows(lines: Iterable[str]):
    """(row number, fields) for every row; fields is None for a row the CSV reader itself refuses (a stray carriage return, say)."""
    reader = csv.reader(lines)
    n = 0
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error:
            row = None
        yield n, row
        n += 1


def read_tag_counts(lines: Iterable[str]) -> tuple[dict[str, int], int]:
    """({tag: times used}, malformed rows) from `tag,category,count[,aliases]` rows. The header row is not counted as malformed."""
    counts: dict[str, int] = {}
    bad = 0
    for n, row in _rows(lines):
        if n == 0 and row and row[:1] == ["tag"]:
            continue
        try:
            counts[_name(row[0])] = _count(row[2])
        except (ValueError, IndexError, TypeError):
            bad += 1
    return counts, bad


def read_pair_counts(lines: Iterable[str], wanted: set[str] | None = None) -> tuple[dict[str, dict[str, int]], int]:
    """({tag: {other tag: times together}}, malformed rows) from `tag_a,tag_b,count` rows, each pair in both directions. With
    `wanted`, only rows whose two tags are both in it are kept (the file has millions of rows)."""
    adj: dict[str, dict[str, int]] = {}
    bad = 0
    for n, row in _rows(lines):
        if n == 0 and row and row[:1] == ["tag_a"]:
            continue
        try:
            a, b, count = _name(row[0]), _name(row[1]), _count(row[2])
        except (ValueError, IndexError, TypeError):
            bad += 1
            continue
        if a != b and (wanted is None or (a in wanted and b in wanted)):
            adj.setdefault(a, {})[b] = count
            adj.setdefault(b, {})[a] = count
    return adj, bad


# ---------- deriving the evidence ----------

def estimate_images(counts: dict[str, int], adj: dict[str, dict[str, int]]) -> int:
    """About how many pictures the counts come from. The file does not say, so: every picture says who is in it (PERSON_TAGS), and
    the pictures with at least one of those tags are counted by inclusion and exclusion over pairs. An estimate, a little low."""
    tags = [t for t in PERSON_TAGS if t in counts]
    total = sum(counts[t] for t in tags)
    total -= sum(adj.get(a, {}).get(b, 0) for i, a in enumerate(tags) for b in tags[i + 1:])
    return max(total, max(counts.values(), default=0))


def _profile(tag: str, counts: dict[str, int], adj: dict[str, dict[str, int]], images: int) -> tuple[dict[str, float], float]:
    """The tag's neighbour profile: for every common tag next to it, the positive pointwise mutual information, and the vector's length."""
    ca = counts[tag]
    vec = {}
    for other, together in adj.get(tag, {}).items():
        if counts.get(other, 0) >= CONTEXT_MIN_COUNT and other != tag:
            pmi = math.log(together * images / (ca * counts[other]))
            if pmi > 0:
                vec[other] = pmi
    return vec, math.sqrt(sum(w * w for w in vec.values())) or 1.0


def noun_and_front(tag: str) -> tuple[str, list[str]] | None:
    """A tag's last word (the noun, plural folded: `crossed legs` -> leg) and the words in front of it; None for a one-word tag or a phrase
    with a link word (`hair over one eye`), which is not a value of an attribute."""
    words = [w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", key(tag).replace("_", " "))]
    if len(words) < 2 or any(w in LINK_WORDS for w in words):
        return None
    return words[-1], words[:-1]


def says_several_values(tags: Iterable[str]) -> bool:
    """Does one of the tags say that several values are wanted at once (`heterochromia`, `multicolored hair`, `two-tone eyes`)?"""
    return any(w in SEVERAL_VALUES_WORDS for tag in tags for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", key(tag).replace("_", " ")))


def front_nouns(names: Iterable[str], counts: dict[str, int]) -> dict[str, set[str]]:
    """For every word that stands right in front of a noun, the nouns it stands in front of (`blue` -> eye, hair, dress, ...), from the tags used at least
    MODIFIER_MIN_COUNT times."""
    out: dict[str, set[str]] = {}
    for name in names:
        parts = noun_and_front(name) if counts.get(name, 0) >= MODIFIER_MIN_COUNT else None
        if parts:
            out.setdefault(parts[1][-1], set()).add(parts[0])
    return out


def kind_of(a: str, b: str, nouns: dict[str, set[str]]) -> float | None:
    """None unless the two tags end in the same noun and differ in the word in front of it. Then the Jaccard overlap of the nouns that the two
    words stand in front of: 0.7 for `blue`/`red`, 0.03 for `blue`/`closed`. 0 when one word stands in front of fewer than MIN_MODIFIER_NOUNS nouns."""
    pa, pb = noun_and_front(a), noun_and_front(b)
    if pa is None or pb is None or pa[0] != pb[0] or pa[1][-1] == pb[1][-1]:
        return None
    x, y = nouns.get(pa[1][-1], set()), nouns.get(pb[1][-1], set())
    if len(x) < MIN_MODIFIER_NOUNS or len(y) < MIN_MODIFIER_NOUNS:
        return 0.0
    return len(x & y) / len(x | y)


def derive(counts: dict[str, int], adj: dict[str, dict[str, int]], vocabulary: Iterable[str] | None = None, images: int | None = None) -> Derived:
    """The evidence for every pair of the vocabulary (tags with at least MIN_TAG_COUNT uses; the library's general tags in the
    build script) that is not hopelessly common together. See the module text for the numbers."""
    images = images or estimate_images(counts, adj)
    solo_n = counts.get(SOLO_TAG, 0)
    others = max(images - solo_n, 1)
    names = sorted(t for t in (counts if vocabulary is None else (key(v) for v in vocabulary)) if counts.get(t, 0) >= MIN_TAG_COUNT and t != SOLO_TAG)
    solo = {t: adj.get(t, {}).get(SOLO_TAG, 0) for t in names}
    profiles: dict[str, tuple[dict[str, float], float]] = {}
    nouns = front_nouns(counts if vocabulary is None else (key(v) for v in vocabulary), counts)
    out = Derived(images, solo_n, {})
    for i, a in enumerate(names):
        ca, row = counts[a], adj.get(a, {})
        for b in names[i + 1:]:
            cb = counts[b]
            seen = row.get(b)
            bound = seen if seen is not None else PAIR_FLOOR - 1          # not listed: seen fewer than PAIR_FLOOR times
            expected = ca * cb / images
            ratio = bound / expected
            solo_expected = solo[a] * solo[b] / solo_n if solo_n else 0.0
            solo_ratio = None
            if solo_expected > 0:
                from_groups = (ca - solo[a]) * (cb - solo[b]) / others      # the pairs the other pictures would give if the tags were unrelated there
                solo_ratio = max(bound - from_groups, 0.0) / solo_expected
            if not ((solo_ratio is not None and solo_expected >= STORE_MIN_EXPECTED_SOLO and solo_ratio <= STORE_MAX_RATIO)
                    or (expected >= STORE_MIN_EXPECTED_ALL and ratio <= STORE_MAX_RATIO)):
                continue
            for t in (a, b):
                if t not in profiles:
                    profiles[t] = _profile(t, counts, adj, images)
            (u, nu), (v, nv) = profiles[a], profiles[b]
            if len(u) > len(v):
                u, v = v, u
            similarity = sum(w * v.get(x, 0.0) for x, w in u.items()) / (nu * nv)
            kind = kind_of(a, b, nouns)
            if similarity >= STORE_MIN_SIMILARITY or (kind is not None and kind >= STORE_MIN_KIND):
                out.pairs[(a, b)] = Pair(None if solo_ratio is None else round(solo_ratio, 4), round(solo_expected, 1), round(ratio, 4), round(expected, 1),
                                         round(similarity, 3), None if kind is None else round(kind, 3))
    used = {t for pair in out.pairs for t in pair}
    out.counts = {t: counts[t] for t in sorted(used)}
    return out


# ---------- the derived file ----------

def write_derived(derived: Derived, path: Path) -> None:
    """The derived evidence as one JSON file: the tag names once, the pairs as number lists (small, and fast to read)."""
    names = list(derived.counts)
    at = {t: i for i, t in enumerate(names)}
    data = {"format": FORMAT, "images": derived.images, "solo": derived.solo, "tags": names, "counts": [derived.counts[t] for t in names],
            "pairs": [[at[a], at[b], *p] for (a, b), p in sorted(derived.pairs.items())]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


class Contradictions:
    """The evidence in memory, and the verdict on a pair of tags. Built by `load_contradictions` (or from a `Derived`)."""

    def __init__(self, counts: dict[str, int], pairs: dict[tuple[str, str], Pair], images: int = 0):
        self.counts, self.pairs, self.images = counts, pairs, images

    @classmethod
    def from_derived(cls, derived: Derived) -> "Contradictions":
        return cls(derived.counts, derived.pairs, derived.images)

    def __len__(self) -> int:
        return len(self.pairs)

    def evidence(self, a: str, b: str) -> Pair | None:
        """What the data says about the pair (None: nothing kept), whatever the limits."""
        ka, kb = key(a), key(b)
        return self.pairs.get((ka, kb) if ka < kb else (kb, ka)) if ka != kb else None

    def competes(self, pair: Pair) -> bool:
        """Do the two tags compete for the same thing: values of one attribute (`blue eyes`/`brown eyes`: the same noun, and the words in
        front of it are the same kind of word)?"""
        return pair.kind is not None and pair.kind >= MIN_KIND

    def verdict(self, a: str, b: str, single: bool) -> Pair | None:
        """The evidence when the two tags contradict under the current limits, else None. A tag used fewer than MIN_TAG_COUNT times or a
        pair with too small an expected count never conflicts. Only pairs that compete (`competes`) are judged, and only for one person
        (`single`: a group has per-person attributes, `cat ears` with `horse ears` are two people): the ratio among solo pictures at most
        ALT_MAX_RATIO, no profile test. With OUTSIDE_COMPETITION the other pairs are judged too, by the strict limits (one person) or the
        plain ratio with the group limits (a group, or nobody known)."""
        pair = self.evidence(a, b)
        if pair is None or min(self.counts.get(key(a), 0), self.counts.get(key(b), 0)) < MIN_TAG_COUNT:
            return None
        if single and self.competes(pair):
            ok = (pair.solo_ratio is not None and pair.solo_expected >= ALT_MIN_EXPECTED and pair.solo_ratio <= ALT_MAX_RATIO
                  and pair.ratio <= ALT_MAX_PLAIN_RATIO and pair.similarity >= ALT_MIN_SIMILARITY)
            return pair if ok else None
        if not OUTSIDE_COMPETITION:
            return None
        if single:
            enough = (pair.solo_ratio is not None and pair.solo_expected >= MIN_EXPECTED and pair.solo_ratio <= MAX_RATIO
                      and pair.ratio <= MAX_PLAIN_RATIO)
        else:
            enough = pair.expected >= GROUP_MIN_EXPECTED and pair.ratio <= GROUP_MAX_RATIO
        return pair if enough and pair.similarity >= MIN_SIMILARITY else None

    def contradicts(self, a: str, b: str, single: bool) -> bool:
        return self.verdict(a, b, single) is not None


def load_contradictions(path: Path) -> Contradictions | None:
    """The derived file, or None when it is missing, too big, or not usable (the caller then skips the check). Malformed pair
    entries are skipped and counted in the log."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            return None
        names, counts = data["tags"], data["counts"]
        if not (isinstance(names, list) and isinstance(counts, list) and len(names) == len(counts)):
            return None
        table = {str(n): int(c) for n, c in zip(names, counts) if isinstance(n, str)}
        pairs: dict[tuple[str, str], Pair] = {}
        bad = 0
        for row in data["pairs"]:
            try:
                i, j, sr, se, r, e, sim, kind = row
                a, b = names[i], names[j]
                if not (isinstance(a, str) and isinstance(b, str) and a != b):
                    raise ValueError
                pairs[(a, b) if a < b else (b, a)] = Pair(None if sr is None else float(sr), float(se), float(r), float(e), float(sim),
                                                          None if kind is None else float(kind))
            except (ValueError, TypeError, IndexError):
                bad += 1
        images = int(data.get("images", 0))
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as e:
        log.warning("the contradiction data in %s could not be read, the check is off: %s", path, e)
        return None
    if bad:
        log.warning("%d malformed entries skipped in %s", bad, path)
    log.info("contradiction data loaded: %d pairs over %d tags", len(pairs), len(table))
    return Contradictions(table, pairs, images)


_loaded: dict[str, tuple[float, Contradictions | None]] = {}
_used: dict[str, float] = {}            # when each was last asked for (time.monotonic), for unload_idle
_lock = threading.Lock()                # one read at a time: a second caller waits for the first one's result instead of reading again


def get_contradictions(folder: Path) -> Contradictions | None:
    """`load_contradictions` for `folder/DERIVED_FILE`, kept in memory while the file does not change (a rebuild is picked up
    without a restart) and until `unload_idle` drops it. Loaded on first use only (planning does that in a thread first:
    PlanContext.load_tag_data). None without the file."""
    path = folder / DERIVED_FILE
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    with _lock:
        cached = _loaded.get(str(path))
        if cached is None or cached[0] != stamp:
            cached = _loaded[str(path)] = (stamp, load_contradictions(path))
        _used[str(path)] = time.monotonic()
        return cached[1]


def unload_idle(seconds: float) -> bool:
    """Forget the data nobody has asked for during the last `seconds` (it is read again when needed). True if any was dropped.
    Never waits: while a read is going on it does nothing."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        now = time.monotonic()
        idle = [k for k, t in _used.items() if now - t >= seconds]
        for k in idle:
            _used.pop(k, None)
            _loaded.pop(k, None)
        return bool(idle)
    finally:
        _lock.release()
