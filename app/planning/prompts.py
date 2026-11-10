"""Step 3: write the prompts. The LLM writes exactly N (the schema fixes the count); code then makes sure the
quality prefix and every LoRA trigger word are there, rejects near-duplicates (one retry), and merges the negative."""
import logging
import re

from app.fields import make_issue, merge_negative, split_tags, tag_key
from app.library.card_models import LoraCard, WorkflowCard
from app.planning import locks
from app.planning import offers as offering
from app.planning.concepts import load_concepts, rating_max
from app.planning.context import PlanContext, block, derive_rng, derive_seed, job_view
from app.planning.look import look_note
from app.planning.mentions import readable
from app.planning.pick_wildcards import pick_wildcards
from app.planning.content_safety import matches_term
from app.planning.scene import forced_detail, rating_of
from app.planning.semantic import related_lists
from app.planning.tagcheck import CheckResult, describe_subjects, is_group, is_people_tag
from app.planning.taglib import key
from app.planning.tagstep import TagStep, concept_lines, records, tag_offer_record
from app.planning.rank import tokenize
from app.planning.weights import WeightRules, clean_negative, enforce, rule_text, strip_weights, typed_groups, weights_allowed
from app.wildcards.expand import drop_invented_refs
from app.wildcards.ideas import IdeaSet, brief_without_tokens, idea_record, missing_tokens, tokens_of

log = logging.getLogger("planning")
DUPLICATE_SIMILARITY = 0.8      # word overlap (Jaccard) above this counts as a near-duplicate
MAX_STYLE_TAGS = 120
MAX_NOTES_CHARS = 6000
MAX_EXAMPLES = 4        # card examples shown to the writer


# ---------- pure helpers (tested directly) ----------

def prefix_tags(card: WorkflowCard) -> list[str]:
    return [t.strip() for t in split_tags(card.prompt_style.quality_prefix) if t.strip()] if card.prompt_style.kind == "tags" else []


def lora_requirements(lora_cards: dict[str, LoraCard]) -> tuple[list[str], list[str]]:
    """(trigger words every prompt needs, style tags of which every prompt needs one). A trigger word starting with
    "@" on a LoRA that has style tags is a style selector, not a fixed trigger."""
    triggers, styles = [], []
    for card in lora_cards.values():
        for word in card.trigger_words:
            if not (card.style_tags and word.startswith("@")) and word not in triggers:
                triggers.append(word)
        styles.extend(t for t in card.style_tags[:MAX_STYLE_TAGS] if t not in styles)
    return triggers, styles


def has_word(prompt: str, word: str) -> bool:
    """Is the word in the prompt, also when a weight cuts through it ("(monochrome ink wash:1.3) style")?"""
    word = word.lower()
    return word in prompt.lower() or word in strip_weights(prompt).lower()


def insert_words(prompt: str, words: list[str], tags: bool, prefix: list[str]) -> str:
    """Add words to a prompt: after the quality prefix for tag prompts, at the start otherwise."""
    if not words:
        return prompt
    if not tags:
        return ", ".join([*words, prompt])
    parts = [t.strip() for t in split_tags(prompt) if t.strip()]
    keep = len(prefix) if [tag_key(t) for t in parts[:len(prefix)]] == [tag_key(t) for t in prefix] else 0
    return ", ".join([*parts[:keep], *words, *parts[keep:]])


def ensure_prefix(prompt: str, prefix: list[str]) -> str:
    """Tag prompts start with the card's quality tags (in that order); any copy elsewhere in the prompt is removed."""
    parts = [t.strip() for t in split_tags(prompt) if t.strip()]
    if [tag_key(t) for t in parts[:len(prefix)]] == [tag_key(t) for t in prefix]:
        return ", ".join(parts)
    drop = {tag_key(t) for t in prefix}
    return ", ".join([*prefix, *[t for t in parts if tag_key(t) not in drop]])


def clean_prompt(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^(prompt\s*\d*\s*[:.)-]\s*)", "", text, flags=re.I).strip(" ,")


