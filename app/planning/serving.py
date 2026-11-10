"""What each wildcard list is for, decided from its own entries instead of its name or folder.

A list can feed several scene slots (a flat file of view phrases holds camera angles and gaze directions), or none (a list of artist
names fits no slot and stays match-only). `Serving` says which entries of a list feed which slot, and how that was decided:

  * "you": the user set a role for the list (the existing role override). It always wins: the whole list feeds the slot that role maps
    to, or none for a role that maps to no slot ("style", "other").
  * "meaning": each entry is scored against the slot descriptions (`wildcards_slot_about`) with the CPU embeddings model, from vectors
    that are already stored. An entry goes to the slot it is clearly closest to (at least `wildcards_fit_floor` close, and
    `wildcards_fit_lead` ahead of the next slot); an entry that fits nothing clearly feeds nothing. The folder name only adds a small
    bonus to the slot its default role maps to; it can tip a close call, never make an entry fit. A slot counts for a list when at least
    MIN_SHARE of its entries went to it. The embeddings are multilingual, so lists in other languages are read the same way.
  * "words": without the embeddings (off, missing, or the entries not embedded yet) the list name and the entries are matched against the
    words of the slots (`wildcards_given_words`). This is a low-confidence fallback and only knows those words.

Every entry that is attributed comes from `plain` (offers.plain_entries): no wildcard syntax, not a whole caption, nothing the content rules
avoid. Nothing in this module reads a list's entries any other way."""
import weakref
from dataclasses import dataclass, field

from app.config import slot_entry_texts
from app.embeddings import dot
from app.planning.semantic import MAX_ENTRIES, usable, vectors_or_background
from app.wildcards.ideas import MAX_IDEA_ENTRY, brief_phrases, normal
from app.wildcards.store import OTHER_ROLE, WildList

MIN_SHARE = 0.25        # a slot counts for a list when this share of the list's entries went to it
HINT = 0.03             # added to the slot the folder's default role maps to (a weak hint, never a requirement)
WORDS_SHARE = 0.4       # ... and by words alone it takes this much: one stray word in an entry ("rim lighting") says little
SURE_SHARE = 0.6        # "sure" when this share of the entries found a slot (or none fits any: then it is just as clear)


@dataclass
class Serving:
    slots: dict[str, list[str]] = field(default_factory=dict)       # slot -> the entries of the list that feed it
    by: str = "meaning"                                             # "you", "meaning" or "words"
    sure: bool = True
    long: int = 0                                                   # entries that are too long to be offered
    seen: int = 0                                                   # entries that were looked at

    def summary(self) -> dict:
        return {"slots": {slot: len(entries) for slot, entries in self.slots.items()}, "by": self.by, "sure": self.sure, "long": self.long}


