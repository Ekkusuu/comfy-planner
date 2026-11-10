"""Which entries of a wildcard list - and which descriptions - are adult, told from meaning and not from a list of words.

A nsfw or explicit picture is offered entries that only an adult picture shows, and its descriptions are checked for them; "looking at viewer" and
"rear view" are ordinary. The tag library has no rating per tag and the co-occurrence counts cannot separate
a body-part framing from an ordinary one, so the content level of a text is read from how close its meaning is to a few fixed phrases that name
adult content (nudity, sexual activity: ADULT_ANCHORS) compared with its closeness to a few that name ordinary things (ORDINARY_ANCHORS). A text
counts as adult when it is at least ADULT_FLOOR close to an adult phrase and at least ADULT_MARGIN closer to it than to any ordinary phrase.
Without search by meaning nothing can be told: no entry is picked out and no description is judged."""
import re

from app.embeddings import dot
from app.planning.concepts import rating_rank
from app.planning.context import PlanContext
from app.planning.semantic import usable, vectors_or_background

ADULT_FLOOR = 0.62       # least closeness in meaning (cosine) to an adult phrase
ADULT_MARGIN = 0.16      # ... and at least this much closer to it than to the closest ordinary phrase
# The anchors are plain English phrases, the same for every user and library. Adult content at nsfw (nudity, sexual activity) and at explicit (only the sexual);
# ordinary content that a picture of any rating shows. Not a list of tags and not a filter: only what entries are compared with.
ADULT_ANCHORS = {"nsfw": ("nudity", "a naked body", "exposed breasts", "sexual activity", "sexual intercourse", "genitals in view"),
                 "explicit": ("sexual activity", "sexual intercourse", "genitals in view")}
ORDINARY_ANCHORS = ("a fully clothed person", "an everyday outfit", "a casual portrait", "a smiling face", "a calm landscape", "an ordinary room",
                    "a pose or a camera angle", "the direction of a gaze", "the time of day or the weather", "a hairstyle")
FRAGMENT = re.compile(r"[,.;:!?]+|\b(?:and|with|while|as|who|her|his|their)\b", re.I)       # where a description is cut into parts that say one thing each
MIN_FRAGMENT_WORDS = 2

_known: dict[tuple, dict[str, bool]] = {}        # (the adult and ordinary anchors) -> {text: adult or not}, so a library is judged once


def wants_adult(rating: str | None) -> bool:
    """Is the picture known to be adult (nsfw or explicit), so the adult entries are what it should be offered?"""
    return rating is not None and rating_rank(rating) >= rating_rank("nsfw")


def fragments(text: str) -> list[str]:
    """A description cut into its parts, plus the whole: a sentence that says a lot cannot be close to one tag, its parts can."""
    parts = [" ".join(p.split()) for p in FRAGMENT.split(text or "")]
    return list(dict.fromkeys([" ".join((text or "").split()), *(p for p in parts if len(p.split()) >= MIN_FRAGMENT_WORDS)]))


async def adult_entries(ctx: PlanContext, entries: list[str], minimum: str = "nsfw") -> set[str]:
    """The texts that are adult: like the adult anchors of the rating `minimum` (explicit: only the sexual ones).
    Empty when search by meaning is not available."""
    entries = list(dict.fromkeys(e for e in entries if e.strip()))
    if not entries or not usable(ctx):
        return set()
    adult, ordinary = ADULT_ANCHORS["explicit" if minimum == "explicit" else "nsfw"], ORDINARY_ANCHORS
    memo = _known.setdefault((adult, ordinary), {})
    todo = [e for e in entries if e not in memo]
    if todo:
        texts = list(dict.fromkeys([*todo, *adult, *ordinary]))
        vectors = await vectors_or_background(ctx, texts)
        if vectors is None:
            return {e for e in entries if memo.get(e)}
        by_text = dict(zip(texts, vectors))
        for entry in todo:
            v = by_text[entry]
            near_adult = max(dot(v, by_text[t]) for t in adult)
            near_ordinary = max(dot(v, by_text[t]) for t in ordinary)
            memo[entry] = near_adult >= ADULT_FLOOR and near_adult - near_ordinary >= ADULT_MARGIN
    return {e for e in entries if memo.get(e)}


async def shows_adult(ctx: PlanContext, description: str, rating: str) -> bool | None:
    """Does a description show adult content at this rating (nsfw: nudity or sex; explicit: plainly sexual)? One of its parts has to be adult
    (see the module doc). None when that cannot be told (no search by meaning), so callers leave the description alone."""
    if not usable(ctx):
        return None
    return bool(await adult_entries(ctx, fragments(description), "explicit" if rating == "explicit" else "nsfw"))