def weight_rules(ctx: PlanContext, wf: dict, card: WorkflowCard, *typed_in: str) -> WeightRules:
    """What weights this workflow may carry (weights.py) and the groups the person typed in `typed_in`."""
    allowed = weights_allowed(card, ctx.library.get_graph(wf["id"]))
    return WeightRules(allowed, typed_groups(*typed_in) if allowed else {})


def finalize_prompts(prompts: list[str], card: WorkflowCard, triggers: list[str], styles: list[str], seed_text: str = "",
                     numbers: list[int] | None = None, rules: WeightRules | None = None) -> tuple[list[str], list[str]]:
    """(prompts, notes): the prompts with prefix, trigger words and one style tag guaranteed, and notes saying what was added.
    `numbers` are the prompts' numbers in the job (1-based) when they are only some of its prompts. With `rules` the weights
    the LLM wrote are made to follow them first (weights.py); without, they are left alone."""
    tags = card.prompt_style.kind == "tags"
    prefix = prefix_tags(card)
    out, added = [], []
    for i, raw in enumerate(prompts):
        n = numbers[i] if numbers else i + 1
        p = clean_prompt(raw)
        if rules:
            p, changed = enforce(p, [*prefix, *triggers, *styles], rules)
            added += [f"prompt {n}: {c}" for c in changed]
        if prefix:
            fixed = ensure_prefix(p, prefix)
            if fixed != p:
                added.append(f"prompt {n}: quality prefix")
            p = fixed
        missing = [t for t in triggers if not has_word(p, t)]
        if styles and not any(has_word(p, s) for s in styles):
            missing.append(styles[(sum(map(ord, seed_text)) + i) % len(styles)])
        if missing:
            added.append(f"prompt {n}: added {', '.join(missing)}")
            p = insert_words(p, missing, tags, prefix)
        out.append(p)
    return out, added


def body_words(prompt: str, shared: list[str]) -> set[str]:
    """The words of a prompt without the shared parts (quality prefix, trigger words, style tags)."""
    text = strip_weights(prompt).lower()        # "(red scarf:1.5)" is the words "red scarf", not "1" and "5" too
    for phrase in shared:
        text = text.replace(phrase.lower(), " ")
    return set(tokenize(text))


def near_duplicates(prompts: list[str], shared: list[str] | None = None, threshold: float = DUPLICATE_SIMILARITY) -> list[tuple[int, int]]:
    """Pairs (i, j), i < j, of prompts that are too alike: the same words apart from the shared ones."""
    bodies = [body_words(p, shared or []) for p in prompts]
    pairs = []
    for i in range(len(prompts)):
        for j in range(i + 1, len(prompts)):
            a, b = bodies[i], bodies[j]
            overlap = len(a & b) / len(a | b) if (a | b) else 1.0
            if overlap >= threshold or prompts[i].strip().lower() == prompts[j].strip().lower():
                pairs.append((i, j))
    return pairs


def duplicate_note(pairs: list[tuple[int, int]]) -> str:
    return "; ".join(f"prompts {i + 1} and {j + 1}" for i, j in pairs[:4])


def restore_tokens(prompts: list[str], tokens: list[str]) -> tuple[list[str], list[str]]:
    """Wildcard tokens from the brief (`__name__`, `{a|b}`) must reach the prompts word for word. A prompt that lost one
    gets it back at its end. Returns (prompts, notes)."""
    out, notes = [], []
    for n, text in enumerate(prompts, 1):
        lost = missing_tokens(text, tokens)
        if lost:
            notes.append(f"prompt {n}: put back {', '.join(lost)}")
            text = ", ".join([text.rstrip(" ,"), *lost])
        out.append(text)
    return out, notes


# ---------- the step ----------

