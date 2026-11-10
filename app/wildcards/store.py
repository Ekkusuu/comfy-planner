"""The wildcard library: lists copied from a Forge-style folder into data/wildcards/, plus the user's per-list settings
(on/off and role) kept in the `wildcard_lists` table. The source folder is only ever read.

A role the user sets is a decision and always wins. Without one a list has its folder's default role, which is only a hint: what a list
serves is read from its entries (planning/serving.py)."""
import fnmatch
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Config
from app.db import Database, now
from app.wildcards.parse import MAX_FILE_BYTES, parse_entries, parse_simple_yaml

MAX_FILES = 2000
OTHER_ROLE = "other"
SAMPLE_COUNT = 5


class WildcardError(Exception):
    """A wildcard request that can't be done; the message is meant for the user."""


@dataclass
class WildList:
    name: str                       # path without ".txt", with "/" (for example "background/background-location")
    folder: str                     # first part of the name, "" for a list in the top folder
    role: str
    default_role: str
    enabled: bool
    entries: list[str] = field(default_factory=list)
    source: str = ""
    role_set: bool = False          # the user set the role (it is not the folder's default): that decision always wins

    @property
    def base(self) -> str:
        return self.name.rsplit("/", 1)[-1]

    def summary(self) -> dict:
        return {"name": self.name, "folder": self.folder, "role": self.role, "default_role": self.default_role, "enabled": self.enabled,
                "count": len(self.entries), "samples": self.entries[:SAMPLE_COUNT], "source": self.source, "role_set": self.role_set}


class Catalog:
    """A snapshot of the lists, for resolving `__name__` references."""

    def __init__(self, lists: list[WildList]):
        self.lists = lists
        self.by_name = {l.name.lower(): l for l in lists}

    def resolve(self, ref: str) -> tuple[list[WildList], str]:
        """(the lists a reference means, "") or ([], why not). A reference is the full name (`background/background-location`),
        a short name that only one list has (`background-location`), or a pattern with `*` (`artist*top`, drawing from every match)."""
        key = ref.strip().lower().strip("/")
        if key in self.by_name:
            return [self.by_name[key]], ""
        if "*" in key:
            found = [l for l in self.lists if fnmatch.fnmatchcase(l.name.lower(), key) or fnmatch.fnmatchcase(l.base.lower(), key)]
            return (found, "") if found else ([], f"no wildcard list matches “{ref}”")
        short = [l for l in self.lists if l.base.lower() == key or l.name.lower().endswith("/" + key)]
        if len(short) == 1:
            return short, ""
        if short:
            return [], f"“{ref}” matches several lists ({', '.join(l.name for l in short[:4])}); use the full name"
        return [], f"there is no wildcard list called “{ref}”"


