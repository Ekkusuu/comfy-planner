"""The tag check: after the LLM has written a tag or mixed prompt, plain code checks its tags against the Danbooru
library and fixes what it can be sure of. Only high-confidence fixes: an exact match, a known alias, a count form like
`1old man`, `by Mara Velden` as the artist tag the library knows. No fuzzy guessing. A tag it doesn't know is left alone
(an Anima prompt may hold any natural language); only a clearly broken one is passed on to the one repair call.

A tag that contradicts another one (`day` and `night`: alternatives that almost never appear together) is dropped when the
data says so, the one the person or the plan asked for staying. That runs only with the contradiction data (see contradictions.py).

What it never touches: the quality prefix, trigger words and style tags (`protected`), wildcard tokens, what the person typed
(`typed`: the request, the `===` text and the constraints), and the numbers of a weight (a fix goes inside `(x:1.4)`).
Locked or hand-edited prompts never come here. With no tag library (a fresh install) nothing here runs.

What differs per model family is one small table, FAMILY_RULES: Anima writes tags with spaces, artists as @name, in an order;
a family that is not in the table gets only the universal checks (duplicates, copies of a trigger word)."""
import logging
import re
from dataclasses import dataclass, field
from itertools import combinations
from typing import TYPE_CHECKING

from app.fields import is_weighted, split_tags, tag_key
from app.planning.concepts import rating_rank
from app.planning.contradictions import Contradictions, says_several_values
from app.planning.mentions import EXTENSION, readable, stem
from app.planning.content_safety import matches_term
from app.planning.taglib import ARTIST, CHARACTER, COPYRIGHT, GENERAL, META, TagLib, key, spaced

if TYPE_CHECKING:
    from app.planning.look import Look

log = logging.getLogger("planning.tags")


@dataclass(frozen=True)
class FamilyRules:
    danbooru: bool          # trained on Danbooru tags: look tags up in the library, write count tags
    spaces: bool = False    # multi-word tags with spaces instead of underscores (score_* keeps them)
    artist_at: bool = False  # an artist is written @name
    order: bool = False     # tags in the Anima order (see ANIMA_ORDER)


# Edit here to teach the check about a model family. The key is the card's `family`.
# Anima: its model card (circlestone-labs/Anima): lower-case tags with spaces, only score_* keep underscores, artists need `@`,
# order [quality/meta/year/safety] [1girl/1boy/1other] [character] [series] [artist] [general]; order inside a group is free.
# Illustrious: trained on Danbooru tags (sources agree, no official statement), but whether it likes spaces or underscores
# is disputed, so its tags are looked up and fixed but left in the form the LLM wrote. Pony, SDXL and the rest: not listed.
FAMILY_RULES = {
    "Anima": FamilyRules(danbooru=True, spaces=True, artist_at=True, order=True),
    "Illustrious": FamilyRules(danbooru=True),
}

def open_subjects(reply: dict | None) -> dict | None:
    """The planner's `subjects` ({girls, boys, others, no_humans}, -1 = not said) with open numbers as None. None when nothing is known."""
    if not reply:
        return None
    out = {k: (reply[k] if reply.get(k, -1) >= 0 else None) for k in ("girls", "boys", "others", "people")}
    out["no_humans"] = True if reply.get("no_humans") else None
    return out if any(v is not None for v in out.values()) else None


def kinds_known(subjects: dict | None) -> int:
    """How many people the subjects name by kind (girls, boys, others)."""
    return sum((subjects or {}).get(k) or 0 for k in ("girls", "boys", "others"))


def headcount(subjects: dict | None) -> int:
    """How many people are in the picture. `people` is the total the request gives when it does not say who they are, so it is not added to the
    kinds that were named: "1 girl" with people 1 is one person, "1 girl" with people 3 is three."""
    return max(kinds_known(subjects), (subjects or {}).get("people") or 0)


def unsaid_people(subjects: dict | None) -> int:
    """People of a kind the request does not say: the total minus the ones named by kind."""
    return max(((subjects or {}).get("people") or 0) - kinds_known(subjects), 0)


def describe_subjects(subjects: dict | None) -> str:
    """The job's subjects as one line for the LLM, or "" when nothing is known."""
    if not subjects:
        return ""
    if subjects.get("no_humans"):
        return "no people in the picture"
    parts = [f"{subjects[k]} {k if subjects[k] != 1 else k[:-1]}" for k in ("girls", "boys", "others") if subjects.get(k)]
    if unsaid_people(subjects):
        parts.append(f"{unsaid_people(subjects)} {'person' if unsaid_people(subjects) == 1 else 'people'} (kind not said)")
    return ", ".join(parts) + (GROUP_NOTE if is_group(subjects) else "")


GROUP_NOTE = " (several people together: write poses and framing for the whole group, not for one person)"


def is_group(subjects: dict | None) -> bool:
    """Do the subjects say that several people are in the picture?"""
    return bool(subjects) and not subjects.get("no_humans") and headcount(subjects) >= 2


# ---------- sex words ----------

# Words that mark one sex. Besides the plain words, each set lists body words that belong to one sex only (and, for the male set, facial hair).
MALE_WORDS = frozenset("male males man men boy boys masculine penis penises testicle testicles scrotum erection foreskin beard mustache moustache stubble sideburns".split())
FEMALE_WORDS = frozenset("female females woman women girl girls feminine vagina vulva pussy clitoris labia breasts".split())


def absent_sex_words(subjects: dict | None) -> frozenset[str]:
    """The words that mark the sex the job's people do not include: `male` for an all-female scene, the reverse for an all-male
    one. Nothing when the subjects are unknown, mixed, or hold people of an unsaid kind. A count the request leaves open counts
    as none (the planner leaves boys open when the request is only about a girl)."""
    if not subjects or subjects.get("no_humans") or subjects.get("others") or unsaid_people(subjects):
        return frozenset()
    girls, boys = subjects.get("girls"), subjects.get("boys")
    if girls and not boys:
        return MALE_WORDS
    if boys and not girls:
        return FEMALE_WORDS
    return frozenset()


def marks_absent_sex(tag: str, absent: frozenset[str]) -> bool:
    """Do the words of this tag mark a sex that is not in the picture (`male swimwear` for an all-female scene)?"""
    return bool(absent) and not absent.isdisjoint(re.findall(r"[a-z]+", tag.lower()))


# ---------- reading tags ----------

TOKEN = re.compile(r"__[^_\s][^\n]*?__|\{[^{}]*\|[^{}]*\}")                       # a wildcard token
COUNT = re.compile(r"(\d+)\+?(girl|boy|other)s?")                                  # 1girl, 3girls, 6+girls
COUNT_LIKE = re.compile(r"\d+\+?\s*(?:old\s+)?(?:girls?|boys?|others?|people|persons?|humans?|m[ae]n|wom[ae]n)", re.I)
BY_ARTIST = re.compile(r"(?:art(?:work)?\s+)?by\s+(.+)|artist\s*:\s*(.+)", re.I)
SENTENCE = re.compile(r"[.;!?]|\band\b|\bthe\b|\bwith\b", re.I)
SUBJECT_TAGS = ("girl", "boy", "other")
MIN_ALIAS_USES = 200        # an alias is only followed to a tag used at least this often
MAX_WORDS = {"tags": 6, "mixed": 4}
LEAD = "meta, year, safety, quality"
SAFETY = {"safe", "sensitive", "nsfw", "explicit", "general", "questionable"}
YEAR = re.compile(r"(?:year[_ ])?(?:19|20)\d\d|newest|recent|mid|early|old|oldest")
SCORE = re.compile(r"score_\d+(?:_up)?")


