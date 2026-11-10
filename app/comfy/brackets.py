"""Literal brackets in prompts.

ComfyUI's CLIP-style text encoders read `(x)` as emphasis (x1.1) and `(x:1.2)` as a weight, and the brackets then vanish
from the text the model sees. On Anima that eats the brackets of `@marlow (blue sedge)` or `aria vale (starlight)`.
`\\(` and `\\)` are ComfyUI's escape for a bracket that is only a bracket.

`protect_literal_brackets` is applied at the last moment, to the text that goes into the graph copy, and only for a
workflow whose encoder parses weights (`parses_weights`). Stored prompts, recipes and LoRA words stay unescaped.
On Krea-2 and Qwen-Image the parser is off (`disable_weights=True` in ComfyUI), so a backslash would reach the model as a
character: there nothing is escaped.
"""
from app.comfy.slots import TEXT_NODES, is_link

# What is known not to parse weights in ComfyUI 0.43 (comfy/text_encoders/*: disable_weights=True). Anything not listed parses,
# which is ComfyUI's default (CLIP, SDXL, and Anima, whose Qwen3 weights are 1.0 but whose T5 weights are applied).
# Keyed on the graph, not on the card's family (the card is written by an LLM and can be a draft):
#  - the CLIPLoader `type` of the encoder: "krea2" (krea2.py), plus the other loader types whose encoder sets disable_weights.
#    "qwen_image" is NOT listed: Anima loads through that type too (the file decides), and Anima parses.
#  - the encode node class: Qwen-Image 2.1 edit's TextEncodeQwenImage21 (qwen3vl.py:187).
NO_WEIGHT_CLIP_TYPES = {"krea2", "z_image", "lumina2", "ovis", "longcat_image", "ernie", "flux2", "ideogram4"}
NO_WEIGHT_ENCODE_PREFIXES = ("TextEncodeQwenImage",)
CLIP_PASS = {"LoraLoader": "clip"}      # nodes a clip passes through on its way from the loader to the encoder


def _clip_type(graph: dict, link, hops: int = 8):
    """The `type` of the CLIPLoader that feeds `link`, following a LoRA's clip pass-through."""
    while is_link(link) and link[0] in graph and hops:
        node = graph[link[0]]
        if "type" in node["inputs"] and node["class_type"].endswith("Loader"):
            return node["inputs"]["type"]
        link, hops = node["inputs"].get(CLIP_PASS.get(node["class_type"], "clip")), hops - 1
    return None


def parses_weights(graph: dict, slots: dict | None = None) -> bool:
    """Does this workflow's text encoder read `(x:1.2)`? False if any of its text-encode nodes is known not to."""
    for node in graph.values():
        kind = node["class_type"]
        if kind not in TEXT_NODES and not kind.startswith(NO_WEIGHT_ENCODE_PREFIXES):
            continue
        if kind.startswith(NO_WEIGHT_ENCODE_PREFIXES) or _clip_type(graph, node["inputs"].get("clip")) in NO_WEIGHT_CLIP_TYPES:
            return False
    return True


# ---------- the escaping ----------

STARTS = (None, ",", "\n")      # what may stand before a "(" that opens a bare emphasis group: start of text, a comma, a new line
CONNECTORS = {"and", "with", "or"}      # "((Eevee)) with ((tan body)) and (whale tail)": after these a group is emphasis, not a qualifier


def _float_ok(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _tokens(text: str) -> list[str]:
    """Characters, with an already escaped bracket (`\\(` or `\\)`) as one token."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and text[i + 1:i + 2] in ("(", ")"):
            out.append(text[i:i + 2])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return out


def _kept_brackets(tokens: list[str]) -> set[int]:
    """Indexes of the "(" and ")" tokens that stay real brackets (ComfyUI emphasis); the rest are only brackets."""
    partner: dict[int, int] = {}
    stack: list[int] = []
    for i, t in enumerate(tokens):
        if t == "(":
            stack.append(i)
        elif t == ")" and stack:
            j = stack.pop()
            partner[j], partner[i] = i, j
    keep: set[int] = set()      # indexes of the "(" and ")" that stay real brackets
    for i in sorted(j for j, t in enumerate(tokens) if t == "(" and j in partner):
        close = partner[i]
        inner = "".join(tokens[i + 1:close])
        if not inner.strip():
            continue
        colon = inner.rfind(":")
        weighted = colon > 0 and _float_ok(inner[colon + 1:])
        before = next((tokens[k] for k in range(i - 1, -1, -1) if not tokens[k].isspace()), None)
        before_at = next((k for k in range(i - 1, -1, -1) if not tokens[k].isspace()), None)
        words = "".join(tokens[:i]).split()
        doubled = tokens[i + 1:i + 2] == ["("] and partner.get(i + 1) == close - 1
        if before in STARTS or doubled or (words and words[-1].lower() in CONNECTORS):
            at_start = True
        elif before in ("(", ")") and before_at in partner:      # inside or right after a group: only if that group is kept
            at_start = min(before_at, partner[before_at]) in keep
        else:
            at_start = False
        if weighted or at_start:
            keep.update((i, close))
    return keep


def protect_literal_brackets(text: str | None) -> str | None:
    """`text` with the brackets that are only brackets escaped as `\\(` and `\\)`.

    Kept as ComfyUI weights: a group with a number (`(x:1.2)`), a bare `(x)` at the start of a tag (start of the text, or
    after a comma or a new line) or after a connecting word (`with ((tan body))`), and a doubled `((x))`, which nobody writes
    as a qualifier: all deliberate emphasis. Escaped: a group that directly follows a word in the same tag
    (`name (qualifier)`), an empty group, and any bracket without a partner. Brackets inside a kept group are decided the
    same way, so `(aria vale (starlight):1.2)` becomes `(aria vale \\(starlight\\):1.2)`. Brackets that are already
    escaped stay as they are, so running it twice changes nothing."""
    if not text or ("(" not in text and ")" not in text):
        return text
    tokens = _tokens(text)
    keep = _kept_brackets(tokens)
    return "".join(("\\" + t if t in "()" and i not in keep else t) if len(t) == 1 else t for i, t in enumerate(tokens))


def emphasis_groups(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    """(tokens, groups): the weight groups ComfyUI would read in `text`, by the same rules as `protect_literal_brackets`.
    `tokens` are the characters (an escaped bracket is one token); each group is (index of its "(", index of its ")"), outer
    groups before the groups inside them. Literal brackets (`name (qualifier)`, a lone bracket) are not groups."""
    tokens = _tokens(text)
    keep = _kept_brackets(tokens)
    groups, stack = [], []
    for i in sorted(keep):
        if tokens[i] == "(":
            stack.append(i)
        else:
            groups.append((stack.pop(), i))
    return tokens, sorted(groups)
