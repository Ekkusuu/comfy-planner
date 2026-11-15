"""The "Install everything needed" job: downloads the pinned files into the app folder in the background, with progress, cancel and resume.

One job runs at a time. Each part (language model program, search model, tag library) is an item with its own state; a part that is
already in place is skipped, and one that fails does not stop the others. The pinned files and their hashes are in catalog.py; the
tag library is built by the same scripts the README describes (scripts/update_taglib.py and scripts/build_contradictions.py)."""
import asyncio
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from app import settings
from app.config import ROOT, Config
from app.setup import catalog
from app.setup.catalog import Asset
from app.setup.downloader import Cancelled, DownloadError, Downloader, extract_zip, host_allowed
from app.setup.status import taglib_missing

log = logging.getLogger("setup")

SCRIPTS = ROOT / "scripts"
LLAMA_UNPACKED_ESTIMATE = 1_300_000_000     # what the two llama.cpp zips take once unpacked
# Runs a script with its own folder variable replaced (update_taglib.py writes to a fixed TARGET), the way `python script.py` would otherwise.
TAGLIB_DRIVER = ("import runpy, sys\nfrom pathlib import Path\n"
                 "g = runpy.run_path(sys.argv[1], run_name='setup_driver')\ng['TARGET'] = Path(sys.argv[2])\nsys.exit(g['main']())")

ScriptRunner = Callable[[list[str], Callable[[str], None], asyncio.Event], Awaitable[int]]


async def run_python(args: list[str], on_line: Callable[[str], None], cancel: asyncio.Event) -> int:
    """Run the app's own Python with `args`, handing each output line to `on_line`. Returns the exit code; stops the program on cancel."""
    kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}          # no console window
    proc = await asyncio.create_subprocess_exec(sys.executable, "-I", "-X", "utf8", "-u", *args, cwd=str(ROOT), stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs)
    try:
        while True:
            if cancel.is_set():
                raise Cancelled()
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), 0.5)
            except asyncio.TimeoutError:
                continue
            if not line:
                return await proc.wait()
            if text := line.decode("utf-8", "replace").strip():
                on_line(text)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


