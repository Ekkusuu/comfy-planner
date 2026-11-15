"""Wildcard offers: which entries of the user's lists reach the LLM for one picture, and for which scene slot.

Before this, a few entries were drawn at random from the chosen lists and shown with the list's role; the scene plan could take them or not,
and what it "used" was whatever happened to match. Now:

  * Every list the picture draws from serves one or more **slots** of the scene plan (camera, pose, outfit, setting, lighting, ...). The LLM names the
    slot when it picks the list (pick_wildcards); without an answer the list's own entries decide (`serving.py`: each entry goes to the slot it
    fits, by meaning; a role the user set for the list always wins; the folder name is only a weak hint). One list can feed several slots (view
    phrases and gaze phrases in one file), and a list that fits no slot is only offered when the request is about its topic. Several lists can
    serve one slot, with each list's share of it limited so one big list does not flood it.
  * The entries offered for a slot are the ones **relevant to the picture**: ranked by meaning against the brief and against what the slot
    is about (`wildcards_slot_about`) when search by meaning is on (the best few reranked, `rank_entries`), by keyword otherwise. Not the top few
    every time: a handful are drawn at random from the best ones, so two runs differ. An entry that the content rules avoid is never a candidate (`plain_entries`).
  * Some slots are offered for **every picture** whose request leaves them open (`wildcards_always_slots`: camera and gaze). Where the camera is
    and where the person looks are two slots the scene plan fills, from entries of any list that holds them. They are offers, not rules: the LLM
    chooses an entry for each from the ones offered for it, or its own words when none fits. When no list covers one, the model writes it and
    the job says so (`gaps`). A request that decides one ("from below", "looking back") wins: that slot is not offered.
  * Where the request asked for something to differ between images (`vary`: camera, pose, ...), one entry of that slot is **assigned** to each image
    and never repeated within the plan while unused ones remain (`PlanState.used`). It is required: the brief and the scene plan must show
    it, and code writes it into the scene's slot if the LLM left it out.
  * A list only serves a slot its entries fit: the entries are compared in meaning with what the slot is about, and a list the LLM picked for a
    slot that falls well below the best list of that slot (camera models, photographers and lens gear in the camera slot, next to view and
    framing lists) is not offered for it (`fitting_lists`; not for a list whose role the user set). Among the entries of a slot, real tags of the tag library (Danbooru's own words:
    `from above`, `dutch angle`) rank a little higher than phrases that are not tags (`is_real_tag`).
  * Which entries are adult is read from their meaning (content_level.py: entries whose meaning is closer to nudity or sexual activity than to
    ordinary things), whatever list or folder holds them (`adult_content`). A list the user set to the `nsfw` role is adult as a whole; the folder's
    default role only counts when meaning cannot be told (no embeddings yet). A list whose entries are mostly adult never fills an open slot on its own.
  * A picture rated below nsfw is offered no adult entries. A picture rated nsfw or explicit is offered adult content: the adult entries of the whole
    library join the lists of the slot they belong to, and about half of what a slot offers is drawn from them (`adult_pools`, `ADULT_SHARE`).
  * A job whose LoRA sets the look (look.py) is offered no light or colour: the lighting slot is left out and no entry that holds a
    look tag (a colour scheme, `purple lighting`) is offered for any slot (`look`).
  * What counts as "used" is what is really in the scene plan, checked in code (`record`).
"""
import random
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from statistics import fmean

from app.embeddings import dot
from app.planning.content_level import adult_entries, judged, wants_adult
from app.planning.context import PlanContext
from app.planning.rank import Bm25
from app.planning.content_safety import matches_term
from app.planning.semantic import clean_query, rerank_blend, usable, vectors_or_background
from app.planning.serving import Serving, serve, serve_now
from app.planning.taglib import GENERAL, POOL_MIN, TagLib
from app.wildcards.expand import find_refs
from app.wildcards.ideas import ADULT_ROLE, IdeaSet, MAX_IDEA_ENTRY, choose, has_phrase, normal, given_roles
from app.wildcards.store import Catalog, WildList

