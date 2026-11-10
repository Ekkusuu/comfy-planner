"""Prompt weights: where `(tag:1.4)` may be written, and the code that enforces it after the LLM.

Weights are allowed only when the workflow card says its prompts are tags or a mix of tags and prose (`prompt_style.kind` is
`tags` or `mixed`) AND the graph's
text encoder reads weights (`parses_weights`). Everywhere else a weight would be read as text, or does nothing.

The LLM is told the rule (config/prompts/weights_on.md or weights_off.md, filled into `{{weights}}`), but this code is the
source of truth. It works on what the LLM wrote, never on prompts a person wrote or edited:

- not allowed: every weight / emphasis group becomes the plain text (`(forest:1.5)` -> `forest`); literal brackets such as
  `aria vale (starlight)` stay.
- allowed: a weight is a single group `(text:number)`, nothing nested. Numbers go into 1.2..1.8 (a bare `(x)` or `((x))`
  becomes 1.4); weights of 1 or less are unwrapped; at most 3 stay (the first ones); a group that touches the quality prefix,
  a trigger word or a style tag is unwrapped. The negative prompt gets no weights from the LLM.
- weights the person typed (in the request, a `===` instruction, a part or a constraint) are theirs: on an allowed workflow
  they are kept exactly as typed, with any number and outside the limit of 3, and put back if the LLM dropped them; on a
  workflow that reads weights as text they are removed and the job card says so.
"""
import re
from dataclasses import dataclass, field

from app.comfy.brackets import emphasis_groups, parses_weights
from app.fields import WEIGHT_TAIL, tag_key
from app.library.card_models import WorkflowCard
from app.llm import client

MIN_WEIGHT, MAX_WEIGHT, DEFAULT_WEIGHT = 1.2, 1.8, 1.4
MAX_WEIGHTS = 3             # LLM weights per prompt
HOLD = "{}"     # stands in for a group the person typed while the rest of the prompt is cleaned


def weights_allowed(card: WorkflowCard, graph: dict) -> bool:
    """May the LLM write `(tag:1.4)` for this workflow? Only for a tags or mixed card whose encoder really reads weights.
    Both must hold, so a wrongly written card can't send weights to an encoder that would read them as text."""
    return card.prompt_style.kind in ("tags", "mixed") and parses_weights(graph)


def rule_text(allowed: bool) -> str:
    """The text for `{{weights}}` in write_prompts.md and apply_tweak.md: the owner edits these two files."""
    return (client.PROMPTS_DIR / ("weights_on.md" if allowed else "weights_off.md")).read_text(encoding="utf-8").strip()


@dataclass
class WeightRules:
    allowed: bool                                       # does this workflow read weights?
    typed: dict[str, str] = field(default_factory=dict)  # the person's own groups: {tag in lower case: group as typed}
    put_back: bool = True       # a typed group the LLM left out is added again (not in a tweak: the instruction may remove it)


# ---------- reading groups ----------

def _body(group: str) -> tuple[str, float | None]:
    """(text inside a group without its number, the number or None) of `(text:1.4)` or `(text)`."""
    inner = group[1:-1]
    m = WEIGHT_TAIL.fullmatch(inner)
    if m:
        return m.group(1).strip(), float(inner[len(m.group(1)):].strip().lstrip(":"))
    return inner.strip(), None


def _rewrite(text: str, decide) -> str:
    """`text` with each weight group replaced by what `decide(group, depth, rank, before, after)` returns (groups inside
    others first, so a group's text already has its inner groups dealt with). `rank` is the group's place in the text."""
    tokens, groups = emphasis_groups(text)
    depth = {g: sum(1 for o in groups if o[0] < g[0] and g[1] < o[1]) for g in groups}
    for rank, (i, j) in sorted(enumerate(groups), key=lambda x: x[1], reverse=True):
        new = decide("".join(tokens[i:j + 1]), depth[(i, j)], rank, "".join(tokens[:i]), "".join(tokens[j + 1:]))
        tokens[i:j + 1] = [new] + [""] * (j - i)
    return "".join(tokens)


def _tidy(text: str) -> str:
    """Empty tags left by a removed group, like "a, , b", closed up."""
    return re.sub(r"\s*,(\s*,)+", ",", text).strip(" ,")


def strip_weights(text: str) -> str:
    """`text` with every weight group reduced to its plain text; literal brackets stay."""
    if "(" not in text:
        return text
    return _tidy(_rewrite(text, lambda group, depth, rank, before, after: _body(group)[0]))


