"""Locks. A locked field is shown to the LLM as a fixed fact, removed from the schema so the model can't
answer it, and never written over. Everything here is plain code."""
from app.fields import make_field, prompt_locks

# every field the planner can fill, in the order the pipeline fills them
LORA_FIELDS = ("loras",)
PROMPT_FIELDS = ("prompts", "negative")
SETTING_FIELDS = ("steps", "cfg", "sampler", "scheduler", "width", "height", "seed_strategy", "seed", "hires", "hires_steps", "hires_denoise")
PLANNED_FIELDS = (*LORA_FIELDS, *PROMPT_FIELDS, *SETTING_FIELDS)


def locked(fields: dict) -> dict:
    """The locked fields of a job: {name: field}."""
    return {name: f for name, f in (fields or {}).items() if f.get("source") == "locked"}


def locked_values(fields: dict) -> dict:
    """{name: value} of the locked fields, for the "locked" block the prompts show the LLM."""
    return {name: f["value"] for name, f in locked(fields).items() if f.get("value") is not None}


def start_replan(fields: dict) -> dict:
    """What a re-plan starts from: only the locked fields. Auto fields are dropped so nothing stale survives."""
    return {name: dict(f) for name, f in locked(fields).items()}


def kept_prompts(fields: dict, count: int) -> dict[int, dict]:
    """Prompts locked one by one: {index: {"text", "reason"}}. Empty when the prompts are locked as a whole (those are
    in `locked`) or when none is locked. A re-plan writes the other prompts around these."""
    field = (fields or {}).get("prompts")
    if not field or field.get("source") == "locked":
        return {}
    items = field.get("items") or []
    return {i: {"text": field["value"][i], "reason": (items[i].get("reason") if i < len(items) else "") or "Locked by you",
                **({"ideas": items[i]["ideas"]} if i < len(items) and items[i].get("ideas") else {})}
            for i, flag in enumerate(prompt_locks(field)) if flag and i < count and field["value"][i].strip()}


def merge(fields: dict, new: dict) -> dict:
    """Add the planner's fields to `fields`, but never over a locked one. `new` is {name: field dict}."""
    out = dict(fields)
    for name, field in new.items():
        if out.get(name, {}).get("source") == "locked":
            continue
        out[name] = field
    return out


def one_line(text: str) -> str:
    """A reason as one short line: first line only, spaces tidied (small models sometimes ramble past the sentence)."""
    line = " ".join((text or "").strip().splitlines()[0].split()) if (text or "").strip() else ""
    return line if len(line) <= 300 else line[:300].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def auto(value, reason: str = "", note: str = "", items: list | None = None, locks: list | None = None) -> dict:
    return make_field(value, "auto", one_line(reason), note, [{**i, "reason": one_line(i.get("reason", ""))} for i in items] if items else None, locks)
