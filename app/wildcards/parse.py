"""Reading wildcard files. A wildcard list is a text file with one entry per line (Forge "Dynamic Prompts" style).

The files are somebody else's text, so every entry is treated as untrusted: control characters are removed and an
entry that is too long is dropped (not cut, so a `{a|b}` group can't be torn in half)."""
import re

MAX_ENTRY_CHARS = 200
MAX_FILE_BYTES = 2_000_000          # a wildcard file larger than this is not a list somebody wrote by hand
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f  ]")


def clean_entry(line: str) -> str:
    """One entry: control characters removed, spaces tidied. Empty if the line is blank or a `#` comment."""
    line = CONTROL.sub("", line.replace("\t", " ").replace("﻿", "")).strip()
    return "" if line.startswith("#") else re.sub(r" {2,}", " ", line)


def parse_entries(text: str) -> list[str]:
    """The entries of a file: blanks and comments dropped, over-long entries dropped, duplicates (any case) removed."""
    seen, out = set(), []
    for line in text.splitlines():
        entry = clean_entry(line)
        if not entry or len(entry) > MAX_ENTRY_CHARS or entry.lower() in seen:
            continue
        seen.add(entry.lower())
        out.append(entry)
    return out


def parse_simple_yaml(text: str) -> dict[str, list[str]]:
    """Lists from a simple YAML wildcard file: `key:` followed by `- item` lines, or a bare `- item` list ("" is the key
    of a bare list). Nested mappings and anything fancier are skipped; this is not a YAML parser."""
    lists: dict[str, list[str]] = {}
    key = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item:
            value = item.group(1).strip().strip("\"'")
            if value:
                lists.setdefault(key, []).append(value)
            continue
        head = re.match(r"^([A-Za-z0-9_][\w\- ]*):\s*$", line)       # a top-level key with nothing after it
        key = head.group(1).strip() if head else "\0skip"
    return {k: v for k, v in lists.items() if k != "\0skip"}
