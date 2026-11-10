"""LoRA covers. A LoRA's cover is the first of these that exists:

1. the user's choice: a gallery image, or a picture they uploaded (set in the Library; "automatic" removes it)
2. a preview file next to the LoRA file (`name.preview.png`, `name.png`, ...), read only
3. Civitai's first still preview picture for the file's hash, fetched once and stored here
4. nothing: the UI shows a placeholder for the LoRA's kind

Every picture that reaches the browser is a small WebP this app wrote under data/thumbs/lora/. The original bytes of a
download, an upload or a preview file are decoded with Pillow and never served. Nothing in ComfyUI's folders is written.
"""
import asyncio
import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from app.config import Config
from app.db import Database, now
from app.events import EventBus
from app.library import civitai
from app.library.loras import LoraFile, LoraScanner
from app.library.lora_extras import LoraExtras
from app.thumbs import BadImage, ensure_thumb, write_thumb

log = logging.getLogger("lora-covers")

COVER_SIZE = 480
STILL_FORMATS = ("JPEG", "PNG", "WEBP")
PREVIEW_SUFFIXES = (".preview.png", ".preview.jpg", ".preview.jpeg", ".preview.webp", ".png", ".jpg", ".jpeg", ".webp")
TRIES_PER_LORA = 3          # pictures tried for one LoRA before it counts as a failure


@dataclass
class Cover:
    source: str | None       # user | file | civitai | None
    path: Path | None        # the picture to serve (a gallery image or preview file is turned into a thumbnail first)
    stamp: int = 0           # changes when the picture does, so the browser fetches it again


def name_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