def idea_brief(ctx: PlanContext, job: dict) -> tuple[str, str, dict]:
    """(the brief that lists are matched on, the same plus the roles its wildcard tokens fill, the brief as the LLM sees it).
    Wildcard tokens the user typed are taken out: a typed `__background-location__` fixes the setting and its words must not
    pull more lists in."""
    parts = [job.get("request", ""), job["description"], *job.get("constraints", [])]
    tokens = tokens_of(*parts)
    brief, filled = brief_without_tokens(" ".join(parts), tokens, ctx.wildcards)
    clean = lambda text: re.sub(r"\s+", " ", brief_without_tokens(text, tokens, None)[0]).strip()
    view = {"request": clean(parts[0]), "job": clean(parts[1]), "constraints": [c for c in map(clean, parts[2:]) if c]}
    return brief, f"{brief} {filled}", view


def writes_prompts(job: dict, fixed: dict, kept: dict[int, dict] | None) -> bool:
    """Does write_prompts have prompts to write (they are not locked and not all kept)?"""
    return "prompts" not in fixed and any(i not in (kept or {}) for i in range(job["count"]))


async def choose_wildcards(ctx: PlanContext, job: dict, card: WorkflowCard) -> tuple[list[tuple[str, str]], list]:
    """The planning step "wildcards": the LLM's choice of lists for this job (see pick_wildcards.py)."""
    _, given, view = idea_brief(ctx, job)
    if job.get("rating") and job["rating"] != "general":        # the content level the request asked for (adult-content lists are what a nsfw request is about)
        view = {**view, "content_level": job["rating"]}
    return await pick_wildcards(ctx, card, view, given)


def usable_lists(ctx: PlanContext) -> list:
    """The wildcard lists that can be offered: enabled and not empty (none without a store)."""
    return [l for l in ctx.wildcards.lists() if l.enabled and l.entries] if ctx.wildcards is not None and ctx.config.wildcards_enabled else []


async def draw_ideas(ctx: PlanContext, job: dict, count: int, seed: int, picks: list[tuple[str, str]] | None = None,
                     state: offering.PlanState | None = None, vary: list[str] | None = None, assigned: dict[str, str] | None = None,
                     rating: str | None = None) -> IdeaSet | None:
    """The wildcard ideas for `count` prompts of this job, drawn from `seed` (None: nothing to offer): entries relevant to the job, by scene
    slot (offers.py). `vary` are the slots the request wants different in every image, `assigned` the entries this job was given for them.
    The scene plan and the first prompt of a job draw with the same seed, so the writer's first ideas are the options the scene plan saw.
    `rating` is the picture's content level: at nsfw or explicit, adult entries are added to the offers (offers.build); `job["look"]` (look.Look) is set
    when a LoRA of the job sets the look: no light or colour is offered. `job["wildcard_gaps"]` is set to the slots the scene plan wants (camera, gaze)
    that no list covers, so the job can say the model wrote them."""
    brief, given, view = idea_brief(ctx, job)
    about = " ".join([view["job"], *view["constraints"]])               # the job itself; the request's "2 images of" is noise for meaning
    related = await related_lists(ctx, usable_lists(ctx), about)        # lists close in meaning to the job (None: meaning search is off)
    ideas = await offering.build(ctx, brief, given, picks or [], [] if assigned else (vary or []), state, derive_rng(seed, "offers"), count, related, always=True, rating=rating,
                                 look=job.get("look"), gaps=job.setdefault("wildcard_gaps", []))
    if assigned:
        ideas = ideas or IdeaSet(per_prompt=[[]])
        ideas.per_prompt[0] = [o for o in ideas.per_prompt[0] if o.get("slot") not in assigned]
        ideas.per_prompt[0] += [{"list": "", "role": "", "text": text, "slot": slot, "required": True} for slot, text in assigned.items()]
    if not ideas:
        return None
    group = is_group(job.get("subjects"))              # a pose made for one person does not fit a group
    ideas.per_prompt = [[o for o in each if not matches_term(o["text"]) and not (group and o["role"] == "pose")]    # the same entry check as everywhere (content_safety.matches_term)
                        for each in ideas.per_prompt]
    if group:       # whichever list it came from, a pose for the pose slot
        ideas.per_prompt = [[o for o in each if o.get("slot") != "pose"] for each in ideas.per_prompt]
    return ideas if any(ideas.per_prompt) else None


