"""Big requests. A job that asks for many different images ("100 unique cyberpunk scenes") or for more than
max_images_per_job is not planned as one job. The LLM first writes one short description (a "brief") per image, in rounds
of about ten, and each brief then becomes its own one-image job that goes through the whole normal pipeline.

The same path serves a request that wants something different in every image (intent.vary: "a different angle each time") and one that wants
images for each LoRA of a kind: the briefs of the whole set are written together, so none repeats another, and brief n belongs to one LoRA."""
import logging
import math
import random

from app.fields import make_issue
from app.library.card_models import WorkflowCard
from app.planning import offers as offering
from app.planning.content_level import shows_adult, wants_adult
from app.planning.context import PlanContext, block
from app.planning.look import look_for, look_note
from app.planning.mentions import plain
from app.planning.pick_wildcards import picking_lists
from app.planning.prompts import choose_wildcards, idea_brief, usable_lists
from app.planning.rank import tokenize
from app.planning.semantic import related_lists
from app.planning.tagcheck import open_subjects
from app.wildcards.expand import drop_invented_refs
from app.wildcards.ideas import tokens_of

log = logging.getLogger("planning")
BRIEFS_PER_ROUND = 10       # descriptions asked for in one LLM call
SEEN_LIMIT = 40             # the newest descriptions a round is shown (all of them are still checked in code)
SAME_IDEA = 0.7             # word overlap (Jaccard) from which two descriptions count as the same idea
OPEN_PICTURE = "(the request names no particular picture: invent a concrete one that fits the request, a subject doing something somewhere)"
EXTRA_ROUNDS = 3            # rounds allowed beyond the minimum, to replace descriptions that were dropped as repeats
NO_FEEDBACK = "(none)"
TOO_TAME = "These descriptions do not show the content level {rating} (nothing in them is nudity or sex; {rating} pictures show it plainly): {texts}. Write {n} new descriptions that do, using the wildcard ideas offered; they must also differ from these."


def expands(item: dict, config) -> bool:
    """Is this planned job written as one description per image? Many different images, more than one job may hold, or a request that wants
    something different in every image (intent.vary: each image needs its own scene to get its own camera angle, pose ...)."""
    wants_variety = bool((item["meta"].get("intent") or {}).get("vary"))
    return item["count"] > 1 and (item["variety"] == "unique" or item["count"] > config.max_images_per_job or wants_variety)


def overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if (a | b) else 1.0


def fresh_briefs(candidates: list[str], seen: list[str], limit: int) -> list[str]:
    """The candidates that are not a repeat of `seen` or of an earlier candidate (word overlap of SAME_IDEA or more),
    at most `limit` of them, in order."""
    words = [set(tokenize(s)) for s in seen]
    kept = []
    for text in candidates:
        text = " ".join(text.split())
        w = set(tokenize(text))
        if len(kept) >= limit:
            break
        if not text or any(overlap(w, other) >= SAME_IDEA for other in words):
            continue
        kept.append(text)
        words.append(w)
    return kept


