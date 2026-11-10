"""Start and stop llama.cpp's `llama-server` (OpenAI-compatible API). One model is loaded at a time."""
import asyncio
import logging
import subprocess
import sys

import httpx

from app.config import Config

log = logging.getLogger("llm")


class LlmServerError(Exception):
    """llama-server didn't start, or didn't stop."""


class LlamaServer:
    def __init__(self, config: Config):
        self.config = config
        self.proc: asyncio.subprocess.Process | None = None

    @property
    def url(self) -> str:
        return self.config.llm_url

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def command(self) -> list[str]:
        c = self.config
        return [str(c.llm_binary), "--model", str(c.llm_model_path), "--host", c.llm_host, "--port", str(c.llm_port),
                "--ctx-size", str(c.llm_ctx_size), "--n-gpu-layers", str(c.llm_gpu_layers), "--jinja",
                # one slot: the app makes one call at a time, and extra slots cost about 1 GB of VRAM
                "--parallel", "1", *c.llm_extra_args]

    # ---------- start ----------

    async def start(self):
        """Launch the server and wait until /health says the model is loaded."""
        if self.running:
            return
        c = self.config
        if not c.llm_binary.is_file():
            raise LlmServerError(f"llama-server not found at {c.llm_binary}. Set [llm] binary in config/app.toml.")
        try:
            model = c.llm_model_path
        except KeyError as e:
            raise LlmServerError(e.args[0]) from e
        if not model.is_file():
            raise LlmServerError(f"LLM model file not found: {model}")
        if await self._answers():
            raise LlmServerError(f"Something is already answering on {self.url} (a leftover llama-server?). Stop it first.")
        log.info("starting llama-server with %s", model.name)
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        self.proc = await asyncio.create_subprocess_exec(*self.command(), stdin=asyncio.subprocess.DEVNULL,
                                                         stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, **kwargs)
        try:
            await self._wait_ready()
        except BaseException:
            await self.stop()
            raise

    async def _answers(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2) as http:
                return (await http.get(f"{self.url}/health")).status_code == 200
        except httpx.HTTPError:
            return False

    async def _wait_ready(self):
        deadline = asyncio.get_running_loop().time() + self.config.llm_start_timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.proc.returncode is not None:
                raise LlmServerError(f"llama-server exited while loading the model (exit code {self.proc.returncode}).")
            if await self._answers():
                return
            await asyncio.sleep(0.5)
        raise LlmServerError(f"llama-server didn't become ready within {self.config.llm_start_timeout:.0f} seconds. "
                             "The GPU is probably out of memory: close games or other GPU-heavy programs, "
                             "or lower the context size in Settings.")

    # ---------- stop ----------

    async def stop(self):
        """Ask it to stop, kill it after stop_timeout, and confirm the process is gone."""
        proc, self.proc = self.proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), self.config.llm_stop_timeout)
        except asyncio.TimeoutError:
            log.warning("llama-server ignored the stop request; killing it")
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError as e:
                raise LlmServerError("llama-server did not exit after being killed.") from e
