"""Jobs and their images in SQLite: create from field values, state changes, ordering."""
from app.config import Config
from app.db import Database, dumps, now
from app.fields import (SEED_STRATEGIES, blocking, clamp_number, make_issue, normalize_fields, plain_values, prompt_locks, round_size,
                        seed_for_image, settle_prompts)
from app.library.card_models import LoraCard, WorkflowCard
from app.library.cards import CardStore
from app.library.workflows import WorkflowLibrary
from app.planning.lora_negative import apply_to_field, asked_to_avoid, word_key
from app.planning.lora_words import clean_prompts
from app.planning.scene import scene_text
from app.planning.intent import intent_text
from app.planning import content_safety
from app.thumbs import remove_thumbs
from app.wildcards.expand import expand_for_image, find_tokens, unknown_refs
from app.wildcards.ideas import idea_record
from app.wildcards.store import Catalog, WildcardStore

ACTIVE = ("queued", "running")
FINISHED_IMAGE = ("done", "failed", "cancelled")
NUMBER_LIMITS = {"steps": (1, 200, int), "cfg": (0.0, 30.0, float), "denoise": (0.0, 1.0, float),
                 "hires_steps": (1, 200, int), "hires_denoise": (0.0, 1.0, float), "hires_cfg": (0.0, 30.0, float)}
SETTING_DEFAULTS = ("steps", "cfg", "sampler", "scheduler", "denoise", "width", "height")


# Issue codes that code recomputes every time a job changes (the content rules' codes join them: content_safety.codes). Other issues
# (from the planner) stay until their field is edited.
STRUCTURAL_CODES = ("no_workflow", "not_runnable", "invalid", "lora_negative_locked")


class JobError(Exception):
    """Bad job input or a state change that isn't allowed; the message is meant for the user. `field` names the job
    field it is about, when there is one."""

    def __init__(self, message: str, field: str = "job"):
        super().__init__(message)
        self.field = field


class ImageBusy(JobError):
    """The image is still waiting for or being made by ComfyUI."""


def planner_texts(meta: dict) -> list[str]:
    """Everything the request understanding and the wildcard offers put on a job besides its prompts (the names and tags the user wrote, the entries
    assigned for what must differ between images): the content rules read it too, so no new field can carry what the prompts must not."""
    assigned = (meta or {}).get("assigned") or {}
    offered = [o.get("text", "") for o in ((meta or {}).get("scene_options") or {}).get("offered") or []]
    return [*intent_text(meta), *map(str, assigned.values()), *offered]


