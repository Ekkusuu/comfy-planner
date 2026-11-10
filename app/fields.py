"""Job field helpers: the {value, source, reason} shape, safety negative merge, seeds, basic clamping."""
import random
import re

SEED_MAX = 2**53          # keep seeds exactly representable in JSON / JS
SEED_STRATEGIES = ("random", "fixed", "increment")


def make_field(value, source: str = "auto", reason: str = "", note: str = "", items: list | None = None, locks: list | None = None,
               base=None) -> dict:
    """A job field: the value, who set it ("auto" or "locked") and why. `note` records a clamp. `items` (optional) is a
    one-line reason per entry of a list value, e.g. what makes each image prompt different. `locks` (prompts only) says
    which of the prompts are locked one by one; see `settle_prompts`. `base` (the negative only) is the value before a LoRA's
    "remove from negative" words were taken out of it; see planning/lora_negative.py."""
    out = {"value": value, "source": source, "reason": reason}
    if note:
        out["note"] = note
    if items:
        out["items"] = items
    if locks is not None:
        out["locks"] = locks
    if base is not None:
        out["base"] = base
    return out


def make_issue(field: str, message: str, severity: str = "blocking", code: str = "", **extra) -> dict:
    """A problem on a job card. `field` says where it shows ("workflow", "prompts", "steps", ... or "job").
    "blocking" stops the job from being queued; "warning" is only shown."""
    return {"field": field, "severity": severity, "code": code or field, "message": message, **extra}


def blocking(issues: list[dict]) -> list[dict]:
    return [i for i in issues if i.get("severity") == "blocking"]


def normalize_fields(raw: dict) -> dict:
    """Accept plain values or full field dicts; plain values count as set by hand (locked)."""
    out = {}
    for name, item in (raw or {}).items():
        if isinstance(item, dict) and "value" in item:
            out[name] = make_field(item["value"], item.get("source", "locked"), item.get("reason", ""), item.get("note", ""),
                                   item.get("items"), item.get("locks"), item.get("base"))
        else:
            out[name] = make_field(item, "locked", "set by hand")
    return out


def prompt_locks(field: dict | None) -> list[bool]:
    """Which prompts of a `prompts` field are locked, one flag per prompt. A field locked as a whole locks them all."""
    if not field:
        return []
    value = field.get("value") or []
    locks = field.get("locks")
    if isinstance(locks, list):      # shorter after the count grew: the new prompts are not locked; longer after it shrank: cut
        return ([bool(x) for x in locks] + [False] * len(value))[:len(value)]
    return [field.get("source") == "locked"] * len(value)


def settle_prompts(field: dict, locks: list[bool] | None = None) -> dict:
    """The `prompts` field in its stored shape: `locks` and `items` have one entry per prompt, and `source` is "locked"
    only when every prompt is locked (a partly locked field is "auto"; the per-prompt flags say which ones stay)."""
    value = field.get("value") or []
    flags = prompt_locks(field) if locks is None else [bool(x) for x in locks]
    flags = (flags + [False] * len(value))[:len(value)]
    items = [i if isinstance(i, dict) else {"reason": ""} for i in field.get("items") or []]
    items = (items + [{"reason": ""}] * len(value))[:len(value)] if items else []
    field = {**field, "locks": flags, "source": "locked" if flags and all(flags) else "auto"}
    if items:
        field["items"] = items
    else:
        field.pop("items", None)
    return field


def plain_values(fields: dict) -> dict:
    """{name: value} from a fields dict."""
    return {name: f["value"] for name, f in fields.items()}


# ---------- safety negative ----------

WEIGHT_TAIL = re.compile(r"(.*?)\s*:\s*-?(?:\d+\.?\d*|\.\d+)\s*", re.S)      # "x:1.2", "x:-0.5", "x:.5"


def bracket_pairs(text: str) -> dict[int, int]:
    """{index of a "(": index of its ")"} and the reverse, for the brackets that have a partner. A bracket without one
    (the `:(` of "smile :(", a lone `(`) is not in it, and neither is a bracket escaped with a backslash."""
    pairs, stack = {}, []
    for i, ch in enumerate(text):
        if ch not in "()" or (i and text[i - 1] == "\\"):
            continue
        if ch == "(":
            stack.append(i)
        elif stack:
            j = stack.pop()
            pairs[j], pairs[i] = i, j
    return pairs


def tag_key(tag: str) -> str:
    """Compare tags ignoring case and any weight wrapper: (x:1.2), (x:-0.5), (x), ((x)), ((x):1.3)."""
    tag = tag.strip().lower()
    while tag.startswith("(") and tag.endswith(")") and bracket_pairs(tag).get(0) == len(tag) - 1:
        inner = tag[1:-1].strip()
        m = WEIGHT_TAIL.fullmatch(inner)
        tag = (m.group(1).strip() if m and m.group(1).strip() else inner)
    return tag


def split_tags(text: str) -> list[str]:
    """Split on commas that are not inside a pair of brackets, so "(a, b:1.2)" stays one tag. A bracket without a partner
    ("smile :(, 1girl") or an escaped one is only a character."""
    pairs = bracket_pairs(text)
    tags, depth, current = [], 0, ""
    for i, ch in enumerate(text):
        if i in pairs:
            depth += 1 if ch == "(" else -1
        if ch == "," and depth <= 0:
            tags.append(current)
            current = ""
        else:
            current += ch
    return [*tags, current]


def remove_tags(text: str | None, remove: list[str]) -> str | None:
    """`text` without the tags in `remove` (compared like merge_negative does); None stays None."""
    if not text or not remove:
        return text
    drop = {tag_key(t) for t in remove}
    return ", ".join(t.strip() for t in split_tags(text) if t.strip() and tag_key(t) not in drop)


def is_weighted(tag: str) -> bool:
    """Does the tag carry weight syntax, like (x:1.2) or ((x))?"""
    return tag_key(tag) != tag.strip().lower()


def merge_negative(negative: str | None, safety: list[str]) -> str:
    """Comma-separated negative prompt with the safety tags added; duplicates removed. Of two copies of a tag the first
    stays in place, but a weighted copy beats a plain one: `bad hands` in the workflow's own negative and
    `(bad hands:1.5)` added later gives `(bad hands:1.5)`."""
    tags, seen = [], {}
    for tag in [*split_tags(negative or ""), *safety]:
        tag = tag.strip()
        key = tag_key(tag)
        if not tag:
            continue
        if key not in seen:
            seen[key] = len(tags)
            tags.append(tag)
        elif is_weighted(tag) and not is_weighted(tags[seen[key]]):
            tags[seen[key]] = tag
    return ", ".join(tags)


# ---------- seeds ----------

def seed_for_image(strategy: str, base: int | None, index: int, rng: random.Random | None = None) -> int:
    """Seed of the index-th image: random, fixed (same for all) or increment (base + index)."""
    rng = rng or random
    if strategy == "random" or base is None:
        return rng.randrange(SEED_MAX)
    if strategy == "increment":
        return base + index
    return base


# ---------- basic clamping ----------

def clamp_number(value, lo, hi):
    """(clamped value, note or ''). The note says what was asked and what was used."""
    clamped = min(max(value, lo), hi)
    return clamped, ("" if clamped == value else f"asked {value}, clamped to {clamped}")


def round_size(value: int, multiple: int = 8, lo: int = 64, hi: int = 4096):
    """(size rounded to a multiple within [lo, hi], note)."""
    rounded = min(max(round(value / multiple) * multiple, lo), hi)
    return rounded, ("" if rounded == value else f"asked {value}, set to {rounded}")
