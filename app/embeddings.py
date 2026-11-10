"""Search by meaning: text -> vector, from a small embedding model run by llama.cpp's `llama-server` on the CPU.

The model never touches the GPU (`--device none`), so it needs no GPU lease and can stay loaded next to ComfyUI or the planning
LLM; it is started when first needed and stopped after `idle_stop` seconds of no use, or when the app stops. Vectors of the
things that stay the same (wildcard list entries, LoRA cards, liked prompts) are kept on disk, one SQLite file per model in
data/cache/embeddings/, keyed by a hash of the text, so each is computed once.

Everything here fails soft: with embeddings off, the model file missing, or the server failing to start or answer, `embed`
returns None and the callers use plain keyword ranking, as before. The first failure is logged as a warning in planner.log;
after a failure the server is not tried again for RETRY_AFTER seconds."""
import asyncio
import hashlib
import logging
import math
import sqlite3
import subprocess
import sys
import time
from array import array
from pathlib import Path

import httpx

from app.config import Config

log = logging.getLogger("planning.embeddings")
BATCH = 32              # texts per request
MAX_CHARS = 600         # longer texts are cut
RETRY_AFTER = 300.0     # seconds to leave a failed server alone
QUERY_MEMORY = 64       # recent query vectors kept in memory


class EmbeddingError(Exception):
    """The embedding server didn't start or didn't answer properly."""


def unit(vec) -> array:
    """The vector scaled to length 1 (so a dot product is the cosine), as a float32 array."""
    norm = math.sqrt(math.sumprod(vec, vec)) or 1.0
    return array("f", (x / norm for x in vec))


def dot(a, b) -> float:
    return math.sumprod(a, b)


# ---------- the server ----------

