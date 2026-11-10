"""Turning wildcard syntax into text, with a seeded random generator so the same seed always gives the same text.

Supported (a Forge "Dynamic Prompts" subset):
  __name__            a random entry of that list; `__folder/name__`, a short name only one list has, or `__artist*top__`
  {a|b|c}             one of the options; `{2$$a|b|c}` or `{1-3$$a|b|c}` picks several (joined with ", "),
                      `{2$$ and $$a|b|c}` with your own separator, `{3::a|1::b}` for weights
  [x::0.3] [:x:0.3] [x:y:0.3]   prompt-editing brackets are reduced to their first text (x, or y when x is empty)
Entries may use all of it, also nested: an entry can hold a `__name__` that holds a `{a|b}`. Expansion stops after a few
rounds; whatever is still unresolved by then, and every reference to a list that doesn't exist, becomes a plain word."""
import random
import re

from app.wildcards.store import Catalog

MAX_DEPTH = 5               # rounds of expansion; a list that refers to itself ends here
MAX_CHARS = 2000            # an expansion that grows past this is flattened at once
REF = re.compile(r"__([A-Za-z0-9][\w\-./*]*?)__")
GROUP = re.compile(r"\{([^{}]*)\}")
COUNT = re.compile(r"^\s*(\d+)(?:\s*-\s*(\d+))?\s*\$\$")
WEIGHT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*::")
EDIT_BRACKETS = re.compile(r"\[([^\[\]:]*):([^\[\]:]*):?\s*(?:\d*\.?\d+)?\]")      # [x::0.3]  [:x:0.3]  [x:y:0.3]


# ---------- finding tokens ----------

def find_refs(text: str) -> list[str]:
    """The distinct `__name__` references in a text (the names without underscores), in order."""
    return list(dict.fromkeys(m.group(1) for m in REF.finditer(text or "")))


def _groups(text: str) -> list[str]:
    """Top-level `{...}` groups holding a choice (a `|` or `$$`), balanced, in order."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(text or ""):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and ("|" in text[start:i] or "$$" in text[start:i]):
                out.append(text[start:i + 1])
    return out


def find_tokens(text: str) -> list[str]:
    """Every wildcard token in a text, exactly as written: `__name__` references and `{a|b}` choices, in order of appearance."""
    tokens = [(m.start(), m.group(0)) for m in REF.finditer(text or "")]
    pos = 0
    for group in _groups(text or ""):
        at = text.index(group, pos)
        pos = at + len(group)
        tokens.append((at, group))
    return list(dict.fromkeys(t for _, t in sorted(tokens)))


def unknown_refs(text: str, catalog: Catalog) -> list[str]:
    """Problems with the `__name__` references typed in a text: one message per reference that matches no list (or several)."""
    return [why for ref in find_refs(text) if (why := catalog.resolve(ref)[1])]


def drop_invented_refs(text: str, typed: str, catalog: Catalog | None) -> str:
    """`text` from the LLM without the `__name__` references it made up: ones that match no list and that the user didn't type
    (asked for "the lewd wildcards", an LLM may write `__lewd_wildcards__`). A reference the user typed stays, also a wrong one:
    the job then says so, and the user can fix the name."""
    made_up = [ref for ref in find_refs(text) if f"__{ref}__" not in (typed or "") and (catalog is None or catalog.resolve(ref)[1])]
    if not made_up:
        return text
    for ref in made_up:
        text = text.replace(f"__{ref}__", "")
    text = re.sub(r"\s*,(\s*,)+", ",", re.sub(r"[ \t]{2,}", " ", text)).strip(" ,")
    return re.sub(r"\s+(and|with|or)$", "", text)


# ---------- expanding ----------

def _pick_options(body: str, rng: random.Random) -> str:
    """The text a `{...}` group turns into."""
    low, high, sep = 1, 1, ", "
    count = COUNT.match(body)
    if count:
        low = int(count.group(1))
        high = int(count.group(2)) if count.group(2) else low
        body = body[count.end():]
        before_bar = body.split("|", 1)[0]
        if "$$" in before_bar:
            sep, body = body.split("$$", 1)
    options, weights = [], []
    for part in body.split("|"):
        w = WEIGHT.match(part)
        weights.append(float(w.group(1)) if w else 1.0)
        options.append(part[w.end():].strip() if w else part.strip())
    n = min(rng.randint(min(low, high), max(low, high)), len(options))
    chosen: list[str] = []
    pool = list(range(len(options)))
    for _ in range(n):
        i = rng.choices(pool, weights=[max(weights[j], 0.0) or 0.0001 for j in pool])[0]
        pool.remove(i)
        if options[i]:
            chosen.append(options[i])
    return sep.join(chosen)


def _groups_resolved(text: str, rng: random.Random) -> str:
    """Every `{...}` group replaced by its pick, innermost first."""
    for _ in range(20):
        new = GROUP.sub(lambda m: _pick_options(m.group(1), rng), text)
        if new == text:
            break
        text = new
    return text


def _plain(ref: str) -> str:
    return ref.replace("*", " ").strip()


def tidy(text: str) -> str:
    """Prompt-editing brackets reduced to their text, doubled commas and spaces cleaned up."""
    text = EDIT_BRACKETS.sub(lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(\s*,){2,}", ",", text)
    return re.sub(r"\s+,", ",", text).strip(" ,")


def expand(text: str, catalog: Catalog, rng: random.Random, depth: int = MAX_DEPTH) -> str:
    """The text with every wildcard token replaced by a random pick. The same `rng` state gives the same result."""
    def draw(m: re.Match) -> str:
        found, _ = catalog.resolve(m.group(1))
        pool = [e for l in found for e in l.entries]
        return rng.choice(pool) if pool else _plain(m.group(1))

    for _ in range(depth):
        new = REF.sub(draw, _groups_resolved(text, rng))
        done = new == text
        text = new
        if done or len(text) > MAX_CHARS:
            break
    text = _groups_resolved(text, rng)
    text = REF.sub(lambda m: _plain(m.group(1)), text)          # unresolved after the last round: plain words
    return tidy(text.replace("{", "").replace("}", ""))


def expand_for_image(text: str, catalog: Catalog | None, seed: int) -> str:
    """A prompt as one image gets it: tokens expanded with a generator seeded from that image's seed."""
    if catalog is None or not find_tokens(text):
        return text
    return expand(text, catalog, random.Random(f"wildcards:{seed}"))
