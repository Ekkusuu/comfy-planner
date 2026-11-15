"""PlanRunner: runs planning in the background under one GPU lease and reports progress.

A planning run (new request, re-plan of some jobs, or a tweak) is a row in the `plans` table. Its status goes
queued -> waiting (for the queue's current image) -> loading (the LLM) -> planning -> done | failed | cancelled. Every change is
published on the event stream as {"type": "plan", "plan": {...}} and can be read with GET /api/plans/{id}. The LLM
is loaded once per run, all calls happen inside it, and the lease confirms the LLM process is gone before the queue
continues, so generation never overlaps planning.

Plans run one at a time, in the order they were submitted: the line. One task takes the lease once and works through the
line with the LLM staying loaded; plans submitted meanwhile join it, and the lease is given back only when the line is empty.
Only the first plan of the line is worked on (and reports waiting / loading / planning); the others stay `queued` and
carry a `queue_position` (1 is next up). A plan still in the line can be removed at once and never touches the GPU.
"""
import asyncio
import logging
import time
from collections import deque

from app.comfy.client import ComfyError
from app.config import Config
from app.db import Database, dumps, now
from app.events import EventBus
from app.fields import make_field, make_issue
from app.gpu import GpuLease
from app.llm.server import LlmServerError
from app.planning import content_safety, locks
from app.planning import offers as offering
from app.planning.briefs import brief_jobs, expands, write_briefs
from app.planning.context import PlanContext
from app.planning.lora_negative import asked_to_avoid
from app.planning.mentions import mentioned, plain
from app.planning.planner import WORKFLOW_META, PlanError, plan_job, plan_request
from app.planning.scene import replan_scene_for_tweak, stored_scene, tweak_touches_scene
from app.planning.separator import parse_request
from app.planning.tweak import apply_tweak
from app.queue.jobs import JobError, JobStore, why_not

log = logging.getLogger("planning")
STAGE_TEXT = {"waiting": "Waiting for the current image to finish", "loading": "Loading the LLM"}
STEP_TEXT = {"loras": "choosing LoRAs", "wildcards": "choosing wildcard lists", "scene": "planning the scene", "prompts": "writing prompts", "settings": "choosing settings"}


