"""The tag step of planning: for a job on a tag or mixed workflow, (1) real tags that fit the job are offered to the LLM
before it writes, (2) what it wrote is checked in code (tagcheck.py), (3) if something is still clearly wrong, one short repair call.

Everything here is skipped, quietly, when the card's prompt style is sentences, or there is no tag library (a fresh install).
Library text is data, never instructions: tags are only compared, and offered to the LLM inside a data block."""
import logging
import random
import re
import time
from dataclasses import dataclass

from app.embeddings import dot
from app.library.card_models import WorkflowCard
from app.planning.artists import asked_artists, wants_artists
from app.planning.concepts import CONCEPT_SAMPLE, load_concepts, subset
from app.fields import split_tags
from app.planning.context import PlanContext, block, tag_name
from app.planning.content_safety import matches_term
from app.planning.offers import SLOT_WEIGHT
from app.planning.scene import SLOTS as SCENE_SLOTS, scene_text
from app.planning.semantic import clean_query, rerank_blend, usable, vectors_or_background
from app.planning.tagcheck import (FAMILY_RULES, VIEW_SLOTS, CheckContext, CheckResult, absent_sex_words, check_prompt, implies_other_person, marks_absent_sex,
                                   named_in_typed, normal_phrase, one_person, phrases_of, restates, stand_ins, written)
from app.planning.taglib import ARTIST, CHARACTER, COPYRIGHT, GENERAL, TagLib, spaced

log = logging.getLogger("planning.tags")
MAX_SUGGESTIONS = 40
MAX_SLOT_TAGS = 10          # per slot; a scene with eight slots offers tens of tags, not two
SLOT_POOL = 30              # the best matches of a slot that count as "good": the offer is a random pick among these
MAX_EXACT_TAGS = 6          # of a slot's offer, at most this many are always the tags that restate its own phrases (no random pick, not left out as recent)
RANK_WEIGHT_FLOOR = 0.4     # the pool's last tag is this likely compared with its first (1.0): relevance leans the pick, it does not decide it
MAX_NAMED = {CHARACTER: 6, ARTIST: 6, COPYRIGHT: 4}       # characters, artists and series only when the brief names them
SEMANTIC_TOP = 60
SEMANTIC_FLOOR = 0.45       # least cosine for a tag found by meaning alone
VIEW_FLOOR = 0.55          # least closeness in meaning of the tag found for a camera or gaze phrase that is not a tag
SEMANTIC_BAND = 0.10        # ... and at most this far below the best tag's score (the rest is drift to near neighbours)
STOP = set("a an the of in on at with and or to for from by is are it its as into over under this that these those some very more most "
           "image images picture pictures photo photos make made create want wants like cute".split())
SLOT_ORDER = ("outfit", "pose", "expression", "body", "action", "setting", "lighting", "camera", "gaze", "mood")
FLOOR_LAST = ("body", "outfit")       # the size floor (tagcheck.top_up) uses these slots' tags last, clothes the very last


def without_phrases(text: str, phrases: list[str]) -> str:
    """The text with whole phrases taken out: a trigger word or a LoRA's name says nothing about what to draw."""
    for phrase in sorted(phrases, key=len, reverse=True):
        words = re.sub(r"^@", "", phrase.strip()).replace("_", " ")
        if not words:
            continue
        text = re.sub(rf"(?<![a-z0-9]){re.escape(words)}(?:s|es)?(?![a-z0-9])", " ", text, flags=re.I)
    return " ".join(text.split())


def stem(word: str) -> str:
    """Plural and -ing forms folded together for matching (`blossoms` -> `blossom`)."""
    for end in ("ing", "es", "s"):
        if word.endswith(end) and len(word) - len(end) >= 3:
            return word[:-len(end)]
    return word


def query_words(text: str) -> set[str]:
    """The words of a request that name something. What is said not to be there ("no people", "without a hat") is left out."""
    text = re.sub(r"\b(?:no|without|not)\s+(?:an?\s+|the\s+)?\w+", " ", text.lower())
    return {stem(w) for w in re.findall(r"[a-z0-9]+", text) if w not in STOP}