async def write_briefs(ctx: PlanContext, item: dict, card: WorkflowCard, progress=None, stop=None, state: offering.PlanState | None = None,
                       parts: int = 1, loras: list[str] | None = None) -> tuple[list[str], list[dict], list[dict | None], list[dict], list]:
    """(briefs, issues, subjects, assigned, picks): up to item["count"] distinct descriptions for a planned job. Each round sees the job, its
    constraints, the content level the request asked for, wildcard ingredients relevant to the job (offers.py: entries the request wants
    different in every image are assigned one per description and never repeat in the plan) and the newest descriptions already written.
    If the LLM runs out of ideas the rounds stop (a bounded number) and the issues say how many were made. `progress(done)` is called after
    every round; `stop()` returning True ends the work (a cancelled plan) with what exists. `subjects` has, for each brief, who is in it
    ({girls, boys, others, no_humans}, None for an open number) or None. `assigned` has, for each brief, {slot: entry} it must show;
    `picks` are the wildcard lists chosen once for the whole set (a job reuses them instead of asking again). `parts` > 1 says the descriptions
    are for several parts of one set (one per LoRA, say): the LLM is told. `loras` are the LoRAs of the set known so far (the group's members; the
    ones the request names are in the item's meta): when one of them sets the look (look.py) no light or colour is offered or written.
    Raises PlanError when nothing at all was written."""
    from app.planning.planner import PlanError      # planner imports this module
    count = item["count"]
    intent = item["meta"].get("intent") or {}
    rating, vary = intent.get("rating") or "general", list(intent.get("vary") or [])
    look = look_for(ctx, list(dict.fromkeys([*(item["meta"].get("loras_named") or []), *(loras or [])])), vary)
    job = {"description": item["description"], "constraints": item["constraints"], "request": plain(item["meta"].get("request", ""), ctx.lora_keys()), "count": 1, "rating": rating}
    brief_text, given, view = idea_brief(ctx, job)
    about = " ".join([view["job"], *view["constraints"]])
    related = await related_lists(ctx, usable_lists(ctx), about)
    picks = (await choose_wildcards(ctx, job, card))[0] if picking_lists(ctx) else []
    open_picture = bool(item["meta"].get("picture_open"))        # the request names no picture: every description invents its own
    told = {"description": OPEN_PICTURE if open_picture else item["description"], "constraints": item["constraints"], "request": job["request"],
            **({"images_in_the_whole_set": count, "parts_of_the_set": parts} if parts > 1 else {})}
    briefs: list[str] = []
    people: dict[str, dict | None] = {}        # brief text -> its subjects
    plan_of: dict[str, dict] = {}              # brief text -> {slot: entry} it must show
    rounds, last_error = 0, ""

    async def ask_round(ask: int, feedback: str = NO_FEEDBACK):
        """One write_briefs call for `ask` descriptions: (the ideas offered, the reply). Every description's subjects and required wildcard entries are noted."""
        ideas = await offering.build(ctx, brief_text, given, picks, vary, state, random.Random(ctx.idea_seed()), ask, related, always=True, rating=rating, look=look)
        inputs = {"job": block("job", told), "variety": item["variety"], "count": ask, "rating": rating, "look": look_note(look),
                  "seen": block("descriptions already written", briefs[-SEEN_LIMIT:]) if briefs else "(none yet)",
                  "ideas": block("wildcard ideas", ideas.block_text("Description")) if ideas else "(none)", "feedback": feedback}
        reply = await ctx.llm.call("write_briefs", inputs, None, {"briefs": {"minItems": ask, "maxItems": ask}, "subjects": {"minItems": ask, "maxItems": ask}})
        return ideas, reply

    catalog = ctx.wildcards.catalog() if ctx.wildcards is not None else None
    typed = " ".join([item["meta"].get("request", ""), item["description"], *item["constraints"]])

    def take(ideas, reply) -> list[str]:
        """The reply's descriptions, cleaned; who is in each and the wildcard entries each must show are noted."""
        cleaned = [drop_invented_refs(b, typed, catalog) for b in reply["briefs"]]
        for text, who in zip(cleaned, reply.get("subjects") or []):
            people[" ".join(text.split())] = open_subjects(who)
        for n, text in enumerate(cleaned):
            plan_of[" ".join(text.split())] = offering.assigned(ideas, n)
        return cleaned

    async def too_tame(texts: list[str]) -> list[str]:
        """The descriptions that do not show the adult content the rating asks for (judged by meaning, content_level.py)."""
        return [t for t in texts if await shows_adult(ctx, t, rating) is False] if wants_adult(rating) else []

    while len(briefs) < count and rounds < math.ceil(count / BRIEFS_PER_ROUND) + EXTRA_ROUNDS:
        if stop and stop():
            break
        rounds += 1
        need = count - len(briefs)
        ask = min(BRIEFS_PER_ROUND, need + 2)       # a little more than needed, because some come back as repeats
        ideas, reply = await ask_round(ask)
        if reply.get("needs_attention"):
            last_error = reply["error"]
            log.warning("write_briefs round %d needs attention: %s", rounds, last_error)
            continue
        cleaned = take(ideas, reply)
        new = fresh_briefs(cleaned, briefs, need)
        written = {" ".join(text.split()): (ideas, n) for n, text in reversed(list(enumerate(cleaned)))}
        if tame := await too_tame(new):         # a nsfw or explicit request got clothed, suggestive descriptions: those are asked for again, once
            log.info("write_briefs round %d: %d of %d descriptions show no adult content, asking again", rounds, len(tame), len(new))
            again_ideas, again = await ask_round(len(tame), TOO_TAME.format(rating=rating, n=len(tame), texts=" | ".join(tame)))
            if not again.get("needs_attention"):
                again_cleaned = take(again_ideas, again)
                where = {" ".join(t.split()): (again_ideas, n) for n, t in reversed(list(enumerate(again_cleaned)))}
                kept = [t for t in new if t not in tame]
                swap = {}
                for old, text in zip(tame, fresh_briefs(again_cleaned, [*briefs, *kept], len(tame))):
                    if not await too_tame([text]):
                        swap[old] = text
                        written[text] = where[text]
                new = [swap.get(t, t) for t in new]
                log.info("write_briefs round %d: %d of %d replaced", rounds, len(swap), len(tame))
        for text in new:        # what the descriptions say of the camera, the gaze ... is counted, so the next descriptions and scenes lean away from it
            offering.remember(written[text][0], state, lambda slot, text=text: text, written[text][1])
        log.info("write_briefs round %d: %d of %d new", rounds, len(new), len(reply["briefs"]))
        briefs += new
        if progress:
            progress(len(briefs))
    if not briefs:
        raise PlanError(f"The LLM could not write descriptions for '{item['description'][:60]}': {last_error or 'nothing it wrote was usable'}")
    issues = []
    if len(briefs) < count and not (stop and stop()):
        issues.append(make_issue("job", f"Asked for {count} different images but only {len(briefs)} distinct ideas could be written, so {len(briefs)} images were planned.",
                                 "warning", "planner_note"))
    return briefs, issues, [people.get(b) for b in briefs], [plan_of.get(b) or {} for b in briefs], picks


