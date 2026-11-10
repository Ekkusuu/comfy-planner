"""What the user adds to a LoRA, kept apart from its card: their own tags and their choice of cover.

It lives in its own table (`lora_extras`), so tagging a LoRA never changes the card or its approval, and building or
re-generating a card never wipes tags or covers. The card's own `tags` field is the file's training tags, written by
code on every build, so it can't hold the user's tags.
"""
import re

from app.db import Database, dumps, now

MAX_TAG_CHARS = 40
MAX_TAGS = 30


def normalize_tag(tag: str) -> str:
    """Trimmed, lower case, inner whitespace collapsed to single spaces, cut to MAX_TAG_CHARS. Tags are data, so
    control characters are dropped. "" means nothing is left."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(tag))
    return re.sub(r"\s+", " ", text).strip().lower()[:MAX_TAG_CHARS].strip()


def normalize_tags(tags) -> list[str]:
    """Normalised, without empty or repeated tags, in the order given, at most MAX_TAGS."""
    return list(dict.fromkeys(t for t in map(normalize_tag, tags) if t))[:MAX_TAGS]


class LoraExtras:
    def __init__(self, db: Database):
        self.db = db
        self._used: tuple[tuple, dict[str, int]] | None = None     # (jobs signature, counts)

    # ---------- read ----------

    def get(self, key: str) -> dict | None:
        return self.db.row("SELECT * FROM lora_extras WHERE key = ?", (key,))

    def all(self) -> dict[str, dict]:
        return {r["key"]: r for r in self.db.rows("SELECT * FROM lora_extras")}

    def tags_by_key(self) -> dict[str, list[str]]:
        """{file: the user's tags}, for the files that have any (the planner adds them to its search text)."""
        return {r["key"]: r["user_tags"] for r in self.db.rows("SELECT key, user_tags FROM lora_extras") if r["user_tags"]}

    def used_counts(self) -> dict[str, int]:
        """{file: finished jobs that used it}. Read from the jobs' LoRA fields; kept until a job finishes or goes away."""
        sig = tuple(self.db.row("SELECT COUNT(*) AS n, MAX(id) AS last, MAX(finished_at) AS at FROM jobs WHERE status = 'done'").values())
        if self._used is None or self._used[0] != sig:
            counts: dict[str, int] = {}
            for row in self.db.rows("SELECT fields FROM jobs WHERE status = 'done'"):
                for name in {l.get("name") for l in (row["fields"].get("loras") or {}).get("value") or [] if isinstance(l, dict)}:
                    if name:
                        counts[name] = counts.get(name, 0) + 1
            self._used = (sig, counts)
        return self._used[1]

    # ---------- write ----------

    def _row(self, key: str) -> dict:
        if self.get(key) is None:
            self.db.execute("INSERT INTO lora_extras (key, updated_at) VALUES (?, ?)", (key, now()))
        return self.get(key)

    def change_tags(self, keys: list[str], add: list[str] = (), remove: list[str] = ()) -> dict[str, list[str]]:
        """Add and remove tags on each file; returns {file: its tags now}. Only this table is touched."""
        add, remove = normalize_tags(add), set(normalize_tags(remove))
        out = {}
        for key in dict.fromkeys(keys):
            row = self._row(key)
            tags = normalize_tags([*(t for t in row["user_tags"] if t not in remove), *add])
            self.db.execute("UPDATE lora_extras SET user_tags = ?, updated_at = ? WHERE key = ?", (dumps(tags), now(), key))
            out[key] = tags
        return out

    def set_cover(self, key: str, kind: str | None, image_id: int | None = None):
        """kind: image (a gallery image id) | upload (the uploaded file is stored by LoraCovers) | None (automatic)."""
        self._row(key)
        self.db.execute("UPDATE lora_extras SET cover_kind = ?, cover_image_id = ?, updated_at = ? WHERE key = ?",
                        (kind, image_id if kind == "image" else None, now(), key))