def keyword_hits(lib: TagLib, query: str) -> list[str]:
    """Pool tags whose every word is in the query (`old man`, `cherry blossoms`, a character's whole name), best first:
    the ones with more words, then the more used ones."""
    words = query_words(query)
    index = lib.word_index()
    stems: dict[str, list[str]] = {}
    for original, names in index.items():
        stems.setdefault(stem(original), []).extend(names)
    cands = {n for w in words for n in stems.get(w, [])}
    hits = []
    for name in cands:
        parts = {stem(p) for p in re.findall(r"[a-z0-9]+", name)}
        if parts and parts <= words and any(re.search("[a-z]{3}", p) for p in parts):       # not `:3`, `3:`, `;3`
            hits.append((len(parts), lib.tags[name][1], name))
    hits.sort(reverse=True)
    return [n for _, _, n in hits]


async def meaning_hits(ctx: PlanContext, lib: TagLib, query: str) -> list[str] | None:
    """Pool tags close in meaning to the query (None when search by meaning is not available: off, not ready, or failing).
    The vectors of the ~9 000 general tags are made once and kept on disk; until they exist this returns None and the keyword hits
    are used (the missing ones are embedded in the background, see vectors_or_background)."""
    if not usable(ctx):
        return None
    names = [n for n in lib.pool() if lib.tags[n][0] == GENERAL and not matches_term(spaced(n))]         # meaning is only used for general tags, never one the content rules avoid
    texts = [spaced(n) for n in names]
    vectors = await vectors_or_background(ctx, texts)
    q = await ctx.embedder.embed([clean_query(query)], query=True)
    if vectors is None or q is None:
        return None
    scored = sorted(((dot(q[0], v), n) for n, v in zip(names, vectors)), reverse=True)[:SEMANTIC_TOP]
    if not scored:
        return []
    log.info("closest tags by meaning: %s", ", ".join(f"{n} {s:.2f}" for s, n in scored[:12]))
    close = [(s, n) for s, n in scored if s >= max(SEMANTIC_FLOOR, scored[0][0] - SEMANTIC_BAND)]       # the gate stays the meaning search's
    blended = await rerank_blend(ctx, clean_query(query), [spaced(n) for _, n in close], [s for s, _ in close])       # the reranker only reorders what passed it
    return [close[i][1] for i in sorted(range(len(close)), key=lambda i: (-blended[i], i))]


async def concept_lines(ctx: PlanContext, text: str, rating: str, rng: random.Random | None = None, drop=None) -> list[str]:
    """The tags offered to the LLM "for the request's words": the real general tags of the library that the request's words point at, by
    keyword and by meaning (reranked), so a vague word ("cozy", "intricate", "rainy") reaches the writers as things a picture can show. One line of at
    most CONCEPT_SAMPLE tags: with `rng` a random subset of the better matches (`subset`), the ones the request names always in it; without it
    the best ones. At nsfw or explicit the words of the book's `skipped_at_high_rating` are not searched for. Never a tag the content rules avoid; tags for which
    `drop(tag)` is true are left out (the look tags of a job whose LoRA sets the look). Nothing without a tag library."""
    lib = ctx.taglib()
    query = load_concepts().for_rating(text, rating)
    if lib is None or len(query.strip()) < 2:
        return []
    by_keyword = keyword_hits(lib, query)
    by_meaning = await meaning_hits(ctx, lib, query)
    names = by_keyword if by_meaning is None else list(dict.fromkeys(_interleave(by_keyword, by_meaning)))
    tags = [spaced(n) for n in names if lib.tags[n][0] == GENERAL and not matches_term(spaced(n)) and not (drop and drop(spaced(n)))]
    better = tags[:2 * CONCEPT_SAMPLE] if rng is not None else tags[:CONCEPT_SAMPLE]
    return [", ".join(subset(better, rng, CONCEPT_SAMPLE, query))] if tags else []


