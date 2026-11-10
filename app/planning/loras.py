"""Step 2: pick LoRAs. Code filters by workflow family and ranks with BM25, the LLM picks from the top 15,
code clamps the weights to each LoRA card's range and drops duplicates."""
import logging
import re

from app.fields import make_issue
from app.library.card_models import LoraCard, WorkflowCard
from app.planning import locks
from app.planning.clamp import clamp_lora_weight
from app.planning.context import PlanContext, block, job_view
from app.planning.mentions import GENERIC_NAME_WORDS, readable, stem, unnamed
from app.planning.rank import Bm25
from app.planning.semantic import lora_similarities, rrf

log = logging.getLogger("planning")
SHORTLIST = 15
EXTRA_SHARE = 0.4           # when the user lets the planner add LoRAs, none may already be on this share of the earlier images (it must stay a choice per image)
SPREAD_FROM = 3             # ... counted once this many images were planned


def lora_text(name: str, card: LoraCard, user_tags: list[str] | tuple = ()) -> str:
    """The words a LoRA is ranked on: file name, description, kind, trigger words, the user's own tags, notes, Civitai name and
    top training tags. The user's tags are only words to match, never shown to the LLM as instructions."""
    civitai = f"{card.civitai.name} {card.civitai.version_name}" if card.civitai else ""
    return " ".join([name, card.what_it_does, card.kind, *card.trigger_words, *user_tags, *card.usage_notes, civitai, *card.tags[:8]])


async def candidates_for(ctx: PlanContext, family: str, description: str, constraints: list[str],
                         allow=None) -> list[tuple[tuple[str, LoraCard], float]]:
    """[((file, card), score)]: LoRA cards of the workflow's family, best match first, at most SHORTLIST. A score above zero means
    the brief points at the card: a keyword (BM25) match, or - when search by meaning is on - a card close enough in meaning.
    With search by meaning the order is the fusion of both rankings (reciprocal rank fusion); without it, BM25 alone.
    `allow(file, card)` drops cards the request rules out (intent.py)."""
    compatible = [(n, c) for n, c in ctx.lora_choices() if c.family == family and (allow is None or allow(n, c))]
    if not compatible:
        return []
    query = " ".join([description, *constraints])
    user_tags = ctx.lora_tags()
    texts = [lora_text(n, c, user_tags.get(n, ())) for n, c in compatible]
    keywords = Bm25(texts).scores(query)
    by_keyword = sorted(range(len(compatible)), key=lambda i: (-keywords[i], i))
    meaning = await lora_similarities(ctx, texts, query)
    if meaning is None:
        return [(compatible[i], keywords[i]) for i in by_keyword[:SHORTLIST]]
    by_meaning = sorted(range(len(compatible)), key=lambda i: (-meaning[i], i))
    floor = ctx.config.embeddings_lora_floor
    fused = rrf(by_meaning, [i for i in by_keyword if keywords[i] > 0])
    return [(compatible[i], max(keywords[i], 1.0 if meaning[i] >= floor else 0.0)) for i in fused[:SHORTLIST]]


def not_overused(scored: list, uses: dict, images: int) -> list:
    """The candidates without the LoRAs the planner already added to a share (EXTRA_SHARE) of the `images` planned so far: a LoRA the user lets
    the planner add must stay a choice made per image, not a habit. Nothing is judged before SPREAD_FROM images exist."""
    if images < SPREAD_FROM:
        return scored
    return [(pair, score) for pair, score in scored if uses.get(pair[0], 0) / images < EXTRA_SHARE]


def names_this_lora(name: str, card: LoraCard, user_tags, brief: str) -> bool:
    """Does the brief itself name this LoRA: a distinctive word of its file name, a trigger word, a style tag or one of the user's tags?"""
    text = brief.lower()
    words = set(re.findall(r"[a-z][a-z0-9]{3,}", name.rsplit(".", 1)[0].lower())) - GENERIC_NAME_WORDS
    phrases = [*card.trigger_words, *card.style_tags, *user_tags]
    said = set(re.findall(r"[a-z][a-z0-9]{4,}", text))         # five letters or more, so "brush" finds "darkbrush"
    return any(w in text.split() or any(s in w for s in said) for w in words) or any(p.lower() in text for p in phrases if p)