async def scene_options(ctx: PlanContext, job: dict, picks: list[tuple[str, str]] | None, seed: int, state: offering.PlanState | None = None,
                        assigned: dict[str, str] | None = None, vary: list[str] | None = None) -> IdeaSet | None:
    """Wildcard options for the scene plan: one draw, several entries per slot, relevant to the job. The lists the brief
    already fixes (a given outfit, setting, ...) are never among them, so what the user named is not offered against."""
    rating = job.get("min_rating") or "general"
    if not job.get("rating_read"):      # a rating that was not read from the whole request is raised by its words, as the scene plan does
        rating = rating_max(rating, load_concepts().rating_min(" ".join([job.get("request", ""), job["description"], *job.get("constraints", [])])))
    ideas = await draw_ideas(ctx, job, 1, derive_seed(seed, "ideas:0"), picks, state, vary, assigned, rating)
    if ideas:
        log.info("scene options offered: %s", "; ".join(ideas.options_text().splitlines()))
    return ideas


def example_view(card: WorkflowCard, habits: dict[str, float] | None, look=None, lib=None) -> list[str]:
    """The card's examples as the writer sees them. They show the shape and length of a prompt, and the writer copies their
    tags (`bokeh, cinematic lighting` ended nearly every prompt). So a tag that most recent prompts already have (`habits`)
    is left out of them, except the quality prefix and the count tags. When a LoRA sets the look (`look`), the look tags of the examples are left out too.
    The card itself is not changed."""
    examples = card.examples[:MAX_EXAMPLES]
    if look is not None and card.prompt_style.kind in ("tags", "mixed"):
        examples = [look.example(e, lib) for e in examples]
    if not habits or card.prompt_style.kind not in ("tags", "mixed"):
        return examples
    keep = {key(t) for t in prefix_tags(card)}
    drop = {name for name in habits if name not in keep and not is_people_tag(name)}
    return [", ".join(t.strip() for t in split_tags(example) if key(tag_key(t)) not in drop) for example in examples]


def card_view(card: WorkflowCard, wf: dict, habits: dict[str, float] | None = None, look=None, lib=None) -> dict:
    return {"workflow": wf["title"], "kind": wf["kind"], "family": card.family, "purpose": card.purpose, "best_for": card.best_for,
            "prompt_style": card.prompt_style.model_dump(), "warnings": card.warnings, "examples": example_view(card, habits, look, lib),
            "input_images": card.input_images}


PROMPT_CHUNK = 6        # prompts written in one LLM call; a longer job is written in chunks that each see the earlier ones
SEEN_PROMPTS = 18       # the prompts a chunk is shown as "already written": your locked ones and the newest written