class SetupManager:
    """Owns the install job. `save` writes settings (the app's normal settings path); `root` is the app folder."""

    def __init__(self, config: Config, save: Callable[[dict], Awaitable[object]], root: Path = ROOT, transport=None,
                 allow=host_allowed, run_script: ScriptRunner = run_python):
        self.config, self.save, self.root = config, save, root
        self.transport, self.allow, self.run_script = transport, allow, run_script
        self.state = "idle"                       # idle | running | done | failed | cancelled
        self.error: str | None = None
        self.items: dict[str, dict] = {}          # item id -> {state: pending|installing|ok|error, detail, done, total}
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._bytes: dict[str, dict[str, int]] = {}      # item id -> asset name -> bytes so far

    # ---------- what is there to do ----------

    def todo(self) -> list[str]:
        """The parts that are not in place yet."""
        c = self.config
        out = []
        if not c.llm_binary.is_file():
            out.append("llama_server")
        if c.embeddings_enabled and not c.embeddings_model.is_file():
            out.append("embeddings_model")
        if taglib_missing(c):
            out.append("tag_library")
        return out

    @property
    def running(self) -> bool:
        return self.state == "running"

    def snapshot(self) -> dict:
        return {"state": self.state, "error": self.error, "items": {k: dict(v) for k, v in self.items.items()}}

    def overlay(self, items: list[dict]) -> list[dict]:
        """Put the job's progress and errors on the status items (a part that is in place stays ok)."""
        for it in items:
            job = self.items.get(it["id"])
            if not job or it["state"] == "ok":
                continue
            if job["state"] == "installing":
                it.update(state="installing", detail=job["detail"], done=job["done"], total=job["total"])
            elif job["state"] == "error":
                it.update(state="error", detail=job["detail"])
            elif job["state"] == "pending" and self.running:
                it["detail"] = "Waiting for the other downloads."
        return items

    # ---------- running ----------

    def start(self) -> bool:
        """Begin installing every missing part. Returns False if a job is already running; raises DownloadError if the disk is too full."""
        if self.running:
            return False
        ids = self.todo()
        if not ids:
            self.state, self.error, self.items = "done", None, {}
            return True
        need = self._download_size(ids) * 2 + (LLAMA_UNPACKED_ESTIMATE if "llama_server" in ids else 0)
        free = shutil.disk_usage(self.root).free
        if free < need:
            raise DownloadError(f"There isn't enough free disk space: about {need / 1e9:.1f} GB is needed and {free / 1e9:.1f} GB is free.")
        self._cancel = asyncio.Event()
        self.state, self.error = "running", None
        self.items = {i: {"state": "pending", "detail": "", "done": 0, "total": self._download_size([i])} for i in ids}
        self._bytes = {i: {} for i in ids}
        self._task = asyncio.create_task(self._run(ids))
        return True

    def _download_size(self, ids: list[str]) -> int:
        sizes = {"llama_server": catalog.LLAMA_ZIP.size + catalog.CUDART_ZIP.size, "embeddings_model": catalog.EMBEDDINGS_MODEL.size,
                 "tag_library": sum(taglib_missing(self.config).values())}
        return sum(sizes[i] for i in ids)

    def cancel(self):
        """Stop the job; what was already downloaded is kept so the next try continues from there."""
        self._cancel.set()

    async def close(self):
        """At shutdown: stop a running job and wait for it."""
        self._cancel.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self, ids: list[str]):
        steps = {"llama_server": self._llama, "embeddings_model": self._embeddings, "tag_library": self._tags}
        failed = 0
        try:
            for id in ids:
                if self._cancel.is_set():
                    raise Cancelled()
                self._set(id, state="installing", detail="Starting")
                try:
                    await steps[id]()
                    self._set(id, state="ok", detail="Done")
                except Cancelled:
                    raise
                except DownloadError as e:
                    self._set(id, state="error", detail=str(e))
                    failed += 1
                except OSError as e:
                    log.warning("setup of %s failed: %s", id, e)
                    self._set(id, state="error", detail=f"The files could not be written: {e.strerror or e}. Check the free disk space and try again.")
                    failed += 1
                except Exception as e:      # a bug or an odd failure must show up as a message, not leave the page waiting
                    log.exception("setup of %s failed", id)
                    self._set(id, state="error", detail=f"Something went wrong ({type(e).__name__}). The details are in the app's log.")
                    failed += 1
            self.state = "failed" if failed else "done"
            self.error = f"{failed} part{'s' if failed != 1 else ''} could not be installed." if failed else None
        except Cancelled:
            self.state, self.error = "cancelled", None
        finally:
            self.items = {k: v for k, v in self.items.items() if v["state"] == "error"}      # keep the errors, drop the rest

    def _set(self, id: str, **fields):
        self.items[id].update(fields)

    def _progress(self, id: str, asset: Asset) -> Callable[[int, int], None]:
        """A progress callback for one asset of an item: the item's byte count is the sum over its assets."""
        def report(done: int, total: int):
            self._bytes[id][asset.name] = done
            self.items[id]["done"] = sum(self._bytes[id].values())
        return report

    def _downloader(self, folder: Path) -> Downloader:
        return Downloader(folder, self.transport, self.allow)

    # ---------- the parts ----------

    async def _llama(self):
        """llama-server and the CUDA libraries it needs, into tools/llama.cpp/<tag>/; then the setting that names the program."""
        zips = self.config.data_dir / "downloads"
        dl = self._downloader(zips)
        self._set("llama_server", detail=f"Downloading llama.cpp {catalog.LLAMA_TAG} (1 of 2)")
        main = await dl.fetch(catalog.LLAMA_ZIP, self._progress("llama_server", catalog.LLAMA_ZIP), self._cancel)
        self._set("llama_server", detail="Downloading the CUDA libraries (2 of 2)")
        cuda = await dl.fetch(catalog.CUDART_ZIP, self._progress("llama_server", catalog.CUDART_ZIP), self._cancel)
        self._set("llama_server", detail="Unpacking")
        folder = self.root / catalog.LLAMA_FOLDER
        await asyncio.to_thread(extract_zip, main, folder, catalog.llama_member_ok)
        await asyncio.to_thread(extract_zip, cuda, folder, lambda name: name in catalog.CUDART_FILES)
        binary = self.root / catalog.LLAMA_BINARY
        if not binary.is_file() or not all((folder / n).is_file() for n in catalog.CUDART_FILES):
            raise DownloadError("The download did not contain all the expected files.")
        for z in (main, cuda):
            z.unlink(missing_ok=True)
        await self.save({"llm_binary": settings.shown(binary)})

    async def _embeddings(self):
        """The search-by-meaning model, then the settings that point at it."""
        self._set("embeddings_model", detail="Downloading the model")
        path = await self._downloader(self.root / catalog.EMBEDDINGS_FOLDER).fetch(
            catalog.EMBEDDINGS_MODEL, self._progress("embeddings_model", catalog.EMBEDDINGS_MODEL), self._cancel)
        await self.save({"embeddings_model": settings.shown(path), "embeddings_enabled": True})

    async def _tags(self):
        """The tag library (scripts/update_taglib.py), then the data about tags that don't go together (scripts/build_contradictions.py)."""
        missing = taglib_missing(self.config)
        data = self.config.data_dir
        if "taglib" in missing:
            self._set("tag_library", detail="Downloading the tag list", done=0, total=None)      # no byte count for the script's steps
            await self._script("tag library", ["-c", TAGLIB_DRIVER, str(SCRIPTS / "update_taglib.py"), str(catalog.taglib_folder(data))])
        if "contradictions" in missing:
            folder = catalog.contradictions_folder(data)
            had = {a.name: (folder / a.name).is_file() for a in (catalog.TAG_COUNTS, catalog.COOCCURRENCE)}
            dl = self._downloader(folder)
            self._set("tag_library", detail="Downloading tag counts", done=0, total=catalog.TAG_COUNTS.size + catalog.COOCCURRENCE.size)
            counts = await dl.fetch(catalog.TAG_COUNTS, self._progress("tag_library", catalog.TAG_COUNTS), self._cancel)
            pairs = await dl.fetch(catalog.COOCCURRENCE, self._progress("tag_library", catalog.COOCCURRENCE), self._cancel)
            self._set("tag_library", detail="Working out which tags don't go together", done=0, total=None)
            await self._script("tag data", [str(SCRIPTS / "build_contradictions.py"), str(pairs), "--tags", str(counts),
                                            "--taglib", str(catalog.taglib_folder(data)), "--out", str(folder / catalog.CONTRADICTIONS_FILE)])
            for a, path in ((catalog.TAG_COUNTS, counts), (catalog.COOCCURRENCE, pairs)):
                if not had[a.name]:
                    path.unlink(missing_ok=True)            # only what this job downloaded; the result is a small file
        if taglib_missing(self.config):
            raise DownloadError("The tag library is still incomplete after the download.")

    async def _script(self, what: str, args: list[str]):
        """Run one of the scripts; its last output line becomes the message if it fails."""
        lines: list[str] = []

        def on_line(text: str):
            lines.append(text)
            self._set("tag_library", detail=text[:140])

        try:
            code = await self.run_script(args, on_line, self._cancel)
        except Cancelled:
            for leftover in self.config.data_dir.glob("taglib.new-*"):       # the half-built folder update_taglib.py leaves when stopped
                shutil.rmtree(leftover, ignore_errors=True)
            raise
        except OSError as e:
            raise DownloadError(f"The {what} step could not be started: {e}") from e
        if code != 0:
            log.warning("setup script failed (%s): %s", what, " | ".join(lines[-5:]))
            raise DownloadError(f"The {what} step failed: {lines[-1] if lines else 'no message'}. Check your internet connection and try again.")