@dataclass
class CheckContext:
    lib: TagLib
    rules: FamilyRules | None          # None: the family is not in FAMILY_RULES
    kind: str                          # the card's prompt_style.kind: "tags" or "mixed"
    protected: list[str] = field(default_factory=list)     # quality prefix tags, trigger words, style tags
    typed: str = ""                    # everything the person typed
    subjects: dict | None = None       # {girls, boys, others, no_humans}: a number or None (open) each
    keep_text: str = ""                # the job's own words the keep check demands (description + constraints)
    family: str = ""                   # the card's family, for the rules only Anima has (the rating tag)
    rating: str = "general"            # the least rating the request asks for (plan_scene)
    detail_level: str = "normal"       # normal | high | very_high: the least number of content tags
    must_keep: list[str] = field(default_factory=list)     # the user's own concrete words (plan_scene)
    avoid: list[str] = field(default_factory=list)         # what the user wants absent (plan_scene): kept out of the positive prompt, the negative holds it
    ignore_words: set[str] = field(default_factory=set)    # vague and meta words the keep check does not demand back
    lora_phrases: list[str] = field(default_factory=list)  # what the chosen LoRA cards say they do
    artist_allowed: bool = False       # the user asked for artists
    artist_limit: int = 1              # how many artist tags one prompt may hold
    offered: list[str] = field(default_factory=list)       # real tags per slot for the size floor, the order it uses them in (see TagStep.offer)
    outfit_offers: set[str] = field(default_factory=set)   # the part of `offered` that came from the outfit slot (clothes)
    outfit: str = ""                   # the scene plan's outfit slot
    slots: dict[str, str] = field(default_factory=dict)    # the scene plan's slots: the size floor only adds tags that restate one of their phrases
    plan_text: str = ""                # everything the scene plan says
    habits: dict[str, float] = field(default_factory=dict)  # tags that most recent prompts have -> the share of them (context.habit_tags)
    contradictions: Contradictions | None = None            # the evidence for contradicting tags; None: that check is skipped
    view_tags: dict[str, list[str]] = field(default_factory=dict)      # a phrase of the plan's camera or gaze -> the real tags closest to it in meaning (TagStep.view_tags)
    lora_files: list[str] = field(default_factory=list)    # the LoRA files of this job: their names are not tags (drop_lora_files)
    look: "Look | None" = None         # look.Look when a LoRA of the job sets the picture's look: light, colour scheme and lens tags go (drop_look)


@dataclass
class CheckResult:
    text: str
    changes: list[dict] = field(default_factory=list)     # {"from", "to", "why"} the person should see
    problems: list[str] = field(default_factory=list)     # what is still wrong, for the repair call
    notes: list[str] = field(default_factory=list)        # things worth knowing that were left as they are (two contradicting tags the person wrote)


def wants_check(card_kind: str) -> bool:
    return card_kind in ("tags", "mixed")


def rules_for(family: str) -> FamilyRules | None:
    return FAMILY_RULES.get(family)


# ---------- one tag ----------

def spaced_form(name: str) -> str:
    """`long_hair` -> `long hair`; an underscore between short parts (`o_o`, `^_^`, `>_<`) stays: it is the face, not a space."""
    parts = name.split("_")
    out = parts[0]
    for left, right in zip(parts, parts[1:]):
        out += (" " if len(left) >= 2 and len(right) >= 2 and left[-1].isalnum() and (right[0].isalnum() or right[0] == "(") else "_") + right
    return out


def written(name: str, cc: CheckContext, like: str = "") -> str:
    """A library tag the way this family writes it. A family without a rule for it keeps the form the LLM used (`like`)."""
    if cc.rules and cc.rules.spaces:
        return name if name.startswith("score_") else spaced_form(name)
    return name if "_" in like and " " not in like else spaced(name)


def is_protected(core: str, cc: CheckContext) -> bool:
    low = core.strip().lower()
    if not low or TOKEN.search(core):
        return True
    if any(tag_key(p) == low or key(p) == key(low) for p in cc.protected):
        return True
    return bool(cc.typed) and re.search(rf"(?<!\w){re.escape(low)}(?!\w)", cc.typed.lower()) is not None


def looks_like_tag(core: str, cc: CheckContext) -> bool:
    words = core.split()
    return 0 < len(words) <= MAX_WORDS.get(cc.kind, 4) and not (len(words) > 3 and SENTENCE.search(core))


def fix_tag(core: str, cc: CheckContext) -> tuple[str, str | None, bool]:
    """(the tag as it should be, why or None, quiet). `core` is one tag without weight brackets. A change is quiet when it
    only changes the way the tag is written (underscores, `@`), which the person doesn't need to be told."""
    raw = core.strip()
    lib, rules = cc.lib, cc.rules
    if not raw or not looks_like_tag(raw, cc):
        return raw, None, False
    # an artist written "by Name" (or "artist: name"): its form changes even when the name is the person's own
    if rules and rules.artist_at:
        m = BY_ARTIST.fullmatch(raw)
        if m:
            hit = lib.artist(m.group(1) or m.group(2))
            if hit:
                new = "@" + spaced(hit.name)
                return new, f"the library knows this artist as {new}", False
        if raw.startswith("@"):
            return raw, None, False
    if is_protected(raw, cc):
        return raw, None, False
    if not rules or not rules.danbooru:
        return raw, None, False
    hit = lib.lookup(raw)
    fix = None
    if hit is None:
        gapped = re.fullmatch(r"(\d+\+?)\s+((?:girl|boy|other)s?)", raw, re.I)          # "3 girls" -> 3girls
        if gapped:
            hit = lib.lookup(gapped.group(1) + gapped.group(2))
        counted = re.fullmatch(r"\d+\s*([a-z][a-z ]+)", raw, re.I)                       # "1old man" -> old man
        if hit is None and counted:
            rest = lib.lookup(counted.group(1), aliases=False)
            if rest and rest.category == 0 and not COUNT.fullmatch(rest.name):
                hit, fix = rest, f"`{raw}` is not a tag; `{written(rest.name, cc, raw)}` is"
    if hit is None:
        new = spaced_form(key(raw)) if rules.spaces and "_" in raw and not raw.startswith("score_") and " " not in raw else raw
        return new, ("written with spaces" if new != raw else None), True
    new = written(hit.name, cc, raw)
    if matches_term(new) and not matches_term(raw):
        return raw, None, False
    if hit.via == "alias":
        if hit.count < MIN_ALIAS_USES or hit.category == ARTIST:       # an artist is only written through `by Name` (a bare word can be a plain word)
            return raw, None, False
        a, b = re.sub(r"[^a-z0-9]", "", raw.lower()), re.sub(r"[^a-z0-9]", "", new.lower())
        return new, fix or f"`{raw}` is an alias of `{new}`", a.startswith(b) or b.startswith(a)      # `trees` -> `tree` is not news
    if fix:
        return new, fix, False
    return new, ("written the library's way" if new != raw else None), True