class WildcardStore:
    def __init__(self, db: Database, config: Config):
        self.db, self.config = db, config
        self._cache: dict[str, tuple[int, int, list[str]]] = {}      # file -> (mtime_ns, size, entries)

    @property
    def root(self) -> Path:
        return self.config.wildcards_dir

    # ---------- reading ----------

    def _entries(self, path: Path) -> list[str]:
        stat = path.stat()
        cached = self._cache.get(str(path))
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
        entries = parse_entries(path.read_text(encoding="utf-8", errors="replace"))
        self._cache[str(path)] = (stat.st_mtime_ns, stat.st_size, entries)
        return entries

    def default_role(self, folder: str) -> str:
        return self.config.wildcards_roles.get(folder.lower(), OTHER_ROLE) if folder else OTHER_ROLE

    def roles(self) -> list[str]:
        """Every role a list can have: the configured ones, the slot roles and "other"."""
        return sorted({*self.config.wildcards_roles.values(), *self.config.wildcards_slot_roles, OTHER_ROLE})

    def lists(self) -> list[WildList]:
        if not self.root.is_dir():
            return []
        rows = {r["name"]: r for r in self.db.rows("SELECT * FROM wildcard_lists")}
        out = []
        for path in sorted(self.root.rglob("*.txt")):
            name = path.relative_to(self.root).with_suffix("").as_posix()
            folder = name.split("/", 1)[0] if "/" in name else ""
            row = rows.get(name) or {}
            default = self.default_role(folder)
            out.append(WildList(name, folder, row.get("role") or default, default, row["enabled"] != 0 if row.get("enabled") is not None else True,
                                self._entries(path), row.get("source") or "", bool(row.get("role"))))
        return out

    def catalog(self) -> Catalog:
        return Catalog(self.lists())

    def get(self, name: str) -> WildList:
        found = next((l for l in self.lists() if l.name.lower() == name.strip().lower().strip("/")), None)
        if found is None:
            raise KeyError(name)
        return found

    def has_lists(self) -> bool:
        return self.root.is_dir() and next(self.root.rglob("*.txt"), None) is not None

    # ---------- settings per list ----------

    def update(self, name: str, enabled: bool | None = None, role: str | None = None) -> WildList:
        """Switch a list on or off and/or change its role (an empty role goes back to the folder's default)."""
        found = self.get(name)
        self._set([found.name], enabled, role)
        return self.get(found.name)

    def update_folder(self, folder: str, enabled: bool | None = None, role: str | None = None) -> list[WildList]:
        """The same change for every list of a folder."""
        names = [l.name for l in self.lists() if l.folder.lower() == folder.strip().lower()]
        if not names:
            raise KeyError(folder)
        self._set(names, enabled, role)
        return [l for l in self.lists() if l.name in names]

    def _set(self, names: list[str], enabled: bool | None, role: str | None):
        if role is not None and role != "" and role not in self.roles():
            raise WildcardError(f"Unknown role “{role}”. Use one of: {', '.join(self.roles())}.")
        for name in names:
            self.db.execute("INSERT OR IGNORE INTO wildcard_lists (name, source, imported_at) VALUES (?, '', ?)", (name, now()))
            if enabled is not None:
                self.db.execute("UPDATE wildcard_lists SET enabled = ? WHERE name = ?", (int(enabled), name))
            if role is not None:
                self.db.execute("UPDATE wildcard_lists SET role = ? WHERE name = ?", (role or None, name))

    # ---------- import ----------

    def import_folder(self, path: str) -> dict:
        """Copy a wildcard folder (the whole tree: .txt lists, and simple .yaml/.yml lists turned into .txt) into
        data/wildcards/. Importing the same folder again replaces its copy; your per-list settings are kept."""
        text = (path or "").strip().strip('"')
        if not text:
            raise WildcardError("Enter the folder with your wildcard files.")
        src = Path(text).expanduser()
        if not src.is_dir():
            raise WildcardError(f"That folder doesn't exist: {text}")
        src = src.resolve()
        root = self.root.resolve()
        if src == root or root in src.parents or src in root.parents:
            raise WildcardError("Pick the folder your wildcards come from, not the app's own copy of them.")
        found, skipped = self._gather(src)
        if not found:
            raise WildcardError("No wildcard lists found there (looking for .txt files with one entry per line).")

        source = str(src)
        old = {r["name"] for r in self.db.rows("SELECT name FROM wildcard_lists WHERE source = ?", (source,))}
        for name in old - found.keys():             # lists that are gone from the source are gone from the copy
            self._file(name).unlink(missing_ok=True)
            self.db.execute("DELETE FROM wildcard_lists WHERE name = ?", (name,))
        total = 0
        for name, (origin, text_) in found.items():
            target = self._file(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if origin is not None:
                shutil.copyfile(origin, target)
            else:
                target.write_text(text_, encoding="utf-8")
            self.db.execute("INSERT INTO wildcard_lists (name, source, imported_at) VALUES (?,?,?) "
                            "ON CONFLICT(name) DO UPDATE SET source = excluded.source, imported_at = excluded.imported_at", (name, source, now()))
            total += len(self._entries(target))
        return {"source": source, "lists": len(found), "entries": total, "skipped": skipped, "removed": len(old - found.keys())}

    def path_of(self, name: str) -> Path:
        """Where the list's file is in the app's copy."""
        return self._file(name)

    def forget(self, name: str):
        """The list's file has been deleted: drop its settings row and cached entries, and its folder when that is now empty."""
        path = self._file(name)
        self.db.execute("DELETE FROM wildcard_lists WHERE name = ?", (name,))
        self._cache.pop(str(path), None)
        root = self.root.resolve()
        for folder in path.parents:
            if folder == root or folder.resolve() == root or any(folder.iterdir()):
                break
            folder.rmdir()

    def _file(self, name: str) -> Path:
        target = (self.root / f"{name}.txt").resolve()
        if self.root.resolve() not in target.parents:
            raise WildcardError(f"Not a valid list name: {name}")
        return target

    def _gather(self, src: Path) -> tuple[dict[str, tuple[Path | None, str]], int]:
        """({list name: (file to copy, or None with the text to write), skipped count}) for a source folder."""
        found: dict[str, tuple[Path | None, str]] = {}
        skipped = 0
        for file in sorted(src.rglob("*")):
            rel = file.relative_to(src)
            if file.is_symlink() or not file.is_file() or any(p.startswith(".") for p in rel.parts):
                continue
            kind = file.suffix.lower()
            if kind not in (".txt", ".yaml", ".yml"):
                skipped += 1
                continue
            if file.stat().st_size > MAX_FILE_BYTES or len(found) >= MAX_FILES:
                skipped += 1
                continue
            base = rel.with_suffix("").as_posix()
            if kind == ".txt":
                if parse_entries(file.read_text(encoding="utf-8", errors="replace")):
                    found.setdefault(base, (file, ""))
                else:
                    skipped += 1
                continue
            lists = parse_simple_yaml(file.read_text(encoding="utf-8", errors="replace"))
            if not lists:
                skipped += 1
            for key, items in lists.items():
                name = f"{base}/{_safe(key)}" if key else base
                found.setdefault(name, (None, "\n".join(items)))
        return found, skipped


def _safe(part: str) -> str:
    return re.sub(r"[^\w\- ]", "_", part).strip() or "list"