def sample_tags(ranked: list[str], count: int, rng: random.Random, avoid: set[str] | frozenset[str] = frozenset(), name=lambda tag: tag) -> list[str]:
    """`count` tags chosen at random from the first SLOT_POOL of `ranked` (best match first), returned in ranked order.
    Better matches are somewhat likelier (RANK_WEIGHT_FLOOR). Tags whose `name` is in `avoid` (used in recent jobs) are only
    taken when too few others are left, so a slot is never starved. A pool no larger than `count` is offered whole.
    The relevance gate is the caller's: `ranked` holds matches only."""
    pool = ranked[:SLOT_POOL]
    if len(pool) <= count:
        return pool
    weight = {tag: 1 - (1 - RANK_WEIGHT_FLOOR) * i / (len(pool) - 1) for i, tag in enumerate(pool)}

    def pick(options: list[str], n: int) -> list[str]:       # weighted, without repeats (a key of u ** (1 / weight) per tag)
        return sorted(options, key=lambda tag: rng.random() ** (1 / weight[tag]), reverse=True)[:n]

    fresh = [tag for tag in pool if name(tag) not in avoid]
    chosen = pick(fresh, count)
    if len(chosen) < count:
        chosen += pick([tag for tag in pool if name(tag) in avoid], count - len(chosen))
    keep = set(chosen)
    return [tag for tag in pool if tag in keep]