def asked_for_lora(description: str, constraints: list[str]) -> bool:
    """True if the job itself (its description or constraints, not the rest of the request) mentions a LoRA."""
    return bool(re.search(r"(?<![a-z])lora", " ".join([description, *constraints]), re.I))


def describe(name: str, card: LoraCard) -> dict:
    """One candidate as the LLM sees it. Training tags are left out: they are statistics, not guidance."""
    w = card.weight
    return {"file": name, "kind": card.kind, "what_it_does": card.what_it_does, "trigger_words": card.trigger_words[:8],
            "style_tags_available": len(card.style_tags), "usage_notes": card.usage_notes[:3],
            "weight": {"recommended": w.rec, "min": w.min, "max": w.max}}


def build_field(reply: dict, cards: dict[str, LoraCard], max_loras: int, relevant: set[str] | None = None, extra_notes: list[str] | None = None) -> dict:
    """The `loras` field from the LLM's reply: only known files, no duplicates, weights clamped, at most max_loras.
    With `relevant` set, only those files may be used (see pick_loras). `extra_notes` are added to the field's note."""
    picked, seen, notes = [], set(), list(extra_notes or [])
    for item in reply.get("loras", []):
        name = item["lora"]
        if name not in cards or name in seen or len(picked) >= max_loras:
            continue
        seen.add(name)
        if relevant is not None and name not in relevant:
            notes.append(f"{name}: not used, nothing in the brief points at it")
            continue
        weight, note = clamp_lora_weight(item["weight"], cards[name].weight)
        picked.append({"name": name, "weight": weight, "reason": item.get("reason", "")})
        if note:
            notes.append(f"{name}: {note}")
    reason = reply.get("reason", "") or ("; ".join(p["reason"] for p in picked if p["reason"]))
    if not picked and notes and not extra_notes:
        reason = "No LoRA used: the brief doesn't ask for one."
    return locks.auto(picked, reason, "; ".join(notes))


def named_loras(ctx: PlanContext, family: str, named: list[str], limit: int) -> tuple[list[tuple[str, LoraCard]], list[dict]]:
    """(the LoRAs the user named that this job can take, an issue for each that it can't). A named LoRA must be one the planner
    may use at all (an approved card, or a draft one when Settings allow drafts, and a file ComfyUI lists), of this workflow's
    family, and within the LoRAs-per-job limit (the first ones written win)."""
    usable = dict(ctx.lora_choices())
    chosen, issues = [], []
    for key in named:
        if key in usable and usable[key].family == family and len(chosen) < limit:
            chosen.append((key, usable[key]))
            continue
        if key in usable and usable[key].family != family:
            why = f"it is a {usable[key].family} LoRA and this workflow takes {family} ones"
        elif key in usable:
            why = f"a job can have {limit} {'LoRA' if limit == 1 else 'LoRAs'} at most (Settings)"
        elif ctx.lora_card(key) is None:
            why = "it has no card yet"
        elif ctx.lora_files and key not in ctx.lora_files:
            why = "ComfyUI does not list this file"
        else:
            why = "its card is not approved yet"
        issues.append(make_issue("loras", f"You named the LoRA {stem(key)}, but it was not added: {why}.", "warning", "lora_mention"))
    return chosen, issues


def named_items(chosen: list[tuple[str, LoraCard]], reasons: dict[str, str] | None = None) -> list[dict]:
    """The named LoRAs as items of the `loras` field, each at its card's recommended weight (within the card's range). `reasons`
    says why a LoRA is on the job when it is not simply "You named this LoRA" (one of a group the request asked for)."""
    return [{"name": name, "weight": clamp_lora_weight(1.0 if c.weight.rec is None else c.weight.rec, c.weight)[0],
             "reason": (reasons or {}).get(name, "You named this LoRA"), "named": True} for name, c in chosen]


