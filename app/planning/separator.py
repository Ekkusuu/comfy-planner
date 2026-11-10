"""The request separator: `---` on its own line or inline, in the user's request, ends one part and starts the next.
Each part is planned on its own, as if it were a request of its own, so the LLM can never merge two parts.
`===` after a general instruction applies that instruction to every part: "watercolor, use the ink LoRA === a knight --- a cabin"."""
import re

from app.wildcards.expand import find_tokens

# three or more hyphens with white space (or the start or end of the text) on both sides: "a --- b", a line of "-----".
# "well-known", "a---b", "---not" and a "--" or "-" are not separators.
SEPARATOR = re.compile(r"(?<!\S)-{3,}(?!\S)")
SHARED = re.compile(r"(?<!\S)={3,}(?!\S)")     # the same rules, with equals signs


def masked_tokens(text: str) -> str:
    """The text with its wildcard tokens blanked out, same length: a separator inside `{a --- b|c}` doesn't count."""
    for token in find_tokens(text):
        text = text.replace(token, "x" * len(token))
    return text


def parse_request(text: str) -> tuple[str, list[str]]:
    """(the general instruction for all parts, the parts). The first `===` ends the general instruction; "" if there is none.
    With nothing after the `===` there is nothing to apply it to, so its text is the request itself."""
    text = text or ""
    m = SHARED.search(masked_tokens(text))
    if not m:
        return "", split_request(text)
    shared, rest = text[:m.start()].strip(), text[m.end():].strip()
    return (shared, split_request(rest)) if split_request(rest) else ("", split_request(shared))


def split_request(text: str) -> list[str]:
    """The parts of a request, trimmed, without empty ones. A separator inside a wildcard token (`{a --- b|c}`) doesn't count;
    the text before the first separator is the first part, so a request without a separator is one part."""
    text = text or ""
    masked = masked_tokens(text)
    parts, start = [], 0
    for m in SEPARATOR.finditer(masked):
        parts.append(text[start:m.start()])
        start = m.end()
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]