@dataclass
class TagStep:
    ctx: PlanContext
    lib: TagLib
    check_context: CheckContext
    family: str

    @classmethod
    def build(cls, ctx: PlanContext, card: WorkflowCard, subjects: dict | None, protected: list[str], typed: str,
              rating: str = "general", detail_level: str = "normal", must_keep: list[str] | None = None,
              ignore_words: set[str] | None = None, lora_phrases: list[str] | None = None, keep_text: str = "",
              scene: dict | None = None, habits: dict[str, float] | None = None, lora_files: list[str] | None = None, look=None) -> "TagStep | None":
        """The tag step for this workflow card, or None when it does not apply (sentences card, or no tag library).
        `rating`, `detail_level`, `must_keep` and `lora_phrases` come from the scene plan, `scene` is the plan itself (its outfit
        and words are what the size floor and the habit rule respect; `habits` are the tags to drop when nothing asks for them, see
        PlanContext.habit_tags: a tweak passes none, it must not change what the person did not ask to change); `typed` is everything the person wrote (never touched),
        `keep_text` the job's own words the keep check demands. `look` (look.Look) is given when a LoRA of the job sets the picture's look: no light or
        colour tags are offered, and the check removes them (drop_look)."""
        if card.prompt_style.kind not in ("tags", "mixed"):
            return None
        lib = ctx.taglib()
        if lib is None:
            return None
        asked = asked_artists(typed)
        cc = CheckContext(lib, FAMILY_RULES.get(card.family), card.prompt_style.kind, protected, typed, subjects, keep_text=keep_text,
                          family=card.family, rating=rating, detail_level=detail_level, must_keep=list(must_keep or []), avoid=[str(a) for a in (scene or {}).get("avoid") or []],
                          ignore_words=set(ignore_words or ()), lora_phrases=list(lora_phrases or []),
                          artist_allowed=wants_artists(typed), artist_limit=asked or 1,
                          outfit=(scene or {}).get("outfit", ""), plan_text=scene_text(scene), habits=dict(habits or {}),
                          slots={k: str(v) for k, v in (scene or {}).items() if k in SCENE_SLOTS and v}, contradictions=ctx.contradictions(), lora_files=list(lora_files or []), look=look)
        return cls(ctx, lib, cc, card.family)

    @property
    def danbooru(self) -> bool:
        return bool(self.check_context.rules and self.check_context.rules.danbooru)

    def check(self, text: str) -> CheckResult:
        return check_prompt(text, self.check_context)

    # ---------- offering real tags ----------

    async def suggestions(self, query: str) -> list[str]:
        """Up to MAX_SUGGESTIONS real tags that match the job, written the way this family writes them. Only for a family trained on
        Danbooru tags. Never a tag the content rules avoid (matches_term); nothing else is held back."""
        if not self.danbooru or not query.strip():
            return []
        lib, started = self.lib, time.monotonic()
        by_keyword = keyword_hits(lib, query)
        by_meaning = await meaning_hits(self.ctx, lib, query)
        names = by_keyword if by_meaning is None else [n for n in dict.fromkeys(_interleave(by_keyword, by_meaning))]
        out, used = [], {}
        for name in names:
            cat = lib.tags[name][0]
            if (cat == ARTIST and not self.check_context.artist_allowed) or matches_term(spaced(name)) \
                    or used.get(cat, 0) >= (min(MAX_NAMED.get(ARTIST, 1), self.check_context.artist_limit) if cat == ARTIST else MAX_NAMED.get(cat, 99)) \
                    or (cat != GENERAL and name not in by_keyword):
                continue
            used[cat] = used.get(cat, 0) + 1
            out.append(self._write(name, cat))
            if len(out) >= MAX_SUGGESTIONS:
                break
        log.info("%d real tags offered (%s) in %.2f s", len(out), "keywords" if by_meaning is None else "keywords and meaning", time.monotonic() - started)
        return out

    async def slot_suggestions(self, slots: dict[str, str], exclude: list[str] | None = None, rng: random.Random | None = None,
                               avoid: set[str] | None = None) -> dict[str, list[str]]:
        """{slot: real tags} for each slot of the scene plan, found by that slot's own words (keywords and meaning).
        Trigger words and LoRA names are taken out of the query first: they say what the file is called, not what to draw.
        The matches are the gate; with `rng` the offer is a random pick among the SLOT_POOL best (see sample_tags), avoiding the
        tag names in `avoid` (the recent jobs' tags) while enough others exist. Without `rng` it is the best MAX_SLOT_TAGS."""
        if not self.danbooru or not slots:
            return {}
        lib, started = self.lib, time.monotonic()
        absent = absent_sex_words(self.check_context.subjects)        # words of a sex the job's people do not include
        alone = one_person(self.check_context.subjects)                # a picture of one person: no tag that needs a second
        exclude = [e for e in (exclude or []) if e and e.strip()]
        out: dict[str, list[str]] = {}
        seen: set[str] = set()
        pools = 0
        look = self.check_context.look
        for slot in SLOT_ORDER:
            value = slots.get(slot) or ""
            if look is not None and slot == "lighting":
                continue            # the LoRA sets the light and colours
            if value.strip().lower() in {"none", "n/a", "no humans", "no body", "no outfit", "no pose", "no action", "no expression", "no gaze", "no mood"} \
                    or value.strip().lower().startswith("no "):
                continue
            query = without_phrases(value, exclude)
            if len(query.strip()) < 2:
                continue
            by_keyword = keyword_hits(lib, query)
            by_meaning = await meaning_hits(self.ctx, lib, query)
            names = by_keyword if by_meaning is None else [n for n in dict.fromkeys(_interleave(by_keyword, by_meaning))]
            ranked = [n for n in names if lib.tags[n][0] == GENERAL and n not in seen and not matches_term(spaced(n)) and not (look is not None and look.is_look(spaced(n), lib))
                      and not (marks_absent_sex(spaced(n), absent) and not named_in_typed(spaced(n), self.check_context))
                      and not (alone and implies_other_person(spaced(n)) and not named_in_typed(spaced(n), self.check_context))]
            # the tags that restate one of the slot's own phrases (`long hair` for `long wavy hair`) are always offered: they say what the plan says
            phrases = phrases_of(query)
            exact = [n for n in ranked if n in by_keyword and restates(spaced(n), phrases)][:MAX_EXACT_TAGS]
            rest = [n for n in ranked[:SLOT_POOL] if n not in exact]
            pools += min(len(ranked), SLOT_POOL)
            room = MAX_SLOT_TAGS - len(exact)
            picked = set(exact) | set(rest[:room] if rng is None else sample_tags(rest, room, rng, avoid or frozenset(), spaced))
            chosen = [n for n in ranked if n in picked]
            seen.update(chosen)
            if chosen:
                out[slot] = [self._write(name, GENERAL) for name in chosen]
        log.info("%d slot tag groups, %d tags offered out of %d good matches (%s, %s) in %.2f s: %s", len(out), len(seen), pools,
                 "random pick" if rng else "best first", "keywords" if not usable(self.ctx) else "keywords and meaning", time.monotonic() - started,
                 "; ".join(f"{slot}: {', '.join(tags)}" for slot, tags in out.items()))
        return out

    async def view_tags(self, slots: dict[str, str]) -> dict[str, list[str]]:
        """{phrase: [the closest real tag]} for the phrases of the plan's camera and gaze that no tag, alias or break-up into tags says
        (`looking at each other`, `rear view`): the general tag closest in meaning to the phrase, judged together with how well it fits what the
        slot is about (`[wildcards] slot_about`; without it `worm` was the nearest tag to `worm's-eye view`). Only a close match counts
        (VIEW_FLOOR): better no tag than a wrong one. The tag check (tagcheck.keep_view) adds it when the prompt shows neither the phrase nor a tag
        for it, so a planned view that is not a tag is not lost. Nothing without search by meaning."""
        if not self.danbooru or not usable(self.ctx):
            return {}
        cc, out = self.check_context, {}
        names = [n for n in self.lib.pool() if self.lib.tags[n][0] == GENERAL and not matches_term(spaced(n))]
        vectors = None
        for slot in VIEW_SLOTS:
            about = self.ctx.config.wildcards_slot_about.get(slot, "")
            for words in phrases_of(slots.get(slot) or ""):
                phrase = " ".join(words)
                if stand_ins(phrase, cc):
                    continue
                vectors = vectors if vectors is not None else await vectors_or_background(self.ctx, [spaced(n) for n in names])
                q = await self.ctx.embedder.embed([clean_query(phrase)], query=True)
                a = await self.ctx.embedder.embed([about], query=True) if about else None
                if vectors is None or q is None:
                    return out
                best = max(zip(names, vectors), key=lambda nv: dot(q[0], nv[1]) + (SLOT_WEIGHT * dot(a[0], nv[1]) if a else 0.0))
                if dot(q[0], best[1]) >= VIEW_FLOOR:
                    out[normal_phrase(phrase)] = [written(best[0], cc)]
        if out:
            log.info("view tags found by meaning: %s", "; ".join(f"{p} -> {t[0]}" for p, t in out.items()))
        return out

    def offer(self, groups: dict[str, list[str]]):
        """The slot offers flattened for the size floor and the repair hints: the slots in their order, most relevant tags first
        in each, but the body slot and then the outfit slot last, so the floor adds clothes only when nothing else is left.
        The tags of the outfit slot are also kept apart (`outfit_offers`) for the rule about a named outfit."""
        order = [slot for slot in groups if slot not in FLOOR_LAST] + [slot for slot in FLOOR_LAST if slot in groups]
        self.check_context.offered = [tag for slot in order for tag in groups[slot]]
        self.check_context.outfit_offers = set(groups.get("outfit", ()))
        return self.check_context.offered

    def _write(self, name: str, category: int) -> str:
        tag = written(name, self.check_context)
        return "@" + tag if category == ARTIST and self.check_context.rules and self.check_context.rules.artist_at else tag

    # ---------- the one repair call ----------

    async def repair(self, numbered: list[tuple[int, str, list[str]]], hints: list[str]) -> list[str] | None:
        """Ask the LLM to repair prompts that still have problems. `numbered` is [(prompt number, text, problems)]. Returns the new
        texts in the same order, or None when the call failed (the prompts then stay as the check left them)."""
        items = [{"number": n, "prompt": text, "problems": problems} for n, text, problems in numbered]
        inputs = {"prompts": block("prompts to repair", items), "tags": block("real tags that may help", hints) if hints else "(none)"}
        reply = await self.ctx.llm.call("repair_tags", inputs, None, {"prompts": {"minItems": len(items), "maxItems": len(items)}})
        if reply.get("needs_attention") or len(reply.get("prompts") or []) != len(items):
            log.warning("repair_tags did not give a usable answer: %s", reply.get("error", "wrong number of prompts"))
            return None
        return [p.strip() for p in reply["prompts"]]


def _interleave(a: list[str], b: list[str]) -> list[str]:
    out = []
    for i in range(max(len(a), len(b))):
        out += [x[i] for x in (a, b) if i < len(x)]
    return out


def tag_offer_record(groups: dict[str, list[str]], prompt: str) -> dict:
    """What a prompt's item stores about the slot tag offers: {"offered": {slot: tags}, "used": the offered tags the prompt has}."""
    in_prompt = {tag_name(t) for t in split_tags(prompt)}
    return {"offered": groups, "used": [t for tags in groups.values() for t in tags if tag_name(t) in in_prompt]}


def records(result: CheckResult) -> list[dict]:
    """The changes of one prompt as they are kept on the job card: [{from, to, why}]."""
    return [{"from": c["from"], "to": c["to"], "why": c["why"]} for c in result.changes]