class EmbeddingServer:
    """llama-server in embedding mode on the CPU. Same idea as LlamaServer (app/llm/server.py), without the GPU. The reranker's server
    (app/rerank.py) is this class with other settings: the properties below are what it changes."""
    what = "embedding"
    args = ["--embeddings", "-c", "4096", "-b", "4096", "-ub", "4096"]

    def __init__(self, config: Config):
        self.config = config
        self.proc: asyncio.subprocess.Process | None = None

    @property
    def model(self) -> Path:
        return self.config.embeddings_model

    @property
    def port(self) -> int:
        return self.config.embeddings_port

    @property
    def threads(self) -> int:
        return self.config.embeddings_threads

    @property
    def start_timeout(self) -> float:
        return self.config.embeddings_start_timeout

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def command(self) -> list[str]:
        c = self.config
        # --device none: no GPU memory at all (-ngl 0 alone still creates a CUDA context of a few hundred MB)
        cmd = [str(c.llm_binary), "--model", str(self.model), "--host", "127.0.0.1", "--port", str(self.port), *self.args,
               "--device", "none", "-ngl", "0", "--parallel", "1"]
        return cmd + (["--threads", str(self.threads)] if self.threads > 0 else [])

    async def start(self):
        if self.running:
            return
        c = self.config
        if not c.llm_binary.is_file():
            raise EmbeddingError(f"llama-server not found at {c.llm_binary}")
        if not self.model.is_file():
            raise EmbeddingError(f"{self.what} model not found: {self.model}")
        if await self._answers():
            raise EmbeddingError(f"something is already answering on {self.url}")
        log.info("starting the %s server with %s", self.what, self.model.name)
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        self.proc = await asyncio.create_subprocess_exec(*self.command(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.DEVNULL, **kwargs)
        deadline = asyncio.get_running_loop().time() + self.start_timeout
        try:
            while asyncio.get_running_loop().time() < deadline:
                if self.proc.returncode is not None:
                    raise EmbeddingError(f"the {self.what} server exited while loading the model (exit code {self.proc.returncode})")
                if await self._answers():
                    return
                await asyncio.sleep(0.3)
            raise EmbeddingError(f"the {self.what} server was not ready after {self.start_timeout:.0f} seconds")
        except BaseException:
            await self.stop()
            raise

    async def _answers(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2) as http:
                return (await http.get(f"{self.url}/health")).status_code == 200
        except httpx.HTTPError:
            return False

    async def stop(self):
        proc, self.proc = self.proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


# ---------- the vectors on disk ----------

class VectorCache:
    """key (hash of the text) -> vector, in one SQLite file. Used from the event loop only."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, data BLOB NOT NULL)")

    def get_many(self, keys: list[str]) -> dict[str, array]:
        found = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            for key, data in self.db.execute(f"SELECT key, data FROM vectors WHERE key IN ({','.join('?' * len(chunk))})", chunk):
                vec = array("f")
                vec.frombytes(data)
                found[key] = vec
        return found

    def put_many(self, items: dict[str, array]):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO vectors (key, data) VALUES (?, ?)", [(k, v.tobytes()) for k, v in items.items()])

    def close(self):
        self.db.close()


# ---------- the client ----------

class Embedder:
    """`await embed(texts)` -> unit vectors, or None when meaning search isn't available. Tests pass a fake `server` and an
    httpx `transport`."""

    def __init__(self, config: Config, server=None, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.server = server or EmbeddingServer(config)
        self.transport = transport
        self._http: httpx.AsyncClient | None = None
        self._cache: VectorCache | None = None
        self._mem: dict[str, array] = {}              # vectors read or computed in this run
        self._queries: dict[str, array] = {}
        self._lock = asyncio.Lock()                   # one batch at a time (the server has one slot)
        self._down_until = 0.0
        self._idle: asyncio.TimerHandle | None = None
        self._warming: set[asyncio.Task] = set()
        self._stopping: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self.config.embeddings_enabled and time.monotonic() >= self._down_until

    # ---------- keys and the disk cache ----------

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.config.embeddings_doc_prefix}\0{text[:MAX_CHARS]}".encode()).hexdigest()

    def _disk(self) -> VectorCache:
        if self._cache is None:
            model = self.config.embeddings_model
            size = model.stat().st_size if model.is_file() else 0
            self._cache = VectorCache(self.config.cache_dir / "embeddings" / f"{model.stem}-{size}.sqlite3")
        return self._cache

    def cached(self, texts: list[str]) -> list[array | None]:
        """The stored vectors of these (document) texts; None for one that hasn't been computed yet. Never starts the server."""
        if not self.config.embeddings_enabled:
            return [None] * len(texts)
        keys = [self._key(t) for t in texts]
        missing = [k for k in dict.fromkeys(keys) if k not in self._mem]
        if missing:
            try:
                self._mem.update(self._disk().get_many(missing))
            except sqlite3.Error as e:
                log.warning("embedding cache unreadable: %s", e)
        return [self._mem.get(k) for k in keys]

    # ---------- computing ----------

    async def embed(self, texts: list[str], query: bool = False) -> list[array] | None:
        """Unit vectors for `texts` (documents, or search queries with the model's query prefix); None if unavailable. Document
        vectors are stored on disk; ones already stored aren't computed again."""
        if not texts:
            return []
        if not self.enabled:
            return None
        try:
            if query:
                return await self._embed_queries(texts)
            have = self.cached(texts)
            todo = list(dict.fromkeys(t for t, v in zip(texts, have) if v is None))
            if todo:
                fresh = await self._request([self.config.embeddings_doc_prefix + t[:MAX_CHARS] for t in todo])
                items = {self._key(t): unit(v) for t, v in zip(todo, fresh)}
                self._mem.update(items)
                self._disk().put_many(items)
            return [self._mem[self._key(t)] for t in texts]
        except (EmbeddingError, httpx.HTTPError, OSError, ValueError, KeyError, sqlite3.Error) as e:
            self._fail(e)
            return None

    async def _embed_queries(self, texts: list[str]) -> list[array]:
        todo = [t for t in dict.fromkeys(texts) if t not in self._queries]
        if todo:
            fresh = await self._request([self.config.embeddings_query_prefix + t[:MAX_CHARS] for t in todo])
            for t, v in zip(todo, fresh):
                if len(self._queries) >= QUERY_MEMORY:
                    self._queries.pop(next(iter(self._queries)))
                self._queries[t] = unit(v)
        return [self._queries[t] for t in texts]

    async def _request(self, texts: list[str]) -> list[list[float]]:
        async with self._lock:
            await self.server.start()
            if self._http is None:
                self._http = httpx.AsyncClient(transport=self.transport, timeout=120)
            url = f"http://127.0.0.1:{self.config.embeddings_port}/v1/embeddings"
            out: list[list[float]] = []
            for i in range(0, len(texts), BATCH):
                reply = await self._http.post(url, json={"input": texts[i:i + BATCH]})
                reply.raise_for_status()
                rows = sorted(reply.json()["data"], key=lambda d: d["index"])
                if len(rows) != len(texts[i:i + BATCH]):
                    raise EmbeddingError("the embedding server returned the wrong number of vectors")
                out += [r["embedding"] for r in rows]
            self._touch()
            return out

    def warm(self, texts: list[str]):
        """Compute these document texts in the background (so a later `cached` finds them). Safe to call again."""
        if not self.enabled or not texts:
            return
        task = asyncio.get_running_loop().create_task(self._warm(texts))
        self._warming.add(task)
        task.add_done_callback(self._warming.discard)

    async def _warm(self, texts: list[str]):
        started = time.monotonic()
        todo = [t for t, v in zip(texts, self.cached(texts)) if v is None]
        for i in range(0, len(todo), 256):        # in pieces, so a stop or a failure loses little
            if await self.embed(todo[i:i + 256]) is None:
                return
        if todo:
            log.info("embedded %d texts in %.0fs", len(todo), time.monotonic() - started)

    @property
    def warming(self) -> bool:
        return bool(self._warming)

    # ---------- failure and shutdown ----------

    def _fail(self, error: Exception):
        log.warning("search by meaning is not available, using keyword search only: %s", error)
        self._down_until = time.monotonic() + RETRY_AFTER
        self._schedule_stop(0)

    def _touch(self):
        self._schedule_stop(self.config.embeddings_idle_stop)

    def _schedule_stop(self, delay: float):
        if self._idle is not None:
            self._idle.cancel()
        self._idle = asyncio.get_running_loop().call_later(delay, self._stop_later)

    def _stop_later(self):
        task = asyncio.get_running_loop().create_task(self._stop_server())
        self._stopping.add(task)
        task.add_done_callback(self._stopping.discard)

    async def _stop_server(self):
        async with self._lock:
            await self.server.stop()

    async def close(self):
        if self._idle is not None:
            self._idle.cancel()
        tasks = [*self._warming, *self._stopping]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.server.stop()
        if self._http is not None:
            await self._http.aclose()
        if self._cache is not None:
            self._cache.close()
