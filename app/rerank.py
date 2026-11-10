"""Reranking: a small model reads a request and each of a few candidate texts together and says how well they fit.

Search by meaning (app/embeddings.py) compares vectors that were made for each text alone. It finds the neighbourhood, and the reranker then puts
the best few of it in better order. Both run on the CPU with llama.cpp's `llama-server` (`--reranking --device none`, a second process next to the
embedding server), so no GPU memory is used. Pairs cannot be stored as vectors; the scores of one run are remembered in memory only.

Everything here fails soft, like the embeddings: with reranking off, the model file missing, the server failing or answering too slowly,
`Reranker.scores` returns None and the callers keep the order the meaning search gave. A model that answers with nonsense is caught by a check
at the first use (a fitting text must outscore an unrelated one); if it fails, reranking is off until the app restarts."""
import asyncio
import logging
import time

import httpx

from app.config import Config
from app.embeddings import EmbeddingError, EmbeddingServer

log = logging.getLogger("planning.embeddings")
QUERY_CHARS = 500           # a longer request is cut
DOC_CHARS = 300             # ... and a longer candidate
MEMORY = 5000               # (request, candidate) scores kept in memory
IDLE_STOP = 300.0           # seconds without use before the server is stopped (it restarts when needed)
RETRY_AFTER = 300.0         # seconds to leave a failed server alone
START_TIMEOUT = 60.0
CHECK_QUERY = "a cup of hot coffee"
CHECK_FITS, CHECK_OTHER = "a steaming cup of coffee", "a rusty bicycle wheel"
CHECK_MARGIN = 0.001        # the fitting text must lead by this much (a broken conversion answers tiny, equal numbers)


class RerankServer(EmbeddingServer):
    """llama-server in reranking mode on the CPU: the embedding server's code with the reranker's model, port and settings."""
    what = "reranker"
    args = ["--reranking", "-c", "2048", "-b", "2048", "-ub", "2048"]

    @property
    def model(self):
        return self.config.reranker_model

    @property
    def port(self) -> int:
        return self.config.reranker_port

    @property
    def threads(self) -> int:
        return self.config.reranker_threads

    @property
    def start_timeout(self) -> float:
        return START_TIMEOUT


class Reranker:
    """`await scores(query, texts)` -> one number per text (higher fits better; the scale depends on the model), or None when reranking isn't
    available. Tests pass a fake `server` and an httpx `transport`."""

    def __init__(self, config: Config, server=None, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.server = server or RerankServer(config)
        self.transport = transport
        self._http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._memo: dict[tuple[str, str], float] = {}
        self._checked = False
        self._off = False                    # the start-up check failed: no reranking in this run
        self._missing_noted = False
        self._down_until = 0.0
        self._idle: asyncio.TimerHandle | None = None
        self._stopping: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        c = self.config
        if not c.reranker_enabled or self._off or time.monotonic() < self._down_until:
            return False
        if not c.reranker_model.is_file():
            if not self._missing_noted:
                self._missing_noted = True
                log.info("no reranker model at %s; using search by meaning without reranking", c.reranker_model)
            return False
        return True

    async def scores(self, query: str, texts: list[str]) -> list[float] | None:
        """How well each text fits the query, or None if unavailable (the callers then keep their own order)."""
        query = query.strip()[:QUERY_CHARS]
        if not texts or not query:
            return []
        if not self.enabled:
            return None
        texts = [t[:DOC_CHARS] for t in texts]
        try:
            async with self._lock:
                if not await self._ready():
                    return None
                todo = [t for t in dict.fromkeys(texts) if (query, t) not in self._memo]
                if todo:
                    got = await self._rank(query, todo)
                    if len(self._memo) + len(todo) > MEMORY:
                        self._memo.clear()
                    self._memo.update({(query, t): s for t, s in zip(todo, got)})
                self._touch()
                return [self._memo[(query, t)] for t in texts]
        except (EmbeddingError, httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as e:
            self._fail(e)
            return None

    async def _ready(self) -> bool:
        """The server is running and has passed the check once in this run."""
        await self.server.start()
        if not self._checked:
            fits, other = await self._rank(CHECK_QUERY, [CHECK_FITS, CHECK_OTHER])
            if fits - other < CHECK_MARGIN:
                self._off = True
                log.warning("the reranker model gave no sensible answer in its start-up check (%.6f for a fitting text, %.6f for an unrelated one); "
                            "reranking is off until the app restarts", fits, other)
                self._schedule_stop(0)
                return False
            self._checked = True
        return True

    async def _rank(self, query: str, texts: list[str]) -> list[float]:
        if self._http is None:
            self._http = httpx.AsyncClient(transport=self.transport, timeout=self.config.reranker_timeout)
        reply = await self._http.post(f"http://127.0.0.1:{self.config.reranker_port}/v1/rerank", json={"query": query, "documents": texts})
        reply.raise_for_status()
        rows = {int(r["index"]): float(r["relevance_score"]) for r in reply.json()["results"]}
        if sorted(rows) != list(range(len(texts))):
            raise EmbeddingError("the reranker returned the wrong number of scores")
        return [rows[i] for i in range(len(texts))]

    # ---------- failure and shutdown ----------

    def _fail(self, error: Exception):
        log.warning("reranking is not available, using search by meaning alone: %s", error)
        self._down_until = time.monotonic() + RETRY_AFTER
        self._schedule_stop(0)

    def _touch(self):
        self._schedule_stop(IDLE_STOP)

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
        for task in self._stopping:
            task.cancel()
        await asyncio.gather(*self._stopping, return_exceptions=True)
        await self.server.stop()
        if self._http is not None:
            await self._http.aclose()