async def write_prompts(ctx: PlanContext, job: dict, wf: dict, card: WorkflowCard, lora_value: list[dict], fixed: dict,
                        kept: dict[int, dict] | None = None, picks: list[tuple[str, str]] | None = None) -> tuple[dict, list[dict]]:
    """(new fields, issues) for `prompts` and `negative`. A job with more than PROMPT_CHUNK prompts to write is written in
    chunks, so a long reply is never cut off and later prompts see the earlier ones: each call gets the prompts so far as
    "Prompts already written" (see write_chunk for the rest). Everything else is as in write_chunk."""
    count = job["count"]
    kept = {i: k for i, k in (kept or {}).items() if i < count}
    free = [i for i in range(count) if i not in kept]
    if "prompts" in fixed or len(free) <= PROMPT_CHUNK:
        return await write_chunk(ctx, job, wf, card, lora_value, fixed, kept, picks)
    written: list[tuple[int, str, dict]] = []     # (job position, text, item) of the prompts written so far
    seen = [(i, k["text"], {"reason": k["reason"], **({"ideas": k["ideas"]} if k.get("ideas") else {})}) for i, k in sorted(kept.items())]
    new, issues, reason, notes = {}, [], "", []
    for start in range(0, len(free), PROMPT_CHUNK):
        chunk = free[start:start + PROMPT_CHUNK]
        context = seen[-SEEN_PROMPTS:]
        # to write_chunk the prompts so far are kept prompts 0..m-1 and the chunk follows them
        pseudo_kept = {n: {"text": text, "reason": item["reason"], **({"ideas": item["ideas"]} if item.get("ideas") else {})} for n, (_, text, item) in enumerate(context)}
        pseudo = {**job, "count": len(context) + len(chunk), "total": count, "chunk": start}
        extra = {"negative": new["negative"]} if "negative" in new else {}
        got, found = await write_chunk(ctx, pseudo, wf, card, lora_value, {**fixed, **extra}, pseudo_kept, picks)
        issues += [i for i in found if i not in issues]      # a warning that every chunk raises is shown once
        field = got.get("prompts")
        if any(i["severity"] == "blocking" for i in found) or not field or not all(v.strip() for v in field["value"][len(context):]):
            break
        if "negative" in got:
            new["negative"] = got["negative"]
        values, items = field["value"][len(context):], field["items"][len(context):]
        written += [(i, text, item) for i, text, item in zip(chunk, values, items)]
        seen += [(i, text, item) for i, text, item in zip(chunk, values, items)]
        renumber = lambda m, chunk=chunk, shift=len(context) + 1: f"prompt {chunk[int(m.group(1)) - shift] + 1}:" if 0 <= int(m.group(1)) - shift < len(chunk) else m.group(0)
        reason, notes = field.get("reason") or reason, [*notes, *([re.sub(r"prompt (\d+):", renumber, field["note"])] if field.get("note") else [])]
    done = {i: (text, item) for i, text, item in written}
    texts = [kept[i]["text"] if i in kept else done.get(i, ("", {}))[0] for i in range(count)]
    items = [{"reason": kept[i]["reason"], **({"ideas": kept[i]["ideas"]} if kept[i].get("ideas") else {})} if i in kept else done.get(i, ("", {"reason": ""}))[1] for i in range(count)]
    new["prompts"] = locks.auto(texts, reason, "; ".join(notes), items, [i in kept for i in range(count)] if kept else None)
    return new, issues


