"""Step 4: generation settings. The LLM only chooses where the card leaves a real choice: a value the card
pins to one option is set by code with a reason, a locked value is left alone, and every answer is clamped to the
card's ranges and to ComfyUI's own sampler and scheduler lists."""
import logging
import random

from app.fields import SEED_MAX, SEED_STRATEGIES, make_issue
from app.library.card_models import WorkflowCard
from app.planning import locks
from app.planning.clamp import bounds, clamp_choice, clamp_lora_weight, clamp_range, clamp_size, round_to_multiple, size_options
from app.planning.context import PlanContext, block, job_view
from app.planning.tagcheck import describe_subjects

log = logging.getLogger("planning")
ASKABLE = ("steps", "cfg", "sampler", "scheduler", "size", "seed_strategy", "hires", "hires_steps", "hires_denoise")
WHY = ("steps", "cfg", "sampler", "scheduler", "size", "seed_strategy", "hires")
FALLBACK_REASON = "The card's recommended value (the LLM did not answer)."


def allowed(card_values: list[str], comfy_values: list[str]) -> list[str]:
    """The card's list limited to what ComfyUI offers; either list alone if the other is empty."""
    if card_values and comfy_values:
        return [v for v in card_values if v in comfy_values] or comfy_values
    return card_values or comfy_values


def parse_size(text: str | None) -> tuple[int, int] | None:
    try:
        w, h = (int(x) for x in str(text).lower().replace("×", "x").split("x"))
        return (w, h) if w > 0 and h > 0 else None
    except ValueError:
        return None


def number_text(value) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


class Plan:
    """What step 4 has decided so far and what is still to ask."""

    def __init__(self):
        self.new: dict = {}
        self.ask: list[str] = []
        self.view: dict = {}
        self.enums: dict = {}
        self.options: list[dict] = []       # sizes the LLM may choose from

    def pin(self, name: str, value, reason: str):
        self.new[name] = locks.auto(value, reason)


def plan_choices(ctx: PlanContext, wf: dict, card: WorkflowCard, fixed: dict) -> Plan:
    """Decide in code what can be decided, and list what the LLM is asked."""
    p, slots, s = Plan(), wf["slots"], card.settings
    d = slots.get("defaults") or {}
    for name, rng, integer in (("steps", s.steps, True), ("cfg", s.cfg, False)):
        b = bounds(rng)
        if name in fixed or not slots.get(name) or b is None:
            continue                     # no range known: the workflow's own value is used
        if b[0] == b[1]:
            p.pin(name, round(b[2]) if integer else b[2], f"The card allows only {number_text(b[2])}.")
        else:
            p.ask.append(name)
            p.view[name] = {"recommended": b[2], "min": b[0], "max": b[1], "workflow_default": d.get(name)}
    for name, card_list, comfy_list in (("sampler", s.samplers, ctx.samplers), ("scheduler", s.schedulers, ctx.schedulers)):
        options = allowed(card_list, comfy_list)
        if name in fixed or not slots.get(name) or not options:
            continue
        if len(options) == 1:
            p.pin(name, options[0], f"The card allows only {options[0]}.")
        else:
            p.ask.append(name)
            p.enums[name] = options
            p.view[f"{name}s"] = {"allowed": options, "workflow_default": d.get(name)}
    size_fixed = {n for n in ("width", "height") if n in fixed}
    if slots.get("width") and slots.get("height") and len(size_fixed) < 2:
        default = (d["width"], d["height"]) if d.get("width") and d.get("height") else None
        options = size_options(card, default)
        if "width" in fixed:
            options = [o for o in options if o["w"] == fixed["width"]["value"]]
        if "height" in fixed:
            options = [o for o in options if o["h"] == fixed["height"]["value"]]
        if not options and default:       # one side is locked to a value the card's list doesn't have: keep the default shape
            w = fixed["width"]["value"] if "width" in fixed else round_to_multiple(round(fixed["height"]["value"] * default[0] / default[1]))
            h = fixed["height"]["value"] if "height" in fixed else round_to_multiple(round(w * default[1] / default[0]))
            options = [{"size": f"{w}x{h}", "w": w, "h": h, "label": "matches your locked side"}]
        if len(options) == 1:
            for key, value in (("width", options[0]["w"]), ("height", options[0]["h"])):
                if key not in fixed:
                    p.pin(key, value, "The only size that fits the card and your locked values." if size_fixed else f"The card allows only {options[0]['size']}.")
        elif options:
            p.ask.append("size")
            p.enums["size"] = [o["size"] for o in options]
            p.options = options
            p.view["sizes"] = [{"size": o["size"], "label": o["label"]} for o in options]
    if "seed_strategy" not in fixed:
        p.ask.append("seed_strategy")
    if slots.get("hires"):
        hires_on_possible = "hires" not in fixed or bool(fixed["hires"]["value"])
        if "hires" not in fixed:
            p.ask.append("hires")
        p.view["hires"] = {"default_on": ctx.config.hires_default}
        for name, rng in (("hires_steps", s.hires.steps if s.hires else None), ("hires_denoise", s.hires.denoise if s.hires else None)):
            b = bounds(rng)
            if name in fixed or b is None or not hires_on_possible:
                continue
            p.view["hires"][name] = {"recommended": b[2], "min": b[0], "max": b[1]}
            if b[0] != b[1]:
                p.ask.append(name)
    p.view["warnings"] = card.warnings
    return p