def typed_groups(*texts: str) -> dict[str, str]:
    """The weight groups the person wrote in these texts: {tag in lower case: the group exactly as typed}. A group nested in
    another counts as part of it."""
    found: dict[str, str] = {}
    for text in texts:
        if "(" not in text:
            continue
        tokens, groups = emphasis_groups(text)
        for i, j in groups:
            group = "".join(tokens[i:j + 1])
            if not any(o[0] < i and j < o[1] for o in groups) and tag_key(group):
                found.setdefault(tag_key(group), group)
    return found


# ---------- enforcing ----------

def _number(value: float) -> str:
    return f"{round(value, 2):g}"


def _touches(body: str, before: str, after: str, protected: list[str]) -> bool:
    """Does the group hold, or cut through, a quality tag, trigger word or style tag?"""
    plain, wrapped = f"{before[-60:]} {body} {after[:60]}".lower(), f"{before[-60:]}({body}){after[:60]}".lower()
    for word in protected:
        word = word.lower().strip()
        if not word:
            continue
        if word in body.lower() or re.search(rf"(?<!\w){re.escape(body.lower())}(?!\w)", word):
            return True
        if word in plain and word not in wrapped:        # the group splits it: "(monochrome ink wash:1.3) style"
            return True
    return False


def enforce(text: str, protected: list[str], rules: WeightRules) -> tuple[str, list[str]]:
    """(the prompt as the weights rules allow it, the tags whose weight was removed or put back as notes).
    `protected` are the quality prefix tags, trigger words and style tags, which are never weighted."""
    notes: list[str] = []       # filled from the end of the prompt to its start, so reversed at the end
    if not rules.allowed:
        return strip_weights(text), [f"weight removed from {_body(g)[0]}" for g in _top_groups(text)]
    held = {}

    def hold(group, depth, rank, before, after):        # the person's own groups wait outside the cleaning
        key = tag_key(group)
        if depth or key not in rules.typed:
            return group
        held[HOLD.format(len(held))] = rules.typed[key]
        return list(held)[-1]

    def clean(group, depth, rank, before, after):
        body, number = _body(group)
        if depth or not body:
            return body
        if number is None and ":" in body:          # "(red scarf:abc)": a number that is not one, so not a weight to repair
            notes.append(f"weight removed from {body}")
            return body
        number = DEFAULT_WEIGHT if number is None else number
        if number <= 1 or _touches(body, before, after, protected):
            notes.append(f"weight removed from {body}")
            return body
        return f"({body}:{_number(min(max(number, MIN_WEIGHT), MAX_WEIGHT))})"

    def limit(group, depth, rank, before, after):
        if rank < MAX_WEIGHTS:
            return group
        notes.append(f"weight removed from {_body(group)[0]}")
        return _body(group)[0]

    if "(" in text:
        text = _tidy(_rewrite(_rewrite(_rewrite(text, hold), clean), limit))
    for mark, group in held.items():
        text = text.replace(mark, group)
    notes.reverse()
    for key, group in rules.typed.items() if rules.put_back else []:       # a typed weight the LLM wrote plain, or left out, is put back
        if group in text:
            continue
        plain = re.search(rf"(?<![\w(]){re.escape(key)}(?![\w:)])", text, re.I)
        text = f"{text[:plain.start()]}{group}{text[plain.end():]}" if plain else ", ".join(t for t in (text.rstrip(" ,"), group) if t)
        notes.append(f"put back {group}")
    return text, notes


def _top_groups(text: str) -> list[str]:
    tokens, groups = emphasis_groups(text) if "(" in text else ([], [])
    return ["".join(tokens[i:j + 1]) for i, j in groups if not any(o[0] < i and j < o[1] for o in groups)]


def clean_negative(tags: list[str], rules: WeightRules, existing: list[str] | None = None) -> list[str]:
    """Negative tags the LLM wrote, without weights: `existing` tags (the workflow's own, already in the job) stay exactly as
    they are, and on an allowed workflow so does a weight the person typed. The LLM adds no weights to a negative."""
    keep = {t.strip() for t in existing or []}
    out = []
    for tag in tags:
        tag = tag.strip()
        key = tag_key(tag)
        if tag in keep:
            out.append(tag)
        elif rules.allowed and key in rules.typed and key != tag.lower():      # the person's own weight on this tag
            out.append(rules.typed[key])
        else:
            out.append(strip_weights(tag))
    return out
