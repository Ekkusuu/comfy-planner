"""Wildcard "ideas" for the prompt writer. LLMs are bad at randomness and good at taste, wildcard lists are the other way
round: so code draws random entries from lists that suit the brief, and the LLM chooses the ones that fit and weaves them in.

Which lists (per job; `choose`). Up to `ideas_per_prompt`, each list once, from five sources in this order:
  1. The LLM's picks (planning/pick_wildcards.py, when `llm_picks_lists` is on): it sees a compact catalog of the lists with
     sample entries, so it finds lists whose names hide their content ("themes" holding "steampunk"), synonyms ("moody" ->
     ambience) and vague requests ("surprise me"). Code checks each pick: the list exists and is enabled, and it does not only serve slots
     that the brief already fixes (below).
  2. Phrase matches: the brief contains an entry of the list as a whole phrase (`phrase_hit`). Only distinctive entries
     count: not pure syntax (`__ref__`, `{a|b}`), at least MIN_PHRASE_CHARS long, not made of very common words, and a
     single word needs 5+ characters ("steampunk" yes, "girl" or "cat" no). A phrase that more than MAX_LISTS_PER_PHRASE
     lists contain ("glowing" is in a dozen) says little about which list is meant and is ignored. The longest phrase comes
     first. This is exact enough to apply to every role, match roles included, without the old problem that long lists
     contain almost any word.
  3. Meaning matches (when `[embeddings]` is on, planning/semantic.py): lists whose name or entries are close in meaning to the
     brief ("moody" -> melancholy, gloomy), best first. Not used for adult-content lists unless they are among the 3 best.
  4. Keyword matches: BM25 over the list's NAME and role (entries don't count, a long list contains almost any word);
     a score of at least `min_match_score`, best first.
  5. Open slots (setting, camera, lighting, outfit, pose, gaze by default): for each slot that no chosen list serves yet, the
     best-scoring list that serves it, a random one among equals. A list serves the slots its entries fit (planning/serving.py),
     whatever its name or folder; lists that fit no slot are only ever offered by 1-4.
Slots vs. phrase matches: a *slot* is an aspect of the picture (where, which shot, which colours). When the brief
already fixes it - it uses one of the slot role's words ("city"), or contains an entry that feeds the slot as a whole phrase
(`given_roles`) - no list is offered for that slot, whoever picked it: a phrase match on a slot list means "the brief decided
this already", not "offer this list". For lists that serve no slot a phrase match means "this list is about what was asked for",
so it is offered. Adult lists (by their entries' meaning, offers.adult_content) never fill an open slot on their own; a picture rated below nsfw is
offered no adult entries, and a nsfw or explicit picture is also offered adult entries of the whole library (offers.build).
Which entries: each prompt gets its own slice of a shuffled copy of every chosen list, so the images of a job get
different ingredients. The draw is seeded, so it can be repeated, and sized to fit `max_idea_entries` per call."""
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from app.config import Config
from app.planning.artists import asked_artists, wants_artists
from app.planning.rank import rank_scored
from app.wildcards.expand import expand, find_refs, find_tokens
from app.wildcards.store import Catalog, WildcardStore, WildList

ADULT_ROLE = "nsfw"         # a role the user can set to say "this whole list is adult" (offers.adult_content); which lists and entries are adult is otherwise
                            # read from the entries' meaning, wherever they are kept. The folder's default role only counts when meaning cannot be told.
MAX_IDEA_CHARS = 120
MAX_IDEA_ENTRY = 80         # longer entries are whole caption dumps (style-oriart: "ori,dark purple sky,westland with..."): as ideas they
                            # drag every image towards the same scene. They stay usable as typed `__name__` tokens.