class PlanRunner:
    def __init__(self, ctx: PlanContext, store: JobStore, lease: GpuLease, comfy, events: EventBus, config: Config, db: Database):
        self.ctx, self.store, self.lease, self.comfy, self.events, self.config, self.db = ctx, store, lease, comfy, events, config, db
        self._line: deque[tuple[int, object]] = deque()     # (plan id, run) in the order submitted; the first one is being worked on
        self._drain_task: asyncio.Task | None = None        # the task that works through the line
        self._cancelled: set[int] = set()       # plans the user cancelled: they stop before their next job

    # ---------- requests ----------

    def plan(self, request: str, workflow_id: str, input_images: list[str] | None = None) -> dict:
        """Start planning a request on the workflow the user chose, in the background. Returns the plan record to poll.
        A request that can't be planned (no text, no or unusable workflow) is refused here, before the GPU is touched."""
        request = (request or "").strip()
        if not parse_request(request)[1]:
            raise JobError("Write what you want first.", "request")
        wf, card = self._chosen(workflow_id, input_images or [])
        images = input_images if wf["kind"] == "edit" else []
        return self._start("plan", request, lambda plan: self._plan_new(plan, request, wf, card, images))

    def replan(self, job_ids: list[int]) -> dict:
        """Re-plan these jobs: their locked fields stay, everything else is planned again."""
        if not job_ids:
            raise JobError("No jobs to re-plan.", "job")
        for job_id in job_ids:
            job = self.store.get_job(job_id, images=False)
            if job["status"] not in ("draft", "ready"):
                raise JobError(f"Job {job_id} is {job['status']}; only jobs that haven't been queued can be re-planned.", "job")
        return self._start("replan", "", lambda plan: self._replan(plan, job_ids), job_ids)

    def cancel(self, plan_id: int) -> dict:
        """Stop a planning run before its next job. Jobs already planned stay; the ones not reached are left as drafts
        ("Re-plan failed" plans them). A plan still waiting in the line is taken out at once and never runs."""
        plan = self.get(plan_id)        # KeyError (404) for an unknown one
        if plan["status"] in ("done", "failed", "cancelled"):
            raise JobError("That planning run has already finished.", "plan")
        place = self._place(plan_id)
        if place:
            del self._line[place]
            self._fail_open_jobs(plan_id, "Planning was cancelled before this job.")
            self._set(plan_id, "cancelled", "Removed from the line")
            self._announce_line()
            return self.get(plan_id)
        self._cancelled.add(plan_id)
        self._set(plan_id, message="Cancelling after the current step")
        return self.get(plan_id)

    def _stopped(self, plan: dict) -> bool:
        return plan["id"] in self._cancelled

    def tweak(self, job_id: int, instruction: str) -> dict:
        """Change a job according to a short instruction, leaving locked fields alone."""
        job = self.store.get_job(job_id, images=False)
        if job["status"] not in ("draft", "ready") or not job["workflow_id"]:
            raise JobError(f"Job {job_id} can't be tweaked: it needs a workflow and must not be queued yet.", "job")
        if not instruction.strip():
            raise JobError("Write what to change.", "instruction")
        return self._start("tweak", instruction.strip(), lambda plan: self._tweak(plan, job_id, instruction), [job_id])

    # ---------- plan records ----------

    def get(self, plan_id: int) -> dict:
        plan = self.db.row("SELECT * FROM plans WHERE id = ?", (plan_id,))
        if plan is None:
            raise KeyError(plan_id)
        plan["seconds"] = round(sum(t["seconds"] for t in plan["timings"]), 1)
        plan["queue_position"] = self._place(plan_id) or None      # 1 = next up; None for the plan being worked on and for finished ones
        marks = ",".join("?" * len(plan["job_ids"]))
        found = {j["id"]: j for j in self.db.rows(f"SELECT id, status, description FROM jobs WHERE id IN ({marks})", tuple(plan["job_ids"]))}
        plan["jobs"] = [found[i] for i in plan["job_ids"] if i in found]
        return plan

    def recent(self, limit: int = 20) -> list[dict]:
        """The newest `limit` plans, and every plan still in the line however old, so a page that reloads sees the whole line."""
        rows = self.db.rows("SELECT id FROM plans WHERE status NOT IN ('done', 'failed', 'cancelled') OR id IN (SELECT id FROM plans ORDER BY id DESC LIMIT ?) "
                            "ORDER BY id DESC", (limit,))
        return [self.get(r["id"]) for r in rows]

    def _place(self, plan_id: int) -> int | None:
        """How many plans are ahead of this one in the line (0: it is being worked on); None if it is not in the line."""
        return next((n for n, (queued, _) in enumerate(self._line) if queued == plan_id), None)

    def _announce_line(self):
        """The line moved: publish every plan in it, so the positions on screen follow."""
        for plan_id, _ in self._line:
            self.events.publish({"type": "plan", "plan": self.get(plan_id)})

    def _set(self, plan_id: int, status: str | None = None, message: str | None = None, **cols):
        updates = {k: v for k, v in {"status": status, "message": message, **cols}.items() if v is not None}
        for key in ("job_ids", "timings"):
            if key in updates:
                updates[key] = dumps(updates[key])
        if status in ("done", "failed", "cancelled"):
            updates["finished_at"] = now()
        self.db.execute(f"UPDATE plans SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?", (*updates.values(), plan_id))
        self.events.publish({"type": "plan", "plan": self.get(plan_id)})

    def _job_event(self, job: dict):
        self.events.publish({"type": "job", "job_id": job["id"], "status": job["status"]})

    def _chosen(self, workflow_id: str, input_images: list[str]):
        """The workflow the user picked and its usable card, or a JobError that says in plain words why it can't be planned with."""
        if not (workflow_id or "").strip():
            raise JobError("Choose a workflow first.", "workflow")
        try:
            wf = self.ctx.library.get(workflow_id)
        except KeyError:
            raise JobError("That workflow doesn't exist. Choose another one.", "workflow") from None
        if not wf["runnable"]:
            raise JobError(f"'{wf['title']}' can't run yet: {why_not(wf)}. Choose another workflow, or fix it in the Library.", "workflow")
        card = self.ctx.workflow_card(wf["id"])
        if card is None:
            raise JobError(f"'{wf['title']}' has no approved card. Approve it in the Library, or allow draft cards in Settings.", "workflow")
        # the first input image is required, the others are optional (an image you don't give is left out of the graph)
        if wf["kind"] == "edit" and not input_images:
            raise JobError(f"'{wf['title']}' edits pictures you give it: add the image to edit.", "input_images")
        return wf, card

    def _start(self, kind: str, request: str, run, job_ids: list[int] | None = None) -> dict:
        plan_id = self.db.execute("INSERT INTO plans (kind, request, status, message, job_ids, model, created_at) VALUES (?,?,?,?,?,?,?)",
                                  (kind, request, "queued", "Waiting to start", dumps(job_ids or []), self.config.llm_model, now())).lastrowid
        for job_id in job_ids or []:
            if kind == "replan":
                self.store.mark_planning(job_id, plan_id)
                self._job_event(self.store.get_job(job_id, images=False))
        self._line.append((plan_id, run))
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())
        self.events.publish({"type": "plan", "plan": self.get(plan_id)})
        return self.get(plan_id)

    async def close(self):
        if self._drain_task is not None:
            self._drain_task.cancel()
            await asyncio.gather(self._drain_task, return_exceptions=True)

    # ---------- the line ----------

    async def _drain(self):
        """Work through the line until it is empty. The jobs of every plan that was waiting or running when the app stops are freed."""
        try:
            while self._line:
                await self._session()
        except asyncio.CancelledError:
            for plan_id, _ in self._line:
                self._fail_open_jobs(plan_id, "The app stopped before planning finished.")
            self._line.clear()
            raise

    async def _session(self):
        """Take the GPU once and run the plans of the line back to back with the LLM staying loaded; plans that join meanwhile are run too.
        The lease is given back when the line is empty, or after a plan on which the LLM server failed (the next session starts it again).
        Every session finishes at least one plan, so the line always gets shorter: if the lease can't be taken (the LLM won't start), the plan
        at the front fails with that message and the others get their turn."""
        done = 0
        try:
            self._begin(self._line[0][0])       # the plan at the front waits for the GPU, which counts as its time
            async with self.lease.session(self._stage):
                while self._line:
                    plan_id, run = self._line[0]
                    if done:        # the first plan began before the lease was taken
                        self._begin(plan_id)
                    restart = await self._work(plan_id, run)
                    self._line.popleft()
                    done += 1
                    self._announce_line()
                    if restart:
                        self.lease.stop_on_leave()      # a failed server is stopped, not kept for the next plan
                        break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if done:
                log.warning("leaving the GPU failed after the plans were done: %s", e)       # nothing is left to fail
                return
            plan_id, _ = self._line.popleft()
            self._fail(plan_id, e, [])
            self._announce_line()

    def _begin(self, plan_id: int):
        """The plan leaves the line: from here on its time counts."""
        self._set(plan_id, started_at=now())

    def _stage(self, name: str):
        """The lease reports waiting / loading for the plan at the front of the line."""
        self._set(self._line[0][0], name, STAGE_TEXT[name])

    # ---------- the run ----------

    async def _work(self, plan_id: int, run) -> bool:
        """Run one plan inside the lease and record how it ended. Whatever happens, jobs left in 'planning' are freed. Returns True when the
        LLM server itself failed, so the session should start it again for the plans that follow."""
        plan = {"id": plan_id, "timings": [], "started": time.monotonic(), "failed": 0}
        try:
            if not self._stopped(plan):
                try:
                    self.ctx.oi = await self.comfy.object_info()
                except ComfyError:
                    self.ctx.oi = None      # ComfyUI is down: sampler / LoRA lists come from the cards only
                self._set(plan_id, "planning", "Reading the request")
                await run(plan)
            if plan_id in self._cancelled:
                self._fail_open_jobs(plan_id, "Planning was cancelled before this job.")
                self._set(plan_id, "cancelled", self._summary(plan_id, "Cancelled"), timings=plan["timings"])
            else:
                failed = f" {plan['failed']} {'job' if plan['failed'] == 1 else 'jobs'} failed." if plan["failed"] else ""
                said = " ".join(n.rstrip(".") + "." for n in plan.get("notes") or [])
                self._set(plan_id, "done", ("Done." + failed if failed else "Done") + (f" {said}" if said else ""), timings=plan["timings"])
        except asyncio.CancelledError:
            raise       # the line frees the jobs of every plan it still holds
        except Exception as e:
            self._fail(plan_id, e, plan["timings"])
            return isinstance(e, LlmServerError)
        finally:
            self._cancelled.discard(plan_id)
        return False

    def _fail(self, plan_id: int, e: Exception, timings: list):
        """Record that a plan failed, with the reason in plain words, and free its jobs."""
        if isinstance(e, (PlanError, LlmServerError, ComfyError, OSError, JobError, ValueError, KeyError)):
            message = str(e.args[0] if isinstance(e, KeyError) and e.args else e)
            log.warning("planning failed: %s", message)
        else:
            message = f"Unexpected error: {type(e).__name__}"
            log.error("planning crashed", exc_info=e)
        self._fail_open_jobs(plan_id, message)
        self._set(plan_id, "failed", message, error=message, timings=timings)

    def _summary(self, plan_id: int, start: str) -> str:
        """"Cancelled: 37 of 100 jobs planned" for the jobs this run made."""
        jobs = self.db.rows("SELECT status FROM jobs WHERE plan_id = ?", (plan_id,))
        planned = sum(1 for j in jobs if j["status"] not in ("planning", "draft"))
        return f"{start}: {planned} of {len(jobs)} {'job' if len(jobs) == 1 else 'jobs'} planned"

    def _fail_open_jobs(self, plan_id: int, message: str):
        for row in self.db.rows("SELECT id FROM jobs WHERE plan_id = ? AND status = 'planning'", (plan_id,)):
            self._job_event(self.store.fail_planning(row["id"], message))

    # ---------- the three kinds of run ----------

    async def _plan_new(self, plan: dict, request: str, wf: dict, card, input_images: list[str]):
        """A new request: first every job is made (the planner splits the request, big jobs and "N images per LoRA" requests are written out as
        one description per image), then the jobs are planned one by one. A part or job that can't be made becomes a draft
        card that says so."""
        shared, parts = parse_request(request)      # the text before `===` goes to every part
        made: list[tuple[dict, dict]] = []      # (job row, item) of every job that has to be planned
        broken: list[tuple[str, str]] = []      # (text, why) of parts and jobs that could not be made
        left_out: list[str] = []                # parts that came after the request reached max_images_per_request
        plan.setdefault("notes", [])            # what the plan says about itself in its message (who was included, who was left out)
        plan["state"] = offering.PlanState()    # wildcard entries used and scenes planned so far, shared by all jobs of this run
        images = 0
        for number, part in enumerate(parts, 1):
            if self._stopped(plan):
                break
            label = f"Part {number} of {len(parts)}: " if len(parts) > 1 else ""
            room = self.config.max_images_per_request - images
            if room < 1:
                left_out.append(part)
                continue
            self._set(plan["id"], "planning", f"{label}reading the request".capitalize())
            try:
                items, meant = await plan_request(self.ctx, part, wf, card, input_images, room, shared)
            except PlanError as e:
                if len(parts) == 1:
                    raise
                broken.append((part, str(e)))
                continue
            if meant.group_kind and not meant.members:      # "N images per <kind> LoRA" with no LoRA of that kind to use: the card says why
                hard = next((i["message"] for i in meant.issues if i.get("code") == "lora_group"), "There is no LoRA to make the images with.")
                if len(parts) == 1:
                    raise PlanError(hard)
                broken.append((part, hard))
                continue
            for item in items:
                try:
                    children = await self._expand(plan, item, card, label, meant, self.config.max_images_per_request - images)
                except PlanError as e:
                    broken.append((item["description"], str(e)))
                    continue
                if meant.issues:
                    children[0]["issues"] = [*children[0]["issues"], *meant.issues]
                    meant.issues = []       # said once
                for child in children:
                    row = self.store.create_planning_job(child["description"], child["count"], child["workflow_id"], child["meta"], plan["id"], plan["id"])
                    made.append((row, child))
                    images += child["count"]
                    self._job_event(row)
                self._set(plan["id"], job_ids=[r["id"] for r, _ in made])
        if left_out:
            broken.append((" --- ".join(left_out), f"Left out: one request can add up to {self.config.max_images_per_request} images, and the parts before "
                                                  f"these already reach it ({len(left_out)} {'part' if len(left_out) == 1 else 'parts'})."))
        if not made and broken:
            raise PlanError(broken[0][1])
        keys = self.ctx.lora_keys()
        for text, why in broken:        # visible as cards that say why; Re-plan plans them from their text
            named = list(dict.fromkeys([*mentioned(shared, keys), *mentioned(text, keys)]))      # a LoRA named there stays named
            meta = {"request": f"{text} (for all images: {shared})" if shared else text, "reason": why, "constraints": [plain(shared, keys)] if shared else [], "workflow": dict(WORKFLOW_META),
                    **({"loras_named": named} if named else {})}
            row = self.store.create_planning_job(plain(text, keys)[:400], 1, wf["id"], meta, plan["id"], plan["id"])
            self._job_event(self.store.fail_planning(row["id"], why))
        if images < 2:      # a request that gives one image is not a batch
            self.store.clear_batch(plan["id"])
        self._set(plan["id"], job_ids=[r["id"] for r in self.db.rows("SELECT id FROM jobs WHERE plan_id = ? ORDER BY id", (plan["id"],))])
        for n, (row, item) in enumerate(made, 1):
            if self._stopped(plan):
                break
            fields = {"input_images": make_field(input_images, "locked", "the image you gave")} if input_images else {}
            await self._plan_one(plan, row["id"], wf, fields, item["issues"], item["meta"], n, len(made))

    async def _expand(self, plan: dict, item: dict, card, label: str, meant, room: int) -> list[dict]:
        """The jobs a planned job becomes. A plain job is itself. A job for "N images per <kind> LoRA" becomes jobs for each LoRA of the group (as
        many as `room` images allow; the ones left out are named in the plan's message and on the first job), each with its LoRA fixed, and the plan's
        message says what those jobs really hold. A job that needs a description per image (many different images, more than a job holds, something
        that must differ in every image) has them written together for the whole set, so no two repeat, and becomes one job per description."""
        members = list(meant.members) if meant.group_kind else []
        per = item["count"]
        if members:
            fit = max(room // per, 0)
            if fit < len(members):
                cut = [(m, f"over the limit of {self.config.max_images_per_request} images per request") for m in members[fit:]]
                members = members[:fit]
                meant.left_out = [*meant.left_out, *cut]
            if not members:
                raise PlanError(f"{per} images for each {meant.group_kind} LoRA do not fit in the limit of {self.config.max_images_per_request} images per request.")
            meant.members = members
        children = await self._jobs(plan, item, card, label, meant, members, per)
        if members:      # said from the jobs that exist, not from what was asked: a LoRA that got fewer images (or none) is named as such
            got: dict[str, int] = {}
            for child in children:
                member = ((child["meta"].get("intent") or {}).get("group") or {}).get("member")
                if member:
                    got[member] = got.get(member, 0) + child["count"]
            text = meant.summary(got, per)
            plan["notes"].append(text)
            if meant.left_out or any(got.get(m, 0) != per for m in members):
                children[0]["issues"].append(make_issue("loras", text, "warning", "planner_note"))
        return children

    async def _jobs(self, plan: dict, item: dict, card, label: str, meant, members: list[str], per: int) -> list[dict]:
        wants_different = item["variety"] == "unique" or bool((item["meta"].get("intent") or {}).get("vary"))
        distinct = expands(item, self.config) or (bool(members) and wants_different)       # also one image per LoRA: each LoRA gets its own idea
        if members and not distinct:       # every LoRA of the group gets a picture of its own
            return await self._member_pictures(plan, item, card, label, meant, members, per)
        if item["meta"].get("picture_open") and not distinct:       # the request names no picture: one concrete picture is written for the whole job
            item = await self._invent_picture(plan, item, card)
        if not members and not distinct:
            return [item]
        total = per * len(members) if members else per
        started = time.monotonic()

        def progress(done: int):
            self._set(plan["id"], "planning", f"{label}writing ideas, {done} of {total}".capitalize())
        briefs, found, subjects, assigned, picks = await write_briefs(self.ctx, {**item, "count": total}, card, progress, lambda: self._stopped(plan), plan["state"],
                                                                      parts=len(members) or 1, loras=members)
        plan["timings"].append({"job_id": None, "step": "briefs", "seconds": round(time.monotonic() - started, 1)})
        children = brief_jobs(item, briefs, subjects, members or None, per, assigned, picks, meant.group_kind)
        children[0]["issues"] += found
        if members and len(briefs) < total:
            children[0]["issues"].append(make_issue("job", f"Only {len(briefs)} of the {total} images could be written, so the last LoRAs got fewer or none.", "warning", "planner_note"))
        return children

    async def _member_pictures(self, plan: dict, item: dict, card, label: str, meant, members: list[str], per: int) -> list[dict]:
        """The jobs of a group whose images are not each their own idea ("2 images per style lora", "one image of a girl with every style lora"):
        one job per LoRA, each with a picture of its own. The descriptions of the whole group are written together (write_briefs: the wildcard
        ideas, the content level, the people; none repeats another, and anything the request does say - the rating, a subject, "looking away" -
        holds for all of them), and job n gets description n. Each job keeps its `per` images, so the counts are the same as for a group that shares
        one description. Their cameras and gazes are chosen when each job is planned, against the entries the earlier jobs of the run used (PlanState)."""
        total = len(members)
        started = time.monotonic()
        briefs, found, subjects, assigned, picks = await write_briefs(
            self.ctx, {**item, "count": total}, card, lambda done: self._set(plan["id"], "planning", f"{label}writing pictures, {done} of {total}".capitalize()),
            lambda: self._stopped(plan), plan["state"], parts=total, loras=members)
        plan["timings"].append({"job_id": None, "step": "briefs", "seconds": round(time.monotonic() - started, 1)})
        shortfall = []
        if len(briefs) < total:         # a LoRA is not left out for want of an idea: the pictures are used again, in turn
            shortfall = [make_issue("job", f"Only {len(briefs)} of the {total} pictures could be written, so some LoRAs share a picture.", "warning", "planner_note")]
            again = lambda xs: [xs[i % len(xs)] for i in range(total)]
            briefs, subjects, assigned = again(briefs), again(subjects), again(assigned)
        children = brief_jobs(item, briefs, subjects, members, 1, assigned, picks, meant.group_kind)
        for child in children:
            child["count"] = per
        children[0]["issues"] += found + shortfall
        return children

    async def _invent_picture(self, plan: dict, item: dict, card) -> dict:
        """The job with a picture of its own: the request said only how to make the pictures ("2 images of whatever"), so the description holds no
        subject. One description is written the way a big request's are (write_briefs: with the wildcard ideas, the content level, the people),
        and every image of the job shows it."""
        started = time.monotonic()
        briefs, found, subjects, _, _ = await write_briefs(self.ctx, {**item, "count": 1}, card, None, lambda: self._stopped(plan), plan["state"])
        plan["timings"].append({"job_id": None, "step": "briefs", "seconds": round(time.monotonic() - started, 1)})
        who = subjects[0] if subjects else None
        return {**item, "description": briefs[0], "issues": [*item["issues"], *found], "meta": {**item["meta"], **({"subjects": who} if who else {})}}

    async def _replan(self, plan: dict, job_ids: list[int]):
        for n, job_id in enumerate(job_ids, 1):
            if self._stopped(plan):
                break
            job = self.store.get_job(job_id, images=False)
            wf = self.ctx.library.get(job["workflow_id"]) if job["workflow_id"] else None
            await self._plan_one(plan, job_id, wf, job["fields"], job["issues"], job["meta"], n, len(job_ids))

    async def _plan_one(self, plan: dict, job_id: int, wf: dict | None, fields: dict, issues: list[dict], meta: dict, n: int, total: int):
        """Plan one job (steps 2-4) and store the result; a job without a usable workflow or card just gets its issues."""
        label = f"Job {n} of {total}" if total > 1 else ""
        job = {**self.store.get_job(job_id, images=False), "fields": fields, "issues": issues, "meta": meta}
        card = self.ctx.workflow_card(wf["id"]) if wf else None
        if wf is not None and card is None:
            issues = [*issues, make_issue("workflow", f"'{wf['title']}' has no approved card, so it can't be planned. Approve it in the Library or allow draft cards in Settings.", code="no_card")]
            self._job_event(self.store.finish_planning(job_id, wf, fields, issues, meta, self.ctx.oi))
            return
        if wf is None or not wf["runnable"]:
            self._job_event(self.store.finish_planning(job_id, wf, fields, issues, meta, self.ctx.oi))
            return
        try:
            result = await plan_job(self.ctx, job, wf, card, lambda step: self._set(plan["id"], "planning", f"{label}: {STEP_TEXT[step]}" if label else STEP_TEXT[step].capitalize()),
                                    plan.setdefault("state", offering.PlanState()))
        except LlmServerError:
            self._job_event(self.store.fail_planning(job_id, "Planning failed: the LLM server stopped answering"))
            raise
        except Exception as e:      # this job fails, the rest of the batch goes on
            log.exception("planning job %s failed", job_id)
            plan["failed"] += 1
            self._job_event(self.store.fail_planning(job_id, f"Planning failed: {type(e).__name__}"))
            return
        plan["timings"].extend({"job_id": job_id, **t} for t in result["timings"])
        done = self.store.finish_planning(job_id, wf, result["fields"], result["issues"], result.get("meta", meta), self.ctx.oi)
        self._job_event(done)
        self._set(plan["id"], "planning", f"{label}: done" if label else "Job planned", timings=plan["timings"])

    async def _tweak(self, plan: dict, job_id: int, instruction: str):
        job = self.store.get_job(job_id, images=False)
        wf, card = self.ctx.library.get(job["workflow_id"]), self.ctx.workflow_card(job["workflow_id"])
        if card is None:
            raise PlanError(f"'{wf['title']}' has no approved card, so it can't be tweaked.")
        self._set(plan["id"], "planning", "Working out the change")
        scene = stored_scene(job)
        started = time.monotonic()
        if scene and tweak_touches_scene(instruction):       # the change is about the picture: make the scene plan again
            self._set(plan["id"], "planning", "Planning the scene again")
            fresh, found = await replan_scene_for_tweak(self.ctx, job, instruction)
            plan["timings"].append({"job_id": job_id, "step": "scene", "seconds": round(time.monotonic() - started, 1)})
            if blocked := [i for i in found if i.get("severity") == "blocking"]:
                for issue in blocked:
                    content_safety.log_block(issue["code"], job_id)
                done = self.store.finalize(job_id, wf, job["count"], job["fields"], [*job["issues"], *found])
                self._job_event(done)
                raise PlanError(blocked[0]["message"])
            if fresh:
                scene = fresh
                self.store.update_meta(job_id, scene=scene, scene_options={}, appearance_options={})
        steps: list[dict] = []          # steps apply_tweak ran on their own (choosing wildcard lists)
        started = time.monotonic()
        patch = await apply_tweak(self.ctx, job, wf, card, instruction, steps, scene=scene)
        plan["timings"] += [{"job_id": job_id, **t} for t in steps]
        plan["timings"].append({"job_id": job_id, "step": "tweak", "seconds": round(max(time.monotonic() - started - sum(t["seconds"] for t in steps), 0), 1)})
        if patch:
            if "negative" in patch and (asked := asked_to_avoid(instruction)):       # words you ask to avoid stay when a LoRA wants them out
                self.store.update_meta(job_id, negative_asked=sorted(asked | set(job["meta"].get("negative_asked") or [])))
            self._job_event(self.store.update_fields(job_id, patch, self.ctx.oi))
        self._set(plan["id"], "planning", "Changed " + (", ".join(patch) if patch else "nothing"), timings=plan["timings"])