def with_named(field: dict, chosen: list[tuple[str, LoraCard]], reasons: dict[str, str] | None = None) -> dict:
    """The `loras` field with the named LoRAs first."""
    items = named_items(chosen, reasons)
    why = [(reasons or {}).get(n) or f"You named {readable(n)}" for n, _ in chosen]
    reason = ("; ".join(why) if reasons else f"You named {', '.join(readable(n) for n, _ in chosen)}") + (f"; {field['reason']}" if field["value"] and field["reason"] else "")
    return locks.auto(items + field["value"], reason, field.get("note", ""))


def excluded_by_intent(intent: dict, group_kind_ok: bool = False):
    """The filter for LoRAs the planner may add on its own: not the ones the request ruled out, not a kind it ruled out, and - when the
    request is "N images per <kind> LoRA" - not a second LoRA of that kind unless the request lets extras come from it."""
    skip, kinds = set(intent.get("skip") or []), set(intent.get("skip_kinds") or [])
    group = (intent.get("group") or {}).get("kind")
    extra_kinds = set(intent.get("extra_kinds") or [])

    def allow(name: str, card: LoraCard) -> bool:
        if name in skip or card.kind in kinds:
            return False
        if extra_kinds and card.kind not in extra_kinds:
            return False
        return not (group and card.kind == group and group not in extra_kinds)
    return allow


def left_out_notes(ctx: PlanContext, family: str, allow, shown: set[str], reply: dict) -> list[str]:
    """Why LoRAs that fit the request's freedom were not used: the cards that cannot be offered at all (not approved, not listed), and the
    candidates the picker looked at and passed over, with the picker's own reason."""
    notes = []
    usable = {n for n, _ in ctx.lora_choices()}
    for row in ctx.cards.of_kind("lora"):
        card = LoraCard.model_validate(row["card"])
        if card.family == family and allow(row["key"], card) and row["key"] not in usable:
            notes.append(f"{stem(row['key'])}: not offered, " + ("its card is not approved yet" if not ctx.lora_files or row["key"] in ctx.lora_files else "ComfyUI does not list the file"))
    for item in reply.get("left_out") or []:
        if item.get("lora") in shown:
            notes.append(f"{stem(item['lora'])}: not used" + (f", {item['reason'].strip().rstrip('.')}" if item.get("reason", "").strip() else ""))
    return notes