MIN_PHRASE_CHARS = 4        # shorter entries are too common as words to tell that a slot is already decided
MIN_SINGLE_WORD_CHARS = 5   # a one-word entry must be at least this long to count as a distinctive phrase match
MAX_PHRASE_WORDS = 6
MAX_LISTS_PER_PHRASE = 3     # a phrase found in more lists than this is too general to pick one of them
# Very common English words: an entry made only of these says nothing about the brief, and neither does a bare person or
# size word (a "woman" in a body list must not pull that list in for "a woman in a garden").
COMMON_WORDS = frozenset("""a an the and or but nor of in on at to by for from with without into onto over under above below
about after before between through during again against while where when which what who whom whose that this these those there
their them they then than thus here have has had been being were was are is am do does did done would could should shall will
might must can may very much more most some any each every other another such only same also just even still own too all both
few many several one two three four five six seven eight nine ten first second third last next never always often sometimes
girl girls woman women lady ladies male female man men boy boys guy person people child children kid kids human humans
young little small large big tall short long great good best nice beautiful pretty cute lovely
thing things something anything nothing everything lot lots top style image picture photo scene background foreground""".split())


@dataclass
class Choice:
    """A list chosen for the brief, and how: by "llm" (its reason in `why`), "phrase" (an entry is in the brief),
    "meaning" (its name or entries are close in meaning to the brief), "keyword" (the name matches) or "slot" (an open slot role)."""
    wild: WildList
    by: str
    why: str = ""


@dataclass
class IdeaSet:
    """What was offered for one prompt-writing call."""
    lists: list[WildList] = field(default_factory=list)          # the chosen lists, in order
    per_prompt: list[list[dict]] = field(default_factory=list)   # for each prompt: [{"list", "role", "text"}]
    chosen: dict[str, dict] = field(default_factory=dict)        # list name -> {"by", "why"}
    pool: dict[str, list[str]] = field(default_factory=dict)     # scene slot -> every entry of the lists that serve it (the offers are drawn from these)

    def lists_record(self) -> list[dict]:
        """Which lists were drawn from and why, in order: [{"list", "role", "by", "why"}]."""
        return [{"list": l.name, "role": l.role, **self.chosen.get(l.name, {"by": "slot", "why": ""})} for l in self.lists]

    def block_text(self, label: str = "Prompt") -> str:
        """The ideas as the prompt shows them, one line per prompt (or per `label`, e.g. "Description")."""
        lines = []
        for n, ideas in enumerate(self.per_prompt, 1):
            parts = slot_parts(ideas)
            if parts:
                lines.append(f"{label} {n} — " + "; ".join(parts))
        return "\n".join(lines)

    def options(self, n: int = 0) -> list[dict]:
        """What was drawn for prompt `n` (the first by default), as [{"list", "role", "text"}]."""
        return list(self.per_prompt[n]) if n < len(self.per_prompt) else []

    def options_text(self, n: int = 0) -> str:
        """The draw for prompt `n` as the scene plan shows it: one line per scene slot ("camera (use this): from below"), or per list for
        entries that have no slot ("outfit (clothing/x): a | b | c")."""
        return "\n".join(slot_parts(self.options(n)))


def normal(text: str) -> str:
    """The words of the text in lower case, in any script (letters and digits of any language; punctuation and underscores split words)."""
    return " ".join(re.findall(r"(?:[^\W_]|')+", (text or "").lower()))


def slot_parts(ideas: list[dict]) -> list[str]:
    """The ideas as prompt lines. Entries that carry a `slot` (offers.py) are shown by slot, "camera (use this): from below" or
    "pose (pick one that fits, or none): a | b | c"; the others, from lists no slot is known for, by list: "style (style/steampunk): a | b"."""
    slots = list(dict.fromkeys(i["slot"] for i in ideas if i.get("slot")))
    parts = []
    for slot in slots:
        mine = [i for i in ideas if i.get("slot") == slot]
        required = any(i.get("required") for i in mine)
        parts.append(f"{slot} ({'use this' if required else 'pick one that fits, or none'}): " + " | ".join(dict.fromkeys(i["text"] for i in mine)))
    loose = [i for i in ideas if not i.get("slot")]
    for name in dict.fromkeys(i["list"] for i in loose):
        mine = [i for i in loose if i["list"] == name]
        parts.append(f"{mine[0]['role']} ({name}): " + " | ".join(i["text"] for i in mine))
    return parts


