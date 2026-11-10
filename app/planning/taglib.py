"""The Danbooru tag library: real tag names with their category and how often they are used, aliases, and
implications. It is plain data read from `data/taglib/` (made by `scripts/update_taglib.py`), treated as untrusted text: it is
only ever compared and looked up, never run, and its text is never passed on as instructions.

Without the folder (a fresh install) `load_taglib` returns None and every caller skips its tag work quietly.
Tags are stored the Danbooru way, with underscores; `key()` turns any spelling into that form."""
import csv
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("planning.tags")
GENERAL, ARTIST, COPYRIGHT, CHARACTER, META = 0, 1, 3, 4, 5
CATEGORY_NAMES = {GENERAL: "general", ARTIST: "artist", COPYRIGHT: "series", CHARACTER: "character", META: "meta"}
# a tag must be used this often to be offered to the LLM (see `pool`)
POOL_MIN = {GENERAL: 1000, CHARACTER: 2000, COPYRIGHT: 3000, ARTIST: 500}
MAX_FILE = 30_000_000        # bytes; a bigger tags.csv is not read


@dataclass(frozen=True)
class Hit:
    name: str           # the canonical tag, with underscores
    category: int
    count: int
    via: str            # "exact" or "alias"


def key(text: str) -> str:
    """Any spelling of a tag in the library's form: lower case, words joined by single underscores."""
    return re.sub(r"[\s_]+", "_", text.strip().lower()).strip("_")


def spaced(name: str) -> str:
    """A library tag the way a prompt writes it with spaces (`long_hair` -> `long hair`)."""
    return name.replace("_", " ")


class TagLib:
    def __init__(self, tags: dict[str, tuple[int, int]], aliases: dict[str, str] | None = None, implications: list[tuple[str, str]] | None = None):
        self.tags = tags
        self.aliases = aliases or {}
        self.implied: dict[str, list[str]] = {}
        for a, b in implications or []:
            self.implied.setdefault(a, []).append(b)
        self._words: dict[str, list[str]] | None = None

    def __len__(self) -> int:
        return len(self.tags)

    def lookup(self, text: str, aliases: bool = True) -> Hit | None:
        """The library tag a text is: exactly, or (when `aliases`) through a known alias."""
        k = key(text)
        if k in self.tags:
            return Hit(k, *self.tags[k], "exact")
        target = self.aliases.get(k) if aliases else None
        if target in self.tags:
            return Hit(target, *self.tags[target], "alias")
        return None

    def category_of(self, text: str) -> int | None:
        k = key(text)
        return self.tags[k][0] if k in self.tags else None

    def artist(self, text: str) -> Hit | None:
        """The artist tag for a name written in either order: `Mara Velden` finds `velden_mara`."""
        k = key(text)
        options = [k]
        parts = k.split("_")
        if len(parts) == 2:
            options.append(f"{parts[1]}_{parts[0]}")
        for option in options:
            hit = self.lookup(option)
            if hit and hit.category == ARTIST:
                return hit
        return None

    def parents(self, name: str) -> list[str]:
        """What a tag implies on Danbooru (`3girls` -> `multiple_girls`)."""
        return self.implied.get(key(name), [])

    # ---------- the tags worth offering ----------

    def pool(self) -> list[str]:
        """Tags used often enough to be offered to the LLM, by category thresholds (about 15 000 on the real list)."""
        return [n for n, (cat, count) in self.tags.items() if count >= POOL_MIN.get(cat, 10**12)]

    def word_index(self) -> dict[str, list[str]]:
        """{word: pool tags that contain it}, built on first use."""
        if self._words is None:
            index: dict[str, list[str]] = {}
            for name in self.pool():
                for word in set(re.findall(r"[a-z0-9]+", name)):
                    index.setdefault(word, []).append(name)
            self._words = index
        return self._words


# ---------- reading the folder ----------

def parse_tags(raw: str) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
    """(tags, aliases) from the CSV text: name, category, count, "alias,alias". Malformed rows are skipped."""
    tags: dict[str, tuple[int, int]] = {}
    pending: list[tuple[str, str, int]] = []
    for row in csv.reader(raw.splitlines()):
        try:
            name, cat, count = key(row[0]), int(row[1]), int(row[2])
        except (ValueError, IndexError):
            continue
        if not name or cat not in CATEGORY_NAMES:
            continue
        tags[name] = (cat, count)
        for alias in (row[3].split(",") if len(row) > 3 else []):
            if alias.strip() and not alias.startswith("/"):       # "/ngirls" style shortcuts are not tags
                pending.append((key(alias), name, count))
    aliases: dict[str, tuple[str, int]] = {}
    for alias, name, count in pending:
        if alias and alias not in tags and (alias not in aliases or count > aliases[alias][1]):
            aliases[alias] = (name, count)
    return tags, {a: v[0] for a, v in aliases.items()}


def load_taglib(folder: Path) -> TagLib | None:
    """The library in `folder`, or None when it isn't there or isn't usable (the caller then does no tag work)."""
    csv_path = folder / "tags.csv"
    try:
        if not csv_path.is_file() or csv_path.stat().st_size > MAX_FILE:
            return None
        tags, aliases = parse_tags(csv_path.read_text(encoding="utf-8"))
        pairs: list[tuple[str, str]] = []
        implications = folder / "implications.json"
        if implications.is_file() and implications.stat().st_size <= MAX_FILE:
            pairs = [(key(a), key(b)) for a, b in json.loads(implications.read_text(encoding="utf-8")) if isinstance(a, str) and isinstance(b, str)]
    except (OSError, ValueError, UnicodeDecodeError) as e:
        log.warning("the tag library in %s could not be read, tag checks are off: %s", folder, e)
        return None
    if len(tags) < 1000:
        return None
    log.info("tag library loaded: %d tags, %d aliases, %d implications", len(tags), len(aliases), len(pairs))
    return TagLib(tags, aliases, pairs)


_loaded: dict[str, tuple[float, TagLib | None]] = {}


def get_taglib(folder: Path) -> TagLib | None:
    """`load_taglib`, kept in memory while the folder's files don't change (a new download is picked up without a restart)."""
    path = folder / "tags.csv"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    cached = _loaded.get(str(folder))
    if cached is None or cached[0] != stamp:
        cached = _loaded[str(folder)] = (stamp, load_taglib(folder))
    return cached[1]