def fix_piece(piece: str, cc: CheckContext, changes: list[dict]) -> str:
    """One comma piece of a prompt, weight brackets kept as they are: the fix goes inside them."""
    text = piece.strip()
    if not text:
        return ""
    inner = tag_key(text) if is_weighted(text) else text
    at = text.lower().find(inner) if inner != text else 0
    new, why, quiet = fix_tag(inner, cc)
    if new != inner:
        if not quiet:
            changes.append({"from": inner, "to": new, "why": why})
        return text[:at] + new + text[at + len(inner):]
    return text


# ---------- snapping, the keep check, the size floor and the rating ----------

SNAP_MAX_WORDS = 5
STOP_WORDS = frozenset("""a an the and or of in on at to for from with without by is are be been being was were do does did have has had can could
would should will may might must this that these those it its as into onto over under around out up down off about after before between
during while when where which who what how than then so if also just only very really quite more most much many some any all each every
make makes made making create creates created image images picture pictures photo photos pic pics prompt prompts render artwork art use
using used lora loras chosen style styles sure way thing things something anything lot lots get got let lets want wants like looks look you named
tag tags tagged wildcard wildcards possible possibly her hers him his me my mine your yours our ours their theirs user users
beside besides near next atop upon against behind inside outside
""".split())
SUBJECT_WORDS = frozenset("""girl girls woman women man men boy boys person people human humans adult adults others solo couple friend friends
""".split())
MODIFIER_WORDS = frozenset("""black white red blue green pink purple yellow brown grey gray gold silver orange cyan teal magenta beige
dark light bright pale deep soft hard big small large little long short thick thin wide narrow high low full half
""".split())
VAGUE_WORDS = frozenset("""lewd erotic erotica sexy seductive teasing suggestive sexual explicit sex hardcore naughty dirty complex detailed
detail intricate elaborate cozy cosy epic moody cute adorable kawaii beautiful gorgeous pretty nice hot attractive
""".split())
NUMBER_WORDS = frozenset("zero one two three four five six seven eight nine ten eleven twelve".split())
RATING_TAGS = frozenset({"safe", "sensitive", "nsfw", "explicit", "general", "questionable"})
DETAIL_FLOOR = {"high": 25, "very_high": 35}