def has_phrase(text: str, phrase: str) -> bool:
    """The phrase appears in the text as whole words (a trailing plural s counts)."""
    return bool(re.search(rf"(?<![^\W_]){re.escape(normal(phrase))}(?:s|es)?(?![^\W_])", normal(text))) if normal(phrase) else False


# ---------- phrase matches ----------

@lru_cache(maxsize=300_000)
def distinctive(entry: str) -> str:
    """The normal form of a wildcard entry when it is a distinctive phrase to look for in a brief, otherwise "": not syntax,
    not short, not made of very common words, and a single word needs MIN_SINGLE_WORD_CHARS characters."""
    if "__" in entry or "{" in entry:
        return ""
    phrase = normal(entry)
    words = phrase.split()
    if len(phrase) < MIN_PHRASE_CHARS or not words or len(words) > MAX_PHRASE_WORDS:
        return ""
    if all(w in COMMON_WORDS or w.isdigit() for w in words):
        return ""
    return "" if len(words) == 1 and len(phrase) < MIN_SINGLE_WORD_CHARS else phrase


def brief_phrases(text: str) -> set[str]:
    """Every run of up to MAX_PHRASE_WORDS words of the text, plus the same runs with a plural s/es taken off the last word
    (so the entry "steampunk" is found in "steampunks"; the same rule as has_phrase)."""
    words = normal(text).split()
    out = set()
    for n in range(1, MAX_PHRASE_WORDS + 1):
        for i in range(len(words) - n + 1):
            run = words[i:i + n]
            out.add(" ".join(run))
            for suffix in ("es", "s"):
                if run[-1].endswith(suffix) and len(run[-1]) > len(suffix):
                    out.add(" ".join([*run[:-1], run[-1][:-len(suffix)]]))
    return out


def phrase_hits(l: WildList, phrases: set[str]) -> set[str]:
    """The distinctive entries of the list that the brief contains as whole phrases. `phrases` is brief_phrases(brief)."""
    return {p for entry in l.entries if (p := distinctive(entry)) in phrases and p}


def phrase_matches(lists: list[WildList], brief: str) -> list[tuple[WildList, str]]:
    """[(list, its best phrase)] for the lists that have a phrase match in the brief: per list the longest phrase, ignoring
    phrases that too many lists share (MAX_LISTS_PER_PHRASE)."""
    phrases = brief_phrases(brief)
    found = {l.name: phrase_hits(l, phrases) for l in lists}
    shared = Counter(p for hits in found.values() for p in hits)
    out = []
    for l in lists:
        specific = [p for p in found[l.name] if shared[p] <= MAX_LISTS_PER_PHRASE]
        if specific:
            out.append((l, max(specific, key=len)))
    return out


def served_by(l: WildList, served: dict[str, dict[str, list[str]]] | None, config: Config) -> dict[str, list[str]]:
    """{slot: the entries of the list that feed it}. `served` is what planning/serving.py read from the entries; without it (callers that
    have no embeddings at hand) a list serves the slot its role maps to, with all of its entries."""
    if served is not None and l.name in served:
        return served[l.name]
    slot = config.wildcards_role_slots.get(l.role, "")
    return {slot: l.entries} if slot else {}


def given_roles(text: str, lists: list[WildList], config: Config, served: dict[str, dict[str, list[str]]] | None = None) -> set[str]:
    """Slot roles the text already fixes. A role is given when the text uses one of its words (config `given_words`, for
    example "city" for the setting) or contains, as a whole phrase, an entry of a list that serves the role's slot."""
    given = set()
    padded = f" {normal(text)} "
    for role in config.wildcards_slot_roles:
        if has_phrase(text, role) or any(has_phrase(text, w) for w in config.wildcards_given_words.get(role, [])):
            given.add(role)
            continue
        slot = config.wildcards_role_slots.get(role, "")
        for l in lists:
            entries = (served_by(l, served, config).get(slot, []) if slot else []) if served is not None else (l.entries if l.role == role else [])
            if any(len(e) >= MIN_PHRASE_CHARS and "__" not in e and "{" not in e and f" {normal(e)} " in padded for e in entries):
                given.add(role)
                break
    return given


