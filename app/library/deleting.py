"""Delete LoRAs, workflows and wildcard lists together with the files behind them.

Every delete has two steps. `*_plan` only reads: it lists the files that would go to the Recycle Bin, the app's own records
that would go with them, and the jobs that would be affected. `delete_*` recomputes the plan itself (nothing is taken from the
caller except which optional things were ticked), moves the files with `trash`, and only when every move worked changes the
database. If a move fails, the database is left as it was.

Files the user owns (LoRAs, models, ComfyUI workflow files, wildcard lists) go to the Recycle Bin. Files this app made itself
(covers, caches, the converted workflow copy) are plain data and are deleted outright. A path is only ever touched when
`trash.inside` finds it below one of the known folders.
"""
import asyncio
import os
from pathlib import Path

from app.comfy.desktop import model_dirs
from app.config import Config
from app.db import Database, dumps
from app.events import EventBus
from app.fields import make_issue
from app.library import trash
from app.library.cards import CardStore
from app.library.lora_covers import name_hash
from app.library.lora_extras import LoraExtras
from app.library.loras import LoraScanner
from app.library.workflows import WorkflowLibrary
from app.queue.jobs import JobStore
from app.wildcards.store import WildcardStore

MODEL_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".bin", ".sft"}
SIDECAR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".json", ".txt", ".info", ".yaml", ".yml", ".md"}
OPEN_STATUSES = ("draft", "ready", "planning")        # jobs that have not been queued yet

# ComfyUI loader node -> [(input, what to call the file, the models folders it is read from)]
LOADERS = {
    "UNETLoader": [("unet_name", "Diffusion model", ("diffusion_models", "unet"))],
    "CheckpointLoaderSimple": [("ckpt_name", "Checkpoint", ("checkpoints",))],
    "ImageOnlyCheckpointLoader": [("ckpt_name", "Checkpoint", ("checkpoints",))],
    "CLIPLoader": [("clip_name", "Text encoder", ("text_encoders", "clip"))],
    "DualCLIPLoader": [(f"clip_name{n}", "Text encoder", ("text_encoders", "clip")) for n in (1, 2)],
    "TripleCLIPLoader": [(f"clip_name{n}", "Text encoder", ("text_encoders", "clip")) for n in (1, 2, 3)],
    "VAELoader": [("vae_name", "VAE", ("vae",))],
    "UpscaleModelLoader": [("model_name", "Upscaler", ("upscale_models",))],
    "ControlNetLoader": [("control_net_name", "ControlNet", ("controlnet",))],
    "CLIPVisionLoader": [("clip_name", "CLIP vision", ("clip_vision",))],
    "StyleModelLoader": [("style_model_name", "Style model", ("style_models",))],
}
ANY_MODEL_FOLDER = tuple(sorted({f for loaders in LOADERS.values() for _, _, folders in loaders for f in folders}))


class DeleteError(Exception):
    """The delete can't be done; the message is meant for the user."""


class Blocked(DeleteError):
    """The item is in use right now (a job is generating with it, or a file can't go to the Recycle Bin)."""


def file_entry(path: Path, root: Path, kind: str) -> dict:
    return {"path": path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name, "root": str(root),
            "folder": root.name, "kind": kind, "size": path.stat().st_size, "full": str(path)}


def sidecars(path: Path) -> list[Path]:
    """Files next to a model file that belong to it: same name, then more suffixes (`name.preview.png`, `name.civitai.info`,
    `name.json`). Another model's files (`name.v2.safetensors` and its pictures) are not."""
    models = {p.stem for p in path.parent.iterdir() if p.is_file() and p.suffix.lower() in MODEL_EXTENSIONS}
    stem = path.stem
    others = [m for m in models if m != stem and m.startswith(stem + ".")]
    return sorted(p for p in path.parent.iterdir()
                  if p.is_file() and p != path and p.name.startswith(stem + ".") and p.suffix.lower() in SIDECAR_EXTENSIONS
                  and not any(p.name.startswith(m + ".") for m in others))


def path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