def brief_jobs(item: dict, briefs: list[str], subjects: list[dict | None] | None = None, members: list[str] | None = None, per_member: int = 1,
               assigned: list[dict] | None = None, picks: list | None = None, group_kind: str = "") -> list[dict]:
    """One planned one-image job per brief. Each carries the parent's constraints, and the parent's wildcard tokens that the
    brief itself lost (as a constraint, the way keep_wildcard_tokens does). With `members` (the LoRAs of an "N images per <kind> LoRA"
    request) brief n belongs to member (n - 1) // per_member: that LoRA is fixed on the job, with its place in the group (and the group's
    kind, `group_kind`, which decides what else the LoRA step may add) in the job's intent.
    `assigned` are the wildcard entries each brief must show; `picks` the wildcard lists chosen for the whole set."""
    from app.planning.planner import WORKFLOW_META
    tokens = tokens_of(item["description"], *item["constraints"])
    base = item["meta"].get("intent") or {}
    jobs = []
    for n, text in enumerate(briefs, 1):
        who = (subjects[n - 1] if subjects and n <= len(subjects) else None) or item["meta"].get("subjects")       # the brief's own, else the job's
        constraints = list(item["constraints"])
        for token in tokens:
            if token not in text and not any(token in c for c in constraints):
                constraints.append(f"Use {token} exactly as written in every prompt")
        member = members[min((n - 1) // per_member, len(members) - 1)] if members else ""
        intent = dict(base)
        if member:
            intent["group"] = {"kind": group_kind, "member": member, "n": members.index(member) + 1, "of": len(members)}
        named = list(dict.fromkeys([*(item["meta"].get("loras_named") or []), *([member] if member else [])]))
        jobs.append({"description": text, "count": 1, "variety": "variations", "workflow_id": item["workflow_id"], "constraints": constraints, "issues": [],
                     "meta": {"request": item["meta"].get("request", ""), "reason": f"Idea {n} of {len(briefs)} for: {item['description']}"[:240],
                              "constraints": constraints, "workflow": dict(WORKFLOW_META), **({"subjects": who} if who else {}), "idea": {"n": n, "of": len(briefs), "from": item["description"]},
                              **({"intent": intent} if intent else {}), **({"assigned": assigned[n - 1]} if assigned and assigned[n - 1] else {}),
                              **({"wildcard_picks": [list(p) for p in picks]} if picks else {}),
                              **({"loras_named": named} if named else {})}})
    return jobs
