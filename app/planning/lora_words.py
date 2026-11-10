"""Taking a LoRA's words out of the prompts when the LoRA leaves the job (the chip's X, a re-plan that drops it).

A LoRA card names the words its LoRA needs in a prompt: `trigger_words` and `style_tags`. Once the LoRA is gone they are
dead weight in the prompt (or worse: `@pine-owl` asks for a style nobody loads). Code removes them - whole tags in a tag
prompt, whole phrases in a sentence prompt, ignoring case - except the words another LoRA still on the job needs. Locked
prompts are cleaned too: the user removed the LoRA, so its words go; the prompt's reason says so."""
import re

from app.fields import WEIGHT_TAIL, bracket_pairs, split_tags, tag_key
from app.library.card_models import LoraCard


def lora_label(name: str) -> str:
    return re.sub(r"\.(safetensors|ckpt|pt)$", "", name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1], flags=re.I)


def words_of(card: LoraCard) -> list[str]:
    """The words the LoRA needs in a prompt: trigger words and style tags, trimmed, without repeats."""
    return list(dict.fromkeys(w.strip() for w in [*card.trigger_words, *card.style_tags] if w and w.strip()))


def words_to_remove(removed: dict[str, LoraCard], kept: dict[str, LoraCard]) -> dict[str, list[str]]:
    """{removed LoRA: its words that no remaining LoRA needs}. LoRAs without any left are left out."""
    still_needed = {tag_key(w) for card in kept.values() for w in words_of(card)}
    out = {name: [w for w in words_of(card) if tag_key(w) not in still_needed] for name, card in removed.items()}
    return {name: words for name, words in out.items() if words}


def trim_group(tag: str, drop: set[str]) -> str:
    """A weight group that holds several tags, `(a, @x:1.2)`, without the dropped ones ("" if nothing is left). Any other
    tag comes back as it was."""
    if not (tag.startswith("(") and tag.endswith(")") and bracket_pairs(tag).get(0) == len(tag) - 1):
        return tag
    inner = tag[1:-1]
    m = WEIGHT_TAIL.fullmatch(inner)
    body, tail = (m.group(1), inner[len(m.group(1)):]) if m else (inner, "")
    raw = [t.strip() for t in split_tags(body) if t.strip()]
    kept = [t for t in (trim_group(t, drop) for t in raw) if t and tag_key(t) not in drop]
    return tag if kept == raw else (f"({', '.join(kept)}{tail})" if kept else "")


def remove_words(prompt: str, words: list[str], tags: bool) -> str:
    """The prompt without `words`. A tag prompt (comma-separated) loses whole tags (a "(tag:1.2)" weight wrapper doesn't
    matter); any other prompt loses whole phrases. Leftover commas and spaces are tidied. Unchanged text comes back as it was."""
    drop = {tag_key(w) for w in words if w.strip()}
    if not drop:
        return prompt
    if tags:
        parts = [t.strip() for t in split_tags(prompt) if t.strip()]
        kept = [t for t in (trim_group(t, drop) for t in parts) if t and tag_key(t) not in drop]
        return prompt if kept == parts else ", ".join(kept)
    text = prompt
    for word in sorted(drop, key=len, reverse=True):
        text = re.sub(rf"(?<![\w@]){re.escape(word)}(?![\w-])", " ", text, flags=re.I)
    if text == prompt:
        return prompt
    text = re.sub(r"\(\s*(?::[\d.]+)?\s*\)", " ", text)               # an emptied "( :1.2)" weight wrapper
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;])(\s*[,;])+", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,;")


def clean_prompts(field: dict, removed: dict[str, LoraCard], kept: dict[str, LoraCard], tags: bool,
                  only: set[int] | None = None) -> tuple[dict, list[int]]:
    """(the `prompts` field with the removed LoRAs' words taken out, the numbers (0-based) of the prompts that changed).
    `only` limits it to those prompts. A changed prompt's reason gets "removed trigger words of <LoRA>" added (locked or not).
    Everything else in the field - locks, source, other reasons, wildcard ideas - stays."""
    per_lora = words_to_remove(removed, kept)
    value = list(field.get("value") or [])
    if not per_lora or not value:
        return field, []
    items = [dict(i) if isinstance(i, dict) else {"reason": ""} for i in field.get("items") or []]
    items = (items + [{"reason": ""}] * len(value))[:len(value)]
    changed = []
    for i, text in enumerate(value):
        if only is not None and i not in only:
            continue
        who = []
        for name, words in per_lora.items():
            new = remove_words(text, words, tags)
            if new != text:
                text, who = new, [*who, lora_label(name)]
        if who:
            value[i] = text
            note = "removed trigger words of " + ", ".join(who)
            items[i]["reason"] = f"{items[i].get('reason') or ''}; {note}".lstrip("; ")
            changed.append(i)
    if not changed:
        return field, []
    return {**field, "value": value, "items": items}, changed
