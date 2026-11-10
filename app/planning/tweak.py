"""Tweak (step 5): a short instruction ("darker", "fewer steps", "portrait") becomes a patch of only the fields
that change. Locked fields are never in the patch."""
import logging
import re
import time

from app.fields import SEED_STRATEGIES, merge_negative, plain_values, split_tags
from app.library.card_models import WorkflowCard
from app.planning import locks
from app.planning.clamp import bounds, clamp_choice, clamp_lora_weight, clamp_range, clamp_size, size_options
from app.planning.concepts import load_concepts
from app.planning.context import PlanContext, block, derive_rng, derive_seed, job_view
from app.planning.look import look_for
from app.planning.mentions import plain, readable
from app.planning.offers import served, served_slots
from app.planning.pick_wildcards import pick_wildcards, picking_lists
from app.planning.prompts import card_view, finalize_prompts, lora_requirements, usable_lists, weight_rules
from app.planning.scene import forced_detail, rating_of, stored_scene
from app.planning.weights import clean_negative, rule_text, typed_groups
from app.wildcards.expand import drop_invented_refs
from app.planning.semantic import related_lists
from app.planning.settings import allowed, parse_size
from app.planning.tagstep import TagStep, concept_lines, records
from app.wildcards.ideas import idea_record, make_ideas, should_offer_for_tweak

log = logging.getLogger("planning")
TWEAKABLE = ("prompts", "negative", "lora_weights", "steps", "cfg", "sampler", "scheduler", "size", "hires", "seed_strategy")