# ---------- which lists ----------

def doc_text(l: WildList) -> str:
    """What a list is matched on by keyword: its name and role, so the words of its topic ("painting", "steampunk", "camera shot").
    Its entries don't count: a long list contains almost any word somewhere, and that would make every list match everything."""
    return f"{re.sub(r'[/_\-]+', ' ', l.name)} {l.role}"


def choose(lists: list[WildList], query: str, given_text: str, config: Config, rng: random.Random,
           picks: list[tuple[str, str]] | None = None, related: list[tuple[WildList, float]] | None = None,
           served: dict[str, dict[str, list[str]]] | None = None, held_back: set[str] | None = None) -> list[Choice]:
    """Up to `ideas_per_prompt` lists for a brief (see the module doc). `picks` are the LLM's choices, [(list name, reason)], best first;
    `related` are the lists close in meaning to the brief, best first (planning/semantic.py; None when meaning search is off); `served` is
    {list name: {slot: entries}} as read from the entries (see `served_by`). `held_back` names the lists that never fill an open slot on
    their own (the adult lists, offers.adult_content); without it, the lists with the ADULT_ROLE."""
    want = max(config.wildcards_ideas_per_prompt, 0)
    if held_back is None:
        held_back = {l.name for l in lists if l.role == ADULT_ROLE}
    usable = [l for l in lists if l.enabled and l.entries and (l.role != "artist" or wants_artists(query))]
    if not want or not usable:
        return []
    scored = rank_scored(usable, doc_text, query, top=len(usable))
    score = {l.name: s for l, s in scored}
    given = given_roles(given_text, usable, config, served)
    given_slots = {config.wildcards_role_slots.get(r, "") for r in given} - {""}
    chosen: list[Choice] = []
    covered: set[str] = set()

    def add(l: WildList, by: str, why: str = ""):
        slots = set(served_by(l, served, config))
        if len(chosen) < want and all(c.wild.name != l.name for c in chosen) and not (slots and slots <= given_slots):
            chosen.append(Choice(l, by, why))
            covered.update(slots)

    by_name = {l.name: l for l in usable}
    for pick in picks or []:
        name, why = pick[0], pick[1]
        if name in by_name:
            add(by_name[name], "llm", why)
    for l, h in sorted(phrase_matches(usable, query), key=lambda x: (-len(x[1]), -score[x[0].name], x[0].name)):
        add(l, "phrase", f"your request says “{h}”")
    for l, s in related or []:
        if l.name in by_name:
            add(by_name[l.name], "meaning", "its name or entries are close to your request")
    for l, s in scored:
        if s >= config.wildcards_min_match_score:
            add(l, "keyword")
    open_slots = [s for s in dict.fromkeys(config.wildcards_role_slots.get(r, "") for r in config.wildcards_slot_roles) if s and s not in given_slots]
    rng.shuffle(open_slots)
    for slot in open_slots:
        pool = [l for l in usable if l.name not in held_back and slot in served_by(l, served, config) and all(c.wild.name != l.name for c in chosen)]
        if slot not in covered and pool:        # the best match; among equals a random one, larger (more general) lists more likely
            best = [l for l in pool if score[l.name] == max(score[x.name] for x in pool)]
            add(rng.choices(best, weights=[len(served_by(l, served, config)[slot]) for l in best])[0], "slot")
    return chosen


def choose_lists(lists: list[WildList], query: str, given_text: str, config: Config, rng: random.Random,
                 picks: list[tuple[str, str]] | None = None, related: list[tuple[WildList, float]] | None = None,
                 served: dict[str, dict[str, list[str]]] | None = None) -> list[WildList]:
    return [c.wild for c in choose(lists, query, given_text, config, rng, picks, related, served)]


