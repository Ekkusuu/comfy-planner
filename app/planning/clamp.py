"""Clamping: whatever the LLM answers, code makes it fit the workflow card and ComfyUI's own lists.

Every function returns (value, note). The note says what was asked and what was used, or is "" when nothing changed.
"""
import math

from app.library.card_models import Range, WorkflowCard

LORA_WEIGHT_DEFAULT = Range(rec=1.0, min=0.5, max=1.2)
SIZE_MULTIPLE = 64


def bounds(r: Range | None) -> tuple[float, float, float] | None:
    """(lo, hi, rec) of a card range. A missing bound falls back to the recommended value (the only one known to work);
    a range with nothing in it is None, which means "no limit known"."""
    if r is None or (r.rec is None and r.min is None and r.max is None):
        return None
    rec = r.rec if r.rec is not None else (r.min if r.min is not None else r.max)
    lo = r.min if r.min is not None else rec
    hi = r.max if r.max is not None else rec
    return lo, hi, rec


def pinned(r: Range | None) -> bool:
    """True when the card allows exactly one value."""
    b = bounds(r)
    return b is not None and b[0] == b[1]


def clamp_range(value, r: Range | None, integer: bool = False):
    """Value limited to the card's [min, max]. Without a known range the value is kept. None gives the recommended value."""
    b = bounds(r)
    if value is None:
        return (round(b[2]) if integer else b[2]) if b else None, ""
    value = round(value) if integer else float(value)
    if b is None:
        return value, ""
    clamped = min(max(value, b[0]), b[1])
    clamped = round(clamped) if integer else round(clamped, 4)
    return clamped, ("" if clamped == value else f"asked {value:g}, clamped to {clamped:g}")


def clamp_choice(value, allowed: list[str], fallback: str | None = None):
    """A sampler or scheduler must be in the allowed list; otherwise the fallback (or the first allowed one) is used."""
    if not allowed or value in allowed:
        return value, ""
    used = fallback if fallback in allowed else allowed[0]
    return used, f"asked {value}, not available; used {used}"


def clamp_lora_weight(value, r: Range | None):
    """LoRA weight within the LoRA card's range (a card with no range uses 0.5 to 1.2; one with only a recommended
    strength allows half to 1.2 times of it)."""
    b = bounds(r) or bounds(LORA_WEIGHT_DEFAULT)
    if r is not None and r.rec is not None and r.min is None and r.max is None:
        b = (r.rec * 0.5, r.rec * 1.2, r.rec)
    weight = round(min(max(float(value), b[0]), b[1]), 3)
    return weight, ("" if weight == round(float(value), 3) else f"asked {float(value):g}, clamped to {weight:g}")


# ---------- sizes ----------

def round_to_multiple(value: int, multiple: int = SIZE_MULTIPLE) -> int:
    return max(multiple, round(value / multiple) * multiple)


def size_options(card: WorkflowCard, default: tuple[int, int] | None) -> list[dict]:
    """Sizes the planner may choose: exactly the card's own list (the sizes its model is known to support), or the
    workflow's default size when the card lists none. Other shapes are never invented: a model can fail at a size
    nobody named. [{"size": "1024x1536", "w", "h", "label"}]"""
    base = [(z.w, z.h, z.label) for z in card.settings.sizes] or ([(*default, "workflow default")] if default else [])
    out: dict[tuple[int, int], str] = {}
    for w, h, label in base:
        out.setdefault((w, h), label or "card size")
    return [{"size": f"{w}x{h}", "w": w, "h": h, "label": label} for (w, h), label in out.items()]


def clamp_size(w: int, h: int, options: list[dict]):
    """(w, h, note): the exact size if it is one of the options, else the option closest in shape and area."""
    if not options or any((o["w"], o["h"]) == (w, h) for o in options):
        return w, h, ""
    best = min(options, key=lambda o: abs(math.log((o["w"] / o["h"]) / (w / h))) * 2 + abs(math.log((o["w"] * o["h"]) / (w * h))))
    return best["w"], best["h"], f"asked {w}x{h}, not allowed; used {best['w']}x{best['h']}"