async def pick_loras(ctx: PlanContext, job: dict, wf: dict, card: WorkflowCard, fixed: dict) -> tuple[dict, list[dict]]:
    """(new fields, issues). A locked `loras` field is left alone and the LLM isn't asked. LoRAs the request fixes (job["named"]: `@` mentions the
    planner said to use, names it resolved, and the LoRA of an "N images per <kind> LoRA" group) are on the job whatever the LLM picks; they use
    up LoRA places, so the LLM may add only what is left, and only what the request's intent (job["intent"]) allows: nothing when the user wants
    no other LoRAs, whatever fits when they said so ("as_fit": the relevance gate is off and the picker says why it passed over a candidate)."""
    if "loras" in fixed:
        return {}, []
    named = job.get("named") or []
    intent = job.get("intent") or {}
    group = intent.get("group") or {}
    if not wf["slots"].get("model"):
        return {"loras": locks.auto([], "This workflow can't take LoRAs.")}, [
            make_issue("loras", f"You named the LoRA {stem(k)}, but it was not added: this workflow can't take LoRAs.", "warning", "lora_mention") for k in named]
    max_loras = ctx.config.max_loras
    chosen, issues = named_loras(ctx, card.family, named, max_loras)
    reasons = {group["member"]: f"One of the {group['kind']} LoRAs you asked for ({group['n']} of {group['of']})"} if group.get("member") else None
    wrap = lambda f: with_named(f, chosen, reasons) if chosen else f
    extras = intent.get("extras") or "default"
    if extras == "default" and group.get("member"):     # "N images per <kind> LoRA" compares that LoRA: more LoRAs go on its images only when the request says so
        extras = "none"
    if extras == "none":
        return {"loras": wrap(locks.auto([], "" if chosen else "You asked for no LoRAs." if not named else ""))}, issues
    room = max_loras - len(chosen)
    allow = excluded_by_intent(intent)
    scored = await candidates_for(ctx, card.family, job["description"], job.get("constraints", []), allow) if room > 0 else []
    taken = {n for n, _ in chosen}
    scored = [(pair, score) for pair, score in scored if pair[0] not in taken]
    free = extras == "as_fit"
    if free:
        scored = not_overused(scored, job.get("lora_uses") or {}, job.get("images_so_far", 0))
    ranked = [pair for pair, _ in scored]
    if not ranked:
        none = locks.auto([], "" if chosen else f"No LoRA card for the {card.family} family is available.")
        return {"loras": wrap(none)}, issues
    cards = dict(ranked)
    # A job of an "N images per <kind> LoRA" request talks about LoRAs in its constraints ("every style lora except X"): that is the group, not a wish
    # for more LoRAs, which only the intent (as_fit) expresses. Any other job asks for one when its own words mention a LoRA.
    asked = free or (not group.get("member") and asked_for_lora(unnamed(job["description"], named), [unnamed(c, named) for c in job.get("constraints", [])]))
    on_job = [{"file": i["name"], "weight": i["weight"]} for i in named_items(chosen)]
    inputs = {"job": {**job_view(job), "user_asked_for_a_lora": asked, **({"user_lets_you_add_loras_that_fit": True} if free else {})},
              "locked": {**locks.locked_values(fixed), **({"loras_already_on_the_job": on_job} if on_job else {}),
                         **({"added_to_other_images_of_this_request": {stem(k): n for k, n in job["lora_uses"].items()}, "other_images_so_far": job.get("images_so_far", 0)}
                            if free and job.get("lora_uses") else {})},
              "candidates": block("LoRA cards", [describe(n, c) for n, c in ranked]), "max_loras": min(room, len(ranked))}
    reply = await ctx.llm.call("pick_loras", inputs, {"lora": list(cards)}, {"loras": {"maxItems": min(room, len(ranked))}}, None if free else ["left_out"])
    if reply.get("needs_attention"):
        log.warning("pick_loras needs attention: %s", reply["error"])
        failed = locks.auto([], "The LLM didn't answer, so no LoRA was chosen.")
        return ({"loras": wrap(failed)},
                [*issues, make_issue("loras", f"LoRA choice failed: {reply['error']}", "warning", "llm_failed")])
    # Models like to add a LoRA "because it is for this model family". Unless the job asks for one, only a LoRA that shares
    # a keyword with the brief (or, with search by meaning, is close to it) may be used; a score above zero means exactly that.
    # A style LoRA changes the look of the whole image, so unasked it needs more: the brief must name it.
    # When the user let the planner add LoRAs as it sees fit, the picker decides among the LoRAs that relate to the brief at all.
    brief = " ".join([job["description"], *job.get("constraints", [])])
    user_tags = ctx.lora_tags()
    if free:        # the user lets the planner add LoRAs, but one still has to relate to this brief (keywords or meaning) before the picker may choose it
        relevant = {name for (name, c), score in scored if score > 0}
    else:
        relevant = None if asked else {name for (name, c), score in scored if score > 0
                                       and (c.kind != "style" or names_this_lora(name, c, user_tags.get(name, ()), brief))}
    notes = left_out_notes(ctx, card.family, allow, set(cards), reply) if free else []
    field = build_field(reply, cards, room, relevant, notes)
    if free and not field["value"] and not field.get("reason"):
        field["reason"] = "No other LoRA fit this picture."
    return {"loras": wrap(field)}, issues
