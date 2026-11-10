"""The planning pipeline: `plan_request` turns the request into jobs, `plan_job` fills one job's LoRAs,
prompts and settings around its locked fields. Plain Python; every LLM call goes through LlmClient.call."""
import logging
import re
import time

from app.fields import clamp_number, make_issue
from app.library.card_models import WorkflowCard
from app.planning import content_safety, locks, models  # noqa: F401  (models registers the reply models of the planning tasks)
from app.planning.context import PlanContext, block, derive_rng
from app.planning.lora_words import clean_prompts
from app.planning.look import look_for
from app.planning.loras import pick_loras
from app.planning import intent as intents
from app.planning.briefs import SAME_IDEA, overlap
from app.planning.mentions import mentioned, plain
from app.planning.pick_wildcards import picking_lists
from app.planning import offers as offering
from app.planning.prompts import choose_wildcards, scene_options, write_prompts, writes_prompts
from app.planning.rank import tokenize
from app.planning.appearance import appearance_options, has_character_lora
from app.planning.scene import appearance_record, plan_scene, stored_scene, used_appearance
from app.planning.settings import pick_settings, warn_locked
from app.planning.tagcheck import open_subjects
from app.planning.weights import typed_groups, weights_allowed
from app.wildcards.expand import drop_invented_refs
from app.wildcards.ideas import tokens_of, used_ideas

log = logging.getLogger("planning")
PLANNED_SHOWN = 8       # the newest earlier scenes of a run that the scene step is told about


class PlanError(Exception):
    """The whole plan can't be made; the message is meant for the user."""


# ---------- step 1: the request -> jobs ----------

WORKFLOW_META = {"source": "locked", "reason": "You chose this workflow"}


# words that say a number is about images ("4 portraits", "3 of a knight"); without one, "five girls" means five girls in one picture
IMAGE_WORDS = re.compile(r"\b(images?|pictures?|pics?|photos?|portraits?|shots?|versions?|variations?|renders?|illustrations?|wallpapers?|drawings?|artworks?|scenes?|each)\b"
                         r"|\b(\d+|two|three|four|five|six|seven|eight|nine|ten) of\b", re.I)


NUMBER = r"\d+|two|three|four|five|six|seven|eight|nine|ten"
PICTURE = r"images?|pictures?|pics?|photos?|renders?|variations?|versions?"
COUNT_LEAD = re.compile(rf"^\s*(?:(?:{NUMBER})\s+(?:{PICTURE}\s+)?|(?:(?:one|{NUMBER})\s+)?(?:{PICTURE})\s+)of\s+", re.I)


def without_count(description: str) -> str:
    """The description without a count the planner left in it ("3 of a red fox in snow", "2 images of a girl"): the count is the job's own field,
    and the words in front of the picture only mislead the steps that read the description."""
    text = COUNT_LEAD.sub("", description, count=1).strip()
    if text == description.strip() or not text:
        return description
    return text[:1].upper() + text[1:]


def people_not_images(count: int, subjects: dict | None, request: str) -> bool:
    """The LLM took the number of people in the picture for the number of images: the count equals the people asked for, and
    nothing in the request is about images."""
    people = sum(v for k, v in (subjects or {}).items() if k in ("girls", "boys", "people", "others") and isinstance(v, int) and v > 0)
    return count > 1 and people == count and not IMAGE_WORDS.search(request)