async def apply_tweak(ctx: PlanContext, job: dict, wf: dict, card: WorkflowCard, instruction: str, steps: list[dict] | None = None,
                      scene: dict | None = None) -> dict:
    """The fields to change as {name: field} (source "auto", reason = the LLM's), or {} if nothing changes.
    Raises ValueError if the LLM gave no usable answer. When the LLM also chose wildcard lists, its time is added to `steps`
    as {"step": "wildcards", "seconds"} (the caller records it separately). `scene` is the scene plan the job keeps (or the
    new one for a change that is about the scene); the prompt writer uses it, and the size floor and rating follow it."""
    fixed, values, slots, s = locks.locked(job["fields"]), plain_values(job["fields"]), wf["slots"], card.settings
    if (job["fields"].get("negative") or {}).get("base") is not None:     # the negative before a LoRA's words were taken out of it:
        values["negative"] = job["fields"]["negative"]["base"]            # the writer changes that one, and the job store takes the words out again
    instruction = plain(instruction, ctx.lora_keys())         # an @name never reaches a prompt
    request = plain(job["meta"].get("request", ""), job["meta"].get("loras_named") or [])
    kept = locks.kept_prompts(job["fields"], job["count"])        # prompts locked one by one
    d = slots.get("defaults") or {}
    loras = values.get("loras") or []
    sampler_options, scheduler_options = allowed(s.samplers, ctx.samplers), allowed(s.schedulers, ctx.schedulers)
    default_size = (values.get("width") or d.get("width"), values.get("height") or d.get("height"))
    sizes = size_options(card, default_size if all(default_size) else None) if slots.get("width") and slots.get("height") else []

    can = {"prompts": "prompts" not in fixed, "negative": "negative" not in fixed and bool(slots.get("negative")),
           "lora_weights": "loras" not in fixed and bool(loras),
           "steps": "steps" not in fixed and bool(slots.get("steps")), "cfg": "cfg" not in fixed and bool(slots.get("cfg")),
           "sampler": "sampler" not in fixed and bool(slots.get("sampler")) and len(sampler_options) > 1,
           "scheduler": "scheduler" not in fixed and bool(slots.get("scheduler")) and len(scheduler_options) > 1,
           "size": not ({"width", "height"} <= set(fixed)) and len(sizes) > 1,
           "hires": "hires" not in fixed and bool(slots.get("hires")), "seed_strategy": "seed_strategy" not in fixed}
    if not any(can.values()):
        return {}
    view = {**card_view(card, wf), "steps": vars_of(s.steps), "cfg": vars_of(s.cfg), "sizes": [{"size": o["size"], "label": o["label"]} for o in sizes],
            "samplers": sampler_options, "schedulers": scheduler_options}
    current = {k: values.get(k) for k in ("prompts", "negative", "loras", "steps", "cfg", "sampler", "scheduler", "width", "height", "hires", "seed_strategy")}
    seed = ctx.idea_seed()      # this tweak's own seed: its idea draw, tag picks and concept subsets derive from it
    ideas = None
    if can["prompts"] and should_offer_for_tweak(instruction, ctx.config):     # "different background", "surprise me": fresh ingredients
        picks = []
        if picking_lists(ctx):      # the LLM sees the job and the change asked for; code matches on the instruction alone
            started = time.monotonic()
            picks, _ = await pick_wildcards(ctx, card, {"job": job["description"], "change_requested": instruction.strip()}, "")
            if steps is not None:
                steps.append({"step": "wildcards", "seconds": round(time.monotonic() - started, 1)})
        related = await related_lists(ctx, usable_lists(ctx), instruction)
        slots = served_slots(await served(ctx, usable_lists(ctx)))        # what the lists serve, read from their entries
        ideas = make_ideas(ctx.wildcards, ctx.config, instruction, "", job["count"], derive_seed(seed, "ideas"), picks, related, slots)
    rules = weight_rules(ctx, wf, card, job["description"], *job["meta"].get("constraints", []), instruction)
    rules.put_back = False
    scene = scene or stored_scene(job)
    book = load_concepts()
    about_text = " ".join([instruction, request, *job["meta"].get("constraints", [])])
    rating = rating_of(scene) if scene else book.rating_min(about_text)
    detail = (scene.get("detail_level") if scene else forced_detail(about_text)) or "normal"
    lora_cards = {x["name"]: c for x in loras if (c := ctx.lora_card(x["name"]))}
    triggers, styles = lora_requirements(lora_cards)
    protect = [t.strip() for t in split_tags(card.prompt_style.quality_prefix) if t.strip()] + triggers + styles
    exclude = [*triggers, *styles, *lora_cards, *(readable(n) for n in lora_cards)]
    tagstep = TagStep.build(ctx, card, job["meta"].get("subjects"), protect, " ".join([request, instruction, *job["meta"].get("constraints", [])]),
                            rating=rating, detail_level=detail, must_keep=(scene or {}).get("must_keep"),
                            ignore_words=book.vague_words() | {w for phrase in exclude for w in re.findall(r"[a-z0-9]+", phrase.lower())},
                            lora_phrases=[c.what_it_does for c in lora_cards.values() if c.what_it_does],
                            keep_text=" ".join([job["description"], *job["meta"].get("constraints", [])]), scene=scene, lora_files=list(lora_cards),
                            look=look_for(ctx, list(lora_cards)))
    tag_offers = await tagstep.slot_suggestions({k: (scene or {}).get(k, "") for k in
                                                 ("subjects", "body", "outfit", "pose", "action", "expression", "camera", "gaze", "setting", "lighting", "mood")},
                                                exclude, derive_rng(seed, "tags"), ctx.recent_tags()) if tagstep and scene else {}
    if tagstep:
        tagstep.offer(tag_offers)
    groups = "\n".join(f"{slot}: " + ", ".join(tags) for slot, tags in tag_offers.items())
    concepts = await concept_lines(ctx, about_text, rating, derive_rng(seed, "concepts")) if tagstep else []
    inputs = {"instruction": instruction.strip(), "weights": rule_text(rules.allowed), "ideas": block("wildcard ideas", ideas.block_text()) if ideas else "(none)", "job": {**job_view({"description": job["description"], "count": job["count"], **job["meta"]}), "current_values": current},
              "scene": block("scene plan", scene) if scene else "(none)",
              "concepts": block("real tags for the request's words", "\n".join(concepts)) if concepts else "(none)",
              "tags": block("real tags that fit the scene", groups) if groups else "(none)",
              "card": block("workflow card", view),
              "locked": {**locks.locked_values(fixed), **({"prompt_numbers_to_keep_as_they_are": [i + 1 for i in sorted(kept)]} if kept else {})}}
    enums = {"sampler": sampler_options, "scheduler": scheduler_options, "size": [o["size"] for o in sizes], "lora": [x["name"] for x in loras]}
    reply = await ctx.llm.call("apply_tweak", inputs, enums, {"prompts": {"minItems": job["count"], "maxItems": job["count"]}},
                               [n for n in TWEAKABLE if not can[n]])
    if reply.get("needs_attention"):
        raise ValueError(reply["error"])
    why, patch = (reply.get("reason") or "").strip(), {}
    note = lambda text: f"Tweak: {text}" if text else "Tweak"
    if can["prompts"] and reply.get("prompts"):
        catalog = ctx.wildcards.catalog() if ctx.wildcards is not None else None
        typed = " ".join([instruction, request, *job["fields"]["prompts"]["value"]])
        texts = [drop_invented_refs(p, typed, catalog) for p in reply["prompts"]]
        texts, added = finalize_prompts(texts, card, triggers, styles, job["description"], rules=rules)
        checks = [tagstep.check(t) for t in texts] if tagstep else []      # the same tag check as for a new plan (no repair call here)
        texts = [c.text for c in checks] if checks else texts
        if not rules.allowed and (mine := typed_groups(instruction)):        # the card has no issue list here, so the note says it
            added.append(f"your weight {', '.join(mine.values())} was taken out: this model reads weights as text")
        if kept:
            texts = [kept[i]["text"] if i in kept else t for i, t in enumerate(texts)]
        items = [{"reason": kept[i]["reason"] if i in kept else "", **({"ideas": idea_record(ideas.per_prompt[i], t, ideas.lists_record())} if ideas and i not in kept and i < len(ideas.per_prompt) and ideas.per_prompt[i] else {}),
                  **({"tagcheck": records(checks[i])} if i < len(checks) and i not in kept and records(checks[i]) else {})}
                 for i, t in enumerate(texts)]
        patch["prompts"] = locks.auto(texts, note(why), "; ".join(added), items if kept or ideas or any("tagcheck" in x for x in items) else None,
                                      [i in kept for i in range(len(texts))] if kept else None)
    if can["negative"] and reply.get("negative") is not None:
        tags = clean_negative(split_tags(reply["negative"]), rules, split_tags(current["negative"] or ""))      # no new weights, the old ones stay
        patch["negative"] = locks.auto(merge_negative(", ".join(t for t in tags if t), ctx.config.safety_negative), note(why))
    if can["lora_weights"] and reply.get("lora_weights"):
        by_name, notes, new = {x["name"]: dict(x) for x in loras}, [], None
        for item in reply["lora_weights"]:
            if item["lora"] in by_name and (c := ctx.lora_card(item["lora"])):
                by_name[item["lora"]]["weight"], n = clamp_lora_weight(item["weight"], c.weight)
                notes += [n] if n else []
        patch["loras"] = locks.auto(list(by_name.values()), note(why), "; ".join(notes))
    for name, rng, integer in (("steps", s.steps, True), ("cfg", s.cfg, False)):
        if can[name] and reply.get(name) is not None:
            value, n = clamp_range(reply[name], rng, integer)
            patch[name] = locks.auto(value, note(why), n)
    for name, options in (("sampler", sampler_options), ("scheduler", scheduler_options)):
        if can[name] and reply.get(name):
            value, n = clamp_choice(reply[name], options, d.get(name))
            patch[name] = locks.auto(value, note(why), n)
    if can["size"] and (asked := parse_size(reply.get("size"))):
        w, h, n = clamp_size(*asked, sizes)
        for key, value in (("width", w), ("height", h)):
            if key not in fixed:
                patch[key] = locks.auto(value, note(why), n)
    if can["hires"] and reply.get("hires") is not None:
        patch["hires"] = locks.auto(bool(reply["hires"]), note(why))
    if can["seed_strategy"] and reply.get("seed_strategy") in SEED_STRATEGIES:
        patch["seed_strategy"] = locks.auto(reply["seed_strategy"], note(why))
    return {name: field for name, field in patch.items() if field["value"] != values.get(name)}


def vars_of(r):
    b = bounds(r)
    return {"recommended": b[2], "min": b[0], "max": b[1]} if b else None
