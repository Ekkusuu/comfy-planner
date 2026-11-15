"""GpuLease: one phase at a time owns the GPU. Planning and card building hold it while the LLM is loaded."""
import asyncio
import logging
import shutil
from contextlib import asynccontextmanager

from app.comfy.client import ComfyError
from app.llm.server import LlmServerError

log = logging.getLogger("gpu")


class GpuLease:
    """`async with lease:` waits for the queue to finish its current image, unloads ComfyUI's models, starts the
    LLM server, and on exit stops the server (confirming the process is gone) and lets the queue continue.

    With `[llm] keep_loaded_seconds` above 0 the server is instead left loaded after a clean exit, so a plan that follows soon
    skips the model load. The queue is released then, but it never generates while the LLM holds the GPU: before each image the
    worker calls `free_for_images`, which stops a kept server first. A kept server is also stopped when nothing takes it
    again within that many seconds. All stops of a kept server happen under the lock, so a new session never meets a half-stopped one."""

    def __init__(self, worker, comfy, server, config=None):
        self.worker, self.comfy, self.server, self.config = worker, comfy, server, config
        self._lock = asyncio.Lock()      # only one holder at a time
        self._kept_until: float | None = None    # loop time when a kept-loaded server is stopped; None = none is kept
        self._watcher: asyncio.Task | None = None
        self._stop_on_leave = False      # set by a holder whose LLM server failed: that server must not be kept

    @property
    def active(self) -> bool:
        return self._lock.locked()

    @property
    def keep_seconds(self) -> float:
        return float(self.config.llm_keep_loaded_seconds) if self.config else 0.0

    @property
    def kept(self) -> bool:
        """True while the LLM server is left loaded between sessions."""
        return self._kept_until is not None

    def stop_on_leave(self):
        """For the holder: the LLM server failed, so don't keep it loaded when the lease is given back."""
        self._stop_on_leave = True

    async def __aenter__(self):
        return await self.enter()

    async def enter(self, progress=None):
        """Take the GPU. `progress(stage)` is called with "waiting" (for the queue to reach an image boundary) and
        "loading" (unloading ComfyUI's models and starting the LLM)."""
        say = progress or (lambda stage: None)
        await self._lock.acquire()
        self._kept_until, self._stop_on_leave = None, False     # taken back: the idle stop finds nothing to do
        try:
            say("waiting")
            await self.worker.hold()
            say("loading")
            await self._free_comfy()
            await self.server.start()
        except BaseException:
            await self._leave()
            raise
        return self

    async def __aexit__(self, exc_type, *exc):
        await self._leave(keep=exc_type is None)

    @asynccontextmanager
    async def session(self, progress=None):
        """`async with lease.session(progress):` like `async with lease:`, with stage reports (see enter)."""
        await self.enter(progress)
        clean = False
        try:
            yield self
            clean = True
        finally:
            await self._leave(keep=clean)

    async def _free_comfy(self):
        try:
            await self.comfy.free()
        except ComfyError:
            log.info("ComfyUI is not reachable; nothing to unload")   # nothing of ours is on the GPU then

    async def _leave(self, keep: bool = False):
        """Give the GPU back: stop the server (or, after a clean session with keep_loaded_seconds set and no image waiting, leave it
        loaded and start the idle timer), then release the queue and the lock."""
        keep = (keep and not self._stop_on_leave and self.keep_seconds > 0 and self.server.running
                and not self.worker.has_work())
        stopped = False
        try:
            if keep:
                self._kept_until = asyncio.get_running_loop().time() + self.keep_seconds
                if self._watcher is None or self._watcher.done():
                    self._watcher = asyncio.create_task(self._watch_idle())
                log.info("LLM kept loaded for %.0f s", self.keep_seconds)
            else:
                await self.server.stop()
                stopped = True
        finally:
            self.worker.release()    # a stuck LLM process raises above, but the queue still continues
            self._lock.release()
        if stopped:
            log.info("LLM stopped; free VRAM: %s", await free_vram())

    # ---------- a server kept loaded ----------

    async def free_for_images(self):
        """Called by the queue worker before each image: stop a kept LLM server so the image gets the whole GPU. If a session
        holds the lock this waits for it, and the session's own exit decides again."""
        if self._kept_until is None:
            return
        async with self._lock:
            if self._kept_until is not None:
                self._kept_until = None
                await self._stop_kept("an image is waiting")

    async def _watch_idle(self):
        """Stop the kept server once nobody has taken it for keep_seconds. The deadline is re-read after every wait and again under
        the lock, so a session that took the server and gave it back meanwhile just pushes the stop later."""
        loop = asyncio.get_running_loop()
        while True:
            until = self._kept_until
            if until is None:
                return
            if until > loop.time():
                await asyncio.sleep(until - loop.time())
                continue
            async with self._lock:
                if self._kept_until is None or self._kept_until > loop.time():
                    continue
                self._kept_until = None
                await self._stop_kept("idle")
            return

    async def _stop_kept(self, why: str):
        try:
            await self.server.stop()
        except LlmServerError as e:
            log.warning("kept LLM server did not stop: %s", e)     # the next start refuses while it still answers, and says why
        log.info("LLM stopped (%s); free VRAM: %s", why, await free_vram())

    async def close(self):
        """App shutdown: end the idle timer (the caller stops the server itself)."""
        self._kept_until = None
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)


async def free_vram() -> str:
    """Free GPU memory from nvidia-smi, for the log (so a leaked LLM process shows up). '?' if unavailable."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return "?"
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(exe, "--query-gpu=memory.free", "--format=csv,noheader",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
        return out.decode().strip() or "?"
    except (OSError, asyncio.TimeoutError):
        return "?"
    finally:
        if proc is not None and proc.returncode is None:   # timed out or cancelled: don't leave it running
            proc.kill()
