"""GpuLease: one phase at a time owns the GPU. Planning and card building hold it while the LLM is loaded."""
import asyncio
import logging
import shutil
from contextlib import asynccontextmanager

from app.comfy.client import ComfyError

log = logging.getLogger("gpu")


class GpuLease:
    """`async with lease:` waits for the queue to finish its current image, unloads ComfyUI's models, starts the
    LLM server, and on exit stops the server (confirming the process is gone) and lets the queue continue."""

    def __init__(self, worker, comfy, server):
        self.worker, self.comfy, self.server = worker, comfy, server
        self._lock = asyncio.Lock()      # only one holder at a time

    @property
    def active(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self):
        return await self.enter()

    async def enter(self, progress=None):
        """Take the GPU. `progress(stage)` is called with "waiting" (for the queue to reach an image boundary) and
        "loading" (unloading ComfyUI's models and starting the LLM)."""
        say = progress or (lambda stage: None)
        await self._lock.acquire()
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

    async def __aexit__(self, *exc):
        await self._leave()

    @asynccontextmanager
    async def session(self, progress=None):
        """`async with lease.session(progress):` like `async with lease:`, with stage reports (see enter)."""
        await self.enter(progress)
        try:
            yield self
        finally:
            await self._leave()

    async def _free_comfy(self):
        try:
            await self.comfy.free()
        except ComfyError:
            log.info("ComfyUI is not reachable; nothing to unload")   # nothing of ours is on the GPU then

    async def _leave(self):
        try:
            await self.server.stop()
        finally:
            self.worker.release()    # a stuck LLM process raises above, but the queue still continues
            self._lock.release()
        log.info("LLM stopped; free VRAM: %s", await free_vram())


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
