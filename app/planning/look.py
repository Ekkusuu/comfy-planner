"""The look of a picture: who sets its colours, light and rendering - the planner, or a style LoRA.

A style LoRA paints its own look. Colour-and-light words next to it override that look (measured: the same job and seeds with and without a
colour-lighting idea, `cinematic lighting` and `depth of field`: the neon colours stay without the LoRA's own look). So when a LoRA of the
job has `sets_look` on its card, the planner does not offer a colour-lighting idea, tells the scene and prompt writers to leave light, colour
schemes and lens effects out, and the tag check removes the light tags and colour schemes. What the request itself says about light stays.

What a "look tag" is comes from data: the `[look]` table of config/concepts.toml (the word endings that make a tag a light tag: `soft lighting`),
and the colour schemes of the user's own lists (the entries that fit the lighting slot, in any list: planning/serving.py).
"""
import re
from dataclasses import dataclass

from app.planning.concepts import load_concepts, normal
from app.planning.context import PlanContext
from app.planning.offers import is_real_tag, plain_entries
from app.planning.serving import serve_now
from app.planning.taglib import TagLib

LIGHTING = "lighting"       # the scene slot that holds light and colours
SLOTS_WITH_LOOK = ("lighting", "setting", "mood", "camera")       # scene slots in which look words get cleaned (the lighting slot entirely)
JOINERS = {"and", "with", "to"}      # words that join colours in a colour scheme (`light red and cyan`)

NOTE = ("A LoRA chosen for this picture sets its look: its own colours, light and rendering. So do not plan or write any lighting, colour scheme or lens "
        "effect: no lighting or colour-palette words (`cinematic lighting`, `neon lighting`, `blue and orange`), no `depth of field`, `bokeh`, `blurry "
        "background` or `lens flare`, even where the workflow card's rules or examples name them, and leave the lighting slot empty. Light or colours the "
        "user wrote themselves in the request are kept as written.")
OPEN = "(open)"


@dataclass(frozen=True)
class Look:
    endings: tuple[str, ...]        # a tag whose last word ends like one of these is a look tag (concepts.toml [look] endings)
    colours: frozenset[str]         # the words of the colour schemes in the user's lists (empty without such lists)

    def is_look(self, text: str, lib: TagLib | None = None) -> bool:
        """Is this tag or phrase about light or a colour scheme? A colour scheme is a phrase made only of colour words (`blue violet`,
        `light red and cyan`) that is not a real tag of the library (`forest` is a colour word of `forest green` and also a place)."""
        words = normal(text).split()
        if not words:
            return False
        if any(words[-1].endswith(e) for e in self.endings):
            return True
        return bool(self.colours) and all(w in self.colours or w in JOINERS for w in words) and any(w in self.colours for w in words) \
            and not is_real_tag(lib, " ".join(words))

    def in_entry(self, entry: str, lib: TagLib | None = None) -> bool:
        """Does a wildcard entry hold a look tag (any comma-separated piece of it)?"""
        return any(self.is_look(piece, lib) for piece in entry.split(","))

    def clean_slot(self, slot: str, value: str, user_text: str) -> str:
        """The slot's text without its look pieces (comma-separated). The lighting slot keeps only the pieces the user's own words contain."""
        if not value:
            return value
        said = set(re.findall(r"[a-z0-9]+", user_text.lower()))
        keep = []
        for piece in (p.strip() for p in value.split(",")):
            words = [w for w in re.findall(r"[a-z0-9]+", piece.lower()) if len(w) > 2]
            asked = bool(words) and all(w in said for w in words)
            if piece and (asked or not (slot == LIGHTING or self.is_look(piece))):
                keep.append(piece)
        return ", ".join(keep)

    def clean_scene(self, scene: dict, user_text: str) -> list[str]:
        """Take the look out of a scene plan's slots (`clean_slot`). Returns the slots that changed."""
        changed = []
        for slot in SLOTS_WITH_LOOK:
            before = str(scene.get(slot) or "")
            after = self.clean_slot(slot, before, user_text)
            if after != before:
                scene[slot] = after
                changed.append(slot)
        return changed

    def example(self, text: str, lib: TagLib | None = None) -> str:
        """A card example (a prompt of tags) without its look tags, so the writer does not copy them."""
        return ", ".join(t.strip() for t in text.split(",") if t.strip() and not self.is_look(t, lib))


def colour_words(ctx: PlanContext) -> frozenset[str]:
    """The words used by the colour schemes of the user's lists: the entries of every enabled list that fit the lighting slot (the palette lists)."""
    store, c = ctx.wildcards, ctx.config
    if store is None or not c.wildcards_enabled:
        return frozenset()
    lists = [l for l in store.lists() if l.enabled and l.entries]
    embedder = getattr(ctx, "embedder", None)
    served = serve_now(lists, c, embedder if embedder is not None and embedder.enabled else None, plain_entries)
    return frozenset(w for s in served.values() for e in s.slots.get(LIGHTING, []) for w in normal(e).split() if w not in JOINERS)


def look_rules(ctx: PlanContext) -> Look:
    book = load_concepts()
    return Look(tuple(book.look_endings), colour_words(ctx))


def sets_look(ctx: PlanContext, lora_names: list[str]) -> bool:
    """Does any of these LoRAs set the look of the picture (its card says so)?"""
    return any((card := ctx.lora_card(name)) is not None and card.sets_look for name in lora_names)


def look_for(ctx: PlanContext, lora_names: list[str], vary: list[str] | None = None) -> Look | None:
    """The rules to apply to a job with these LoRAs, or None when nothing sets its look. A request that wants the lighting to differ in every image
    asks for lighting itself: nothing is left out then."""
    if LIGHTING in (vary or []) or not sets_look(ctx, lora_names):
        return None
    return look_rules(ctx)


def look_note(look: Look | None) -> str:
    """What the scene, brief and prompt writers are told about the look."""
    return NOTE if look is not None else OPEN
