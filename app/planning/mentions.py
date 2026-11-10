"""LoRA mentions: `@` + a known LoRA file name, without `.safetensors`, in the request names that LoRA.
Plain functions, no I/O. Anything else that starts with `@` (`@velden mara`, `@brkt`) is an artist or style tag and is left alone."""
import re

GENERIC_NAME_WORDS = {"style", "styles", "mixed", "anima", "anime", "base", "lora", "model", "krea", "illustrious", "pony", "epoch"}   # say nothing about the LoRA itself
EXTENSION = re.compile(r"\.(safetensors|ckpt|pt)$", re.I)


def stem(key: str) -> str:
    """The name typed after the `@`: the file name without its folder and extension."""
    return EXTENSION.sub("", key.replace("\\", "/").rsplit("/", 1)[-1])


def pattern(keys: list[str]) -> re.Pattern | None:
    """One regex for every known stem, longest first. The `@` must not be part of a longer word, and the name must end at a
    character that can't belong to a name (so `@foo` doesn't match inside `@foobar` or `@foo-v2`, but does before `.`, `,` or `)`)."""
    stems = sorted({stem(k) for k in keys if stem(k)}, key=lambda s: (-len(s), s))
    if not stems:
        return None
    names = "|".join(r"\s+".join(map(re.escape, s.split())) for s in stems)
    return re.compile(rf"(?<![\w@])@({names})(?![\w-]|\.\w)", re.I)


def scan(text: str, keys: list[str]) -> list[tuple[int, int, str]]:
    """[(start, end, file)] of every mention, in the order they are written. A stem that two files share names the first."""
    found = pattern(keys)
    by_stem: dict[str, str] = {}
    for key in keys:
        by_stem.setdefault(re.sub(r"\s+", " ", stem(key)).lower(), key)
    return [(m.start(), m.end(), by_stem[re.sub(r"\s+", " ", m.group(1)).lower()]) for m in found.finditer(text or "")] if found else []


def mentioned(text: str, keys: list[str]) -> list[str]:
    """The LoRA files the text names, each once, in the order they are first written."""
    return list(dict.fromkeys(key for _, _, key in scan(text, keys)))


def readable(key: str) -> str:
    """A name a person (and the LLM) can read: "style-a-anima-base-epoch-05" is "style a". Words that only say which model
    family or version it is are left out (all words stay if nothing else is left)."""
    words = [w for w in re.split(r"[-_\s]+", stem(key)) if w]
    kept = [w for w in words if w.lower().rstrip("0123456789") not in GENERIC_NAME_WORDS | {"v", "e", ""}]
    return " ".join(kept or words)


def plain(text: str, keys: list[str], numbered: list[str] | None = None) -> str:
    """The text with each mention replaced by plain words ("use @style-a-anima-base-epoch-05" becomes "use the chosen
    LoRA"), so the token and the file's name words can never reach a prompt. The original text is kept elsewhere
    (job.meta.request). With `numbered` (the mentioned files, in order) each reads "the chosen LoRA [2]" instead: the request
    understanding step (intent.py) answers "LoRA 2" for it, and code maps that back to the file."""
    found = scan(text, keys)
    for start, end, key in reversed(found):
        mark = f" [{numbered.index(key) + 1}]" if numbered and key in numbered else ""
        text = f"{text[:start]}the chosen LoRA{mark}{text[end:]}"
    text = re.sub(r"\b(?:the\s+)+(the chosen LoRA)", r"\1", text, flags=re.I)         # "without the @x" is not "without the the chosen LoRA"
    return re.sub(r"\b(the chosen LoRA(?: \[\d+\])?)\s+(?:loras?)\b", r"\1", text, flags=re.I)


def unnamed(text: str, keys: list[str]) -> str:
    """The text without the words `plain` wrote for the mentioned LoRAs ("the chosen LoRA"): what the job itself says. The word
    "LoRA" in them must not count as the job asking for a LoRA, which would let the planner add others without a reason."""
    text = re.sub(r"\bthe\s+chosen\s+LoRA(?:\s*\[\d+\])?", "", text, flags=re.I)
    text = re.sub(r"\bthe\s+named\s+LoRA\b|\bthe\s+LoRA\s+(?:you|I|the\s+user)\s+named\b"
                  r"|\bLoRA\s+named\s+by\s+the\s+user\b", "", text, flags=re.I)
    for key in keys:        # jobs planned before the wording changed
        words = r"\s+".join(map(re.escape, readable(key).split()))
        text = re.sub(rf"(?:\bthe\s+)?{words}\s+LoRA\b", "", text, flags=re.I)
    return " ".join(text.split())