SLOT_WEIGHT = 0.5           # how much "is this entry about the slot" counts next to "is it relevant to the picture"
POOL_FACTOR = 4             # entries are drawn at random from the best POOL_FACTOR x k
MAX_LISTS_PER_SLOT = 6      # lists whose entries compete for one slot
PER_LIST_POOL = 150         # most entries one list adds to one slot's pool (an even sample of a longer list), so one big list does not flood it
TAG_BONUS = 0.05            # added to the rank of an entry that is a real tag of the tag library
FIT_SAMPLE = 60             # entries of a list that are compared with the slot (an even sample of a longer list)
FIT_TOP = 5                 # ... and a list's fit is the mean of its best this many
ADULT_SHARE = 0.5           # the share of a slot's offers drawn from adult entries when the picture is nsfw or explicit and the slot has some
ADULT_LIST_SHARE = 0.5      # a list at least this share of whose entries are adult is an adult list: it never fills an open slot on its own


@dataclass
class PlanState:
    """What a whole planning run knows across its jobs: the wildcard entries already assigned or used per slot (so a batch never repeats one
    while others exist), and short digests of the scenes planned so far (so the next one is asked to be different)."""
    used: dict[str, Counter] = field(default_factory=dict)
    scenes: list[dict] = field(default_factory=list)
    loras: Counter = field(default_factory=Counter)         # LoRAs the planner added on its own to earlier images of the run, and how often

    def mark(self, slot: str, text: str):
        self.used.setdefault(slot, Counter())[normal(text)] += 1

    def count(self, slot: str, text: str) -> int:
        return self.used.get(slot, Counter()).get(normal(text), 0)

    def remember_scene(self, scene: dict | None, limit: int = 40):
        if scene:
            self.scenes.append({k: scene.get(k, "") for k in ("subjects", "body", "outfit", "pose", "camera", "gaze", "setting", "lighting") if scene.get(k)})
            del self.scenes[:-limit]


def plain_entries(l: WildList) -> list[str]:
    """The entries of a list that can be offered as they are: no wildcard syntax, not a whole caption, nothing the content rules avoid (`matches_term`)."""
    return list(dict.fromkeys(e.strip() for e in l.entries if e.strip() and "__" not in e and "{" not in e and len(e) <= MAX_IDEA_ENTRY and not matches_term(e)))


def entry_lists(l: WildList, catalog: Catalog) -> list[WildList]:
    """The lists whose entries a list offers: itself, or - when all it holds are references to other lists (`random/rng-shot` is three
    `__camera/...__` lines) - the lists it refers to."""
    if plain_entries(l):
        return [l]
    found = [r for e in l.entries for ref in find_refs(e) for r in catalog.resolve(ref)[0] if r.enabled and r.name != l.name]
    return list({r.name: r for r in found}.values())


def slot_of(config, role: str, picked: str = "") -> str:
    """The scene slot a list serves: what the LLM said when it picked the list, else what its role maps to ("" when neither says)."""
    slots = set(config.wildcards_slot_about)
    if picked in slots:
        return picked
    return config.wildcards_role_slots.get(role, "")


def is_real_tag(lib: TagLib | None, entry: str) -> bool:
    """Is the entry a tag of the tag library that pictures really use (a general tag, spelled that way or through an alias)?"""
    hit = lib.lookup(entry) if lib is not None else None
    return bool(hit and hit.category == GENERAL and hit.count >= POOL_MIN[GENERAL])


async def rank_entries(ctx: PlanContext, entries: list[str], query: str, slot: str, rerank: bool = False) -> list[float]:
    """A relevance score per entry: closeness in meaning to the picture plus closeness to what the slot is about (search by meaning),
    or keyword overlap with the picture (BM25) when meaning search is not available; real tags of the tag library get TAG_BONUS on top.
    With `rerank` the best of the meaning scores are blended with the reranker's score for the picture and the slot (semantic.rerank_blend); the
    callers that only compare lists leave it off. Higher is better."""
    about = ctx.config.wildcards_slot_about.get(slot, "")
    scores = None
    if usable(ctx) and entries and (clean_query(query) or about):
        vectors = await vectors_or_background(ctx, entries)
        q = await ctx.embedder.embed([clean_query(query) or about], query=True)
        a = await ctx.embedder.embed([about], query=True) if about else None
        if vectors is not None and q is not None:
            scores = [dot(q[0], v) + (SLOT_WEIGHT * dot(a[0], v) if a else 0.0) for v in vectors]
            if rerank:
                scores = await rerank_blend(ctx, ". ".join(x for x in (clean_query(query), about) if x), entries, scores)
    if scores is None:
        scores = [float(k) for k in Bm25(entries).scores(f"{query} {about}")] if entries else []
    lib = ctx.taglib()
    return [s + (TAG_BONUS if is_real_tag(lib, e) else 0.0) for s, e in zip(scores, entries)]