async def plan_request(ctx: PlanContext, request: str, wf: dict, card: WorkflowCard, input_images: list[str] | None = None,
                       room: int | None = None, shared: str = "") -> tuple[list[dict], intents.Resolved]:
    """Step 1. The user has chosen the workflow; the planner sees only its card and splits the request into jobs on it.
    Returns (jobs, intent): one dict per job {description, count, variety, workflow_id, constraints, issues, meta}, and what the
    request asks for besides the pictures (intent.py: content level, LoRAs wanted and ruled out, "N images per <kind> LoRA", ...),
    already checked against the cards. `issues` holds the planner's own notes (warnings). `room` is how many images may still be added to
    the request (default: the most one request may have); counts are cut to fit and a job that is cut says so. `shared` is the general
    instruction typed before `===`: the planner reads it, and code makes it a constraint of every job, so it can't get lost. LoRAs named with
    `@` in `request` or `shared` are numbered in what the planner reads ("the chosen LoRA [1]"); the planner says whether each is
    wanted. The wanted ones are recorded in every job's meta (`loras_named`) and written as plain words in what the later steps read.
    Raises PlanError if no plan can be made."""
    input_images = input_images or []
    keys = ctx.lora_keys()
    named = list(dict.fromkeys([*mentioned(shared, keys), *mentioned(request, keys)]))
    original, original_shared = request, shared
    request, shared = plain(request, keys, named), plain(shared, keys, named)
    most = ctx.config.max_images_per_request
    room = most if room is None else room
    told = f"{request.strip()}\n\nFor every image of this request: {shared}" if shared else request.strip()
    inputs = {"request": told, "max_images": most, "input_images": input_images or "none",
              "workflow": block("the chosen workflow card", {"title": wf["title"], "family": card.family, "kind": wf["kind"], "purpose": card.purpose,
                                                             "best_for": card.best_for, "prompt_style": card.prompt_style.kind}),
              "loras": lora_overview(ctx, card.family, bool(wf["slots"].get("model")))}
    k = len(named)      # one verdict for each LoRA mentioned with @ (none when there is no mention)
    reply = await ctx.llm.call("plan_request", inputs, {"mention": list(range(1, k + 1))} if k else None,
                               {"count": {"maximum": most}, **({"mentions": {"minItems": k, "maxItems": k}} if k else {})}, None if k else ["mentions"])
    if reply.get("needs_attention"):
        raise PlanError(f"The planner could not understand the request: {reply['error']}")
    meant = intents.resolve(ctx, reply.get("intent"), named, card.family, bool(wf["slots"].get("model")), told)
    fixed = list(meant.use)         # the LoRAs the user wants on every image; they are what `loras_named` always was
    asked = reply.get("intent") or {}
    jobs, left_out = [], 0
    planned = distinct_jobs(reply["jobs"])
    if meant.group_kind:        # "N images per <kind> LoRA" is ONE job: its count is the images for each LoRA, the app makes the rest (runner._expand)
        planned = planned[:1]
    for item in planned:
        count, note = clamp_number(meant.per or item["count"] if meant.group_kind else item["count"], 1, most)
        if not meant.group_kind and people_not_images(count, item.get("subjects"), request):
            count = 1
        asked_count = count
        count = min(count, room)
        if count < 1:       # the request as a whole is already at max_images_per_request
            left_out += 1
            continue
        room -= count
        cut = f"asked {asked_count}, cut to {count}" if count < asked_count else ""
        typed = f"{original} {original_shared}"
        catalog = ctx.wildcards.catalog() if ctx.wildcards is not None else None
        item["description"] = without_count(drop_invented_refs(item["description"], typed, catalog))
        item["constraints"] = [drop_invented_refs(c, typed, catalog) for c in item["constraints"]]
        constraints = [c.strip() for c in item["constraints"] if c.strip() and c.strip() != shared]
        if shared:
            constraints.insert(0, shared)
        issues = [make_issue("job", text, "warning", "planner_note") for text in item["issues"] if text.strip()]
        if cut:
            issues.append(make_issue("job", f"Only {count} of the {asked_count} images asked for: one request can add up to {most} images.", "warning", "planner_note"))
        subjects = open_subjects(item.get("subjects"))
        jobs.append({"description": item["description"].strip(), "count": count, "variety": item["variety"], "workflow_id": wf["id"], "constraints": constraints,
                     "issues": issues,
                     "meta": {"request": f"{original.strip()} (for all images: {original_shared})" if shared else original.strip(), "reason": item["reason"], "constraints": constraints, "workflow": dict(WORKFLOW_META),
                              "intent": {**meant.meta(), "asked": asked},
                              **({"subjects": subjects} if subjects else {}), **({"count_note": cut or note} if cut or note else {}), **({"loras_named": fixed} if fixed else {}),
                              **({} if item.get("picture_given", True) else {"picture_open": True})}})
    if left_out and jobs:
        jobs[-1]["issues"].append(make_issue("job", f"{left_out} more {'job' if left_out == 1 else 'jobs'} of this request left out: one request can add up to {most} images.", "warning", "planner_note"))
    keep_wildcard_tokens(original, jobs)
    if weights_allowed(card, ctx.library.get_graph(wf["id"])):
        keep_typed_weights(original, jobs)
    return jobs, meant


def distinct_jobs(jobs: list[dict]) -> list[dict]:
    """The planner's jobs without the repeats: a model that runs on writes the same job again and again, and every copy would be planned
    and drawn. A job that says (nearly) the same as an earlier one, in its description and constraints, is that job again."""
    kept, seen = [], []
    for job in jobs:
        words = set(tokenize(" ".join([job["description"], *job["constraints"]])))
        if any(overlap(words, other) >= SAME_IDEA for other in seen):
            continue
        kept.append(job)
        seen.append(words)
    return kept