class LoraCovers:
    def __init__(self, db: Database, config: Config, scanner: LoraScanner, civ: civitai.CivitaiClient, extras: LoraExtras,
                 events: EventBus, background: bool = False):
        self.db, self.config, self.scanner, self.civitai, self.extras, self.events = db, config, scanner, civ, extras, events
        self.background = background          # fetch Civitai previews by itself after a list or a rescan (off in tests)
        self.queue: list[LoraFile] = []
        self.queued: set[str] = set()         # hashes waiting or being fetched
        self._runner: asyncio.Task | None = None

    @property
    def folder(self) -> Path:
        return self.config.thumbs_dir / "lora"

    def civitai_path(self, sha256: str) -> Path:
        return self.folder / f"civitai-{sha256}.webp"

    def upload_path(self, key: str) -> Path:
        return self.folder / f"upload-{name_hash(key)}.webp"

    # ---------- which picture ----------

    def preview_file(self, path: Path | None) -> Path | None:
        """A picture lying next to the LoRA file, by the usual names."""
        if path is None:
            return None
        return next((p for suffix in PREVIEW_SUFFIXES if (p := path.with_name(path.stem + suffix)).is_file()), None)

    def resolve(self, key: str, path: Path | None, sha256: str, extras: dict | None) -> Cover:
        """The cover to use now, by the order in the module doc. Cheap: a few file checks, no reading of pictures."""
        if extras and extras["cover_kind"] == "image":
            image = self.db.row("SELECT id, file_path FROM images WHERE id = ? AND status = 'done'", (extras["cover_image_id"],))
            found = self.config.images_dir / image["file_path"] if image and image["file_path"] else None
            if found and found.is_file():
                return Cover("user", found, image["id"])
        elif extras and extras["cover_kind"] == "upload" and self.upload_path(key).is_file():
            return Cover("user", self.upload_path(key), self.upload_path(key).stat().st_mtime_ns)
        preview = self.preview_file(path)
        if preview:
            return Cover("file", preview, preview.stat().st_mtime_ns)
        stored = self.civitai_path(sha256) if sha256 else None
        if stored and stored.is_file():
            return Cover("civitai", stored, stored.stat().st_mtime_ns)
        return Cover(None, None)

    def info(self, key: str, path: Path | None, sha256: str, extras: dict | None) -> dict:
        """What the list sends for one LoRA: where its cover comes from and the address to load it from."""
        cover = self.resolve(key, path, sha256, extras)
        url = f"/api/loras/{quote(key)}/cover?v={cover.source}-{cover.stamp}" if cover.source else None
        return {"source": cover.source, "url": url, "chosen": bool(extras and extras["cover_kind"])}

    def serve_path(self, key: str) -> Path | None:
        """The small WebP for this LoRA's cover (made now if needed), or None. Blocking: call it from a thread."""
        path = self.scanner.resolve(key)
        sha = self.scanner.hashes.cached(path) if path else ""
        extras = self.extras.get(key)
        cover = self.resolve(key, path, sha or "", extras)
        if cover.source is None:
            return None
        if cover.source == "user" and extras["cover_kind"] == "image":     # a gallery image: the gallery's own thumbnail cache
            return ensure_thumb(self.config, extras["cover_image_id"], cover.path, COVER_SIZE)
        if cover.source in ("user", "civitai"):                           # an upload or a download: already our own small WebP
            return cover.path
        target = self.folder / f"file-{name_hash(key)}.webp"          # a preview file next to the LoRA
        if not (target.is_file() and target.stat().st_mtime_ns >= cover.path.stat().st_mtime_ns):
            try:
                write_thumb(cover.path, target, COVER_SIZE)
            except BadImage:
                return None
        return target

    # ---------- the user's choice ----------

    def choose_image(self, key: str, image_id: int):
        """Raises BadImage (shown to the user) when the image isn't a finished one that still exists."""
        image = self.db.row("SELECT file_path FROM images WHERE id = ? AND status = 'done'", (image_id,))
        if not image or not image["file_path"] or not (self.config.images_dir / image["file_path"]).is_file():
            raise BadImage("A cover has to be a finished image that still exists.")
        self.extras.set_cover(key, "image", image_id)

    def choose_upload(self, key: str, data: bytes):
        """Decode what the user sent and keep only our own small copy. Raises BadImage."""
        write_thumb(io.BytesIO(data), self.upload_path(key), COVER_SIZE, STILL_FORMATS)
        self.extras.set_cover(key, "upload")

    def go_automatic(self, key: str):
        self.extras.set_cover(key, None)
        self.upload_path(key).unlink(missing_ok=True)

    # ---------- Civitai previews ----------

    def schedule(self, files: list[LoraFile]):
        """Queue a background fetch for each LoRA that has no cover yet and hasn't failed before. Returns at once."""
        if not self.background:
            return
        extras, misses = self.extras.all(), {r["sha256"] for r in self.db.rows("SELECT sha256 FROM lora_preview_misses")}
        for f in files:
            if f.path is None or not f.sha256 or f.sha256 in misses or f.sha256 in self.queued:
                continue
            if self.resolve(f.name, f.path, f.sha256, extras.get(f.name)).source is None:
                self.queue.append(f)
                self.queued.add(f.sha256)
        if self.queue and self._runner is None:
            self._runner = asyncio.create_task(self._run())

    def forget_misses(self):
        """Rescan: ask again about the LoRAs that failed before."""
        self.db.execute("DELETE FROM lora_preview_misses")

    async def _run(self):
        try:
            while self.queue:
                f = self.queue.pop(0)
                try:
                    if await self.fetch_one(f):
                        self.events.publish({"type": "lora_cover", "key": f.name})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("preview for %s crashed", f.name)
                    self._miss(f.sha256, "unexpected error")
                finally:
                    self.queued.discard(f.sha256)
        finally:
            self._runner = None

    async def close(self):
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)

    def _miss(self, sha256: str, reason: str) -> bool:
        self.db.execute("INSERT OR REPLACE INTO lora_preview_misses (sha256, reason, at) VALUES (?,?,?)", (sha256, reason[:200], now()))
        log.info("no Civitai preview for %s: %s", sha256[:12], reason)
        return False

    async def fetch_one(self, f: LoraFile) -> bool:
        """Get and store the Civitai preview for this file. True if a picture was stored; otherwise the reason is
        remembered (until the next Rescan) and nothing is asked again."""
        result = await self.civitai.by_hash(f.sha256)       # a cached reply costs no request
        if result.status != "ok":
            return self._miss(f.sha256, result.message or result.status)
        reason = "Civitai shows no still picture for it"
        for url in civitai.preview_urls(result.data)[:TRIES_PER_LORA]:
            got = await self.civitai.fetch_image(url)
            if got.status != "ok":
                reason = got.message
                continue
            try:
                await asyncio.to_thread(write_thumb, io.BytesIO(got.data), self.civitai_path(f.sha256), COVER_SIZE, STILL_FORMATS)
            except BadImage as e:
                reason = str(e)
                continue
            return True
        return self._miss(f.sha256, reason)