class Deleter:
    def __init__(self, db: Database, config: Config, scanner: LoraScanner, library: WorkflowLibrary, cards: CardStore,
                 extras: LoraExtras, wildcards: WildcardStore, store: JobStore, events: EventBus):
        self.db, self.config, self.scanner, self.library, self.cards = db, config, scanner, library, cards
        self.extras, self.wildcards, self.store, self.events = extras, wildcards, store, events

    # ---------- folders the app may delete in ----------

    def lora_roots(self) -> list[Path]:
        return self.scanner.folders()

    def model_roots(self, folders=ANY_MODEL_FOLDER) -> list[Path]:
        return [d for f in folders for d in model_dirs(self.config.comfy_install_dir, f)]

    def workflow_roots(self) -> list[Path]:
        return [Path(self.config.comfy_install_dir) / "user" / "default" / "workflows"]

    # ---------- jobs ----------

    def _jobs(self, uses) -> dict:
        """The unfinished jobs that use the item: running ones, queued ones, and ones not queued yet."""
        out = {"running": [], "queued": [], "open": []}
        for job in self.db.rows("SELECT id, status, description, workflow_id, fields FROM jobs WHERE status IN "
                                "('running','queued','draft','ready','planning')"):
            if uses(job):
                bucket = job["status"] if job["status"] in out else "open"
                out[bucket].append({"id": job["id"], "label": f"#{job['id']} {job['description'][:50]}".strip()})
        return out

    @staticmethod
    def _blocked(jobs: dict, what: str, problems: list[str]) -> str | None:
        if jobs["running"]:
            ids = ", ".join(f"#{j['id']}" for j in jobs["running"])
            return f"Job {ids} is generating with this {what} right now. Wait for it to finish, or cancel it in the Queue, then delete."
        return problems[0] if problems else None

    def _cancel(self, jobs: dict):
        for job in jobs["queued"]:
            self.store.cancel_job(job["id"])

    # ---------- LoRAs ----------

    def lora_plan(self, key: str) -> dict:
        """What deleting this LoRA would remove. KeyError when the app knows nothing of it."""
        path = self.scanner.resolve(key)
        card, extras = self.cards.get("lora", key), self.extras.get(key)
        if path is None and card is None and extras is None:
            raise KeyError(key)
        files, problems = self._lora_files(key, path), []
        for f in files:
            if (why := trash.check_recyclable(Path(f["full"]))):
                problems.append(f"{f['path']}: {why}")
        jobs = self._jobs(lambda j: key in lora_names(j))
        used_by = [c["key"] for c in self.cards.of_kind("workflow")
                   if key in (r["name"] for r in c["card"].get("default_loras", []))]
        records = self._lora_records(key, path, card, extras)
        if path is None:
            records.insert(0, "The LoRA file itself is already gone from the loras folder.")
        kept = ["Finished jobs and images made with it stay in the Gallery."]
        if used_by:
            kept.append("Workflow cards that name it as a default LoRA: " + ", ".join(used_by) + ". Those workflows keep naming it.")
        return {"kind": "lora", "key": key, "title": key, "files": files, "models": [], "records": records, "kept": kept,
                "jobs": jobs, "blocked": self._blocked(jobs, "LoRA", problems)}

    def _lora_files(self, key: str, path: Path | None) -> list[dict]:
        """The LoRA file and the files that belong to it. With the LoRA file already gone (a move that stopped half way), the
        pictures and info files it left are found by its name."""
        for root in self.lora_roots():
            base = path if path is not None else root / key
            if path is not None and not trash.inside(path, [root]):
                continue
            if path is None and not base.parent.is_dir():
                continue
            files = [file_entry(path, root, "LoRA file")] if path is not None else []
            for side in sidecars(base):
                if trash.inside(side, [root]):
                    kind = "Preview picture" if side.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4"} else "Info file"
                    files.append(file_entry(side, root, kind))
            return files
        if path is not None:
            raise DeleteError("This LoRA file is outside the folders ComfyUI reads LoRAs from, or is a link to another place. "
                              "The app only deletes files inside those folders.")
        return []

    def _sha(self, path: Path | None, card: dict | None) -> str:
        if path is not None and path.is_file():
            cached = self.scanner.hashes.cached(path)
            if cached:
                return cached
        return card["card"].get("sha256", "") if card else ""

    def _lora_records(self, key: str, path: Path | None, card: dict | None, extras: dict | None) -> list[str]:
        records = []
        if card:
            records.append(f"Its card ({card['status']})")
        if extras:
            tags = len(extras["user_tags"])
            records.append("Your tags and cover choice" if tags else "Your cover choice")
        if self.cover_files(key, self._sha(path, card)):
            records.append("Cover pictures the app stored for it")
        if path is not None and self.db.row("SELECT 1 FROM file_hashes WHERE path = ?", (str(path),)):
            records.append("Its saved file hash")
        return records

    def cover_files(self, key: str, sha: str) -> list[Path]:
        """The pictures and cache files the app wrote for this LoRA (never the user's own files)."""
        thumbs = self.config.thumbs_dir / "lora"
        found = [thumbs / f"upload-{name_hash(key)}.webp", thumbs / f"file-{name_hash(key)}.webp"]
        if sha:
            found += [thumbs / f"civitai-{sha}.webp", self.config.cache_dir / "safetensors" / f"{sha}.json",
                      self.config.cache_dir / "civitai" / f"{sha}.json"]
        return [p for p in found if p.is_file()]

    async def delete_lora(self, key: str, cancel_jobs: bool = False) -> dict:
        plan = self.lora_plan(key)
        self._refuse(plan)
        path, card = self.scanner.resolve(key), self.cards.get("lora", key)
        sha = self._sha(path, card)
        moved = await asyncio.to_thread(trash.trash_files, [Path(f["full"]) for f in plan["files"]])
        if cancel_jobs:
            self._cancel(plan["jobs"])
        self.scanner.deleted.add(key)
        self.db.execute("DELETE FROM cards WHERE kind = 'lora' AND key = ?", (key,))
        self.db.execute("DELETE FROM lora_extras WHERE key = ?", (key,))
        if path is not None:
            self.db.execute("DELETE FROM file_hashes WHERE path = ?", (str(path),))
        shared = sha and self.db.row("SELECT 1 FROM file_hashes WHERE sha256 = ?", (sha,))      # the same file under another name
        if sha and not shared:
            self.db.execute("DELETE FROM lora_preview_misses WHERE sha256 = ?", (sha,))
        for p in self.cover_files(key, "" if shared else sha):
            p.unlink(missing_ok=True)
        self.events.publish({"type": "library", "kind": "lora", "key": key})
        return {"deleted": key, "files": len(moved)}

    # ---------- workflows ----------

    def workflow_plan(self, wf_id: str) -> dict:
        """What deleting this workflow would remove, and which of the model files it uses could go too."""
        wf = self.library.get(wf_id)
        files, problems = [], []
        source = Path(wf["source_path"])
        if source.is_file():
            root = next((r for r in self.workflow_roots() if trash.inside(source, [r])), None)
            if root:
                files.append(file_entry(source, root, "ComfyUI workflow file"))
                if (why := trash.check_recyclable(source)):
                    problems.append(f"{source.name}: {why}")
        models = self._models(wf_id)
        jobs = self._jobs(lambda j: j["workflow_id"] == wf_id)
        card = self.cards.get("workflow", wf_id)
        records = ["The workflow in the Library"]
        if card:
            records.append(f"Its card ({card['status']})")
        records.append("The app's converted copy of it")
        kept = ["Finished jobs and images made with it stay in the Gallery; their jobs no longer name a workflow."]
        if source.is_file() and not files:
            kept.append("The workflow file in " + str(source.parent) + " is outside ComfyUI's workflow folder, so it is left alone.")
        elif not source.is_file():
            kept.append("The ComfyUI file it was imported from is already gone.")
        return {"kind": "workflow", "key": wf_id, "title": wf["title"], "files": files, "models": models, "records": records,
                "kept": kept, "jobs": jobs, "blocked": self._blocked(jobs, "workflow", problems)}

    def model_refs(self, graph: dict) -> list[tuple[str, tuple, str]]:
        """(what the file is, the models folders it is read from, the name in the workflow) for every model file a graph
        loads. LoRAs are left out: they have their own place in the Library."""
        refs = []
        for node in graph.values():
            cls = node["class_type"]
            if "lora" in cls.lower():
                continue
            if cls in LOADERS:
                refs += [(role, folders, node["inputs"][name]) for name, role, folders in LOADERS[cls]
                         if isinstance(node["inputs"].get(name), str)]
            else:
                refs += [("Model file", ANY_MODEL_FOLDER, v) for v in node["inputs"].values()
                         if isinstance(v, str) and Path(v).suffix.lower() in MODEL_EXTENSIONS]
        return refs

    def find_model(self, folders: tuple, name: str) -> tuple[Path, Path] | None:
        """(the file, the models folder it is in) for a name from a workflow, or None when it is not on disk."""
        rel = name.replace("\\", "/")
        for root in self.model_roots(folders):
            path = root / rel
            if path.is_file():
                return path, root
        return None

    def _models(self, wf_id: str) -> list[dict]:
        """Each model file this workflow uses, with whether it can be deleted along with it."""
        loras = [path_key(r) for r in self.lora_roots()]
        others: dict[str, list[str]] = {}               # file -> titles of the other workflows that use it
        for other in self.library.list():
            if other["id"] != wf_id:
                for _, folders, name in self.model_refs(self.library.get_graph(other["id"])):
                    if (found := self.find_model(folders, name)):
                        others.setdefault(path_key(found[0]), []).append(other["title"])
        out, seen = [], set()
        for role, folders, name in self.model_refs(self.library.get_graph(wf_id)):
            found = self.find_model(folders, name)
            if not found or path_key(found[0]) in seen:
                continue
            path, root = found
            seen.add(path_key(path))
            entry = {**file_entry(path, root, role), "id": path_key(path), "used_by": sorted(set(others.get(path_key(path), []))),
                     "removable": False, "why_kept": ""}
            if entry["used_by"]:
                entry["why_kept"] = "Also used by " + ", ".join(entry["used_by"])
            elif any(path_key(path).startswith(r + os.sep) for r in loras):
                entry["why_kept"] = "It is in a LoRA folder"
            elif not trash.inside(path, [root]):
                entry["why_kept"] = "It is a link to another place"
            elif (why := trash.check_recyclable(path)):
                entry["why_kept"] = why
            else:
                entry["removable"] = True
            out.append(entry)
        return sorted(out, key=lambda m: (not m["removable"], m["kind"], m["path"].lower()))

    async def delete_workflow(self, wf_id: str, models: list[str] = (), cancel_jobs: bool = False) -> dict:
        plan = self.workflow_plan(wf_id)
        self._refuse(plan)
        removable = {m["id"]: m for m in plan["models"] if m["removable"]}
        unknown = [m for m in models if m not in removable]
        if unknown:
            raise DeleteError("One of the model files you ticked can't be deleted any more (another workflow uses it now). Look at the list again.")
        paths = [Path(f["full"]) for f in plan["files"]] + [Path(removable[m]["full"]) for m in dict.fromkeys(models)]
        moved = await asyncio.to_thread(trash.trash_files, paths)
        api_path = Path(self.library.get(wf_id)["api_path"])
        if cancel_jobs:
            self._cancel(plan["jobs"])
        self._release_jobs(wf_id, plan["jobs"])
        self.db.execute("DELETE FROM cards WHERE kind = 'workflow' AND key = ?", (wf_id,))
        self.db.execute("DELETE FROM workflows WHERE id = ?", (wf_id,))
        if api_path.resolve().parent == self.config.workflows_dir.resolve():       # our own copy only
            api_path.unlink(missing_ok=True)
        self.events.publish({"type": "library", "kind": "workflow", "key": wf_id})
        return {"deleted": wf_id, "files": len(moved)}

    def _release_jobs(self, wf_id: str, jobs: dict):
        """Jobs stop naming the deleted workflow. Finished ones are only unlinked from it (their images keep their recipe, which
        names the workflow). Queued and unqueued ones become drafts without a workflow, with a blocking issue, so nothing tries to
        run them; the job card lets the user pick another workflow."""
        for job in self.db.rows("SELECT id, status, issues FROM jobs WHERE workflow_id = ?", (wf_id,)):
            if job["status"] in ("queued", "ready", "draft", "planning"):
                issue = make_issue("workflow", "The workflow for this job was deleted. Choose another one.", code="no_workflow")
                self.db.execute("DELETE FROM images WHERE job_id = ? AND status = 'pending'", (job["id"],))
                self.db.execute("UPDATE jobs SET status = 'draft', workflow_id = NULL, issues = ? WHERE id = ?",
                                (dumps([*job["issues"], issue]), job["id"]))
            else:
                self.db.execute("UPDATE jobs SET workflow_id = NULL WHERE id = ?", (job["id"],))

    # ---------- wildcard lists ----------

    def wildcard_plan(self, name: str) -> dict:
        found = self.wildcards.get(name)            # KeyError: no such list
        path = self.wildcards.path_of(found.name)
        root = self.wildcards.root
        files, problems = [], []
        if trash.inside(path, [root]):
            files.append(file_entry(path, root, "Wildcard list"))
            if (why := trash.check_recyclable(path)):
                problems.append(f"{path.name}: {why}")
        row = self.db.row("SELECT source FROM wildcard_lists WHERE name = ?", (found.name,))
        records = ["Its on/off switch and role"] if row else []
        kept = []
        if row and row["source"]:
            kept.append(f"Your original in {row['source']} is not touched. Re-importing that folder would bring the list back.")
        return {"kind": "wildcard", "key": found.name, "title": found.name, "files": files, "models": [], "records": records,
                "kept": kept, "jobs": {"running": [], "queued": [], "open": []}, "blocked": problems[0] if problems else None,
                "entries": len(found.entries)}

    def delete_wildcard(self, name: str) -> dict:
        plan = self.wildcard_plan(name)
        self._refuse(plan)
        moved = trash.trash_files([Path(f["full"]) for f in plan["files"]])
        self.wildcards.forget(plan["key"])
        self.events.publish({"type": "library", "kind": "wildcard", "key": plan["key"]})
        return {"deleted": plan["key"], "files": len(moved)}

    # ---------- shared ----------

    @staticmethod
    def _refuse(plan: dict):
        if plan["blocked"]:
            raise Blocked(plan["blocked"])


def lora_names(job: dict) -> set[str]:
    """The LoRA files a job's fields name."""
    value = (job["fields"].get("loras") or {}).get("value") or []
    return {l.get("name") for l in value if isinstance(l, dict)}