def normal_phrase(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", (text or "").lower()))


def word_forms(word: str) -> set[str]:
    """A word and the forms a tag may use for it (`rainy` -> rain, `friends` -> friend, `talking` -> talk)."""
    out = {word}
    for end in ("ing", "es", "s"):
        if word.endswith(end) and len(word) - len(end) >= 3:
            out.add(word[:-len(end)])
    if word.endswith("y") and len(word) > 4:
        out.add(word[:-1])
    return out


def covered(word: str, text: str) -> bool:
    """Is this word in the prompt, in one of the forms a tag uses?"""
    tokens = set(re.findall(r"[a-z0-9']+", text.lower()))
    forms = word_forms(word)
    return any(forms & word_forms(token) for token in tokens)


def snap_phrase(core: str, cc: CheckContext) -> list[str] | None:
    """A phrase that is not a tag, turned into real tags when every content word is covered (`black lace kimono`
    -> `kimono, lace`). None when it cannot be fully covered: Anima accepts natural language, so the phrase stays.
    Aliases count first, then the longest known tags (a real `black kimono`-style tag is preferred when it exists)."""
    if not cc.rules or not cc.rules.danbooru or cc.kind not in ("tags", "mixed"):
        return None
    if cc.lib.lookup(core) is not None or is_weighted(core) or TOKEN.search(core) or is_protected(core, cc):
        return None
    words = re.findall(r"[a-z0-9']+", core.lower())
    if not 2 <= len(words) <= SNAP_MAX_WORDS:
        return None
    candidates = []
    for size in range(min(4, len(words)), 0, -1):
        for index in combinations(range(len(words)), size):
            phrase = " ".join(words[i] for i in index)
            hit = cc.lib.lookup(phrase)
            if not hit or hit.category != 0 or (size == 1 and phrase in MODIFIER_WORDS):
                continue
            # `orange glow` must not become `orange (fruit)`: a tag's qualifier has to be in the phrase too
            qualifier = re.search(r"\(([^)]*)\)", hit.name)
            if qualifier and not set(re.findall(r"[a-z0-9']+", qualifier.group(1).lower())) <= set(words):
                continue
            candidates.append((size, hit.count, index, hit.name))
    candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
    chosen, used = [], set()
    for _, _, index, name in candidates:
        if used & set(index):
            continue
        used |= set(index)
        chosen.append(name)
    content = [i for i, word in enumerate(words)
               if word not in STOP_WORDS | MODIFIER_WORDS | VAGUE_WORDS | NUMBER_WORDS and not word.isdigit()]
    if not chosen or not content or any(i not in used for i in content):
        return None
    out = []
    for name in chosen:
        tag = written(name, cc)
        if tag not in out:
            out.append(tag)
    return out if len(out) > 1 or key(out[0]) != key(core) else None


def drop_lora_style(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """A tag copied word for word from a LoRA card's `what_it_does` is not a scene detail: it describes the file.
    Removed unless the user typed it (or it is a trigger word or style tag)."""
    phrases = [" ".join(re.findall(r"[a-z0-9]+", p.lower())) for p in cc.lora_phrases]
    phrases = [p for p in phrases if len(p.split()) >= 2]
    if not phrases:
        return tags
    out = []
    for tag in tags:
        core = normal_phrase(tag_key(tag))
        if core and len(core.split()) >= 2 and not is_protected(tag_key(tag), cc) and any(
                re.search(rf"(?<![a-z0-9]){re.escape(core)}(?:s|es)?(?![a-z0-9])", p) for p in phrases):
            changes.append({"from": tag_key(tag), "to": None, "why": "it describes the LoRA card, not this picture"})
            continue
        out.append(tag)
    return out


def named_in_typed(tag: str, cc: CheckContext) -> bool:
    """A name the person wrote themselves, also when the check turned "by Mara Velden" into `@velden mara`."""
    core = tag_key(tag).lstrip("@").replace("_", " ")
    parts = core.split()
    options = [core, " ".join(reversed(parts))] if len(parts) == 2 else [core]
    return any(re.search(rf"(?<![a-z0-9]){re.escape(o)}(?:s|es)?(?![a-z0-9])", (cc.typed or "").lower()) for o in options)


def drop_plan_artifacts(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """A word copied from the mention wording or the scene plan itself ("the chosen LoRA", "very_high detail") is not a
    tag and is removed. What the person typed is left alone."""
    out = []
    for tag in tags:
        core = key(tag_key(tag))
        artifact = core in ("chosen", "lora", "you", "named", "very_high", "detail_level", "detail_level_very_high") or "very_high" in core
        if artifact and not is_protected(tag_key(tag), cc):
            changes.append({"from": tag_key(tag), "to": None, "why": "a word from the plan, not a tag"})
            continue
        out.append(tag)
    return out


def drop_lora_files(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """The name of a LoRA file of this job is not a tag, however the writer spelled it (`lark1 epoch14.safetensors`, `lark1_epoch14`, `lark1 epoch14`):
    removed, unless the user typed it or it is a trigger word. Any model file name (`.safetensors`, `.ckpt`, `.pt`) is dropped the same way."""
    names = {key(n) for f in cc.lora_files for n in (f, stem(f), readable(f)) if n}
    out = []
    for tag in tags:
        core = tag_key(tag)
        if (EXTENSION.search(core.strip()) or key(core) in names) and not is_protected(core, cc):
            changes.append({"from": core, "to": None, "why": "a LoRA file name, not a tag"})
            continue
        out.append(tag)
    return out


FILLER_WORDS = STOP_WORDS | frozenset("myself yourself himself herself itself ourselves themselves".split())


def drop_filler(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """A piece that is one little word and no tag (`herself`, `the`, `with`) is prose that slipped between the tags: removed. A real tag of the
    library, a trigger word and what the user typed stay."""
    out = []
    for tag in tags:
        core = tag_key(tag)
        if core.strip().lower() in FILLER_WORDS and cc.lib.lookup(core) is None and not is_protected(core, cc):
            changes.append({"from": core, "to": None, "why": "a word of prose, not a tag"})
            continue
        out.append(tag)
    return out


def is_style_token(tag: str, cc: CheckContext) -> bool:
    """Is the piece an `@` token (an artist or LoRA style selector)? A real tag of the library that happens to start with `@` (`@_@`, the
    swirling eyes) is a tag, not a token."""
    core = tag_key(tag).strip()
    return core.startswith("@") and cc.lib.lookup(core) is None


def drop_unasked_artists(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """An artist or @style tag stays only when the user named it, asked for artists, or a chosen LoRA needs it (its style tags are in
    `protected`). A token with no name in it (`@`, `@ @`) is never one. An offered artist list gives one, or the number the request asked for."""
    kept = 0
    out = []
    for tag in tags:
        core = tag_key(tag)
        artist = cc.lib.category_of(core) == ARTIST or is_style_token(tag, cc)
        if is_style_token(tag, cc) and not re.search(r"[a-z0-9]", core.lower()) and not is_protected(core, cc):
            changes.append({"from": tag, "to": None, "why": "an @ token with no name in it"})
            continue
        if not artist or is_protected(core, cc) or named_in_typed(tag, cc):
            out.append(tag)
            continue
        if not cc.artist_allowed or kept >= max(cc.artist_limit, 0):
            changes.append({"from": tag, "to": None, "why": "no artist was asked for"})
            continue
        kept += 1
        out.append(tag)
    return out


MIN_TAG_USES = 1000        # a library tag used less often than this is not a word that carries a picture (`shown` is an artist, `captured` a rare tag)


def real_tag_for(word: str, cc: CheckContext) -> str:
    """The real tag for a word when the library knows it (`rainy` finds `rain`), else the word itself."""
    return real_tag(word, cc) or word


def real_tag(word: str, cc: CheckContext) -> str | None:
    """The library's general tag for a word or phrase, written the way the prompt writes tags: exact, by alias, or a form of the word
    (`rainy` finds `rain`). None when the library has no such tag that is used often enough to mean something in a picture."""
    for form in dict.fromkeys([word, *sorted(word_forms(word), key=len, reverse=True)]):
        hit = cc.lib.lookup(form)
        if hit and hit.category == GENERAL and hit.count >= MIN_TAG_USES and not matches_term(hit.name):
            return written(hit.name, cc)
    return None


def demanded_tags(phrase: str, cc: CheckContext) -> list[str]:
    """The tags to demand back for a word or phrase of the user's: the phrase itself when it is a real tag; else the real tags among its
    words (`her back` -> `back`). Words the library does not know are not demanded: they say nothing about the picture (`different`, `time`,
    `viewing`), and a prompt that lacks them is not wrong. The repair step handles what is really broken."""
    whole = real_tag(phrase, cc)
    if whole:
        return [whole]
    return [t for w in phrase.split() if (t := real_tag(w, cc))]


def names_avoided(text: str, cc: CheckContext) -> bool:
    """Does the text (a tag, a phrase) hold every word of something the user wants absent (`round glasses` for the wish `glasses`)?"""
    return any((words := normal_phrase(a).split()) and all(covered(w, text) for w in words) for a in cc.avoid)


def drop_avoided(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """Take out the tags that show what the user wants absent, and the wish written as a tag (`without glasses`): the negative prompt
    holds it. A real tag that itself says something is missing (`no humans`) stays, and so do the protected tags."""
    if not cc.avoid:
        return tags
    out = []
    for tag in tags:
        core = tag_key(tag)
        negation = normal_phrase(core).split()[:1] == ["no"] and cc.lib.lookup(core) is not None
        fixed = TOKEN.search(core) or any(key(p) == key(core) for p in cc.protected)       # a wildcard token, the quality prefix, a trigger word
        if names_avoided(normal_phrase(core), cc) and not negation and not fixed:
            changes.append({"from": tag, "to": None, "why": "you asked for it to be absent (it is in the negative prompt)"})
            continue
        out.append(tag)
    return out


def keep_words(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """Every must_keep phrase and every concrete word the user typed must survive. A missing one is added back, as a
    real tag where the library knows one. Vague words ("lewd") and meta words ("image", "complex") are not demanded; a typed word that is
    no real tag of the library is not demanded either (`different`, `viewing`, `time` say nothing a picture can show)."""
    text = " ".join(tags)
    out = list(tags)
    present = {key(tag_key(t)) for t in out}
    for phrase in cc.must_keep:
        phrase = normal_phrase(phrase)
        if not phrase or all(covered(word, text) for word in phrase.split()) or names_avoided(phrase, cc):
            continue
        tag = real_tag_for(phrase, cc)         # the plan's own must-keep phrase (a visible thing the model chose): a real tag, else the phrase as written
        if key(tag) not in present:
            out.append(tag)
            present.add(key(tag))
            changes.append({"from": None, "to": tag, "why": "your request says it"})
        text = " ".join(out)
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9'-]*", (cc.keep_text or cc.typed or "").lower())
             if len(w) > 1 and w not in STOP_WORDS | SUBJECT_WORDS | VAGUE_WORDS | NUMBER_WORDS | cc.ignore_words
             and not w.isdigit() and not covered(w, text) and not names_avoided(w, cc)]
    for word in dict.fromkeys(words):
        for tag in demanded_tags(word, cc):
            if key(tag) in present:
                continue
            out.append(tag)
            present.add(key(tag))
            changes.append({"from": None, "to": tag, "why": "your request says it"})
        text = " ".join(out)
    return out


VIEW_SLOTS = ("camera", "gaze")        # the scene plan's slots whose phrases must reach the prompt (a good prompt shows the angle and the gaze)


def stand_ins(phrase: str, cc: CheckContext) -> list[str]:
    """The real tags that say a phrase of the plan's camera or gaze: the phrase when it is a tag (or an alias of one), the tags it breaks up
    into (`snap_phrase`), else the closest tags by meaning that TagStep.view_tags found. Empty when the library has nothing."""
    hit = cc.lib.lookup(phrase)
    if hit and hit.category == GENERAL and hit.count >= MIN_TAG_USES and not matches_term(hit.name):
        return [written(hit.name, cc)]
    return snap_phrase(phrase, cc) or cc.view_tags.get(normal_phrase(phrase), [])


def keep_view(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """The scene plan's camera and gaze must reach the prompt. A phrase of either slot that the prompt does not show (as the phrase, as a tag
    that restates it, or as one of its stand-in tags) gets its closest real tags (`stand_ins`) added, so a planned `full length shot` or
    `looking at each other` is never lost for want of being a tag. A tag that contradicts one already there, or that the user wants absent,
    is not added."""
    out = list(tags)
    present = {key(tag_key(t)) for t in out}
    for slot in VIEW_SLOTS:
        for words in phrases_of(cc.slots.get(slot, "")):
            phrase = " ".join(words)
            wanted = stand_ins(phrase, cc)
            text = " ".join(out)
            if not wanted or said(phrase, text) or any(key(w) in present for w in wanted) or any(restates(tag_key(t), [words]) for t in out):
                continue
            for tag in wanted:
                core = key(tag_key(tag))
                if core in present or matches_term(tag) or names_avoided(normal_phrase(tag), cc) or contradicts_present(tag, out, cc):
                    continue
                out.append(tag)
                present.add(core)
                changes.append({"from": None, "to": tag, "why": f"the scene plan's {slot}"})
    return out


def covered_tag(tag: str, text: str) -> bool:
    """Is every word of the tag already in the text (`back` inside `arched back`)?"""
    return all(covered(w, text) for w in normal_phrase(tag).split())


def head_and_modifiers(tag: str) -> tuple[str, set[str]]:
    """A tag's head noun (its last word) and the words before it, plurals folded (`lace-trimmed thighhighs` -> thighhigh, {lace, trimmed})."""
    words = [w.rstrip("s") for w in re.findall(r"[a-z0-9]+", tag_key(tag).lower())]
    return (words[-1], set(words[:-1])) if words else ("", set())


def near_duplicate(tag: str, present: list[str]) -> bool:
    """Is the tag the same thing as one already there: the same head noun, and one of them without a modifier or both
    sharing one (`lace thighhighs` and `lace-trimmed thighhighs`; `red dress` and `long red dress`)? `red dress` and `black dress` are two."""
    head, mods = head_and_modifiers(tag)
    for other in present:
        other_head, other_mods = head_and_modifiers(other)
        if head and head == other_head and (not mods or not other_mods or mods & other_mods):
            return True
    return False


def outfit_named(cc: CheckContext) -> bool:
    """Did the user name what is worn? A must_keep phrase (their own words) that the scene plan's outfit slot holds."""
    for phrase in cc.must_keep:
        words = normal_phrase(phrase).split()
        if words and all(covered(word, cc.outfit) for word in words):
            return True
    return False


TOP_UP_OUTFIT_MAX = 2       # clothes the size floor may add when the outfit is open: a person wears one outfit, the floor must not dress a second
PHRASE_STOP = frozenset("a an the of in on at to for from with and or by".split())     # words a tag's phrase test skips
OTHER_PERSON_WORDS = frozenset("person persons people other others another partner partners lap couple".split())      # a tag with one of these needs a second person
OWN_WORDS = frozenset("own self herself himself themselves".split())          # `hands on own lap` is one person's, whatever the lap


def phrase_words(text: str) -> list[str]:
    """The words of a phrase in lower case; a hyphenated compound stays one word (`half-closed eyes` -> half-closed, eyes)."""
    return re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", (text or "").lower().replace("_", " "))


def phrases_of(text: str) -> list[list[str]]:
    """The comma (semicolon, full stop, line) separated phrases of a text, each as its words."""
    return [words for piece in re.split(r"[,;.\n]", text or "") if (words := phrase_words(piece))]


def slot_phrases(slots: dict[str, str]) -> list[list[str]]:
    """Every phrase of every slot of the scene plan."""
    return [words for value in slots.values() for words in phrases_of(str(value or ""))]


PLURAL_ONLY = frozenset("shorts pants jeans glasses trunks tights briefs boxers panties bottoms scissors".split())      # `shorts` is not the plural of `short`


def singular(word: str) -> str:
    """A word without its plural ending (`eyes` -> eye, `thighs` -> thigh, `dresses` -> dress); only plurals are folded, never `-ing` or `-y`,
    so `building` is not `build` and `shorts` is not `short`."""
    if word in PLURAL_ONLY or len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    return word[:-1] if word.endswith("s") and not word.endswith(("ss", "us", "is")) else word


def in_words(word: str, phrase: list[str]) -> bool:
    """Is the word among the phrase's words, plurals folded (`eyes` and `eye`)? A hyphenated word also counts when its parts are all
    there (`half-closed` for `half closed`); a hyphenated word of the phrase is not split (`half-closed eyes` has no `closed`)."""
    if any(singular(word) == singular(w) for w in phrase):
        return True
    return "-" in word and all(in_words(part, phrase) for part in word.split("-"))


def restates(tag: str, phrases: list[list[str]], same_head: bool = True) -> bool:
    """Do all the content words of the tag (stop words aside, plurals folded) occur inside ONE phrase, and does the tag end on the same
    word as that phrase (its head: `long hair` and `brown hair` restate `long wavy brown hair`; `blue skin` is not in `blue eyes, fair
    skin`, whose words are in two phrases; a bare `back` or `cowboy` does not restate `leaning back` or `cowboy shot`)? A tag that only
    repeats what the plan says is not a new idea. `same_head=False` drops the second test."""
    words = [w for w in phrase_words(tag_key(tag)) if w not in PHRASE_STOP]
    if not words:
        return False
    for phrase in phrases:
        content = [w for w in phrase if w not in PHRASE_STOP]
        if all(in_words(w, phrase) for w in words) and (not same_head or singular(words[-1]) == singular(content[-1])):
            return True
    return False


def one_person(subjects: dict | None) -> bool:
    """Do the subjects say that exactly one person is in the picture? (An open count counts as none.)"""
    if not subjects or subjects.get("no_humans"):
        return False
    return headcount(subjects) == 1


def implies_other_person(tag: str) -> bool:
    """Does the tag need a second person (`sitting on person`, `leaning on person`, `hand on another's shoulder`)? A tag about one's own body does not."""
    words = set(phrase_words(tag_key(tag)))
    return not words.isdisjoint(OTHER_PERSON_WORDS) and words.isdisjoint(OWN_WORDS)


def top_up(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """A complex request gets at least the detail level's content tags, but only from tags that restate the scene plan:
    every content word of the tag is inside one phrase of one slot (`restates`), so the floor writes the plan's own words as real
    tags and never adds a neighbour in meaning (`blue skin` for `blue eyes, fair skin`). `offered` comes in the order TagStep.offer
    sets. A tag is also skipped when it contradicts the count tags, the safety rule or a tag already there (`contradicts_present`), marks a sex the job's people do not include,
    needs a second person in a picture of one (unless the user typed it), or repeats a tag already there (`near_duplicate`).
    Clothes from the outfit slot are added only when the outfit is open, and then at most TOP_UP_OUTFIT_MAX of them. A request
    that cannot reach the floor this way stays below it: shorter, not wrongly drawn."""
    need = DETAIL_FLOOR.get(cc.detail_level, 0)
    if not need:
        return tags
    out, present = list(tags), {key(tag_key(t)) for t in tags}
    content = {key(tag_key(t)) for t in out if not is_protected(tag_key(t), cc) and not TOKEN.search(t)}
    absent = absent_sex_words(cc.subjects)
    alone = one_person(cc.subjects) or people(out)["solo"]
    phrases = slot_phrases(cc.slots)
    clothes_left = 0 if outfit_named(cc) else TOP_UP_OUTFIT_MAX
    for candidate in cc.offered:
        if len(content) >= need:
            break
        core = key(tag_key(candidate))
        if not core or core in present or is_protected(core, cc) or matches_term(candidate) or marks_absent_sex(candidate, absent):
            continue
        if cc.lib.category_of(core) == ARTIST and not cc.artist_allowed:
            continue
        worn = candidate in cc.outfit_offers
        if not restates(candidate, phrases) or (alone and implies_other_person(candidate)):
            continue
        if (worn and not clothes_left) or near_duplicate(candidate, out) or conflicts([*out, candidate]) or contradicts_present(candidate, out, cc):
            continue
        if worn:
            clothes_left -= 1
        out.append(candidate)
        present.add(core)
        content.add(core)
        changes.append({"from": None, "to": candidate, "why": "the detail level you asked for"})
    return out


def said(tag: str, text: str) -> bool:
    """Is the tag's name in the text as whole words (a plural s counts)?"""
    plain = key(tag_key(tag)).replace("_", " ")
    plain = plain[:-1] if plain.endswith("s") and not plain.endswith("ss") and len(plain) > 3 else plain      # `brown eyes` is said by "one brown eye" too
    return re.search(rf"(?<![a-z0-9]){re.escape(plain)}(?:s|es)?(?![a-z0-9])", text.lower()) is not None


def drop_look(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """When a LoRA sets the picture's look (`cc.look`), tags about light, colour scheme and lens effects are removed: written next to the LoRA they override
    its own colours and rendering. What the person typed, the quality prefix, trigger words and style tags stay."""
    if cc.look is None:
        return tags
    out = []
    for tag in tags:
        core = tag_key(tag)
        if not TOKEN.search(tag) and cc.look.is_look(core, cc.lib) and not is_protected(core, cc):
            changes.append({"from": core, "to": None, "why": "the LoRA sets the look: light, colour and lens tags override it"})
            continue
        out.append(tag)
    return out


HABIT_MAX_DROPS = 2         # most tags that one prompt loses to this rule
HABIT_KEEP_FLOOR = 15       # ... and never below this many content tags: a short prompt keeps its habits


def drop_habits(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """A general tag that most of the recent prompts already have (`cc.habits`, from the stored jobs) is dropped, unless
    something asks for it: the user's words, the scene plan, this job's tag offers or the protected tags. At most
    HABIT_MAX_DROPS per prompt, the most common first, and never below HABIT_KEEP_FLOOR content tags. The count tags,
    `solo`, the rating and the quality prefix are never touched. A habit fades from the stored jobs once it is dropped, so
    the tag comes back when it is no longer in most of them."""
    if not cc.habits:
        return tags
    asked = " ".join([cc.typed, cc.keep_text, cc.plan_text, *cc.must_keep])
    offered = {key(tag_key(t)) for t in cc.offered}
    content = [t for t in tags if not is_protected(tag_key(t), cc) and not TOKEN.search(t)]
    habitual = [t for t in content if key(tag_key(t)) in cc.habits and bucket(t, cc) == 5 and key(tag_key(t)) not in offered
                and not said(t, asked)]
    habitual.sort(key=lambda t: -cc.habits[key(tag_key(t))])
    drop = []
    for tag in habitual[:HABIT_MAX_DROPS]:
        if len(content) - len(drop) <= HABIT_KEEP_FLOOR:
            break
        drop.append(tag)
        changes.append({"from": tag, "to": None, "why": f"{cc.habits[key(tag_key(tag))]:.0%} of your recent prompts have it"})
    return [t for t in tags if t not in drop]


# ---------- contradicting tags ----------

# Which of two contradicting tags stays: the lower tier. Inside a tier the tag with more words (the more specific one) stays, then the earlier one.
TIER_LOCKED, TIER_TYPED, TIER_MUST_KEEP, TIER_PLAN, TIER_OFFERED, TIER_OTHER = range(6)


def one_person_tags(tags: list[str], cc: CheckContext) -> bool:
    """Is the picture about exactly one person: the subjects say so, or the prompt has `solo` or a single count tag and nothing that
    says several? Nobody known counts as no: the stricter group limits then apply."""
    p = people(tags)
    total = sum(max(p[k], default=0) for k in ("girls", "boys", "others"))
    if p["multiple"] or p["none"] or total >= 2 or is_group(cc.subjects):
        return False
    return p["solo"] or total == 1 or one_person(cc.subjects)


def general_name(tag: str, cc: CheckContext) -> str | None:
    """The library's name of a general tag (aliases followed), or None for a wildcard token, an unknown tag or a character, series, artist or meta tag."""
    if TOKEN.search(tag):
        return None
    hit = cc.lib.lookup(tag_key(tag))
    return hit.name if hit and hit.category == GENERAL else None


def keep_tier(tag: str, cc: CheckContext, offered: set[str]) -> int:
    """How much a tag is wanted: 0 never removed (quality prefix, trigger and style tags, wildcard tokens, count tags, the rating),
    1 the person typed it, 2 it is one of their must-keep words, 3 the scene plan says it, 4 it was offered for this job, 5 anything else."""
    core = tag_key(tag)
    name = key(core)
    if (not name or TOKEN.search(tag) or any(key(p) == name for p in cc.protected) or is_people_tag(name) or name in RATING_TAGS
            or name == key(cc.rating) or bucket(tag, cc) < 2):
        return TIER_LOCKED
    if cc.typed and said(core, cc.typed):
        return TIER_TYPED
    if cc.must_keep and (said(core, " ".join(cc.must_keep)) or restates(core, [phrase_words(m) for m in cc.must_keep])):
        return TIER_MUST_KEEP
    if (cc.plan_text and said(core, cc.plan_text)) or (cc.slots and restates(core, slot_phrases(cc.slots))):
        return TIER_PLAN
    return TIER_OFFERED if name in offered else TIER_OTHER


def drop_contradictions(tags: list[str], cc: CheckContext, changes: list[dict], notes: list[str]) -> list[str]:
    """Of two tags that contradict (the evidence in `cc.contradictions`: alternatives that almost never appear together), the less wanted
    one is removed (`keep_tier`). Never removed: protected tags, count tags, the rating, and anything the person wrote or the keep check
    demands; if both of a pair are the person's, both stay and `notes` says so. The rest of the prompt keeps its order. Only the
    library's general tags are judged. With no evidence loaded this does nothing."""
    con = cc.contradictions
    if con is None or not cc.rules or not cc.rules.danbooru or cc.kind not in ("tags", "mixed") or len(tags) < 2:
        return tags
    if says_several_values(tag_key(t) for t in tags):          # `heterochromia`, `multicolored hair`: several values at once is what the prompt asks for
        return tags
    single = one_person_tags(tags, cc)
    names = [general_name(t, cc) for t in tags]
    offered = {key(tag_key(t)) for t in cc.offered}
    tiers = [keep_tier(t, cc, offered) for t in tags]
    # A word the writer left out and the keep check put back (`time of day` -> `day`) is a weak sign of what was asked: it never outranks a tag the writer wrote (it ranks as an unbacked tag, and stays only because the keep check wants it)
    put_back = {key(c["to"]) for c in changes if c.get("from") is None and c.get("to") and c.get("why") == "your request says it"}
    tiers = [TIER_OTHER if tier != TIER_LOCKED and key(tag_key(t)) in put_back else tier for t, tier in zip(tags, tiers)]
    rank = lambda i: (tiers[i], -len(names[i].split("_")), i)
    removed: dict[int, int] = {}
    for i in range(len(tags)):
        for j in range(i + 1, len(tags)):
            if i in removed:
                break
            if j in removed or names[i] is None or names[j] is None or names[i] == names[j] or not con.contradicts(names[i], names[j], single):
                continue
            keep, lose = (i, j) if rank(i) < rank(j) else (j, i)
            said_both = f"`{tag_key(tags[i])}` and `{tag_key(tags[j])}` usually do not go together, but you asked for one of them, so both stay"
            if tiers[lose] <= TIER_MUST_KEEP:
                if tiers[lose] != TIER_LOCKED and said_both not in notes:
                    notes.append(said_both)
                    log.info("tag check: %s", said_both)
                continue
            rest = [t for k, t in enumerate(tags) if k != lose and k not in removed]
            if len(keep_words(rest, cc, [])) > len(rest):          # the keep check would put it back: the request wants it
                note = f"`{tag_key(tags[lose])}` seems to contradict `{tag_key(tags[keep])}`, but the request asks for it, so it stays"
                if note not in notes:
                    notes.append(note)
                    log.info("tag check: %s", note)
                continue
            removed[lose] = keep
            changes.append({"from": tags[lose], "to": None,
                            "why": f"it contradicts `{tag_key(tags[keep])}`: the two are alternatives that almost never appear together"})
    return [t for k, t in enumerate(tags) if k not in removed]


def contradicts_present(candidate: str, tags: list[str], cc: CheckContext) -> bool:
    """Would adding this tag put a contradiction in the prompt (it contradicts a general tag that is already there)?"""
    con = cc.contradictions
    if con is None or not cc.rules or not cc.rules.danbooru:
        return False
    name = general_name(candidate, cc)
    if name is None or says_several_values(tag_key(t) for t in [*tags, candidate]):
        return False
    single = one_person_tags(tags, cc)
    return any((other := general_name(t, cc)) and other != name and con.contradicts(name, other, single) for t in tags)


def write_rating(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """Anima tag and mixed cards get the rating tag in the lead group: sensitive, nsfw or explicit. A general
    request writes nothing. Other families get no rating tag."""
    if cc.family != "Anima" or cc.kind not in ("tags", "mixed") or cc.rating not in ("sensitive", "nsfw", "explicit"):
        return tags
    if any(key(tag_key(t)) == cc.rating for t in tags):
        return tags
    out = []
    for tag in tags:
        if key(tag_key(tag)) in RATING_TAGS and not is_protected(tag_key(tag), cc):
            changes.append({"from": tag, "to": None, "why": f"the rating is {cc.rating}"})
            continue
        out.append(tag)
    lead = 0
    while lead < len(out) and any(key(tag_key(out[lead])) == key(p) for p in cc.protected):
        lead += 1
    out.insert(lead, cc.rating)
    changes.append({"from": None, "to": cc.rating, "why": "the request asks for this rating"})
    return out


# ---------- who is in the picture ----------

def people(tags: list[str]) -> dict:
    """What the count tags of a prompt say: {"girls": set of numbers, "boys": ..., "others": ..., "multiple": set of
    "girl"/"boy"/"other", "solo": bool, "none": bool}. 6+ counts as 6."""
    out = {"girls": set(), "boys": set(), "others": set(), "multiple": set(), "solo": False, "none": False}
    for tag in tags:
        k = key(tag_key(tag))
        m = COUNT.fullmatch(k)
        if m:
            out[m.group(2) + "s"].add(int(m.group(1)))
        elif k.startswith("multiple_") and k[9:].rstrip("s") in SUBJECT_TAGS:
            out["multiple"].add(k[9:].rstrip("s"))
        elif k == "solo":
            out["solo"] = True
        elif k == "no_humans":
            out["none"] = True
    return out


def count_tag(kind: str, n: int) -> str:
    """3 girls -> `3girls`, 1 -> `1girl`, 6 or more -> `6+girls`."""
    return f"{n}{kind}" if n == 1 else (f"{n}{kind}s" if n < 6 else f"6+{kind}s")


def wanted_tags(subjects: dict) -> tuple[list[str], set[str], bool]:
    """(tags the prompt must have, count tags that must not be there, is it a group?) for a job's subjects.
    An open (None) number forces nothing and removes nothing."""
    need, drop = [], set()
    known = 0
    for kind in SUBJECT_TAGS:
        n = subjects.get(kind + "s")
        if n is None:
            continue
        known += n
        drop |= {count_tag(kind, i) for i in range(0, 7)} | {f"multiple_{kind}s"}
        if n >= 1:
            need.append(count_tag(kind, n))
            if n >= 2:
                need.append(f"multiple_{kind}s")
    if subjects.get("no_humans"):
        need = ["no_humans"]
        drop |= {count_tag(k, i) for k in SUBJECT_TAGS for i in range(1, 7)} | {f"multiple_{k}s" for k in SUBJECT_TAGS}
    group = headcount(subjects) >= 2 and not subjects.get("no_humans")     # people of an unsaid kind get no tag, but they are not one person either
    if group:
        drop |= {count_tag(kind, 1) for kind in SUBJECT_TAGS}
    return need, {d for d in drop if d not in need}, group


def settle_people(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """Count tags and `solo` follow the job's subjects when they are known: missing ones are added after the quality prefix,
    ones that contradict them are removed. Tags the person typed stay."""
    subjects = cc.subjects or {}
    need, drop, group = wanted_tags(subjects)
    if not need and not drop and not group:
        return tags
    lead = 0
    while lead < len(tags) and any(key(tag_key(tags[lead])) == key(p) for p in cc.protected):
        lead += 1
    out = []
    for tag in tags:
        k = key(tag_key(tag))
        if (k in drop or (group and k == "solo") or (subjects.get("no_humans") and k == "solo")) and not is_protected(tag_key(tag), cc):
            changes.append({"from": tag, "to": None, "why": "the job is about " + ("a group" if group else "other people than this tag says")})
            continue
        out.append(tag)
    present = {key(tag_key(t)) for t in out}
    added = []
    for name in need:
        if name not in present:
            tag = written(name, cc)
            added.append(tag)
            changes.append({"from": None, "to": tag, "why": "the job's subject"})
    counts = [i for i, t in enumerate(out) if COUNT.fullmatch(key(tag_key(t))) or key(tag_key(t)).startswith("multiple_")]
    at = max(counts) + 1 if counts else min(lead, len(out))          # next to the count tags that are already there, else after the quality prefix
    return [*out[:at], *added, *out[at:]]


def add_implied_counts(tags: list[str], cc: CheckContext) -> list[str]:
    """`3girls` without `multiple girls` is not how the model was trained (Danbooru implies the second from the first)."""
    out, present = [], {key(tag_key(t)) for t in tags}
    for tag in tags:
        out.append(tag)
        k = key(tag_key(tag))
        if COUNT.fullmatch(k):
            for parent in cc.lib.parents(k) or ([f"multiple_{k.lstrip('0123456789+')}"] if int(COUNT.fullmatch(k).group(1)) > 1 else []):
                if parent.startswith("multiple_") and parent not in present:
                    present.add(parent)
                    out.append(written(parent, cc))
    return out


def conflicts(tags: list[str]) -> list[str]:
    """Contradictions among the count tags that were not settled by the job's subjects, in words."""
    p = people(tags)
    found = []
    for kind in ("girls", "boys", "others"):
        if len(p[kind]) > 1:
            found.append("different " + kind + " counts: " + ", ".join(count_tag(kind[:-1], n) for n in sorted(p[kind])))
        if p[kind] and min(p[kind]) == 1 and kind[:-1] in p["multiple"]:
            found.append(f"1{kind[:-1]} together with multiple {kind}")
    total = sum(max(p[k], default=0) for k in ("girls", "boys", "others"))
    if p["solo"] and (total >= 2 or p["multiple"]):
        found.append("solo together with several people")
    if p["none"] and total:
        found.append("no humans together with people")
    return found


# ---------- order (Anima) ----------

def is_people_tag(name: str) -> bool:
    """Is this tag name one that says who is in the picture (`1girl`, `3girls`, `multiple girls`, `solo`, `no humans`)?"""
    k = key(name)
    return bool(COUNT.fullmatch(k)) or k in ("no_humans", "solo") or k.startswith("multiple_")


def bucket(tag: str, cc: CheckContext) -> float:
    """Where a tag goes in the Anima order: 0 quality/meta/year/safety, 1 people counts, 2 character, 3 series, 4 artist, 5 the rest."""
    k = key(tag_key(tag))
    if tag.startswith("@"):
        return 4
    if SCORE.fullmatch(k) or k in SAFETY or YEAR.fullmatch(k):
        return 0
    if is_people_tag(k):
        return 1 + people_rank(k) / 100
    cat = cc.lib.category_of(k)
    return {META: 0, CHARACTER: 2, COPYRIGHT: 3, ARTIST: 4}.get(cat, 5)


def people_rank(k: str) -> int:
    """Inside the people group: no humans, then girls, boys, others, each with its `multiple` after its count, then solo."""
    if k == "no_humans":
        return 0
    kind = next((i for i, name in enumerate(SUBJECT_TAGS) if name in k), 3)
    return 10 + kind * 2 + (1 if k.startswith("multiple_") else 0)       # `solo` has no kind, so it comes last: `1girl, solo`


def put_in_order(tags: list[str], cc: CheckContext) -> list[str]:
    """The Anima order. Protected tags (quality prefix, trigger words, style tags) keep their places; the others are sorted
    into the places that are left, keeping their own order inside a group."""
    movable = [i for i, t in enumerate(tags) if not any(key(tag_key(t)) == key(p) for p in cc.protected) and not TOKEN.search(t)]
    ordered = sorted((tags[i] for i in movable), key=lambda t: bucket(t, cc))      # sorted() is stable
    out = list(tags)
    for i, tag in zip(movable, ordered):
        out[i] = tag
    return out


# ---------- the whole prompt ----------

def trigger_copies(tags: list[str], cc: CheckContext, changes: list[dict]) -> list[str]:
    """A copy of a trigger word that the LLM garbled (`Style-A` for `Style_A`) is removed: same letters and digits, other
    spelling. Only when the real one is in the prompt, and never for a short word."""
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    real = {t for t in cc.protected if len(squash(t)) >= 5 and not t.startswith("@")}
    have = {t for t in real if any(tag_key(x) == tag_key(t) for x in tags)}
    out = []
    for tag in tags:
        if tag_key(tag) not in {tag_key(t) for t in real} and any(squash(tag_key(tag)) == squash(t) for t in have):
            changes.append({"from": tag, "to": None, "why": "a garbled copy of a trigger word"})
            continue
        out.append(tag)
    return out


def drop_duplicates(tags: list[str]) -> list[str]:
    """A tag twice (any spelling, any weight): the first stays, but a weighted copy beats a plain one."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for tag in tags:
        k = key(tag_key(tag))
        if k not in seen:
            seen[k] = len(out)
            out.append(tag)
        elif is_weighted(tag) and not is_weighted(out[seen[k]]):
            out[seen[k]] = tag
    return out


def check_prompt(text: str, cc: CheckContext) -> CheckResult:
    """The prompt after the tag check, what changed, and what is still wrong."""
    if not wants_check(cc.kind):
        return CheckResult(text)
    changes: list[dict] = []
    fixed = [fix_piece(p, cc, changes) for p in split_tags(text) if p.strip()]
    fixed = [t for t in fixed if t]
    fixed = drop_lora_style(fixed, cc, changes)
    fixed = drop_plan_artifacts(fixed, cc, changes)
    fixed = drop_lora_files(fixed, cc, changes)
    fixed = drop_filler(fixed, cc, changes)
    tags: list[str] = []
    for piece in fixed:
        core = tag_key(piece) if is_weighted(piece) else piece
        snapped = snap_phrase(core, cc) if not is_weighted(piece) else None
        if snapped:
            changes.append({"from": core, "to": ", ".join(snapped), "why": "these are the real tags for that phrase"})
            tags.extend(snapped)
        else:
            tags.append(piece)
    tags = drop_duplicates([t for t in tags if t])
    tags = trigger_copies(tags, cc, changes)
    if cc.rules and cc.rules.danbooru:
        tags = settle_people(tags, cc, changes)
        tags = add_implied_counts(tags, cc)
    tags = drop_unasked_artists(tags, cc, changes)
    tags = keep_words(tags, cc, changes)
    tags = keep_view(tags, cc, changes)
    tags = drop_look(tags, cc, changes)
    tags = drop_habits(tags, cc, changes)
    notes: list[str] = []
    tags = drop_contradictions(tags, cc, changes, notes)       # before the size floor, which then refills from tags that do not contradict
    tags = top_up(tags, cc, changes)
    tags = drop_avoided(tags, cc, changes)
    tags = write_rating(tags, cc, changes)
    if cc.rules and cc.rules.danbooru and cc.rules.order and cc.kind == "tags":
        tags = put_in_order(tags, cc)
    problems = conflicts([t for t in tags if not is_protected(tag_key(t), cc)]) if cc.rules and cc.rules.danbooru else []      # what the person typed is theirs
    for tag in tags:
        core = tag_key(tag)
        if COUNT_LIKE.fullmatch(core) and cc.lib.lookup(core) is None and not is_protected(core, cc):
            problems.append(f"`{core}` is not a real tag")
    return CheckResult(", ".join(tags), changes, problems, notes)
