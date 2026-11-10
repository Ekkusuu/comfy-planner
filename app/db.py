"""SQLite schema and a few query helpers (stdlib sqlite3, no ORM). JSON columns hold dicts/lists."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,                 -- t2i | edit
    runnable INTEGER NOT NULL,
    missing TEXT NOT NULL,              -- JSON list of missing files/nodes
    warnings TEXT NOT NULL,             -- JSON list (conversion warnings + slot problems)
    notes TEXT NOT NULL,                -- JSON list of Note/MarkdownNote texts
    slots TEXT NOT NULL,                -- JSON slot map
    slots_confirmed INTEGER NOT NULL DEFAULT 0,
    cover_image_id INTEGER,             -- image pinned as the card cover (NULL = newest done image)
    api_path TEXT NOT NULL,             -- our converted copy under data/workflows/
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT REFERENCES workflows(id),   -- NULL: the planner couldn't settle on a workflow (see issues)
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,               -- draft|planning|ready|queued|running|done|failed|cancelled
    count INTEGER NOT NULL,
    fields TEXT NOT NULL,               -- JSON {name: {value, source, reason}}
    position REAL NOT NULL DEFAULT 0,   -- queue order
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    plan_id INTEGER,                    -- the planning run that made or last re-planned this job
    issues TEXT NOT NULL DEFAULT '[]',  -- JSON [{field, severity: blocking|warning, code, message}]
    meta TEXT NOT NULL DEFAULT '{}',    -- JSON {request, reason, constraints, workflow: {source: "locked", reason}}
    cleared_at TEXT,                    -- set when a finished job is cleared from the Queue page (its images stay)
    batch_id INTEGER                    -- the plan (request) that made this job, when that request gave 2 or more images
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                 -- plan | replan
    request TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,               -- queued|waiting|loading|planning|done|failed|cancelled
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    job_ids TEXT NOT NULL DEFAULT '[]', -- JSON
    timings TEXT NOT NULL DEFAULT '[]', -- JSON [{job_id, step, seconds}]
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,                    -- when the plan left the line and was taken up (NULL: still waiting, or removed from the line)
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,               -- 1-based number inside the job
    status TEXT NOT NULL,               -- pending|running|done|failed|cancelled
    seed INTEGER NOT NULL,
    prompt_id TEXT,
    file_path TEXT,                     -- relative to data/images
    recipe TEXT NOT NULL,               -- JSON: everything needed to reproduce this image
    error TEXT,
    rating INTEGER,                     -- reserved for feedback (+1 / -1)
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (job_id, idx)
);
CREATE INDEX IF NOT EXISTS images_job ON images(job_id);
CREATE TABLE IF NOT EXISTS cards (
    kind TEXT NOT NULL,                 -- workflow | lora
    key TEXT NOT NULL,                  -- workflow id | LoRA file name as ComfyUI lists it
    status TEXT NOT NULL,               -- draft | approved
    card TEXT NOT NULL,                 -- JSON (the pydantic model for this kind)
    pending TEXT,                       -- JSON: a re-generated card waiting for the user to accept or reject it
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS file_hashes (   -- SHA256 cache: valid while path, size and mtime are unchanged
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lora_extras (   -- what the user adds to a LoRA; never touched by a card build, so a rebuild can't wipe it
    key TEXT PRIMARY KEY,               -- LoRA file name as ComfyUI lists it
    user_tags TEXT NOT NULL DEFAULT '[]',   -- JSON list. The card's own `tags` are the file's training tags, rewritten on every build
    cover_kind TEXT,                    -- NULL = automatic | image (a gallery image) | upload (a file the user gave)
    cover_image_id INTEGER,             -- the gallery image, when cover_kind = image
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lora_preview_misses (   -- LoRAs whose Civitai preview couldn't be had; not asked again until the next Rescan
    sha256 TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wildcard_lists (   -- one row per imported list; enabled / role only when the user changed them
    name TEXT PRIMARY KEY,              -- path without .txt, for example background/background-location
    source TEXT NOT NULL DEFAULT '',    -- the folder it was imported from
    enabled INTEGER,                    -- NULL = on
    role TEXT,                          -- NULL = the folder's default role (config [wildcards.roles])
    imported_at TEXT NOT NULL
);
"""

JSON_COLUMNS = {"missing", "warnings", "notes", "slots", "fields", "recipe", "card", "pending", "issues", "meta", "job_ids", "timings", "user_tags"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Columns added after the first release, for databases created before them."""
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(workflows)")}
        if "cover_image_id" not in columns:
            self.conn.execute("ALTER TABLE workflows ADD COLUMN cover_image_id INTEGER")
            self.conn.commit()
        if "issues" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}:
            self._rebuild_jobs()
        if "cleared_at" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN cleared_at TEXT")
            self.conn.commit()
        if "batch_id" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN batch_id INTEGER")
            self.conn.commit()
        if "started_at" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(plans)")}:
            self.conn.execute("ALTER TABLE plans ADD COLUMN started_at TEXT")
            self.conn.commit()
        self.conn.execute("CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch_id)")
        self.conn.commit()

    def _rebuild_jobs(self):
        """Planning needs jobs.workflow_id to allow NULL and three new columns. SQLite can't relax NOT NULL in place, so
        build the new table next to it, copy the rows, drop the old one and rename (SQLite's documented way; foreign keys
        are off meanwhile, and the images table keeps pointing at the name 'jobs')."""
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.executescript("""
                BEGIN;
                CREATE TABLE jobs_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT REFERENCES workflows(id),
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    fields TEXT NOT NULL,
                    position REAL NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    plan_id INTEGER,
                    issues TEXT NOT NULL DEFAULT '[]',
                    meta TEXT NOT NULL DEFAULT '{}'
                );
                INSERT INTO jobs_new (id, workflow_id, description, status, count, fields, position, error, created_at, started_at, finished_at)
                    SELECT id, workflow_id, description, status, count, fields, position, error, created_at, started_at, finished_at FROM jobs;
                DROP TABLE jobs;
                ALTER TABLE jobs_new RENAME TO jobs;
                COMMIT;
            """)
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def rows(self, sql: str, params=()) -> list[dict]:
        return [decode(r) for r in self.conn.execute(sql, params).fetchall()]

    def row(self, sql: str, params=()) -> dict | None:
        r = self.conn.execute(sql, params).fetchone()
        return decode(r) if r else None

    def close(self):
        self.conn.close()


def decode(row: sqlite3.Row) -> dict:
    """Row -> dict, with JSON columns parsed."""
    out = dict(row)
    for key in JSON_COLUMNS & out.keys():
        if out[key] is not None:
            out[key] = json.loads(out[key])
    return out


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