def lora_overview(ctx: PlanContext, family: str, takes_loras: bool) -> str:
    """How many usable LoRAs of each kind the workflow's family has, as a data block: it tells the planner which groups exist
    ("style" has 5) without handing it file names."""
    if not takes_loras:
        return "(this workflow takes no LoRAs)"
    counts: dict[str, int] = {}
    for _, c in ctx.lora_choices():
        if c.family == family:
            counts[c.kind] = counts.get(c.kind, 0) + 1
    return block("LoRAs of this workflow's family, by kind", counts or "none")


def keep_wildcard_tokens(request: str, jobs: list[dict]):
    """Wildcard tokens the user typed (`__name__`, `{a|b}`) must stay in the plan. One the planner left out of every job
    becomes a constraint of the job it fits best (the only job, or the one whose description shares the most words with
    the text around the token)."""
    for token in tokens_of(request):
        if not jobs or any(token in " ".join([j["description"], *j["constraints"]]) for j in jobs):
            continue
        around = set(tokenize(request[max(request.index(token) - 80, 0):request.index(token) + len(token) + 80].replace(token, " ")))
        job = max(jobs, key=lambda j: len(around & set(tokenize(j["description"]))))
        job["constraints"].append(f"Use {token} exactly as written in every prompt")
        job["meta"]["constraints"] = job["constraints"]


def keep_typed_weights(request: str, jobs: list[dict]):
    """Weights the user typed (`(forest:1.5)`) must reach the prompts. One the planner left out of every job becomes a
    constraint of the job it fits best, like a wildcard token. (Only asked for on a workflow that reads weights.)"""
    for key, group in typed_groups(request).items():
        if not jobs or any(key in typed_groups(j["description"], *j["constraints"]) for j in jobs):
            continue
        at = request.index(group)
        around = set(tokenize(request[max(at - 80, 0):at + len(group) + 80].replace(group, " ")))
        job = max(jobs, key=lambda j: len(around & set(tokenize(j["description"]))))
        job["constraints"].append(f"Use {group} exactly as written in every prompt")
        job["meta"]["constraints"] = job["constraints"]


# ---------- steps 2-4: one job ----------

def drop_lora_words(ctx: PlanContext, job: dict, card: WorkflowCard, fields: dict, fixed: dict, kept: dict) -> dict:
    """A re-plan that drops a LoRA the job had takes its trigger words out of the prompts it keeps as they were (locked ones;
    the others were just written without them). `fields` is returned with the prompts cleaned."""
    names = lambda f: [x["name"] for x in (f or {}).get("value") or []]
    now_on = set(names(fields.get("loras")))
    gone = [n for n in dict.fromkeys(names(job["fields"].get("loras"))) if n not in now_on]
    if not gone or not (fields.get("prompts") or {}).get("value"):
        return fields
    removed = {n: c for n in gone if (c := ctx.lora_card(n))}
    stay = {n: c for n in now_on if (c := ctx.lora_card(n))}
    only = None if "prompts" in fixed else set(kept)
    cleaned, changed = clean_prompts(fields["prompts"], removed, stay, card.prompt_style.kind == "tags", only)
    if changed:
        log.info("removed the trigger words of %s from prompt(s) %s", ", ".join(gone), ", ".join(str(i + 1) for i in changed))
    return {**fields, "prompts": cleaned}


