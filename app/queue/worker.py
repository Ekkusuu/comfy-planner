"""The queue worker: one asyncio task that runs queued jobs, one image (one /prompt) at a time."""
import asyncio
import logging
import time
from datetime import date

from app.comfy.client import ComfyError, ComfyInterrupted, ComfyUnreachable
from app.comfy.graph import GraphError, build_graph, negative_text
from app.config import Config
from app.events import EventBus
from app.library.workflows import WorkflowLibrary
from app.queue.jobs import JobStore
from app.thumbs import BadImage, make_thumbs

log = logging.getLogger("worker")
POLL_SECONDS = 2.0
PREVIEW_SIGNAL_SECONDS = 0.4   # at most about 2 to 3 "new preview" signals a second per job


class QueueWorker:
    def __init__(self, store: JobStore, library: WorkflowLibrary, comfy, config: Config, events: EventBus):
        self.store, self.library, self.comfy, self.config, self.events = store, library, comfy, config, events
        self.paused_by_user = False
        self.held = False                        # held by the GPU lease (planning / card building)
        self.unreachable: str | None = None      # message while ComfyUI can't be reached
        self.current: dict | None = None         # {"job_id", "image_id", "prompt_id"}
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()             # set while no image is being generated
        self._idle.set()
        self._released = asyncio.Event()         # set while the GPU lease isn't holding the queue
        self._released.set()
        self._run_task: asyncio.Task | None = None   # the comfy.run() call of the current image
        self._cancelled_jobs: set[int] = set()
        self._preview: tuple[int, int, bytes] | None = None   # (job_id, number, bytes): newest step preview of the image running now
        self._preview_signalled = 0.0
        self._task: asyncio.Task | None = None
        self._thumb_tasks: set[asyncio.Task] = set()   # thumbnails being cut in threads (kept so they aren't garbage collected)
        self.before_image = None                 # async callable run before each image: stops an LLM kept loaded (GpuLease.free_for_images)

    # ---------- lifecycle ----------

    def start(self):
        self.store.recover()
        self._task = asyncio.create_task(self.run_forever())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._preview = None

    def wake(self):
        self._wake.set()

    def pause(self):
        self.paused_by_user = True
        self._emit("queue")

    def resume(self):
        self.paused_by_user = False
        self.wake()
        self._emit("queue")

    async def hold(self):
        """Stop starting new images and wait for the current one to finish (the GPU lease needs the GPU)."""
        self.held = True
        self._released.clear()
        self._emit("queue")
        await self._idle.wait()

    def release(self):
        """End a hold. The queue continues unless the user paused it."""
        self.held = False
        self._released.set()
        self.wake()
        self._emit("queue")

    def has_work(self) -> bool:
        """True when images would start now if the GPU were free: a job is queued or running and the user hasn't paused the queue."""
        return not self.paused_by_user and self.store.has_runnable()

    def state(self) -> dict:
        return {"paused": self.paused_by_user, "held": self.held, "unreachable": self.unreachable, "current": self.current}

    def _emit(self, kind: str, **data):
        self.events.publish({"type": kind, **data, "queue": self.state()} if kind == "queue" else {"type": kind, **data})

    # ---------- main loop ----------

    async def run_forever(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker error")
                await asyncio.sleep(POLL_SECONDS)

    async def tick(self):
        """Run the next queued job, or wait for work."""
        job = None if self.paused_by_user or self.held else self.store.next_queued()
        if job is None:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            return
        if not await self._comfy_ready():
            await asyncio.sleep(POLL_SECONDS)
            return
        if self.held:
            return   # the GPU lease took the queue while we were checking ComfyUI
        await self.run_job(job)

    async def _comfy_ready(self) -> bool:
        """Check ComfyUI is up; (un)set the 'not reachable' pause message."""
        up = await self.comfy.is_up()
        message = None if up else f"ComfyUI isn't reachable at {self.config.comfy_url}. Start it, and the queue continues."
        if message != self.unreachable:
            self.unreachable = message
            self._emit("queue")
        return up

    # ---------- one job ----------

    async def run_job(self, job: dict):
        self._cancelled_jobs.discard(job["id"])
        self.store.start_job(job["id"])
        self._emit("job", job_id=job["id"], status="running")
        wf = self.library.get(job["workflow_id"])
        api_graph = self.library.get_graph(job["workflow_id"])
        for image in job["images"]:
            fresh = self.store.get_job(job["id"], images=False)
            if fresh["status"] != "running":
                break   # cancelled while running
            if image["status"] != "pending":
                continue
            await self._gpu_ready()
            if not await self._run_image(job, image, wf, api_graph):
                self.store.requeue_job(job["id"])   # ComfyUI went away: try again later
                self._emit("job", job_id=job["id"], status="queued")
                return
        final = self.store.finish_job(job["id"])
        self._emit("job", job_id=job["id"], status=final["status"])

    async def _gpu_ready(self):
        """Wait until the GPU lease isn't holding the queue (it may hold it between images) and no LLM is left loaded on the GPU.
        Nothing here may yield between the last check and the start of the image, so a lease taken meanwhile is noticed and waited for."""
        while True:
            await self._released.wait()
            if self.before_image:
                await self.before_image()
            if self._released.is_set():
                return

    async def _run_image(self, job: dict, image: dict, wf: dict, api_graph: dict) -> bool:
        """Generate one image. Returns False if ComfyUI is unreachable (image goes back to pending)."""
        store, job_id = self.store, job["id"]
        recipe = image["recipe"]
        self._idle.clear()
        store.set_image(image["id"], "running")
        self.current = {"job_id": job_id, "image_id": image["id"], "idx": image["idx"], "prompt_id": None}
        self._emit("image", job_id=job_id, image_id=image["id"], idx=image["idx"], status="running")
        try:
            graph = build_graph(api_graph, wf["slots"], recipe, self.config.safety_negative)
            recipe = {**recipe, "negative_sent": negative_text(wf["slots"], recipe, self.config.safety_negative) if wf["slots"].get("negative") else None}
            self._run_task = asyncio.ensure_future(self.comfy.run(graph, lambda e: self._on_event(job_id, image, e),
                                                         lambda data: self._on_preview(job_id, data)))
            outputs = await self._run_task
            if not outputs:
                raise ComfyError("ComfyUI finished but returned no saved image.")
            path = self._save(job_id, image["idx"], await self.comfy.view(outputs[0]))
            self._cut_thumbs(image["id"], path)
            store.set_image(image["id"], "done", file_path=path, recipe=recipe, prompt_id=(self.current or {}).get("prompt_id"))
            self._emit("image", job_id=job_id, image_id=image["id"], idx=image["idx"], status="done", file=path)
        except ComfyUnreachable:
            store.set_image(image["id"], "pending")
            self.unreachable = f"ComfyUI isn't reachable at {self.config.comfy_url}. Start it, and the queue continues."
            self._emit("queue")
            return False
        except ComfyInterrupted:
            self._image_cancelled(job_id, image)
        except asyncio.CancelledError:
            if job_id not in self._cancelled_jobs:
                raise   # the app is shutting down
            self._image_cancelled(job_id, image)
        except (ComfyError, GraphError) as e:
            store.set_image(image["id"], "failed", error=str(e))
            self._emit("image", job_id=job_id, image_id=image["id"], idx=image["idx"], status="failed", error=str(e))
        finally:
            self.current = self._run_task = self._preview = None
            self._idle.set()
        return True

    def _image_cancelled(self, job_id: int, image: dict):
        self._cancelled_jobs.discard(job_id)
        self.store.set_image(image["id"], "cancelled", error="cancelled")
        self._emit("image", job_id=job_id, image_id=image["id"], idx=image["idx"], status="cancelled")

    def _on_event(self, job_id: int, image: dict, event: dict):
        if event["type"] == "queued" and self.current:
            self.current["prompt_id"] = event["prompt_id"]
        self._emit("progress", job_id=job_id, image_id=image["id"], idx=image["idx"], **event)

    def _on_preview(self, job_id: int, data: bytes):
        """Keep the newest step preview (memory only, one frame) and tell the UI, a few times a second at most."""
        if not self.current or self.current["job_id"] != job_id:
            return   # a late frame of an image that is over
        number = (self._preview[1] + 1) if self._preview and self._preview[0] == job_id else 1
        self._preview = (job_id, number, data)
        now = time.monotonic()
        if now - self._preview_signalled >= PREVIEW_SIGNAL_SECONDS:
            self._preview_signalled = now
            self._emit("preview", job_id=job_id, number=number)

    def preview_for(self, job_id: int) -> bytes | None:
        """The newest step preview of this job's running image, or None."""
        return self._preview[2] if self._preview and self._preview[0] == job_id else None

    def _save(self, job_id: int, idx: int, data: bytes) -> str:
        """Write the PNG to data/images/YYYY-MM-DD/<job>-<n>.png; returns the path relative to data/images."""
        rel = f"{date.today().isoformat()}/{job_id}-{idx}.png"
        target = self.config.images_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return rel

    def _cut_thumbs(self, image_id: int, rel: str):
        """Make the thumbnails the Gallery and the Queue ask for first in a thread, without holding up the next image.
        A page that asks before they are done just makes them itself (thumbs.ensure_thumb); older images still get theirs that way."""
        def cut():
            try:
                make_thumbs(self.config, image_id, self.config.images_dir / rel)
            except (BadImage, OSError):
                log.warning("could not cut thumbnails for image %s", image_id, exc_info=True)
        task = asyncio.ensure_future(asyncio.to_thread(cut))
        self._thumb_tasks.add(task)
        task.add_done_callback(self._thumb_tasks.discard)

    # ---------- cancel ----------

    async def cancel_job(self, job_id: int) -> dict:
        """Cancel a job; if it's the running one also interrupt ComfyUI and drop its pending prompt."""
        was_running = self.store.get_job(job_id, images=False)["status"] == "running"
        job = self.store.cancel_job(job_id)
        if was_running and self.current and self.current["job_id"] == job_id:
            try:
                if self.current.get("prompt_id"):
                    await self.comfy.delete_queued([self.current["prompt_id"]])
                await self.comfy.interrupt()
            except ComfyError:
                log.warning("could not interrupt ComfyUI", exc_info=True)
            self._cancelled_jobs.add(job_id)
            if self._run_task:
                self._run_task.cancel()   # don't wait for a prompt we just removed from ComfyUI's queue
        self._emit("job", job_id=job_id, status="cancelled")
        return job