class JobStore:
    def __init__(self, db: Database, config: Config, library: WorkflowLibrary, wildcards: WildcardStore | None = None,
                 cards: CardStore | None = None):
        self.db, self.config, self.library, self.wildcards, self.cards = db, config, library, wildcards, cards

    # ---------- create ----------

    def create_job(self, workflow_id: str, count: int, raw_fields: dict, description: str = "",
                   queue: bool = False, object_info: dict | None = None) -> dict:
        """Validate the hand-made job, store it as 'ready' with one image row (and recipe) per image."""
        content_safety.require_ready()
        try:
            wf = self.library.get(workflow_id)
        except KeyError:
            raise JobError(f"Unknown workflow '{workflow_id}'.") from None
        if not wf["runnable"]:
            raise JobError(f"Workflow '{wf['title']}' is not runnable: {why_not(wf)}")
        fields = self.prepare_fields(wf, count, raw_fields, object_info)
        issues = self.apply_lora_negative(wf, fields)
        job_id = self.db.execute(
            "INSERT INTO jobs (workflow_id, description, status, count, fields, issues, created_at) VALUES (?,?,?,?,?,?,?)",
            (workflow_id, description, "ready", count, dumps(fields), dumps(issues), now())).lastrowid
        self._insert_images(job_id, self._recipes(wf, fields, count))
        if queue:
            self.queue_job(job_id)
        return self.get_job(job_id)

    def prepare_fields(self, wf: dict, count: int, raw: dict, oi: dict | None) -> dict:
        """Normalize + validate + clamp fields. Returns the stored {name: {value, source, reason}} dict."""
        if not 1 <= count <= self.config.max_images_per_job:
            raise JobError(f"count must be between 1 and {self.config.max_images_per_job}.", "count")
        raw = dict(raw or {})
        if "prompt" in raw and "prompts" not in raw:
            raw["prompts"] = [raw.pop("prompt")]
        if "input_image" in raw and "input_images" not in raw:
            raw["input_images"] = [raw.pop("input_image")]
        legacy = raw.pop("prompt_locks", None)          # an earlier UI kept per-prompt locks in a field of their own
        fields = normalize_fields(raw)
        if isinstance(legacy, dict) and isinstance(legacy.get("value"), list) and "locks" not in (fields.get("prompts") or {}):
            fields.get("prompts", {})["locks"] = [bool(x) for x in legacy["value"]]

        prompts = [p for p in (fields.get("prompts", {}).get("value") or []) if isinstance(p, str)]
        if not prompts:
            raise JobError("At least one prompt is required.", "prompts")
        if len(prompts) not in (1, count):
            raise JobError(f"Give 1 prompt (used for all images) or exactly {count} prompts; got {len(prompts)}.", "prompts")
        fields["prompts"]["value"] = prompts
        fields["prompts"] = settle_prompts(fields["prompts"])
        self._check_wildcards(prompts)

        for name, (lo, hi, cast) in NUMBER_LIMITS.items():
            if name in fields and fields[name]["value"] is not None:
                value, note = clamp_number(cast(fields[name]["value"]), lo, hi)
                fields[name].update(value=value, **({"note": note} if note else {}))
        for name in ("width", "height"):
            if name in fields and fields[name]["value"] is not None:
                value, note = round_size(int(fields[name]["value"]))
                fields[name].update(value=value, **({"note": note} if note else {}))

        strategy = fields.get("seed_strategy", {}).get("value", "random")
        if strategy not in SEED_STRATEGIES:
            raise JobError(f"seed_strategy must be one of {', '.join(SEED_STRATEGIES)}.", "seed_strategy")
        fields.setdefault("seed_strategy", normalize_fields({"x": strategy})["x"])["value"] = strategy

        loras = (fields.get("loras", {}).get("value")) or []
        if len(loras) > self.config.max_loras:
            raise JobError(f"At most {self.config.max_loras} LoRAs per job.", "loras")
        for lora in loras:
            if not lora.get("name"):
                raise JobError("Each LoRA needs a name.", "loras")
            lora["weight"], _ = clamp_number(float(lora.get("weight", 1.0)), -2.0, 2.0)
        fields["loras"] = fields.get("loras") or normalize_fields({"x": []})["x"]
        fields["loras"]["value"] = loras

        if wf["kind"] == "edit" and not (fields.get("input_images", {}).get("value") or []):
            raise JobError("This is an image-edit workflow: give input_image (a file uploaded to ComfyUI).", "input_images")
        if oi:
            _check_against_object_info(fields, oi)
        return fields

    def apply_lora_negative(self, wf: dict, fields: dict, meta: dict | None = None) -> list[dict]:
        """Put the negative of the job in its final form: the negative tags that the cards of the job's LoRAs ask to
        remove are taken out, unless you locked the negative or asked for the tag yourself (`meta` has your request). Changes
        `fields["negative"]`; returns the warnings. The one place this is done, for planned and hand-made jobs alike, so the job
        card and the recipe (what ComfyUI gets) always agree. Does nothing for a workflow without a negative prompt."""
        if self.cards is None or not wf["slots"].get("negative"):
            return []
        meta = meta or {}
        wanted = {}
        for item in (fields.get("loras") or {}).get("value") or []:
            row = self.cards.get("lora", item["name"])
            words = LoraCard.model_validate(row["card"]).remove_from_negative if row else []
            if words:
                wanted[item["name"]] = words
        field = fields.get("negative")
        if not wanted and not (field and "base" in field):
            return []
        asked = asked_to_avoid(meta.get("request", "")) | set(meta.get("negative_asked") or [])
        field, issues = apply_to_field(field, (wf["slots"].get("defaults") or {}).get("negative") or "", wanted,
                                       {word_key(t) for t in self.config.safety_negative}, asked)
        if field is not None:
            fields["negative"] = field
        return issues

    def _check_wildcards(self, prompts: list[str]):
        """A `__name__` in a prompt must be a list we have: ComfyUI must never receive the raw token."""
        catalog = self._catalog_for(prompts)
        for n, text in enumerate(prompts, 1):
            for why in unknown_refs(text, catalog) if catalog is not None else []:
                raise JobError(f"Prompt {n} uses a wildcard that can't be filled in: {why}. "
                               "Import your wildcard lists in the Library, or fix the name.", "prompts")

    def _catalog_for(self, prompts: list[str]) -> Catalog | None:
        """The wildcard lists, when any prompt has a token to fill in (otherwise None, so plain jobs never touch them)."""
        if not any(find_tokens(p) for p in prompts):
            return None
        return self.wildcards.catalog() if self.wildcards is not None else Catalog([])

    def _recipes(self, wf: dict, fields: dict, count: int) -> list[dict]:
        return build_recipes(wf, fields, count, self.config.hires_default, self._catalog_for(fields["prompts"]["value"]))

    def _insert_images(self, job_id: int, recipes: list[dict]):
        for i, recipe in enumerate(recipes, 1):
            self.db.execute("INSERT INTO images (job_id, idx, status, seed, recipe, created_at) VALUES (?,?,?,?,?,?)",
                            (job_id, i, "pending", recipe["seed"], dumps(recipe), now()))

    # ---------- read ----------

    def get_job(self, job_id: int, images: bool = True) -> dict:
        job = self.db.row("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if job is None:
            raise KeyError(job_id)
        if images:
            job["images"] = self.db.rows("SELECT * FROM images WHERE job_id = ? ORDER BY idx", (job_id,))
        return job

    def list_jobs(self, status: str | None = None) -> list[dict]:
        """Jobs with per-image status counts (no recipes)."""
        sql = ("SELECT j.*, p.request AS batch_request FROM jobs j LEFT JOIN plans p ON p.id = j.batch_id"
               + (" WHERE j.status = ?" if status else "") + " ORDER BY j.position, j.id")
        jobs = self.db.rows(sql, (status,) if status else ())
        counts: dict[int, dict] = {}
        for r in self.db.rows("SELECT job_id, status, COUNT(*) AS n FROM images GROUP BY job_id, status"):
            counts.setdefault(r["job_id"], {})[r["status"]] = r["n"]
        for job in jobs:
            job["image_counts"] = counts.get(job["id"], {})
        return jobs

    def get_image(self, image_id: int) -> dict:
        image = self.db.row("SELECT i.*, j.workflow_id FROM images i JOIN jobs j ON j.id = i.job_id WHERE i.id = ?", (image_id,))
        if image is None:
            raise KeyError(image_id)
        return image

    def list_images(self, limit: int = 100, offset: int = 0, job_id: int | None = None) -> list[dict]:
        """Finished images, newest first (the future gallery)."""
        where, params = "i.status = 'done'", []
        if job_id is not None:
            where, params = where + " AND i.job_id = ?", [job_id]
        return self.db.rows(f"""SELECT i.*, j.workflow_id FROM images i JOIN jobs j ON j.id = i.job_id
                                WHERE {where} ORDER BY i.finished_at DESC, i.id DESC LIMIT ? OFFSET ?""",
                            (*params, limit, offset))

    def delete_image(self, image_id: int):
        """Remove an image's row, its file and its cached thumbnails. A pinned workflow cover falls back to the
        newest image. The job stays, even with no images left."""
        image = self.get_image(image_id)
        if image["status"] in ("pending", "running"):
            raise ImageBusy("This image hasn't finished yet. Cancel its job instead.")
        self.db.execute("UPDATE workflows SET cover_image_id = NULL WHERE cover_image_id = ?", (image_id,))
        self.db.execute("DELETE FROM images WHERE id = ?", (image_id,))
        if image["file_path"]:
            (self.config.images_dir / image["file_path"]).unlink(missing_ok=True)
        remove_thumbs(self.config, image_id)

    # ---------- state changes ----------

    def _next_position(self) -> float:
        row = self.db.row("SELECT MAX(position) AS p FROM jobs WHERE status IN ('queued','running')")
        return (row["p"] or 0) + 1

    def queue_job(self, job_id: int) -> dict:
        content_safety.require_ready()
        job = self.get_job(job_id, images=False)
        if job["status"] not in ("ready", "draft"):
            raise JobError(f"Job {job_id} is {job['status']}; only ready jobs can be queued (use retry for finished ones).")
        open_issues = blocking(job["issues"])
        if not open_issues:   # the content rules hold for every job, including ones made by hand, and for the filled-in prompts
            recipes = (r["recipe"]["prompt"] for r in self.db.rows("SELECT recipe FROM images WHERE job_id = ?", (job_id,)))
            hit = content_safety.check_content([job["description"], *(job["fields"].get("prompts", {}).get("value") or []), *recipes, *planner_texts(job["meta"])])
            open_issues = [hit] if hit else []
            if hit:
                content_safety.log_block(hit["code"], job_id)
        if open_issues:
            raise JobError(f"Job {job_id} has an open issue: {open_issues[0]['message']}", open_issues[0]["field"])
        self.db.execute("UPDATE jobs SET status = 'queued', position = ?, error = NULL WHERE id = ?", (self._next_position(), job_id))
        return self.get_job(job_id)

    def cancel_job(self, job_id: int) -> dict:
        """Cancel a ready/queued/running job. A running image is marked cancelled by the worker."""
        job = self.get_job(job_id, images=False)
        if job["status"] not in ("draft", "ready", "queued", "running"):
            raise JobError(f"Job {job_id} is already {job['status']}.")
        self.db.execute("UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ?", (now(), job_id))
        self.db.execute("UPDATE images SET status = 'cancelled' WHERE job_id = ? AND status = 'pending'", (job_id,))
        return self.get_job(job_id)

    def delete_job(self, job_id: int):
        """Remove a job that never ran (a draft/ready card). Its image rows go with it."""
        job = self.get_job(job_id, images=False)
        if job["status"] not in ("draft", "ready"):
            raise JobError(f"Job {job_id} is {job['status']}; only jobs that haven't been queued can be deleted (cancel it instead).")
        self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def clear_job(self, job_id: int) -> dict:
        """Take a finished job off the Queue page. The job row and its images stay (the Gallery and the recipes use
        them); retrying the job brings it back."""
        job = self.get_job(job_id, images=False)
        if job["status"] not in ("done", "failed", "cancelled"):
            raise JobError(f"Job {job_id} is {job['status']}; only finished jobs can be cleared (cancel it first).")
        self.db.execute("UPDATE jobs SET cleared_at = ? WHERE id = ?", (now(), job_id))
        return self.get_job(job_id, images=False)

    def retry_job(self, job_id: int, mode: str = "failed") -> dict:
        """Put images back to pending and the job back in the queue. mode: 'failed' (failed+cancelled only) or 'all'."""
        job = self.get_job(job_id, images=False)
        if job["status"] not in ("failed", "cancelled", "done"):
            raise JobError(f"Job {job_id} is {job['status']}; only finished jobs can be retried.")
        if mode not in ("failed", "all"):
            raise JobError("mode must be 'failed' or 'all'.")
        statuses = "('failed','cancelled')" if mode == "failed" else "('failed','cancelled','done','pending')"
        reset = self.db.execute(
            f"UPDATE images SET status = 'pending', error = NULL, started_at = NULL, finished_at = NULL WHERE job_id = ? AND status IN {statuses}",
            (job_id,)).rowcount
        if reset == 0:
            raise JobError(f"Job {job_id} has no failed or cancelled images to retry.")
        self.db.execute("UPDATE jobs SET status = 'queued', position = ?, error = NULL, finished_at = NULL, cleared_at = NULL WHERE id = ?",
                        (self._next_position(), job_id))
        return self.get_job(job_id)

    def update_fields(self, job_id: int, patch: dict, object_info: dict | None = None) -> dict:
        """Change fields of a ready or draft job (images are rebuilt; random seeds are re-rolled). A field you change
        becomes locked; the others keep their source. Send a field as {"value": ..., "source": "auto"} to change it
        without locking it. `workflow_id`, `count` and `description` can be changed too; a workflow you pick is locked."""
        job = self.get_job(job_id, images=False)
        if job["status"] not in ("draft", "ready"):
            raise JobError(f"Job {job_id} is {job['status']}; only ready jobs can be edited.")
        patch = dict(patch)
        for alias, name in (("prompt", "prompts"), ("input_image", "input_images")):
            if alias in patch and name not in patch:
                patch[name] = [patch.pop(alias)]
        workflow_id = patch.pop("workflow_id", job["workflow_id"])
        count = patch.pop("count", job["count"])
        description = patch.pop("description", job["description"])
        meta = dict(job["meta"])
        if workflow_id != job["workflow_id"] and workflow_id is not None:
            self.library.get(workflow_id)       # KeyError (404) for an unknown one
            meta["workflow"] = {"source": "locked", "reason": "You chose this workflow"}
        wf = self.library.get(workflow_id) if workflow_id else None
        if isinstance(patch.get("prompts"), dict):
            patch["prompts"] = _prompts_edit(job["fields"].get("prompts"), patch["prompts"])
        fields = {**job["fields"], **patch}
        cleaned = self._drop_lora_words(job["fields"], fields, patch, workflow_id)
        issues = [i for i in job["issues"] if i["field"] not in patch and not (i["field"] == "workflow" and workflow_id != job["workflow_id"])]
        self.db.execute("UPDATE jobs SET description = ?, workflow_id = ?, meta = ? WHERE id = ?", (description, workflow_id, dumps(meta), job_id))
        result = self.finalize(job_id, wf, count, fields, issues, object_info)
        return {**result, "removed_trigger_words": cleaned} if cleaned else result

    def update_meta(self, job_id: int, **values) -> dict:
        """Change values of a job's meta (the scene plan of a tweak); fields and status stay as they are."""
        job = self.get_job(job_id, images=False)
        meta = {**job["meta"], **values}
        self.db.execute("UPDATE jobs SET meta = ? WHERE id = ?", (dumps(meta), job_id))
        return {**job, "meta": meta}

    def _drop_lora_words(self, before: dict, fields: dict, patch: dict, workflow_id: str | None) -> dict:
        """A LoRA the patch takes off the job takes its trigger words and style tags out of the prompts (planning/lora_words.py;
        locked prompts too). Changes `fields["prompts"]`; returns {"loras": [names], "prompts": how many changed} or {}.
        Not done when the same patch writes the prompts itself."""
        if self.cards is None or "loras" not in patch or "prompts" in patch or not (before.get("prompts") or {}).get("value"):
            return {}
        names = lambda f: [x.get("name") for x in ((f.get("value") if isinstance(f, dict) and "value" in f else f) or []) if isinstance(x, dict)]
        now_on = set(names(patch["loras"]))
        gone = [n for n in dict.fromkeys(names(before.get("loras") or {})) if n and n not in now_on]
        if not gone:
            return {}
        card_of = lambda name: (LoraCard.model_validate(row["card"]) if (row := self.cards.get("lora", name)) else None)
        removed = {n: c for n in gone if (c := card_of(n))}
        kept = {n: c for n in now_on if (c := card_of(n))}
        row = self.cards.get("workflow", workflow_id) if workflow_id else None
        tags = bool(row) and WorkflowCard.model_validate(row["card"]).prompt_style.kind == "tags"
        field, changed = clean_prompts(fields["prompts"], removed, kept, tags)
        if not changed:
            return {}
        fields["prompts"] = field
        return {"loras": gone, "prompts": len(changed)}

    def set_locks(self, job_id: int, names: list[str], locked: bool, prompts: list[int] | None = None) -> dict:
        """Lock or unlock fields (a lock keeps the value when the job is re-planned).
        `prompts` locks or unlocks only the prompts with these numbers (0 is the first); "prompts" in `names` means all."""
        job = self.get_job(job_id, images=False)
        if job["status"] not in ("draft", "ready"):
            raise JobError(f"Job {job_id} is {job['status']}; only ready jobs can be edited.")
        fields = job["fields"]
        for name in names:
            if name == "prompts" and "prompts" in fields:
                fields["prompts"] = settle_prompts(fields["prompts"], [locked] * len(fields["prompts"]["value"]))
            elif name in fields:
                fields[name]["source"] = "locked" if locked else "auto"
            else:
                raise JobError(f"This job has no field called '{name}'.", name)
        if prompts:
            field = fields.get("prompts")
            if not field:
                raise JobError("This job has no prompts yet.", "prompts")
            flags = prompt_locks(field)
            for i in prompts:
                if not 0 <= i < len(flags):
                    raise JobError(f"This job has no prompt number {i + 1}.", "prompts")
                flags[i] = locked
            fields["prompts"] = settle_prompts(field, flags)
        self.db.execute("UPDATE jobs SET fields = ? WHERE id = ?", (dumps(fields), job_id))
        return self.get_job(job_id)

    # ---------- planning ----------

    def create_planning_job(self, description: str, count: int, workflow_id: str | None, meta: dict, plan_id: int,
                            batch_id: int | None = None) -> dict:
        """A card the planner is still working on: no fields or images yet. `batch_id` is the plan whose request made it."""
        job_id = self.db.execute(
            "INSERT INTO jobs (workflow_id, description, status, count, fields, created_at, plan_id, meta, batch_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (workflow_id, description, "planning", count, "{}", now(), plan_id, dumps(meta), batch_id)).lastrowid
        return self.get_job(job_id, images=False)

    def clear_batch(self, batch_id: int):
        """The request turned out to give one image only: its job is not part of a batch."""
        self.db.execute("UPDATE jobs SET batch_id = NULL WHERE batch_id = ?", (batch_id,))

    def mark_planning(self, job_id: int, plan_id: int):
        """A re-plan starts: the card shows as planning until it is finalized."""
        self.db.execute("UPDATE jobs SET status = 'planning', plan_id = ?, error = NULL WHERE id = ?", (plan_id, job_id))

    def finish_planning(self, job_id: int, wf: dict | None, fields: dict, issues: list[dict], meta: dict,
                        object_info: dict | None = None) -> dict:
        """Store the planner's result: validate it like a hand-made job, build the image rows, and set the state
        (ready, or draft if an issue blocks it)."""
        job = self.get_job(job_id, images=False)
        self.db.execute("UPDATE jobs SET meta = ? WHERE id = ?", (dumps(meta), job_id))
        return self.finalize(job_id, wf, job["count"], fields, issues, object_info)

    def fail_planning(self, job_id: int, message: str) -> dict:
        self.db.execute("UPDATE jobs SET status = 'draft', error = ? WHERE id = ? AND status = 'planning'", (message, job_id))
        return self.get_job(job_id)

    def finalize(self, job_id: int, wf: dict | None, count: int, raw_fields: dict, issues: list[dict], oi: dict | None = None) -> dict:
        """The one place that turns fields + issues into a stored job: validates and clamps the fields, records
        structural problems (no workflow, no prompts, ...) as blocking issues, rebuilds the image rows when nothing
        blocks, and sets the status to ready or draft."""
        content_safety.require_ready()
        issues = [i for i in issues if i.get("code") not in (*STRUCTURAL_CODES, *content_safety.codes())]
        fields = normalize_fields(raw_fields)
        job = self.get_job(job_id, images=False)
        if wf is None:
            issues.append(make_issue("workflow", "No workflow is chosen for this job.", code="no_workflow"))
        elif not wf["runnable"]:
            issues.append(make_issue("workflow", f"Workflow '{wf['title']}' is not runnable: {why_not(wf)}", code="not_runnable"))
        else:
            try:
                fields = self.prepare_fields(wf, count, raw_fields, oi)
                issues += self.apply_lora_negative(wf, fields, job["meta"])
            except JobError as e:
                issues.append(make_issue(e.field, str(e), code="invalid"))
        scene = scene_text(job["meta"].get("scene"))
        texts = [job["description"], *(fields.get("prompts", {}).get("value") or []), *([scene] if scene else []), *planner_texts(job["meta"])]
        hit = content_safety.check_content(texts) or (content_safety.check_content([scene], strict=True) if scene else None)
        if hit:
            issues.append(hit)
            content_safety.log_block(hit["code"], job_id)
        self.db.execute("DELETE FROM images WHERE job_id = ?", (job_id,))
        stuck = bool(blocking(issues))
        recipes = [] if stuck else self._recipes(wf, fields, count)
        if recipes and (hit := content_safety.check_content([self.get_job(job_id, images=False)["description"], *(r["prompt"] for r in recipes)])):
            issues.append(hit)      # a wildcard filled in something the written prompt didn't say
            content_safety.log_block(hit["code"], job_id)
            stuck, recipes = True, []
        self._insert_images(job_id, recipes)
        self.db.execute("UPDATE jobs SET fields = ?, count = ?, issues = ?, status = ?, error = NULL WHERE id = ?",
                        (dumps(fields), count, dumps(issues), "draft" if stuck else "ready", job_id))
        return self.get_job(job_id)

    # ---------- ordering ----------

    def queued_ids(self) -> list[int]:
        return [r["id"] for r in self.db.rows("SELECT id FROM jobs WHERE status = 'queued' ORDER BY position, id")]

    def set_order(self, ids: list[int]):
        """Queued jobs in this order; queued jobs not listed keep their relative order after the listed ones."""
        queued = self.queued_ids()
        unknown = [i for i in ids if i not in queued]
        if unknown:
            raise JobError(f"Not queued: {unknown}")
        order = [*dict.fromkeys(ids), *[i for i in queued if i not in ids]]
        first = self._first_position()
        for n, job_id in enumerate(order):
            self.db.execute("UPDATE jobs SET position = ? WHERE id = ?", (first + n, job_id))

    def _first_position(self) -> float:
        row = self.db.row("SELECT MIN(position) AS p FROM jobs WHERE status = 'queued'")
        return row["p"] or 1

    def move_job(self, job_id: int, delta: int):
        """Move a queued job up (negative) or down (positive) by `delta` places."""
        queued = self.queued_ids()
        if job_id not in queued:
            raise JobError(f"Job {job_id} is not queued.")
        i = queued.index(job_id)
        queued.insert(min(max(i + delta, 0), len(queued) - 1), queued.pop(i))
        self.set_order(queued)

    # ---------- worker helpers ----------

    def recover(self):
        """After a restart: a job that was running goes back to the queue, its running image to pending."""
        self.db.execute("UPDATE images SET status = 'pending', started_at = NULL WHERE status = 'running'")
        self.db.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
        self.db.execute("UPDATE jobs SET status = 'draft', error = 'Planning was interrupted when the app stopped. Re-plan this job.' "
                        "WHERE status = 'planning'")
        self.db.execute("UPDATE plans SET status = 'failed', error = 'The app stopped before planning finished.', finished_at = ? "
                        "WHERE status NOT IN ('done', 'failed', 'cancelled')", (now(),))

    def next_queued(self) -> dict | None:
        row = self.db.row("SELECT id FROM jobs WHERE status = 'queued' ORDER BY position, id LIMIT 1")
        return self.get_job(row["id"]) if row else None

    def start_job(self, job_id: int):
        self.db.execute("UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ? AND status = 'queued'",
                        (now(), job_id))

    def requeue_job(self, job_id: int):
        """Back to queued (ComfyUI went away); keeps its place."""
        self.db.execute("UPDATE jobs SET status = 'queued' WHERE id = ? AND status = 'running'", (job_id,))

    def set_image(self, image_id: int, status: str, **cols):
        """Update an image row. cols may include prompt_id, file_path, error, recipe (dict)."""
        if "recipe" in cols:
            cols["recipe"] = dumps(cols["recipe"])
        stamp = {"running": {"started_at": now()}, "pending": {"started_at": None}}.get(status, {"finished_at": now()})
        cols = {**stamp, **cols, "status": status}
        self.db.execute(f"UPDATE images SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?", (*cols.values(), image_id))

    def finish_job(self, job_id: int) -> dict:
        """Set done/failed from image states (unless the job was cancelled meanwhile)."""
        job = self.get_job(job_id)
        if job["status"] != "running":
            return job
        statuses = {i["status"] for i in job["images"]}
        failed = [i for i in job["images"] if i["status"] == "failed"]
        status = "failed" if failed else ("done" if statuses <= {"done"} else "cancelled")
        error = failed[0]["error"] if failed else None
        self.db.execute("UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?", (status, error, now(), job_id))
        return self.get_job(job_id)


def build_recipes(wf: dict, fields: dict, count: int, hires_default: bool, catalog: Catalog | None = None) -> list[dict]:
    """One complete recipe per image: job values + workflow defaults + that image's prompt and seed. Wildcard tokens in a
    prompt are filled in here, per image, with a generator seeded from that image's seed; the recipe keeps the prompt
    ComfyUI gets and, when it differs, the prompt as written (`prompt_template`)."""
    values = plain_values(fields)
    defaults = wf["slots"].get("defaults") or {}
    hires_defaults = defaults.get("hires") or {}
    prompts = values["prompts"]
    strategy = values.get("seed_strategy", "random")
    base_seed = values.get("seed")
    common = {name: values.get(name) if values.get(name) is not None else defaults.get(name) for name in SETTING_DEFAULTS}
    common.update(
        workflow_id=wf["id"], workflow_title=wf["title"], seed_strategy=strategy,
        negative=values["negative"] if values.get("negative") is not None else defaults.get("negative"),
        loras=values.get("loras") or [], input_images=values.get("input_images") or [],
        hires=bool(values.get("hires", hires_default)) and bool(wf["slots"].get("hires")),
        hires_steps=values.get("hires_steps") if values.get("hires_steps") is not None else hires_defaults.get("steps"),
        hires_denoise=values.get("hires_denoise") if values.get("hires_denoise") is not None else hires_defaults.get("denoise"),
        hires_cfg=values.get("hires_cfg") if values.get("hires_cfg") is not None else hires_defaults.get("cfg"))
    recipes = []
    for i in range(count):
        template = prompts[min(i, len(prompts) - 1)]
        seed = seed_for_image(strategy, base_seed, i)
        prompt = expand_for_image(template, catalog, seed)
        recipes.append({**common, "index": i + 1, "prompt": prompt, **({"prompt_template": template} if prompt != template else {}),
                        "seed": seed})
    return recipes


def _prompts_edit(old: dict | None, new: dict) -> dict:
    """A hand-edited `prompts` field: prompts whose text you changed get the reason "Edited by you" (the reasons of the
    others stay), and entries without a reason of their own are padded to the new length."""
    value, old_value = new.get("value") or [], (old or {}).get("value") or []
    old_items = (old or {}).get("items") or []
    items = list(new.get("items") or [])
    if not items and old_items:
        items = [{"reason": old_items[i].get("reason", "") if i < len(old_items) else "",
                  **({"tagcheck": old_items[i]["tagcheck"]} if i < len(old_items) and old_items[i].get("tagcheck") else {})} for i in range(len(value))]
        for i, text in enumerate(value):
            if i < len(old_value) and text != old_value[i]:
                items[i] = {"reason": "Edited by you"}        # the tag check's record belonged to the old text
    for i, item in enumerate(items):    # the wildcard ideas stay with their prompt (new ones from a tweak win); what counts as used follows the edit
        before = (old_items[i] if i < len(old_items) else {}).get("ideas") or {}
        ideas = item.get("ideas") or before
        if ideas.get("offered") and i < len(value):
            item["ideas"] = idea_record(ideas["offered"], value[i], ideas.get("lists"))
    return {**new, **({"items": items} if items else {})}


def why_not(wf: dict) -> str:
    parts = [f"{m['problem']}: {m['class']}" + (f".{m['input']} = {m['value']}" if m.get("input") else "") for m in wf["missing"]]
    return "; ".join(parts + [w for w in wf["warnings"] if isinstance(w, str)]) or "unknown reason"


def _check_against_object_info(fields: dict, oi: dict):
    """Sampler, scheduler and LoRA names must exist in ComfyUI."""
    sampler_in = oi.get("KSampler", {}).get("input", {}).get("required", {})
    for name, key in (("sampler", "sampler_name"), ("scheduler", "scheduler")):
        value = fields.get(name, {}).get("value")
        options = sampler_in.get(key, [[]])[0]
        if value is not None and options and value not in options:
            raise JobError(f"{name} '{value}' is not available in ComfyUI.")
    lora_options = oi.get("LoraLoaderModelOnly", {}).get("input", {}).get("required", {}).get("lora_name", [[]])[0]
    for lora in fields["loras"]["value"]:
        if lora_options and lora["name"] not in lora_options:
            raise JobError(f"LoRA '{lora['name']}' not found in ComfyUI's loras folder.")
