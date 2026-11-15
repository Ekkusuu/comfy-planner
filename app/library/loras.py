"""LoRA files: find them on disk (the folders ComfyUI reads), hash them once, and read their metadata."""
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from app.comfy.client import ComfyError
from app.comfy.desktop import model_dirs
from app.config import Config
from app.db import Database
from app.library.safetensors_meta import SafetensorsError, read_header, sha256_file, summarize

LORA_EXTENSIONS = {".safetensors"}


@dataclass
class LoraFile:
    name: str                 # as ComfyUI lists it (relative to a loras folder, forward slashes): what jobs use
    path: Path | None         # where it is on disk (None: ComfyUI lists it but we can't find the file)
    size: int = 0
    mtime_ns: int = 0
    sha256: str = ""


class HashCache:
    """SHA256 per file, cached in SQLite by path + size + mtime so a rescan only re-reads files that changed."""

    def __init__(self, db: Database):
        self.db = db

    def cached(self, path: Path) -> str | None:
        st = path.stat()
        row = self.db.row("SELECT * FROM file_hashes WHERE path = ?", (str(path),))
        return row["sha256"] if row and row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns else None

    def store(self, path: Path, size: int, mtime_ns: int, sha256: str):
        self.db.execute("INSERT OR REPLACE INTO file_hashes (path, size, mtime_ns, sha256) VALUES (?,?,?,?)",
                        (str(path), size, mtime_ns, sha256))

    async def get(self, path: Path) -> str:
        """The file's SHA256; reads the whole file (in a thread) only when it isn't cached or has changed."""
        found = self.cached(path)
        if found:
            return found
        st = path.stat()
        digest = await asyncio.to_thread(sha256_file, path)
        self.store(path, st.st_size, st.st_mtime_ns, digest)
        return digest


class LoraScanner:
    def __init__(self, db: Database, config: Config, comfy):
        self.config, self.comfy = config, comfy
        self.hashes = HashCache(db)
        self.deleted: set[str] = set()      # LoRAs the app deleted: ComfyUI may go on listing them for a while, so they stay out of the list

    def folders(self) -> list[Path]:
        return model_dirs(self.config.comfy_install_dir, "loras")

    async def names(self, refresh: bool = False) -> list[str]:
        """LoRA names ComfyUI offers (from /object_info, the copy the app already holds unless `refresh`: Rescan and the workflow
        re-check read it again); if it isn't running, whatever is in the folders."""
        try:
            oi = await self.comfy.object_info(refresh=refresh)
            listed = oi.get("LoraLoaderModelOnly", {}).get("input", {}).get("required", {}).get("lora_name", [[]])[0]
            if listed:
                return [n for n in (str(n).replace("\\", "/") for n in listed) if not self.is_deleted(n)]
        except ComfyError:
            pass
        return sorted(rel for folder in self.folders() for rel in _walk(folder))

    def is_deleted(self, name: str) -> bool:
        """Deleted by the app and not back on disk since."""
        return name in self.deleted and self.resolve(name) is None

    def resolve(self, name: str) -> Path | None:
        for folder in self.folders():
            path = folder / name
            if path.is_file() and path.resolve().is_relative_to(folder.resolve()):
                return path
        return None

    async def scan(self, refresh: bool = False) -> list[LoraFile]:
        """Every LoRA with its size, mtime and SHA256 (cached files are instant). `refresh` asks ComfyUI for its list again."""
        files = []
        for name in await self.names(refresh):
            path = self.resolve(name)
            if path is None:
                files.append(LoraFile(name, None))
            elif path.suffix.lower() in LORA_EXTENSIONS:
                st = path.stat()
                files.append(LoraFile(name, path, st.st_size, st.st_mtime_ns, await self.hashes.get(path)))
        return files

    def metadata(self, file: LoraFile) -> dict:
        """The header digest for a LoRA; the raw header is cached in data/cache/safetensors/<sha256>.json."""
        cache = self.config.cache_dir / "safetensors" / f"{file.sha256}.json"
        if cache.is_file():
            header = json.loads(cache.read_text(encoding="utf-8"))
        else:
            header = read_header(file.path)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(_without_offsets(header), ensure_ascii=False), encoding="utf-8")
        return summarize(header)


def _walk(folder: Path):
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in LORA_EXTENSIONS:
            yield p.relative_to(folder).as_posix()


def _without_offsets(header: dict) -> dict:
    """The header with each tensor reduced to dtype and shape (offsets are useless in a cache and bulky)."""
    return {k: (v if k == "__metadata__" else {"dtype": v.get("dtype"), "shape": v.get("shape")}) for k, v in header.items()}


__all__ = ["LoraFile", "HashCache", "LoraScanner", "SafetensorsError"]
