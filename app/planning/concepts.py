"""The few word lists the planner reads from config/concepts.toml: which words ask for a content rating, which words raise a rating word by one
level, the words about looks that the tag search leaves out at the nsfw and explicit ratings, and the word endings of light tags (look.py).

Plain data, read on every call so an edit needs no restart. There are no lists of tags here: the real tags for a vague word ("cozy", "intricate")
are found in the tag library by meaning when the request is planned (tagstep.concept_lines). Nothing here decides what may be made. Library text is data.
"""
import random
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from app.config import ROOT

CONCEPTS_PATH = ROOT / "config" / "concepts.toml"
RATINGS = ("general", "sensitive", "nsfw", "explicit")       # from least to most
TAG_WORDS = re.compile(r"[a-z0-9][a-z0-9'-]*")
CONCEPT_SAMPLE = 8      # a list longer than this gives a random subset of this many tags per run (when a generator is passed)


def subset(tags: list[str], rng: random.Random | None, size: int = CONCEPT_SAMPLE, named: str = "") -> list[str]:
    """A random `size` of the tags in their file order, so different runs of a vague word see different ones. A tag the text
    names itself (`named`: the user's words) is always kept, and does not count against `size`. Without `rng`, or when the list
    is no longer than `size`, all of them."""
    if rng is None or len(tags) <= size:
        return list(tags)
    locked = {i for i, tag in enumerate(tags) if named and has_phrase(named, tag)}
    others = [i for i in range(len(tags)) if i not in locked]
    keep = locked | set(rng.sample(others, min(size, len(others))))
    return [tag for i, tag in enumerate(tags) if i in keep]


def rating_rank(rating: str) -> int:
    """0 for general up to 3 for explicit; an unknown rating counts as general."""
    return RATINGS.index(rating) if rating in RATINGS else 0


def rating_max(a: str, b: str) -> str:
    return a if rating_rank(a) >= rating_rank(b) else b


def normal(text: str) -> str:
    """The text as plain lower-case words, so a phrase can be looked for across punctuation."""
    return " ".join(re.findall(r"[a-z0-9']+", (text or "").lower()))


def has_phrase(text: str, phrase: str) -> bool:
    """The phrase appears as whole words; a plural s/es on its last word counts."""
    phrase = normal(phrase)
    return bool(phrase) and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?:s|es)?(?![a-z0-9])", normal(text)) is not None


@dataclass(frozen=True)
class ConceptBook:
    ratings: dict[str, tuple[str, ...]]     # rating -> words that ask for at least that rating
    intensifiers: tuple[str, ...] = ()      # words that raise a rating word by one level when they stand right before it ("very lewd")
    skipped_at_high_rating: tuple[str, ...] = ()   # words about looks that the tag search leaves out when the rating is nsfw or explicit
    look_endings: tuple[str, ...] = ()      # [look] endings: a tag whose last word ends like this is a light tag ("lighting")

    def intensified(self, text: str, rating: str) -> str:
        """The rating one level higher (at most explicit) when the text intensifies a rating word - an intensifier right before one ("very nsfw",
        "extremely lewd"), or one that is a rating word itself ("spicy") - and the rating is already above general. Whether the request set a limit
        is not decided here: the caller does not ask when it did (intent.resolve)."""
        if rating_rank(rating) < rating_rank("sensitive"):
            return rating
        rating_words = {w for words in self.ratings.values() for w in words}
        for word in self.intensifiers:
            if (word in rating_words and has_phrase(text, word)) or any(has_phrase(text, f"{word} {w}") for w in rating_words):
                return RATINGS[min(rating_rank(rating) + 1, len(RATINGS) - 1)]
        return rating

    def rating_min(self, text: str) -> str:
        """The least rating the request's own words ask for (general when it names none)."""
        out = "general"
        for rating, words in self.ratings.items():
            if any(has_phrase(text, w) for w in words):
                out = rating_max(out, rating)
        return out

    def vague_words(self) -> set[str]:
        """The words that are not drawable things by their own file: the rating words. (Other vague words - "cozy", "detailed" - need no list: the keep check
        only demands words that are real tags, and clean_must_keep also drops the fixed VAGUE_WORDS of tagcheck.py.)"""
        return {w for words in self.ratings.values() for w in words}

    def every_tag(self) -> list[tuple[str, str]]:
        """[(table, word)] of every word in the file, for the test that none matches the content rules."""
        tables = {**self.ratings, "intensifiers": self.intensifiers, "skipped_at_high_rating": self.skipped_at_high_rating, "look": self.look_endings}
        return [(table, word) for table, words in tables.items() for word in words]

    def for_rating(self, text: str, rating: str) -> str:
        """The text as the tag search reads it at this rating: at nsfw and explicit without the `skipped_at_high_rating` words, so that cute appearance
        words never bring tags into adult pictures."""
        if rating_rank(rating) < rating_rank("nsfw"):
            return text
        for word in self.skipped_at_high_rating:
            text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(word)}(?:s|es)?(?![A-Za-z0-9])", " ", text, flags=re.I)
        return " ".join(text.split())


def load_concepts(path: Path | None = None) -> ConceptBook:
    """Read the file. A missing file gives an empty book: the planner then works without these word lists."""
    path = path or CONCEPTS_PATH
    if not path.is_file():
        return ConceptBook({})
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    ratings = {str(k): tuple(str(w) for w in v) for k, v in (raw.get("ratings") or {}).items() if k in RATINGS}
    intensifiers = tuple(str(w) for w in (raw.get("ratings") or {}).get("intensifiers") or [])
    skipped_at_high_rating = tuple(str(w) for w in (raw.get("skipped_at_high_rating") or {}).get("words") or [])
    look = raw.get("look") or {}
    return ConceptBook(ratings, intensifiers, skipped_at_high_rating, tuple(str(t) for t in look.get("endings") or []))
