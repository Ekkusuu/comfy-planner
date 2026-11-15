"""Tag data read in threads and dropped when idle, the embedder's memory use, and the start-up work done when a plan is created."""
import asyncio
import json
import threading
import time

import httpx
import pytest

from app.embeddings import Embedder
from app.planning import contradictions as con_mod
from app.planning import taglib as tag_mod
from app.planning.context import PlanContext
from tests.test_embeddings import FakeEmbeddingServer, FakeModel, model_config  # noqa: F401  (fixture)


def write_contradictions(folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / con_mod.DERIVED_FILE).write_text(json.dumps({"format": 2, "images": 10, "tags": ["a", "b"], "counts": [5, 5], "pairs": []}), encoding="utf-8")


def test_idle_tag_data_is_dropped_and_read_again_when_asked_for(tmp_path):
    con_mod._loaded.clear()
    con_mod._used.clear()
    write_contradictions(tmp_path)
    first = con_mod.get_contradictions(tmp_path)
    assert first is not None and not con_mod.unload_idle(600)              # used a moment ago: stays
    assert con_mod.unload_idle(0) and not con_mod._loaded                  # idle for 0 seconds counts as idle: dropped
    assert con_mod.get_contradictions(tmp_path) is not first               # reloaded on demand


def test_unloading_never_waits_for_a_read_in_progress(tmp_path):
    with tag_mod._lock:
        done = []
        thread = threading.Thread(target=lambda: done.append(tag_mod.unload_idle(0)))
        thread.start()
        thread.join(2)
        assert done == [False]                  # returned at once instead of waiting for the lock


async def test_load_tag_data_runs_in_threads_and_the_data_is_ready_after(tmp_path, config, monkeypatch):
    con_mod._loaded.clear()
    con_mod._used.clear()
    write_contradictions(config.data_dir / con_mod.DATA_FOLDER)
    where = []
    real = con_mod.load_contradictions

    def spy(path):
        where.append(threading.current_thread() is threading.main_thread())
        return real(path)

    monkeypatch.setattr(con_mod, "load_contradictions", spy)
    ctx = PlanContext(config, None, None, None, None)
    await ctx.load_tag_data()
    assert where == [False]                                                # read off the event loop's thread
    assert ctx.contradictions() is not None and where == [False]          # and not read again


async def test_checking_for_missing_vectors_does_not_load_them(model_config):  # noqa: F811
    server, model = FakeEmbeddingServer(), FakeModel(model_config)
    emb = Embedder(model_config, server, httpx.MockTransport(model))
    texts = [f"entry {i}" for i in range(30)]
    emb.warm(texts)
    while emb.warming:
        await asyncio.sleep(0.01)
    assert not emb._mem                                                    # computed vectors went to disk, none are held
    assert emb.missing(texts) == [] and emb.missing(texts + ["new one"]) == ["new one"]
    assert not emb._mem                                                    # asking which exist read no vectors
    again = Embedder(model_config, FakeEmbeddingServer(), httpx.MockTransport(FakeModel(model_config)))
    again.warm(texts)
    while again.warming:
        await asyncio.sleep(0.01)
    assert not again._mem and server.starts == 1                           # a second start-up finds everything stored and holds nothing
    assert all(v is not None for v in again.cached(texts)) and len(again._mem) == 30     # a search reads back what it needs
    await emb.close()
    await again.close()


async def test_the_vectors_in_memory_are_dropped_when_the_server_stops_when_idle(model_config):  # noqa: F811
    model_config.embeddings_idle_stop = 0.05
    server = FakeEmbeddingServer()
    emb = Embedder(model_config, server, httpx.MockTransport(FakeModel(model_config)))
    first = await emb.embed(["one", "two"])
    assert len(emb._mem) == 2
    await asyncio.sleep(0.3)
    assert server.stops >= 1 and not emb._mem                              # freed with the server
    again = await emb.embed(["one"])                                       # read back from disk: the same vector
    assert list(again[0]) == list(first[0])
    await emb.close()


async def test_start_early_starts_the_server_once_and_not_when_it_is_off_or_broken(model_config):  # noqa: F811
    server = FakeEmbeddingServer()
    emb = Embedder(model_config, server, httpx.MockTransport(FakeModel(model_config)))
    emb.start_early()
    emb.start_early()                                                      # asked twice while starting: one start
    await asyncio.sleep(0.05)
    assert server.starts == 1
    await emb.close()
    model_config.embeddings_enabled = False
    off = FakeEmbeddingServer()
    Embedder(model_config, off, httpx.MockTransport(FakeModel(model_config))).start_early()
    await asyncio.sleep(0.05)
    assert off.starts == 0
    model_config.embeddings_enabled = True
    broken = FakeEmbeddingServer()
    broken.fail = True
    emb = Embedder(model_config, broken, httpx.MockTransport(FakeModel(model_config)))
    emb.start_early()                                                      # a start that fails is just the old fallback to keywords
    await asyncio.sleep(0.05)
    assert not emb.enabled
    await emb.close()