async def list_fit(ctx: PlanContext, l: WildList, slot: str) -> float | None:
    """How well a list's entries fit what the slot is about: the mean closeness in meaning of its best FIT_TOP entries (an even sample of
    FIT_SAMPLE of a long list) to the slot's description. None when that can not be told (no meaning search, nothing to compare)."""
    about = ctx.config.wildcards_slot_about.get(slot, "")
    entries = plain_entries(l)
    if len(entries) > FIT_SAMPLE:
        entries = [entries[i * len(entries) // FIT_SAMPLE] for i in range(FIT_SAMPLE)]
    if not about or not entries or not usable(ctx):
        return None
    vectors = await vectors_or_background(ctx, entries)
    a = await ctx.embedder.embed([about], query=True)
    if vectors is None or a is None:
        return None
    top = sorted((dot(a[0], v) for v in vectors), reverse=True)[:FIT_TOP]
    return sum(top) / len(top)


async def fitting_slot(ctx: PlanContext, l: WildList, slot: str) -> str:
    """The slot the LLM named for a list, unless the list's entries fit another slot clearly better (more than `embeddings_margin` closer in
    meaning to what that slot is about): a list of expressions is not offered as the lighting. Without search by meaning the LLM's slot stands."""
    fits = {s: await list_fit(ctx, l, s) for s in ctx.config.wildcards_slot_about}
    known = {s: f for s, f in fits.items() if f is not None}
    if slot not in known:
        return slot
    best = max(known, key=lambda s: known[s])
    return best if known[best] - known[slot] > ctx.config.embeddings_margin else slot


async def served(ctx: PlanContext, lists: list[WildList]) -> dict[str, Serving]:
    """What each list serves, read from its entries (serving.py). Only `plain_entries` ever become candidates."""
    return await serve(ctx, lists, plain_entries)


def served_slots(sv: dict[str, Serving]) -> dict[str, dict[str, list[str]]]:
    """{list name: {slot: entries}}, the shape ideas.choose reads."""
    return {name: s.slots for name, s in sv.items()}


def spread(entries: list[str], limit: int) -> list[str]:
    """At most `limit` entries, an even sample of a longer list (the same every time)."""
    return entries if len(entries) <= limit else [entries[i * len(entries) // limit] for i in range(limit)]


async def fitting_lists(ctx: PlanContext, slot: str, mine: list[WildList], lists: list[WildList], sv: dict[str, Serving] | None = None) -> list[WildList]:
    """The lists of `mine` that fit the slot: each one's fit (`list_fit`) is within `embeddings_margin` of the best fit among `mine` and the other
    lists that serve this slot. A list whose fit can not be told is kept, and so is one whose role the user set. A pick of the LLM for a slot never
    brings in entries about something else: the photographers of a "camera" folder are not viewing angles."""
    roles = {r for r, s in ctx.config.wildcards_role_slots.items() if s == slot}
    if sv is not None:
        rivals = [replace(l, entries=sv[l.name].slots[slot]) for l in lists if slot in sv.get(l.name, Serving()).slots]
    else:
        rivals = [l for l in lists if l.role in roles]
    rivals = [l for l in rivals if all(l.name != m.name for m in mine)]
    fits = {l.name: await list_fit(ctx, l, slot) for l in [*mine, *rivals]}
    known = [f for f in fits.values() if f is not None]
    if not known:
        return mine
    best = max(known)
    return [l for l in mine if fits[l.name] is None or l.role_set or fits[l.name] >= best - ctx.config.embeddings_margin]


def spread_pick(entries: list[str], scores: list[float], k: int, rng: random.Random, state: PlanState | None, slot: str) -> list[str]:
    """`k` entries for a slot: drawn at random from the best POOL_FACTOR x k among the entries this plan has used least (unused ones first, the
    better-ranked first within a use count). When fewer than k are unused, the least used ones fill the rest."""
    n = len(entries)
    if not n or k < 1:
        return []
    uses = [state.count(slot, e) if state is not None else 0 for e in entries]
    order = sorted(range(n), key=lambda i: (uses[i], -scores[i], i))
    cut = uses[order[min(k, n) - 1]]        # the highest use count needed to fill k places
    pool = [i for i in order[:max(POOL_FACTOR * k, k)] if uses[i] <= cut]
    return [entries[i] for i in rng.sample(pool, min(k, len(pool)))]


def offer_draw(entries: list[str], scores: list[float], size: int, rng: random.Random, state: PlanState, slot: str, adult: set[str]) -> list[str]:
    """`size` entries to offer for a slot (`spread_pick`). When the slot has adult entries (`adult`: the picture is nsfw or explicit), ADULT_SHARE of
    the places go to them and the rest to the other entries, so the adult ones are on offer however the ordinary ones rank."""
    mine = [i for i, e in enumerate(entries) if e in adult]
    if not mine or len(mine) == len(entries):
        return spread_pick(entries, scores, size, rng, state, slot)
    others = [i for i in range(len(entries)) if i not in set(mine)]
    take = max(1, round(size * ADULT_SHARE))
    chosen = spread_pick([entries[i] for i in mine], [scores[i] for i in mine], take, rng, state, slot)
    return chosen + spread_pick([entries[i] for i in others], [scores[i] for i in others], size - len(chosen), rng, state, slot)


async def slot_lists(ctx: PlanContext, picks: list, lists: list[WildList], skip_slots: set[str], need: set[str], query: str,
                     sv: dict[str, Serving] | None = None, held_back: set[str] | None = None) -> dict[str, list[WildList]]:
    """{slot: the lists that serve it}. A list that serves a slot appears once in it, holding only the entries that feed that slot (at most
    PER_LIST_POOL of them, and at most MAX_LISTS_PER_SLOT lists per slot). The picked lists serve the slot the LLM named, else every slot
    their own entries fit (`sv`, serving.py; a list that fits none is a loose list under ""). For every slot in `need` (what the request wants
    different in every image) that has none, the lists serving it, the ones that fit the slot's topic best. Slots in `skip_slots` (the brief
    already decides them) get nothing unless they are in `need`. The lists in `held_back` (adult lists, `adult_content`; without it, the
    lists with the ADULT_ROLE) are not brought in for a slot in `need`."""
    c = ctx.config
    sv = sv if sv is not None else await served(ctx, lists)
    by_name = {l.name: l for l in lists}
    if held_back is None:
        held_back = {l.name for l in lists if l.role == ADULT_ROLE}
    catalog = Catalog(lists)
    out: dict[str, list[WildList]] = {}

    def put(where: str, source: WildList, entries: list[str]):
        if entries and all(x.name != source.name for x in out.setdefault(where, [])):
            out[where].append(replace(source, entries=spread(entries, PER_LIST_POOL) if where else entries))

    for p in picks:
        name, slot_name = p[0], (p[2] if len(p) > 2 else "")
        l = by_name.get(name)
        if l is None:
            continue
        named = slot_name if slot_name in c.wildcards_slot_about else ""
        if named:        # the LLM's own choice of slot is checked against what the list holds
            named = await fitting_slot(ctx, l, named)
        for source in entry_lists(l, catalog):       # a list of references serves what the lists it points at serve
            entries = plain_entries(source)
            targets = {named: entries} if named else ((sv[source.name].slots if source.name in sv else {}) or {"": entries})
            for where, found in targets.items():
                if where not in skip_slots or where in need:
                    put(where, source, found)
    for slot in list(out):
        if slot:
            out[slot] = await fitting_lists(ctx, slot, out[slot], lists, sv)
    for slot in need:
        if not slot or out.get(slot):
            continue
        pool = [(by_name[n], s.slots[slot]) for n, s in sv.items() if slot in s.slots and n in by_name and n not in held_back]
        about = c.wildcards_slot_about.get(slot, "")
        rated = []
        for l, entries in pool:
            scores = await rank_entries(ctx, spread(entries, 60), about, slot)
            top = sorted(scores, reverse=True)[:5]
            rated.append((fmean(top) if top else float("-inf"), l.name, l, entries))
        for _, _, l, entries in sorted(rated, key=lambda x: (-x[0], x[1]))[:MAX_LISTS_PER_SLOT]:
            put(slot, l, entries)
        if out.get(slot):
            out[slot] = await fitting_lists(ctx, slot, out[slot], lists, sv)         # camera makes and photographers do not come along with the view lists
    return {s: ls[:MAX_LISTS_PER_SLOT] if s else ls for s, ls in out.items() if ls}


@dataclass
class AdultContent:
    entries: set[str] = field(default_factory=set)      # the adult entries of the library
    lists: set[str] = field(default_factory=set)        # the lists that are adult as a whole (most of their entries, or the user set the role)


async def adult_content(ctx: PlanContext, lists: list[WildList]) -> AdultContent:
    """Which entries and lists of the library are adult, read from the entries' meaning (content_level.py) wherever they are kept. A list
    the user set to the ADULT_ROLE is their decision: all of it is adult. When meaning cannot be told (no embeddings, or the vectors are still
    being computed) a list whose folder defaults to that role is taken as adult, the only hint left. Artist lists are names: never adult."""
    plain = {l.name: plain_entries(l) for l in lists if l.role != "artist"}
    every = [e for es in plain.values() for e in es]
    out = AdultContent(entries=set(await adult_entries(ctx, every)))
    told = usable(ctx) and judged(every)
    for l in lists:
        es = plain.get(l.name, [])
        if l.role == ADULT_ROLE and (l.role_set or not told):
            out.entries.update(es)
            out.lists.add(l.name)
        elif es and sum(e in out.entries for e in es) >= ADULT_LIST_SHARE * len(es):
            out.lists.add(l.name)
    return out


def without(pools: dict[str, list[WildList]], held: set[str]) -> dict[str, list[WildList]]:
    """The pools with the `held` entries taken out of every list (a list left empty is dropped, and a slot left empty)."""
    out = {}
    for slot, ls in pools.items():
        kept = [replace(l, entries=[e for e in l.entries if e not in held]) for l in ls]
        if kept := [l for l in kept if l.entries]:
            out[slot] = kept
    return out


async def adult_pools(ctx: PlanContext, lists: list[WildList], pools: dict[str, list[WildList]], skip_slots: set[str], need: set[str],
                      sv: dict[str, Serving] | None = None, adult: set[str] | None = None) -> set[str]:
    """Bring the adult content of the whole library into `pools` (changed in place) for a nsfw or explicit picture: a list that has adult entries
    (`adult`, else by meaning) and is not in its slot's pool yet joins it with only those entries. The slot is the one each entry fits (serving.py); an adult
    entry that fits no slot goes to the slot the list's role maps to. A slot the brief already fixes gets nothing. Returns the adult entries."""
    c = ctx.config
    sv = sv if sv is not None else await served(ctx, lists)
    candidates = [l for l in lists if l.role != "artist"]
    plain = {l.name: plain_entries(l) for l in candidates}
    if adult is None:
        adult = await adult_entries(ctx, [e for l in candidates for e in plain[l.name]])
    for l in candidates:
        by_slot: dict[str, list[str]] = {}
        placed = set()
        for slot, entries in (sv[l.name].slots if l.name in sv else {}).items():
            by_slot[slot] = [e for e in entries if e in adult]
            placed.update(by_slot[slot])
        rest = [e for e in plain[l.name] if e in adult and e not in placed]
        if rest:
            by_slot.setdefault(slot_of(c, l.role), []).extend(rest)
        for slot, mine in by_slot.items():
            if mine and not (slot in skip_slots and slot not in need) and not any(x.name == l.name for x in pools.get(slot, [])):
                pools.setdefault(slot, []).append(replace(l, entries=spread(mine, PER_LIST_POOL)))
    return adult


async def build(ctx: PlanContext, brief: str, given_text: str, picks: list, vary: list[str], state: PlanState | None, rng: random.Random,
                images: int = 1, related: list | None = None, assigned_before: dict[str, str] | None = None, always: bool = False,
                rating: str | None = None, look=None, gaps: list[str] | None = None) -> IdeaSet | None:
    """The wildcard offers for `images` pictures of one brief: an IdeaSet whose entries carry their `slot` and whether it is `required`
    (assigned because the request wants that to differ between images). None when wildcards are off or nothing fits.
    `brief` is what relevance is judged against; `given_text` is what decides which slots the brief already fixes. With `always`
    (the offers for a scene plan or a prompt, not for descriptions) the slots of `wildcards_always_slots` the brief leaves open are
    offered even when no chosen list serves them. `rating` is the content level of the picture: at nsfw or explicit, adult entries of the
    whole library are added to the offers (`adult_pools`). `look` (look.Look) is given when a LoRA of the job sets the look: the lighting slot
    is not offered and entries with a look tag are left out. `gaps`, when given, is filled with the open always-slots that no list covers (the
    model writes those itself); it is empty when there are no lists at all."""
    store, c = ctx.wildcards, ctx.config
    if gaps is not None:
        gaps.clear()
    if store is None or not c.wildcards_enabled or images < 1:
        return None
    lists = [l for l in store.lists() if l.enabled and l.entries]
    if not lists:
        return None
    sv = await served(ctx, lists)         # what each list serves, read from its entries
    found = await adult_content(ctx, lists)         # which entries and lists are adult, read from the entries
    # the lists the rules choose (the LLM's picks, phrase and meaning matches, keyword matches, open slots), each once
    chosen = choose(lists, brief, given_text, c, rng, [(p[0], p[1]) for p in picks], related, served_slots(sv), found.lists)
    pick_slots = {p[0]: (p[2] if len(p) > 2 else "") for p in picks}
    merged = [(ch.wild.name, ch.why, pick_slots.get(ch.wild.name, "")) for ch in chosen]
    given_slots = {c.wildcards_role_slots.get(r, "") for r in given_roles(given_text, lists, c, served_slots(sv))} - {""}
    if look is not None:
        given_slots.add("lighting")         # the LoRA's look is not argued with: no lighting offers
    need = set(vary) - ({"lighting"} if look is not None else set())
    open_always = {s for s in c.wildcards_always_slots if s not in given_slots and s in c.wildcards_slot_about} if always else set()
    pools = await slot_lists(ctx, merged, lists, given_slots, need | open_always, brief, sv, found.lists)
    adult: set[str] = set()
    if wants_adult(rating):
        adult = await adult_pools(ctx, lists, pools, given_slots, need, sv, found.entries)
    else:         # a picture below nsfw is offered no adult entries, whichever list holds them
        pools = without(pools, found.entries)
    if gaps is not None:
        gaps.extend(s for s in c.wildcards_always_slots if s in open_always and not pools.get(s))
    if not pools:
        return None
    blocked: set[str] = set()
    if look is not None:
        lib = ctx.taglib()
        blocked = {e for ls in pools.values() for l in ls for e in plain_entries(l) if look.in_entry(e, lib)}
    order = [ch.wild for ch in chosen] + [l for ls in pools.values() for l in ls]        # the lists in the order they were chosen
    result = IdeaSet(lists=[l for l in {l.name: l for l in order}.values() if any(l.name == x.name for ls in pools.values() for x in ls)],
                     per_prompt=[[] for _ in range(images)])
    result.chosen = {ch.wild.name: {"by": ch.by, "why": ch.why} for ch in chosen}
    local = PlanState(used={s: Counter(n) for s, n in state.used.items()} if state is not None else {})
    for slot, slot_ls in pools.items():
        if not slot:        # lists no slot is known for (a style, material or concept list ...): a few entries of each, as they always were
            for l in slot_ls:
                entries = [e for e in plain_entries(l) if e not in blocked]
                scores = await rank_entries(ctx, entries, brief, "") if entries else []
                for n in range(images):
                    for text in spread_pick(entries, scores, c.wildcards_entries_per_list, rng, local, f"list:{l.name}"):
                        local.mark(f"list:{l.name}", text)
                        result.per_prompt[n].append({"list": l.name, "role": l.role, "text": text})
            continue
        entries, owner = [], {}
        for l in slot_ls:
            for e in plain_entries(l):
                if normal(e) not in owner and e not in blocked:
                    entries.append(e)
                    owner[normal(e)] = l
        if not entries:
            continue
        result.pool[slot] = entries
        scores = await rank_entries(ctx, entries, brief, slot, rerank=True)
        for n in range(images):
            if slot in need:
                for text in spread_pick(entries, scores, 1, rng, state, slot):
                    if state is not None:
                        state.mark(slot, text)
                    l = owner[normal(text)]
                    result.per_prompt[n].append({"list": l.name, "role": l.role, "text": text, "slot": slot, "required": True})
            else:       # the pictures of one call do not get the same offers either
                for text in offer_draw(entries, scores, c.wildcards_offer_size, rng, local, slot, adult):
                    local.mark(slot, text)
                    l = owner[normal(text)]
                    result.per_prompt[n].append({"list": l.name, "role": l.role, "text": text, "slot": slot, "required": False})
    return result if any(result.per_prompt) else None


def assigned(offers: IdeaSet | None, n: int = 0) -> dict[str, str]:
    """{slot: entry} the picture must show (the required offers of picture `n`)."""
    return {o["slot"]: o["text"] for o in (offers.options(n) if offers else []) if o.get("required")}


def enforce(scene: dict, required: dict[str, str]) -> list[str]:
    """Make the scene plan show every required entry: a slot that does not contain the entry (whole words, any case) is replaced by it.
    Returns the slots that were rewritten, so the job can say so."""
    fixed = []
    for slot, text in required.items():
        if text and not has_phrase(str(scene.get(slot) or ""), text):
            scene[slot] = text
            fixed.append(slot)
    return fixed


def remember(offers: IdeaSet | None, state: PlanState | None, text_of, n: int = 0):
    """Count in the plan's state every entry of a slot's lists that the picture really has, offered or not: `text_of(slot)` is the text the
    picture says that slot in (the scene plan's slot, or a whole description). A gaze the scene plan wrote in its own words ("looking at
    viewer", for the hundredth time) is counted like an offered one, so the next pictures lean away from it. A required entry was counted when
    it was assigned."""
    if not offers or state is None:
        return
    options = offers.options(n)
    assigned = {(o["slot"], normal(o["text"])) for o in options if o.get("required")}
    slots = dict.fromkeys([*offers.pool, *(o["slot"] for o in options if o.get("slot"))])
    for slot in slots:
        text = text_of(slot)
        if not text:
            continue
        entries = dict.fromkeys([*offers.pool.get(slot, []), *(o["text"] for o in options if o.get("slot") == slot)])
        for entry in entries:
            if (slot, normal(entry)) not in assigned and has_phrase(text, entry):
                state.mark(slot, entry)


def record(offers: IdeaSet | None, scene: dict | None, n: int = 0, state: PlanState | None = None, enforced: list[str] | None = None) -> dict:
    """The job's `scene_options`: what was offered, which offers the scene plan really holds (checked here, whole words), and the slot each
    served. Entries the plan has (offered or not) are counted in the plan's state so later pictures lean away from them."""
    if not offers or not offers.options(n):
        return {}
    options = offers.options(n)
    text = " . ".join(str((scene or {}).get(k) or "") for k in ("subjects", "body", "outfit", "pose", "action", "expression", "camera", "gaze", "setting", "lighting", "mood"))
    used = [o for o in options if has_phrase(text, o["text"])]
    remember(offers, state, lambda slot: str((scene or {}).get(slot) or ""), n)
    return {"offered": options, "used": [o["text"] for o in used], "lists": offers.lists_record(),
            **({"required": {o["slot"]: o["text"] for o in options if o.get("required")}} if any(o.get("required") for o in options) else {}),
            **({"enforced": enforced} if enforced else {})}


def named(ctx: PlanContext, text: str, avoid: list[str] | None = None) -> dict[str, str]:
    """{slot: entry} for each slot of `wildcards_always_slots` whose entry the text says as a whole phrase ("a girl looking away" has the gaze
    entry `looking away`; the longest such entry wins). The picture's own words decide that slot, and the scene plan keeps them as they are
    (`enforce`), whatever near-synonym it would rather write. An entry the plan says to avoid is not named. The entries are the ones each list
    serves (serving.py, from the vectors already stored)."""
    store, c = ctx.wildcards, ctx.config
    if store is None or not c.wildcards_enabled:
        return {}
    lists = [l for l in store.lists() if l.enabled and l.entries]
    sv = serve_now(lists, c, ctx.embedder if usable(ctx) else None, plain_entries)
    out = {}
    for slot in c.wildcards_always_slots:
        hits = [e for s in sv.values() for e in s.slots.get(slot, []) if has_phrase(text, e) and not any(has_phrase(a, e) for a in avoid or [])]
        if hits:
            out[slot] = max(hits, key=len)
    return out