def draw(lists: list[WildList], catalog: Catalog, count: int, config: Config, seed: int, artist_limit: int = 1) -> IdeaSet:
    """For each of `count` prompts, a different handful of entries from each list (nested tokens in entries resolved).
    Sized to `max_idea_entries` in total: first fewer entries per list, then fewer lists. An artist list gives at most
    `artist_limit` entries per prompt (one by default): an artist wildcard flooded a prompt with fourteen names."""
    limit = max(config.wildcards_max_idea_entries, 0)
    if not lists or count < 1 or limit < count:
        return IdeaSet()
    lists = lists[:max(limit // count, 0)]
    per_list = max(min(config.wildcards_entries_per_list, limit // (count * len(lists))), 0)
    if not lists or not per_list:
        return IdeaSet()
    take = {l.name: min(per_list, max(artist_limit, 0)) if l.role == "artist" else per_list for l in lists}
    orders = {}
    for l in lists:                     # one shuffle per list; prompt n takes the n-th slice, so prompts differ
        order = [i for i, entry in enumerate(l.entries) if len(entry) <= MAX_IDEA_ENTRY]
        random.Random(f"{seed}:{l.name}").shuffle(order)
        orders[l.name] = order
    per_prompt = []
    for n in range(count):
        rng = random.Random(f"{seed}:{n}")
        ideas, seen = [], set()
        for l in lists:
            order = orders[l.name]
            step = take[l.name]
            for j in range(min(step, len(order))):
                text = expand(l.entries[order[(n * step + j) % len(order)]], catalog, rng)[:MAX_IDEA_CHARS].strip()
                if text and text.lower() not in seen:
                    seen.add(text.lower())
                    ideas.append({"list": l.name, "role": l.role, "text": text})
        per_prompt.append(ideas)
    return IdeaSet(lists, per_prompt)


def make_ideas(store: WildcardStore | None, config: Config, query: str, given_text: str, count: int, seed: int,
               picks: list[tuple[str, str]] | None = None, related: list[tuple[WildList, float]] | None = None,
               served: dict[str, dict[str, list[str]]] | None = None) -> IdeaSet | None:
    """The ideas to offer for `count` prompts, or None when wildcards are off, none are imported, or nothing fits.
    `picks` are the LLM's choice of lists, [(name, reason)]; `related` the lists close in meaning, `served` what the lists serve (see `choose`)."""
    if store is None or not config.wildcards_enabled or count < 1:
        return None
    lists = store.lists()
    choices = choose(lists, query, given_text, config, random.Random(f"{seed}:lists"), picks, related, served)
    result = draw([c.wild for c in choices], Catalog(lists), count, config, seed, asked_artists(query) or 1)
    result.chosen = {c.wild.name: {"by": c.by, "why": c.why} for c in choices}
    return result if any(result.per_prompt) else None


# ---------- afterwards ----------

def used_ideas(offered: list[dict], prompt: str) -> list[str]:
    """The offered ideas that appear in the final prompt: whole-word, ignoring case."""
    return [i["text"] for i in offered if has_phrase(prompt, i["text"])]


def idea_record(offered: list[dict], prompt: str, lists: list[dict] | None = None) -> dict:
    """What a prompt's item stores: the ideas offered for it, which of them it ended up using, and the lists they came
    from and why (IdeaSet.lists_record)."""
    return {"offered": offered, "used": used_ideas(offered, prompt), **({"lists": lists} if lists else {})}


def should_offer_for_tweak(instruction: str, config: Config) -> bool:
    text = (instruction or "").lower()
    return any(w in text for w in config.wildcards_tweak_words)


def missing_tokens(prompt: str, tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in prompt]


def brief_without_tokens(text: str, tokens: list[str], store: WildcardStore | None) -> tuple[str, str]:
    """(the brief with its wildcard tokens taken out, the roles those tokens already fill). A typed `__background-location__`
    fixes the setting, and its words ("background") must not pull more lists in."""
    roles = []
    if store is not None and tokens:
        catalog = store.catalog()
        roles = [l.role for t in tokens for ref in find_refs(t) for l in catalog.resolve(ref)[0]]
    for t in tokens:
        text = text.replace(t, " ")
    return text, " ".join(dict.fromkeys(roles))


def tokens_of(*texts: str) -> list[str]:
    """The wildcard tokens in these texts, in order, without repeats."""
    return list(dict.fromkeys(t for text in texts for t in find_tokens(text)))