def sample(entries: list[str]) -> list[str]:
    """The entries that are looked at: all of a list up to MAX_ENTRIES, an even sample of a longer one (the same as the stored vectors)."""
    if len(entries) <= MAX_ENTRIES:
        return entries
    return [entries[i * len(entries) // MAX_ENTRIES] for i in range(MAX_ENTRIES)]


def long_entries(l: WildList) -> int:
    return sum(1 for e in l.entries if len(e) > MAX_IDEA_ENTRY and "__" not in e and "{" not in e)


def hint_slot(l: WildList, config) -> str:
    return config.wildcards_role_slots.get(l.default_role, "")


# ---------- the user's role ----------

def by_you(l: WildList, entries: list[str], config) -> Serving:
    slot = config.wildcards_role_slots.get(l.role, "") if l.role_set else ""
    return Serving({slot: entries} if slot and entries else {}, "you", True, long_entries(l), len(entries))


# ---------- meaning ----------

_rows: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()     # embedder -> (slot descriptions, {entry: its closeness to each slot})


def closeness(embedder, config, texts: list[str], vectors) -> dict[str, list[float]] | None:
    """{text: closeness to each slot, in `wildcards_slot_about` order} for texts that have vectors. Kept per embedder, so every entry is
    scored once however many times the lists are looked at."""
    about = slot_entry_texts(config)
    held = embedder.cached(about)
    if not about or any(v is None for v in held):
        return None
    key = tuple(about)
    memo = _rows.get(embedder)
    if memo is None or memo[0] != key:
        memo = _rows[embedder] = (key, {})
    rows = memo[1]
    for text, vec in zip(texts, vectors):
        if text not in rows:
            rows[text] = [dot(a, vec) for a in held]
    return rows


def by_meaning(l: WildList, looked: list[str], rows: dict[str, list[float]], config) -> Serving:
    slots = list(config.wildcards_slot_about)
    hint = hint_slot(l, config)
    floor, lead = config.wildcards_fit_floor, config.wildcards_fit_lead
    placed: dict[str, list[str]] = {}
    for e in looked:
        row = [v + (HINT if s == hint else 0.0) for s, v in zip(slots, rows[e])]
        order = sorted(range(len(row)), key=lambda i: -row[i])
        best = order[0]
        if row[best] >= floor and (len(order) == 1 or row[best] - row[order[1]] >= lead):
            placed.setdefault(slots[best], []).append(e)
    kept = {s: es for s, es in placed.items() if len(es) >= MIN_SHARE * len(looked)}
    found = sum(len(es) for es in kept.values())
    return Serving(kept, "meaning", found == 0 or found >= SURE_SHARE * len(looked), long_entries(l), len(looked))


# ---------- words ----------

def slot_words(config) -> dict[str, set[str]]:
    """{phrase: the slots it belongs to}: the slot's own name, its roles' names and their given words (`wildcards_given_words`)."""
    index: dict[str, set[str]] = {}
    for slot in config.wildcards_slot_about:
        roles = [r for r, s in config.wildcards_role_slots.items() if s == slot]
        for word in [slot, *roles, *(w for r in roles for w in config.wildcards_given_words.get(r, []))]:
            if phrase := normal(word):
                index.setdefault(phrase, set()).add(slot)
    return index


def hits(text: str, index: dict[str, set[str]]) -> dict[str, int]:
    """{slot: length of its longest phrase found in the text} (whole words, a trailing plural s counts)."""
    found: dict[str, int] = {}
    for phrase in brief_phrases(text):
        for slot in index.get(phrase, ()):
            found[slot] = max(found.get(slot, 0), len(phrase))
    return found


def winner(found: dict[str, int]) -> str:
    """The slot with the longest phrase; "" when several tie."""
    if not found:
        return ""
    top = max(found.values())
    best = [s for s, n in found.items() if n == top]
    return best[0] if len(best) == 1 else ""


_words: dict[tuple, Serving] = {}


def by_words(l: WildList, looked: list[str], config) -> Serving:
    key = (l.name, l.default_role, hash(tuple(looked)), hash(tuple(sorted((r, tuple(w)) for r, w in config.wildcards_given_words.items()))),
           hash(tuple(sorted(config.wildcards_role_slots.items()))), hash(tuple(config.wildcards_slot_about)))
    if key in _words:
        return _words[key]
    index = slot_words(config)
    named = list(hits(l.name.replace("_", " "), index))                  # the slots the name (folder included) mentions
    evidence = {s for s in [*named, hint_slot(l, config)] if s}
    if not named and not hint_slot(l, config) and l.default_role not in ("", OTHER_ROLE):
        evidence = set()
        looked = []         # the folder's default role is a kind that fits no slot (style, artist ...): read by words alone, it serves none
    counts: dict[str, int] = {}
    own = {}
    for e in looked:
        own[e] = winner(hits(e, index))
        if own[e]:
            counts[own[e]] = counts.get(own[e], 0) + 1
    slots = evidence | {s for s, n in counts.items() if n >= WORDS_SHARE * len(looked)}
    placed: dict[str, list[str]] = {}
    for e in looked:
        slot = own[e] if own[e] in slots else (next(iter(evidence)) if len(evidence) == 1 else next(iter(slots)) if len(slots) == 1 else "")
        if slot:
            placed.setdefault(slot, []).append(e)
    if len(_words) > 5000:
        _words.clear()
    _words[key] = Serving(placed, "words", False, long_entries(l), len(looked))
    return _words[key]


# ---------- all lists ----------

def serve_now(lists: list[WildList], config, embedder, plain) -> dict[str, Serving]:
    """{list name: Serving}, from the vectors already stored (never starts the embeddings server). `plain(list)` gives the entries that can
    be offered (offers.plain_entries). Lists whose entries are not all embedded yet are read by their words."""
    out: dict[str, Serving] = {}
    cached = getattr(embedder, "cached", None) if embedder is not None and config.embeddings_enabled else None
    for l in lists:
        entries = plain(l)
        if l.role_set:
            out[l.name] = by_you(l, entries, config)
            continue
        looked = sample(entries)
        rows = None
        if cached is not None and looked:
            vectors = cached(looked)
            if all(v is not None for v in vectors):
                rows = closeness(embedder, config, looked, vectors)
        out[l.name] = by_meaning(l, looked, rows, config) if rows is not None else by_words(l, looked, config)
    return out


async def serve(ctx, lists: list[WildList], plain) -> dict[str, Serving]:
    """`serve_now`, after making sure the vectors it needs exist: the slot descriptions (stored once) and the entries (a few missing ones are
    embedded now, many are embedded in the background and those lists are read by their words this time)."""
    if usable(ctx) and ctx.config.wildcards_slot_about:
        pending = [e for l in lists if not l.role_set for e in sample(plain(l))]
        if pending and await ctx.embedder.embed(slot_entry_texts(ctx.config)) is not None:
            await vectors_or_background(ctx, list(dict.fromkeys(pending)))
    return serve_now(lists, ctx.config, ctx.embedder if usable(ctx) else None, plain)



def digest(served: dict[str, Serving]) -> dict:
    """A short summary of what a set of lists serve, for the import result: lists per slot, lists that serve several slots (mixed), lists that serve
    none (match-only) and lists whose reading is low-confidence."""
    per_slot: dict[str, int] = {}
    for s in served.values():
        for slot in s.slots:
            per_slot[slot] = per_slot.get(slot, 0) + 1
    return {"slots": per_slot, "mixed": sum(len(s.slots) > 1 for s in served.values()), "none": sum(not s.slots for s in served.values()),
            "low": sum(not s.sure for s in served.values()), "lists": len(served)}