async def write_chunk(ctx: PlanContext, job: dict, wf: dict, card: WorkflowCard, lora_value: list[dict], fixed: dict,
                      kept: dict[int, dict] | None = None, picks: list[tuple[str, str]] | None = None) -> tuple[dict, list[dict]]:
    """(new fields, issues) for `prompts` and `negative`. Locked fields are skipped; if both are locked there is no call.
    `kept` are prompts locked one by one ({index: {text, reason}}): they stay word for word, count towards N, and only the
    other prompts are written, each clearly different from the kept ones. `picks` are the lists the LLM chose for the ideas."""
    has_negative_slot = bool(wf["slots"].get("negative"))
    count = job["count"]
    kept = {i: k for i, k in (kept or {}).items() if i < count}
    free = [i for i in range(count) if i not in kept]
    want_prompts = "prompts" not in fixed and bool(free)
    want_negative = "negative" not in fixed and has_negative_slot
    if not (want_prompts or want_negative):
        return {}, []
    lora_cards = {item["name"]: c for item in lora_value if (c := ctx.lora_card(item["name"]))}
    triggers, styles = lora_requirements(lora_cards)
    rules = weight_rules(ctx, wf, card, job["description"], *job.get("constraints", []))
    # wildcard tokens the user wrote; one the LLM made up in an earlier step (a job planned before this check) is not among them
    catalog = ctx.wildcards.catalog() if ctx.wildcards is not None else None
    typed = job.get("request", "")
    tokens = [t for t in tokens_of(job["description"], *job.get("constraints", [])) if drop_invented_refs(t, typed, catalog) == t]
    subjects = job.get("subjects")
    scene = job.get("scene") or {}
    book = load_concepts()
    about_text = " ".join([typed, job["description"], *job.get("constraints", [])])
    rating = rating_max(rating_of(scene) if scene else book.rating_min(about_text), job.get("min_rating") or "general")     # never below what the request asked for
    detail = scene.get("detail_level") if scene else forced_detail(about_text)
    lora_names = [item["name"] for item in lora_value]
    exclude = [*triggers, *styles, *lora_names, *(readable(n) for n in lora_names)]
    ignore = book.vague_words() | {word for phrase in exclude for word in tokenize(phrase)}
    lora_phrases = [c.what_it_does for c in lora_cards.values() if c.what_it_does]
    user_text = " ".join([typed, *job.get("constraints", [])])
    # what the check demands back: the job's own words. An image of a big request has a description the LLM wrote, whose words are not the user's:
    # there only the constraints count (the scene plan's must_keep already holds the concrete words of the description).
    keep_text = " ".join([] if job.get("batch") else [job["description"], *job.get("constraints", [])]) or " ".join(job.get("constraints", []))
    look = job.get("look")      # look.Look when a LoRA of this job sets the picture's look
    tagstep = TagStep.build(ctx, card, subjects, [t.strip() for t in split_tags(card.prompt_style.quality_prefix) if t.strip()] + triggers + styles,
                            user_text, rating=rating, detail_level=detail or "normal", must_keep=scene.get("must_keep"),
                            ignore_words=ignore, lora_phrases=lora_phrases, keep_text=keep_text, scene=scene, habits=ctx.habit_tags(), lora_files=lora_names, look=look)
    seed = job["seed"] if job.get("seed") is not None else ctx.idea_seed()      # plan_job gives one seed per job; its draws all derive from it
    chunk = job.get("chunk", 0)                                                # a later chunk of a long job draws other ideas and tags
    ideas, hints, tag_offers = None, [], {}
    if want_prompts:    # random ingredients from the user's wildcard lists, a different set for each prompt
        brief, given, view = idea_brief(ctx, job)
        about = " ".join([view["job"], *view["constraints"]])
        ideas = await draw_ideas(ctx, job, len(free), derive_seed(seed, f"ideas:{chunk}"), picks, rating=rating)
        if ideas and chunk == 0 and job.get("scene_offers") and job["scene_offers"].options(0):       # the first prompt gets what the scene plan was offered
            ideas.per_prompt[0] = job["scene_offers"].options(0)
        if tagstep:
            if scene:
                tagstep.check_context.view_tags = await tagstep.view_tags(tagstep.check_context.slots)      # a planned camera or gaze that is no tag still reaches the prompt
                tag_offers = await tagstep.slot_suggestions({key: scene.get(key, "") for key in
                                                             ("subjects", "body", "outfit", "pose", "action", "expression", "camera", "gaze", "setting", "lighting", "mood")},
                                                            exclude, derive_rng(seed, f"tags:{chunk}"), ctx.recent_tags())
                hints = tagstep.offer(tag_offers)
            if not hints:
                hints = await tagstep.suggestions(about)
    drop = [*([] if want_prompts else ["prompts"]), *([] if want_negative else ["negative_additions"])]
    lora_info = [{"file": n, "weight": next((i["weight"] for i in lora_value if i["name"] == n), None), "what_it_does": c.what_it_does,
                  "trigger_words": [t for t in c.trigger_words if t in triggers], "style_tags": c.style_tags[:MAX_STYLE_TAGS],
                  "usage_notes": c.usage_notes}
                 for n, c in lora_cards.items()]
    people = describe_subjects(subjects)
    groups = "\n".join(f"{slot}: " + ", ".join(tags) for slot, tags in tag_offers.items())
    lib = ctx.taglib()
    concepts = await concept_lines(ctx, about_text, rating, derive_rng(seed, "concepts"), (lambda t: look.is_look(t, lib)) if look else None) if tagstep else []
    inputs = {"job": {**job_view(job), **({"people": people} if people else {})}, "count": len(free) if want_prompts else "(the prompts are fixed; write only the negative additions)",
              "card": block("workflow card", card_view(card, wf, tagstep.check_context.habits if tagstep else None, look, lib)), "loras": block("LoRA cards", lora_info) if lora_info else "(none)",
              "look": look_note(look),
              "scene": block("scene plan", scene) if scene else "(none)",
              "concepts": block("real tags for the request's words", "\n".join(concepts)) if concepts else "(none)",
              "ideas": block("wildcard ideas", ideas.block_text()) if ideas else "(none)",
              "keep_tokens": ", ".join(tokens) if tokens else "(none)", "weights": rule_text(rules.allowed),
              "tags": block("real tags that match this job", groups or ", ".join(hints)) if (groups or hints) else "(none)",
              "locked": locks.locked_values(fixed), "retry": "(none)",
              "kept": block("prompts already written", [{"number": i + 1, "prompt": k["text"]} for i, k in sorted(kept.items())]) if kept and want_prompts else "(none)",
              "liked": await ctx.liked_for(wf["id"], f"{job['description']} {' '.join(job.get('constraints', []))}") or "(none yet)",
              "notes": block("workflow notes", card.notes_full[:MAX_NOTES_CHARS]) if card.use_full_notes and card.notes_full else "(not needed)"}
    enums, limits = {}, ({"prompts": {"minItems": len(free), "maxItems": len(free)}} if want_prompts else {})
    issues = []

    def whole(written: list[str]) -> list[str]:
        """All N prompts: the kept ones where they are, the newly written ones in the gaps."""
        it = iter(written)
        return [kept[i]["text"] if i in kept else next(it) for i in range(count)]

    def open_pairs(texts: list[str], shared: list[str]) -> list[tuple[int, int]]:
        """Near-duplicate pairs that involve at least one newly written prompt (two kept prompts are your business)."""
        return [(i, j) for i, j in near_duplicates(texts, shared) if i in free or j in free] if count > 1 else []

    async def ask() -> dict:
        reply = await ctx.llm.call("write_prompts", inputs, enums, limits, drop)
        if not reply.get("needs_attention") and want_prompts and len(reply.get("prompts") or []) != len(free):
            return {"needs_attention": True, "error": f"expected {len(free)} prompts, got {len(reply.get('prompts') or [])}"}
        return reply

    def polish(raw: list[str], numbers: list[int]) -> tuple[list[str], list[str], list[CheckResult]]:
        """Prompts the LLM wrote, made right: the quality prefix, trigger words, a style tag and the user's wildcard tokens guaranteed,
        weights following the rules, and (tag and mixed workflows with a tag library) the tag check. Plus notes and the check's results."""
        keep = " ".join([typed, *tokens])
        texts = [drop_invented_refs(p, keep, catalog) for p in raw]
        written, added = finalize_prompts(texts, card, triggers, styles, job["description"], numbers, rules)
        written, restored = restore_tokens(written, tokens)
        checks = [tagstep.check(t) for t in written] if tagstep else []
        return [c.text for c in checks] if checks else written, [*added, *restored], checks

    def finish(reply: dict) -> tuple[list[str], list[str], list[CheckResult]]:
        return polish([p["prompt"] for p in reply["prompts"]], [i + 1 for i in free])

    async def repair(texts: list[str], checks: dict[int, CheckResult], notes: list[str]) -> list[str]:
        """The prompts whose check still found a problem go to the LLM once (repair_tags.md); the answer goes through the same
        steps and the check again. A repair that does not leave fewer problems is dropped."""
        bad = [(i, checks[i]) for i in free if i in checks and checks[i].problems]
        if not bad:
            return texts
        fixed = await tagstep.repair([(i + 1, texts[i], c.problems) for i, c in bad], hints)
        if fixed:
            written, more, results = polish(fixed, [i + 1 for i, _ in bad])
            for (i, old), text, res in zip(bad, written, results):
                if len(res.problems) < len(old.problems):
                    texts[i] = text
                    checks[i] = CheckResult(text, [*old.changes, {"from": None, "to": None, "why": "repaired by the LLM: " + "; ".join(old.problems)}, *res.changes], res.problems)
                    notes.extend(m for m in more if m.startswith(f"prompt {i + 1}"))
        for i, c in bad:
            if checks[i].problems:
                notes.append(f"prompt {i + 1}: tag check could not settle: {'; '.join(checks[i].problems)}")
        return texts

    offered = dict(zip(free, ideas.per_prompt)) if ideas else {}
    tagchecks: dict[int, CheckResult] = {}         # job position -> what the tag check did to that prompt

    def prompts_field(texts: list[str], reply: dict, note: str = "") -> dict:
        variations = iter(p["variation"] for p in reply.get("prompts") or [])
        items = []
        for i in range(count):
            item = {"reason": kept[i]["reason"]} if i in kept else {"reason": next(variations, "")}
            if i in tagchecks and records(tagchecks[i]):
                item["tagcheck"] = records(tagchecks[i])
            if tag_offers and i not in kept:        # which real tags were offered for this job's scene and which this prompt took
                item["tag_offers"] = tag_offer_record(tag_offers, texts[i])
            if i in kept and kept[i].get("ideas"):
                item["ideas"] = kept[i]["ideas"]
            elif offered.get(i):
                item["ideas"] = idea_record(offered[i], texts[i], ideas.lists_record())
            items.append(item)
        return locks.auto(texts, reply.get("reason", ""), note, items, [i in kept for i in range(count)] if kept else None)

    if want_prompts and not rules.allowed and (mine := typed_groups(job.get("request", ""), job["description"], *job.get("constraints", []))):
        issues.append(make_issue("prompts", f"You typed {'a weight' if len(mine) == 1 else 'weights'}, {', '.join(mine.values())}: this model reads weights as text, "
                                            "so they were taken out of the prompts.", "warning", "weights_removed"))
    reply = await ask()
    new: dict = {}
    if reply.get("needs_attention"):
        log.warning("write_prompts needs attention: %s", reply["error"])
        issues.append(make_issue("prompts" if want_prompts else "negative", f"The LLM could not write the prompts: {reply['error']}", "blocking" if want_prompts else "warning", "llm_failed"))
        reply = {}
        if want_prompts and kept:       # your locked prompts must not be lost with the failed ones
            new["prompts"] = prompts_field(whole([""] * len(free)), reply)
    elif want_prompts:
        written, added, checks = finish(reply)
        tagchecks.update(zip(free, checks))
        shared = [*prefix_tags(card), *triggers, *styles]
        texts = whole(written)
        pairs = open_pairs(texts, shared)
        if pairs:     # one retry, telling the model which prompts were too alike
            rewrite = (f"Rewrite the {len(free)} prompts you were asked for so each one is clearly different from the others and from the locked prompts, which stay as they are,"
                       if kept else f"Rewrite all {count} prompts so each one is clearly different")
            inputs["retry"] = (f"{duplicate_note(pairs)} were nearly identical. {rewrite} "
                               "in composition, pose, framing, lighting or details. Previous attempt:\n" + "\n".join(f"{n}. {t[:300]}" for n, t in enumerate(texts, 1)))
            again = await ask()
            if not again.get("needs_attention"):
                written2, added2, checks2 = finish(again)
                texts2 = whole(written2)
                pairs2 = open_pairs(texts2, shared)
                if len(pairs2) <= len(pairs):
                    reply, texts, added, pairs = again, texts2, added2, pairs2
                    tagchecks.update(zip(free, checks2))
            if pairs:
                issues.append(make_issue("prompts", f"These prompts are very similar even after a retry: {duplicate_note(pairs)}.", "warning", "similar_prompts"))
        if tagstep and any(c.problems for c in tagchecks.values()):
            texts = await repair(texts, tagchecks, added)
        new["prompts"] = prompts_field(texts, reply, "; ".join(added))
    if want_negative:    # the workflow's own negative always applies, even when the LLM added nothing
        avoided = [a for a in scene.get("avoid") or [] if a.strip()]        # what the user wants absent from the picture (plan_scene)
        additions = clean_negative([a for a in [*(reply.get("negative_additions") or []), *avoided] if a.strip()], rules)
        default = (wf["slots"].get("defaults") or {}).get("negative") or ""
        new["negative"] = locks.auto(merge_negative(default, [*additions, *ctx.config.safety_negative]),
                                     ("The workflow's own negative plus: " + ", ".join(additions)) if additions else "The workflow's own negative prompt.")
    return new, issues