async def plan_job(ctx: PlanContext, job: dict, wf: dict, card: WorkflowCard, progress=None, state: offering.PlanState | None = None) -> dict:
    """Fill the LoRAs, prompts and settings of one job around its locked fields.

    Returns {"fields", "issues", "timings"}. Locked fields come back untouched; every other planned field is new
    (an unlocked field from an earlier plan is thrown away first, so nothing stale survives a re-plan). `state` is what the whole planning
    run knows (wildcard entries used so far, scenes planned so far): the scene of this job leans away from them."""
    say = progress or (lambda step: None)
    state = state if state is not None else offering.PlanState()
    fixed = locks.locked(job["fields"])
    kept = locks.kept_prompts(job["fields"], job["count"])
    fields = locks.start_replan(job["fields"])
    issues = [i for i in job["issues"] if i.get("code") == "planner_note"]
    timings = []
    named = job["meta"].get("loras_named") or []
    intent = job["meta"].get("intent") or {}
    seed = ctx.idea_seed()      # one seed per job: its wildcard draws, tag picks and concept subsets all derive from it
    jv = {"description": job["description"], "count": job["count"], "constraints": job["meta"].get("constraints", []), "seed": seed,
          "request": plain(job["meta"].get("request", ""), ctx.lora_keys()), "subjects": job["meta"].get("subjects"), "named": named,
          "intent": intent, "min_rating": intent.get("rating") or "general", "rating_read": bool(intent.get("asked")), "lora_uses": dict(state.loras), "images_so_far": len(state.scenes),
          **({"batch": True} if job["meta"].get("idea") else {})}

    async def step(name: str, make):
        say(name)
        started = time.monotonic()
        new, found = await make()
        timings.append({"step": name, "seconds": round(time.monotonic() - started, 1)})
        issues.extend(found)
        return new

    fields = locks.merge(fields, await step("loras", lambda: pick_loras(ctx, jv, wf, card, fixed)))
    lora_value = (fields.get("loras") or {}).get("value") or []
    state.loras.update(i["name"] for i in lora_value if not i.get("named"))
    jv["look"] = look_for(ctx, [i["name"] for i in lora_value], intent.get("vary"))      # set when a LoRA of this job sets the picture's look
    if jv["look"] is not None:
        log.info("a LoRA of this job sets the look: no light or colour is offered, planned or kept")
    picks = [tuple(p) for p in job["meta"].get("wildcard_picks") or []]     # a big request chose its lists once for all its jobs
    if not picks and writes_prompts(jv, fixed, kept) and picking_lists(ctx):
        picks = await step("wildcards", lambda: choose_wildcards(ctx, jv, card))
    scene, blocked, options, looks = stored_scene(job), False, None, None
    if scene is None and writes_prompts(jv, fixed, kept):       # a re-plan or a tweak keeps the scene it already has
        before = len(issues)
        job["meta"].pop("scene_options", None)
        job["meta"].pop("wildcard_gaps", None)
        job["meta"].pop("appearance_options", None)
        assigned = dict(job["meta"].get("assigned") or {})         # what the request wants different in every image, given to this image (briefs.py)

        async def make_scene():
            nonlocal options, looks
            options = await scene_options(ctx, jv, picks, seed, state, assigned, intent.get("vary") or [])     # relevant wildcard options for what the request leaves open
            user_words = " ".join([jv["request"], jv["description"], *jv["constraints"]])
            looks = None if has_character_lora(ctx, lora_value) else \
                appearance_options(ctx.taglib(), user_words, jv.get("subjects"), derive_rng(seed, "appearance"))        # hair and eye options when the look is open
            if looks:
                log.info("appearance options offered: %s", "; ".join(f"{k}: {', '.join(v)}" for k, v in looks.items()))
            return await plan_scene(ctx, jv, jv["min_rating"], options, derive_rng(seed, "concepts"), looks, state.scenes[-PLANNED_SHOWN:])
        scene = await step("scene", make_scene)
        blocked = any(i.get("severity") == "blocking" for i in issues[before:])
        for issue in issues[before:]:
            if issue.get("severity") == "blocking":
                content_safety.log_block(issue["code"], job.get("id"))
        if scene:
            said = offering.named(ctx, " ".join([jv["request"], jv["description"], *jv["constraints"]]), scene.get("avoid"))       # a camera or gaze the picture's own words name stays as named
            required = {**said, **offering.assigned(options)}
            enforced = offering.enforce(scene, required) if required else []
            job["meta"]["scene"] = scene
            if jv.get("wildcard_gaps"):         # slots the scene plan wants that none of the user's lists covers: the model wrote them
                job["meta"]["wildcard_gaps"] = list(jv["wildcard_gaps"])
            if record := offering.record(options, scene, 0, state, enforced):
                job["meta"]["scene_options"] = record
                log.info("scene options used: %s", ", ".join(record["used"]) or "none")
            if record := appearance_record(looks, scene):
                job["meta"]["appearance_options"] = record
                log.info("appearance options used: %s", ", ".join(record["used"]) or "none")
            state.remember_scene(scene)
    if scene:
        jv["scene"] = scene
        jv["scene_offers"] = options
    if not blocked:
        fields = locks.merge(fields, await step("prompts", lambda: write_prompts(ctx, jv, wf, card, lora_value, fixed, kept, picks)))
    fields = drop_lora_words(ctx, job, card, fields, fixed, kept)
    prompts = (fields.get("prompts") or {}).get("value") or []
    if record := job["meta"].get("scene_options"):     # which of the scene's options reached the final prompts
        record["in_prompts"] = used_ideas(record["offered"], " . ".join(prompts))
        log.info("scene options in the final prompts: %s", ", ".join(record["in_prompts"]) or "none")
    if record := job["meta"].get("appearance_options"):
        record["in_prompts"] = used_appearance(record, prompts)
        log.info("appearance options in the final prompts: %s", ", ".join(record["in_prompts"]) or "none")
    fields = locks.merge(fields, await step("settings", lambda: pick_settings(ctx, jv, wf, card, fixed, lora_value, prompts)))
    issues.extend(warn_locked(ctx, wf, card, fixed))
    log.info("planned job '%s': %s", job["description"][:60], ", ".join(f"{t['step']} {t['seconds']}s" for t in timings))
    return {"fields": fields, "issues": issues, "timings": timings, "meta": job["meta"]}
