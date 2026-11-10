"""Negative words a LoRA works better without. A LoRA card can list tags to take out of the negative prompt of every
job that uses the LoRA ("remove from negative"). Plain functions, no I/O; the job store applies them in one place
(`JobStore.apply_lora_negative`), so planning, re-planning, tweaks and hand edits all end with the same negative.

The negative field of a job keeps the value before any removal as `base`: the field's `value` is what the job card shows
and ComfyUI gets, and a LoRA that leaves the job gives its words back because the value is rebuilt from `base`."""
import re

from app.fields import make_field, make_issue, split_tags, tag_key
from app.planning.mentions import readable

# a word like "no", "without" or "negative:" starts a list of things the person does not want: "no painting, cartoon or blur"
AVOID = re.compile(r"\b(?:no|not|without|avoid|avoiding|never|exclude|excluding|negative(?:\s+prompt)?(?:\s+tags?)?)\b[\s:]*([^.;!?\n]*)", re.I)
SPLIT = re.compile(r",|\band\b|\bor\b", re.I)
ARTICLES = re.compile(r"^(?:a|an|the|any|more|extra)\s+", re.I)
POLITE = re.compile(r"\s+(?:please|thanks|thank you)$", re.I)


def word_key(text: str) -> str:
    """A tag to compare: lower case, no weight, underscores and spaces alike. `(Oil_Painting:1.2)` is `oil painting`."""
    return re.sub(r"[\s_]+", " ", tag_key(text)).strip()


def asked_to_avoid(*texts: str) -> set[str]:
    """The words (as `word_key`s) the person wrote as things they do not want, in text they typed: "no painting, cartoon" gives
    painting and cartoon. Whole comma- or "and"-separated pieces only, so "a girl without a hat" gives "hat"."""
    found = set()
    for text in texts:
        for match in AVOID.finditer(text or ""):
            for piece in SPLIT.split(match.group(1)):
                key = word_key(POLITE.sub("", ARTICLES.sub("", piece.strip())))
                if key:
                    found.add(key)
    return found


def take_out(negative: str, wanted: dict[str, list[str]], keep: set[str]) -> tuple[str, list[tuple[str, str]]]:
    """(the negative without the tags the LoRAs want gone, [(tag as written, LoRA)] removed). `wanted` is {LoRA: words}. Only
    whole comma-separated tags match, ignoring case, weights and underscores. Tags whose key is in `keep` stay. Nothing removed:
    the negative comes back exactly as it was."""
    owner: dict[str, str] = {}
    for lora, words in wanted.items():
        for word in words:
            owner.setdefault(word_key(word), lora)
    kept, removed = [], []
    for tag in (t.strip() for t in split_tags(negative or "")):
        key = word_key(tag)
        if key and key in owner and key not in keep:
            removed.append((tag, owner[key]))
        elif tag:
            kept.append(tag)
    return (", ".join(kept), removed) if removed else (negative, [])


def removal_note(removed: list[tuple[str, str]]) -> str:
    """"removed `painting`, `crayon`: style-a asks for it" (one clause per LoRA)."""
    by_lora: dict[str, list[str]] = {}
    for tag, lora in removed:
        by_lora.setdefault(lora, [])
        if tag not in by_lora[lora]:
            by_lora[lora].append(tag)
    return "; ".join(f"removed {', '.join(f'`{t}`' for t in tags)}: {readable(lora)} asks for it" for lora, tags in by_lora.items())


def apply_to_field(field: dict | None, default: str, wanted: dict[str, list[str]], safety: set[str], asked: set[str]) -> tuple[dict | None, list[dict]]:
    """(the negative field as it should be stored, issues). `field` is the job's negative field (None: the job uses the workflow's
    `default`), `wanted` the words of the LoRAs on the job, `safety` the keys of the safety tags (never removed: the graph adds
    them back) and `asked` the keys of words the person asked to avoid (kept). The field comes back None when there is none
    and nothing to remove.

    - locked by you: it stays word for word, and a warning says if a LoRA's words are in it;
    - otherwise the value is rebuilt from `base` (the value before an earlier removal), so a LoRA that left gives its words back."""
    if field is not None and field.get("source") == "locked":
        field = {k: v for k, v in field.items() if k not in ("base", "note")} if "base" in field else field      # the lock freezes what is there
        _, removed = take_out(field.get("value") or "", wanted, safety)
        if not removed:
            return field, []
        names = "; ".join(dict.fromkeys(f"{readable(lora)} asks to remove `{tag}`" for tag, lora in removed))
        return field, [make_issue("negative", f"The negative prompt is locked, so it was left as it is: {names}.", "warning", "lora_negative_locked")]
    had_base = field is not None and "base" in field
    base = field["base"] if had_base else (field.get("value") if field is not None else None)
    base = default if base is None else base
    text, removed = take_out(base, wanted, safety | asked)
    if field is None:
        return (make_field(text, "auto", "The workflow's own negative prompt.", removal_note(removed), base=base), []) if removed else (None, [])
    out = {k: v for k, v in field.items() if k != "base" and not (had_base and k == "note")}
    if removed:
        out.update(value=text, base=base, note=removal_note(removed))
    elif had_base:
        out["value"] = base
    return out, []
