"""Appearance options for the scene plan. The wildcard library has no clean hair or eye lists, and a model that is only asked
to "vary a look" settles on one answer (`long wavy brown hair, yellow eyes` in every run). So when the user left the look open, code
draws a few hair and eye options at random from the tag library (general tags such as `blue hair`, `green eyes`, `ponytail`) and the
scene plan may build on them. The draw is local and seeded, like every other draw of a job.

Nothing is drawn when the user's words already give a hair or eye word, when they name a character (the character has its own look), for a
picture without people, or without a tag library. Skin colour and body build are left out on purpose. A group gets one set of options."""
import random
import re

from app.planning.content_safety import matches_term
from app.planning.tagstep import keyword_hits
from app.planning.taglib import CHARACTER, GENERAL, TagLib

HAIR_COLOURS = ("black", "brown", "blonde", "red", "orange", "pink", "purple", "blue", "green", "white", "grey", "aqua")
EYE_COLOURS = ("brown", "blue", "green", "red", "purple", "yellow", "pink", "orange", "grey", "black", "aqua")
HAIR_STYLES = ("short hair", "medium hair", "long hair", "very long hair", "ponytail", "twintails", "braid", "side ponytail", "hair bun",
               "bob cut", "wavy hair")
APPEARANCE_MIN_COUNT = 100_000     # a tag must be used this often in the library: rare looks (`white eyes`, `curly hair`) are left out
HAIR_COLOUR_PICKS = 2              # hair colours drawn ...
HAIR_STYLE_PICKS = 1               # ... plus one length or style: "about 3 hair options"
EYE_PICKS = 3
LOOK_WORDS = re.compile(r"\b(?:hair|haired|hairstyle|eyes?|eyed|blonde?|brunettes?|redheads?|ginger|bald|ponytails?|twintails?|pigtails?|"
                        r"braids?|braided|bangs)\b", re.I)         # a word that already gives a hair or eye look


def known(lib: TagLib, names: list[str]) -> list[str]:
    """The tags among `names` (written with spaces) that the library has as general tags used often enough, and that the content rules do not avoid."""
    out = []
    for name in names:
        hit = lib.lookup(name, aliases=False)
        if hit and hit.category == GENERAL and hit.count >= APPEARANCE_MIN_COUNT and not matches_term(name):
            out.append(name)
    return out


def names_a_character(lib: TagLib, text: str) -> bool:
    """Does the text name a character the library knows (every word of a character tag is in the text)? Such a character has its own look."""
    return any(lib.tags[name][0] == CHARACTER for name in keyword_hits(lib, text))


def has_character_lora(ctx, loras: list[dict]) -> bool:
    """Is one of the job's LoRAs a character? It has its own look, so no hair or eye options are drawn against it."""
    return any((card := ctx.lora_card(item["name"])) and card.kind == "character" for item in loras)


def draw(options: list[str], count: int, rng: random.Random) -> list[str]:
    return rng.sample(options, min(count, len(options)))


def appearance_options(lib: TagLib | None, text: str, subjects: dict | None, rng: random.Random) -> dict[str, list[str]] | None:
    """{"hair": [...], "eyes": [...]} drawn from the tag library for a request that leaves the look open, else None.
    `text` is everything the user wrote about the job (request, description, constraints)."""
    if lib is None or (subjects or {}).get("no_humans") or LOOK_WORDS.search(text or "") or names_a_character(lib, text or ""):
        return None
    colours = known(lib, [f"{c} hair" for c in HAIR_COLOURS])
    styles = known(lib, list(HAIR_STYLES))
    eyes = known(lib, [f"{c} eyes" for c in EYE_COLOURS])
    hair = [*draw(colours, HAIR_COLOUR_PICKS, rng), *draw(styles, HAIR_STYLE_PICKS, rng)]
    looks = {"hair": hair, "eyes": draw(eyes, EYE_PICKS, rng)}
    return looks if any(looks.values()) else None