def apply_reply(ctx: PlanContext, wf: dict, card: WorkflowCard, fixed: dict, p: Plan, reply: dict) -> dict:
    """Clamp the LLM's answers into fields (reply is {} when it didn't answer: recommended values are used)."""
    new, d, s = p.new, (wf["slots"].get("defaults") or {}), card.settings
    answered = bool(reply)
    why = lambda name: ((reply.get("reasons") or {}).get(f"why_{name}") or "").strip() if answered else FALLBACK_REASON
    for name, rng, integer in (("steps", s.steps, True), ("cfg", s.cfg, False)):
        if name in p.ask:
            value, note = clamp_range(reply.get(name), rng, integer)
            new[name] = locks.auto(value, why(name), note)
    for name in ("sampler", "scheduler"):
        if name in p.ask:
            value, note = clamp_choice(reply.get(name), p.enums[name], d.get(name))
            new[name] = locks.auto(value, why(name), note)
    if "size" in p.ask:
        asked = parse_size(reply.get("size")) or ((d.get("width"), d.get("height")) if d.get("width") and d.get("height") else (p.options[0]["w"], p.options[0]["h"]))
        w, h, note = clamp_size(*asked, p.options)
        for key, value in (("width", w), ("height", h)):
            if key not in fixed:
                new[key] = locks.auto(value, why("size"), note)
    if "seed_strategy" in p.ask:
        strategy = reply.get("seed_strategy")
        new["seed_strategy"] = locks.auto(strategy if strategy in SEED_STRATEGIES else "random", why("seed_strategy") or "Different seeds for every image.")
    if "hires" in p.ask:
        wanted = reply.get("hires")
        new["hires"] = locks.auto(ctx.config.hires_default if wanted is None else bool(wanted), why("hires") or "The default from settings.")
    hires_on = bool((fixed.get("hires") or new.get("hires") or {}).get("value", ctx.config.hires_default)) if wf["slots"].get("hires") else False
    if hires_on and s.hires:
        for name, rng, integer in (("hires_steps", s.hires.steps, True), ("hires_denoise", s.hires.denoise, False)):
            b = bounds(rng)
            if name in fixed or b is None:
                continue
            if b[0] == b[1]:
                new[name] = locks.auto(round(b[2]) if integer else b[2], f"The card allows only {number_text(b[2])}.")
            else:
                value, note = clamp_range(reply.get(name), rng, integer)
                new[name] = locks.auto(value, why("hires") or "Within the card's hires range.", note)
    return new


async def pick_settings(ctx: PlanContext, job: dict, wf: dict, card: WorkflowCard, fixed: dict, lora_value: list[dict],
                        prompts: list[str], rng: random.Random | None = None) -> tuple[dict, list[dict]]:
    """(new fields, issues): steps, cfg, sampler, scheduler, width/height, seed strategy (and seed), hires."""
    p = plan_choices(ctx, wf, card, fixed)
    issues, reply = [], {}
    if p.ask:
        decided = {**locks.locked_values(fixed), **{n: f["value"] for n, f in p.new.items()}}
        drop = [n for n in ASKABLE if n not in p.ask] + [f"why_{n}" for n in WHY if n not in p.ask]
        people = describe_subjects(job.get("subjects"))
        inputs = {"job": {**job_view(job), **({"people": people} if people else {}), "loras": [i["name"] for i in lora_value], "first_prompts": [t[:240] for t in prompts[:2]]},
                  "card": block("workflow card", p.view), "locked": decided}
        reply = await ctx.llm.call("pick_settings", inputs, p.enums, None, drop)
        if reply.get("needs_attention"):
            log.warning("pick_settings needs attention: %s", reply["error"])
            issues.append(make_issue("settings", f"Settings choice failed, the card's recommended values are used: {reply['error']}", "warning", "llm_failed"))
            reply = {}
    new = apply_reply(ctx, wf, card, fixed, p, reply)
    strategy = (fixed.get("seed_strategy") or new.get("seed_strategy") or {}).get("value", "random")
    if strategy != "random" and "seed" not in fixed:
        new["seed"] = locks.auto((rng or random).randrange(SEED_MAX), "A random starting seed, shared by the images as the strategy says.")
    return new, issues


# ---------- locked values outside the card ----------

def warn_locked(ctx: PlanContext, wf: dict, card: WorkflowCard, fixed: dict) -> list[dict]:
    """Warnings (not errors) for locked values the card doesn't allow: a lock is never changed, but you should know."""
    out, s = [], card.settings
    ranges = {"steps": s.steps, "cfg": s.cfg, "hires_steps": s.hires.steps if s.hires else None, "hires_denoise": s.hires.denoise if s.hires else None}
    for name, rng in ranges.items():
        b = bounds(rng)
        if name in fixed and b and not (b[0] <= fixed[name]["value"] <= b[1]):
            allows = f"only {number_text(b[0])}" if b[0] == b[1] else f"{number_text(b[0])} to {number_text(b[1])}"
            out.append(make_issue(name, f"Your locked value {number_text(fixed[name]['value'])} is outside what the card allows ({allows}). It is kept as you set it.",
                                  "warning", "locked_outside_card"))
    for name, card_list, comfy_list in (("sampler", s.samplers, ctx.samplers), ("scheduler", s.schedulers, ctx.schedulers)):
        options = allowed(card_list, comfy_list)
        if name in fixed and options and fixed[name]["value"] not in options:
            out.append(make_issue(name, f"Your locked {name} '{fixed[name]['value']}' is not one the card allows.", "warning", "locked_outside_card"))
    for item in (fixed.get("loras") or {}).get("value") or []:
        lora = ctx.lora_card(item["name"])
        if lora:
            weight, note = clamp_lora_weight(item.get("weight", 1.0), lora.weight)
            if note:
                out.append(make_issue("loras", f"Your locked weight {number_text(item.get('weight', 1.0))} for {item['name']} is outside its range (it would be {number_text(weight)}).",
                                      "warning", "locked_outside_card"))
    return out
