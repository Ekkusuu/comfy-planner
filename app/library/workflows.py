"""Workflow registry: import a workflow file, keep our own converted API copy, edit its slot map."""
import json
import re
from pathlib import Path

from app.comfy.client import ComfyClient
from app.comfy.convert import ConvertError, check_runnable, convert, extract_notes
from app.comfy.slots import detect_slots
from app.config import Config
from app.db import Database, dumps, now
from app.fields import remove_tags


class WorkflowError(Exception):
    """Import or slot edit failed; the message is meant for the user."""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "workflow"


class WorkflowLibrary:
    def __init__(self, db: Database, config: Config, comfy: ComfyClient):
        self.db, self.config, self.comfy = db, config, comfy

    # ---------- import ----------

    async def import_path(self, path: str | Path) -> dict:
        """Read a workflow file (never modified), convert it, check models, detect slots, store a copy."""
        path = Path(path)
        if not path.is_file():
            raise WorkflowError(f"File not found: {path}")
        try:
            workflow = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise WorkflowError(f"{path.name} is not valid JSON: {e}") from e
        oi = await self.comfy.object_info()
        converted = _convert_or_fail(workflow, oi, path.name)
        missing = check_runnable(converted.graph, oi)
        slots = detect_slots(converted.graph)
        warnings = [*converted.warnings, *slots["problems"]]
        runnable = not missing and not slots["problems"]

        existing = self.db.row("SELECT id, slots, slots_confirmed FROM workflows WHERE source_path = ?", (str(path),))
        wf_id = existing["id"] if existing else self._free_id(slugify(path.stem))
        if existing and existing["slots_confirmed"]:
            slots = existing["slots"]   # keep the user's confirmed slot map
        api_path = self.config.workflows_dir / f"{wf_id}.api.json"
        api_path.parent.mkdir(parents=True, exist_ok=True)
        api_path.write_text(json.dumps(converted.graph, indent=1, ensure_ascii=False), encoding="utf-8")

        values = (wf_id, path.stem, str(path), slots["kind"], int(runnable), dumps(missing), dumps(warnings),
                  dumps(extract_notes(workflow)), dumps(slots), str(api_path), now())
        self.db.execute(
            """INSERT INTO workflows (id, title, source_path, kind, runnable, missing, warnings, notes, slots,
                                      api_path, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, kind=excluded.kind, runnable=excluded.runnable,
                 missing=excluded.missing, warnings=excluded.warnings, notes=excluded.notes, slots=excluded.slots,
                 api_path=excluded.api_path, updated_at=excluded.updated_at""",
            (*values, values[-1]))
        return self.get(wf_id)

    async def recheck(self) -> list[dict]:
        """Check every workflow's models and nodes again against ComfyUI's current lists (after models were
        added, or ComfyUI was restarted with other model folders)."""
        oi = await self.comfy.object_info(refresh=True)
        for wf in self.db.rows("SELECT * FROM workflows"):
            missing = check_runnable(self.get_graph(wf["id"]), oi)
            runnable = not missing and not wf["slots"].get("problems")
            self.db.execute("UPDATE workflows SET runnable = ?, missing = ?, updated_at = ? WHERE id = ?",
                            (int(runnable), dumps(missing), now(), wf["id"]))
        return self.list()

    def _free_id(self, base: str) -> str:
        taken = {r["id"] for r in self.db.rows("SELECT id FROM workflows")}
        wf_id, n = base, 2
        while wf_id in taken:
            wf_id, n = f"{base}-{n}", n + 1
        return wf_id

    # ---------- read ----------

    def list(self) -> list[dict]:
        return [_summary(self._with_cover(w)) for w in self.db.rows("SELECT * FROM workflows ORDER BY title")]

    def get(self, wf_id: str) -> dict:
        wf = self.db.row("SELECT * FROM workflows WHERE id = ?", (wf_id,))
        if wf is None:
            raise KeyError(wf_id)
        return self._with_cover(self._clean_default_negative(wf))

    def _clean_default_negative(self, wf: dict) -> dict:
        """The workflow's default negative without the tags listed in strip_from_default_negative (config)."""
        defaults = wf["slots"].get("defaults") or {}
        if not defaults.get("negative"):
            return wf
        negative = remove_tags(defaults["negative"], self.config.strip_from_default_negative)
        return {**wf, "slots": {**wf["slots"], "defaults": {**defaults, "negative": negative}}}

    def _with_cover(self, wf: dict) -> dict:
        """cover_image_id = the pinned image, else the newest done image made with this workflow (None if there
        is none); cover_pinned says which."""
        pinned = self.db.row("SELECT i.id FROM images i JOIN jobs j ON j.id = i.job_id "
                             "WHERE i.id = ? AND i.status = 'done' AND j.workflow_id = ?", (wf["cover_image_id"], wf["id"]))
        newest = None if pinned else self.db.row(
            "SELECT i.id FROM images i JOIN jobs j ON j.id = i.job_id WHERE j.workflow_id = ? AND i.status = 'done' "
            "ORDER BY i.finished_at DESC, i.id DESC LIMIT 1", (wf["id"],))
        found = pinned or newest
        return {**wf, "cover_image_id": found["id"] if found else None, "cover_pinned": pinned is not None}

    def set_cover(self, wf_id: str, image_id: int | None) -> dict:
        """Pin a finished image made with this workflow as its cover; None goes back to the newest image."""
        self.get(wf_id)
        if image_id is not None:
            image = self.db.row("SELECT i.status, j.workflow_id FROM images i JOIN jobs j ON j.id = i.job_id WHERE i.id = ?", (image_id,))
            if image is None:
                raise KeyError(image_id)
            if image["status"] != "done" or image["workflow_id"] != wf_id:
                raise WorkflowError("A cover has to be a finished image made with this workflow.")
        self.db.execute("UPDATE workflows SET cover_image_id = ? WHERE id = ?", (image_id, wf_id))
        return self.get(wf_id)

    def get_graph(self, wf_id: str) -> dict:
        return json.loads(Path(self.get(wf_id)["api_path"]).read_text(encoding="utf-8"))

    # ---------- remove ----------

    def remove(self, wf_id: str, jobs_to: str) -> bool:
        """Take a workflow out of the app: its row, its card and our converted copy. Its jobs keep their images, prompts and
        seeds and move to the workflow `jobs_to`; each image's recipe still names the workflow that made it. The file the
        workflow was imported from is never touched, and nothing imports it again unless you press Import for it. Returns
        False (and does nothing) when the workflow is already gone, so it is safe to run twice."""
        if self.db.row("SELECT id FROM workflows WHERE id = ?", (wf_id,)) is None:
            return False
        self.get(jobs_to)       # KeyError: the jobs need somewhere to go
        api_path = Path(self.get(wf_id)["api_path"]).resolve()
        self.db.execute("UPDATE jobs SET workflow_id = ? WHERE workflow_id = ?", (jobs_to, wf_id))
        self.db.execute("DELETE FROM cards WHERE kind = 'workflow' AND key = ?", (wf_id,))
        self.db.execute("DELETE FROM workflows WHERE id = ?", (wf_id,))
        if api_path.parent == self.config.workflows_dir.resolve():       # only our own copy, never a file elsewhere
            api_path.unlink(missing_ok=True)
        return True

    # ---------- slots ----------

    def update_slots(self, wf_id: str, slots: dict) -> dict:
        """Replace the slot map (after the user edited it) and mark it confirmed."""
        graph = self.get_graph(wf_id)
        _check_slot_refs(slots, graph)
        slots = {**slots, "kind": "edit" if slots.get("input_images") else "t2i"}
        self.db.execute("UPDATE workflows SET slots = ?, slots_confirmed = 1, kind = ?, updated_at = ? WHERE id = ?",
                        (dumps(slots), slots["kind"], now(), wf_id))
        return self.get(wf_id)


def _convert_or_fail(workflow: dict, oi: dict, name: str):
    try:
        return convert(workflow, oi)
    except ConvertError as e:
        raise WorkflowError(f"Could not import {name}: {e}") from e


def _summary(wf: dict) -> dict:
    """List view: no slots/notes bodies."""
    return {k: wf[k] for k in ("id", "title", "source_path", "kind", "runnable", "missing", "warnings", "slots_confirmed",
                                      "cover_image_id", "cover_pinned")}


def _check_slot_refs(slots: dict, graph: dict):
    """Every {node, input} in the slot map must exist in the graph."""
    def walk(value, where):
        if isinstance(value, dict):
            if "node" in value and "input" in value and value["node"] not in graph:
                raise WorkflowError(f"Slot '{where}' points at node {value['node']}, which isn't in this workflow.")
            for key, item in value.items():
                walk(item, key)
        elif isinstance(value, list):
            for item in value:
                walk(item, where)
    walk(slots, "slots")
