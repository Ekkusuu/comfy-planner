"""Reranking (app/rerank.py, planning/semantic.rerank_blend): stubbed, no real model."""
import json
import random

import httpx
import pytest

from app.config import Config
from app.planning import offers as O
from app.planning import semantic
from app.planning.concepts import load_concepts
from app.planning.context import PlanContext
from app.planning.taglib import GENERAL, TagLib
from app.planning.tagstep import concept_lines, meaning_hits
from app.rerank import CHECK_FITS, CHECK_OTHER, CHECK_QUERY, Reranker
from tests.content_probes import SUBJECT_TERMS

AVOIDED = SUBJECT_TERMS[0]       # a placeholder word of the content rules' entry filter


class FakeRerankServer:
    def __init__(self):
        self.starts = self.stops = 0
        self.fail = False

    async def start(self):
        from app.embeddings import EmbeddingError
        if self.fail:
            raise EmbeddingError("the fake reranker did not load")
        self.starts += 1

    async def stop(self):
        self.stops += 1


class FakeRerankModel:
    """The httpx side: POST /v1/rerank. `scores` maps document text -> score."""

    def __init__(self, scores):
        self.scores, self.requests = scores, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        docs = body["documents"]
        return httpx.Response(200, json={"results": [{"index": i, "relevance_score": self.scores[d]}
                                                     for i, d in enumerate(docs)]})


@pytest.fixture
def rerank_config(config, tmp_path) -> Config:
    config.reranker_enabled = True
    config.reranker_model = tmp_path / "fake-reranker.gguf"
    config.reranker_model.write_bytes(b"not a real model")
    config.reranker_pool, config.reranker_weight = 4, 0.7
    return config


@pytest.fixture
def parts(rerank_config):
    server = FakeRerankServer()
    model = FakeRerankModel({CHECK_FITS: 0.9, CHECK_OTHER: 0.1,
                             "a": 0.0, "b": 0.25, "c": 1.0, "d": 0.75})
    return Reranker(rerank_config, server, httpx.MockTransport(model)), server, model


def ctx_with(config, db, reranker) -> PlanContext:
    return PlanContext(config, None, None, None, db, reranker=reranker)


# ---------- score fusion and ordering ----------

async def test_fusion_blends_both_scores_scaled_per_query(parts, rerank_config, db):
    reranker, _, _ = parts
    ctx = ctx_with(rerank_config, db, reranker)
    out = await semantic.rerank_blend(ctx, "some query", ["a", "b", "c", "d"], [0.9, 0.7, 0.5, 0.3])
    assert out == pytest.approx([0.48, 0.525, 0.78, 0.615])      # 0.7 * rerank + 0.3 * cosine, back on [0.3, 0.9]
    assert sorted(range(4), key=lambda i: (-out[i], i)) == [2, 3, 1, 0]      # the reranker liked c and d best


async def test_weight_zero_keeps_cosine_order_and_one_follows_the_reranker(parts, rerank_config, db):
    reranker, _, _ = parts
    ctx = ctx_with(rerank_config, db, reranker)
    rerank_config.reranker_weight = 0.0
    assert await semantic.rerank_blend(ctx, "some query", ["a", "b", "c", "d"], [0.9, 0.7, 0.5, 0.3]) == \
        pytest.approx([0.9, 0.7, 0.5, 0.3])
    rerank_config.reranker_weight = 1.0
    out = await semantic.rerank_blend(ctx, "some query", ["a", "b", "c", "d"], [0.9, 0.7, 0.5, 0.3])
    assert sorted(range(4), key=lambda i: (-out[i], i)) == [2, 3, 1, 0]


async def test_only_the_pool_is_reordered(parts, rerank_config, db):
    reranker, _, model = parts
    model.scores.update({"a": 0.0, "b": 1.0})
    rerank_config.reranker_pool = 2
    ctx = ctx_with(rerank_config, db, reranker)
    out = await semantic.rerank_blend(ctx, "some query", ["a", "b", "c", "d"], [0.9, 0.7, 0.5, 0.3])
    assert out == pytest.approx([0.76, 0.84, 0.5, 0.3])      # a and b swapped, c and d untouched behind them


async def test_rerank_scores_follow_document_order_and_are_remembered(parts):
    reranker, _, model = parts
    assert await reranker.scores("some query", ["c", "a"]) == pytest.approx([1.0, 0.0])
    n = len(model.requests)
    assert await reranker.scores("some query", ["c", "a"]) == pytest.approx([1.0, 0.0])
    assert len(model.requests) == n      # same pairs: remembered, not asked again


# ---------- fallback ----------

async def test_missing_file_falls_back_to_cosine(rerank_config, db, tmp_path):
    rerank_config.reranker_model = tmp_path / "no-such-model.gguf"
    reranker = Reranker(rerank_config, FakeRerankServer(), httpx.MockTransport(FakeRerankModel({})))
    assert await reranker.scores("q", ["a"]) is None
    ctx = ctx_with(rerank_config, db, reranker)
    scores = [0.9, 0.7]
    assert await semantic.rerank_blend(ctx, "q", ["a", "b"], scores) == scores
    assert reranker.server.starts == 0      # the server is never started


async def test_switched_off_falls_back_to_cosine(parts, rerank_config, db):
    reranker, server, _ = parts
    rerank_config.reranker_enabled = False
    assert await reranker.scores("q", ["a"]) is None and server.starts == 0


async def test_smoke_test_failure_disables_reranking_for_the_run(rerank_config, db):
    server = FakeRerankServer()
    model = FakeRerankModel({CHECK_FITS: 0.5, CHECK_OTHER: 0.5, "x": 0.9, "y": 0.1})
    reranker = Reranker(rerank_config, server, httpx.MockTransport(model))
    assert await reranker.scores("anything", ["x", "y"]) is None      # the check pair ties: no sensible answer
    assert reranker._off and not reranker.enabled
    assert len(model.requests) == 1      # only the check reached the server
    assert await reranker.scores("anything", ["x", "y"]) is None
    assert len(model.requests) == 1      # ... and nothing after it


async def test_a_failing_server_falls_back_and_leaves_it_alone_for_a_while(parts, rerank_config):
    reranker, server, _ = parts
    server.fail = True
    assert await reranker.scores("q", ["a"]) is None and not reranker.enabled
    server.fail = False
    assert await reranker.scores("q", ["a"]) is None      # not retried yet


# ---------- the entry check applies to candidates ----------

class AvoidedClosestEmbedder:
    """Whatever the texts, the query is closest to the avoided tag: it must still never come out."""

    enabled = True

    def __init__(self):
        self.asked: list[str] = []

    def cached(self, texts):
        return [None] * len(texts)

    async def embed(self, texts, query=False):
        self.asked += texts
        out = []
        for t in texts:
            low = t.lower()
            out.append([1.0, 0.0] if query or AVOIDED in low or "star" in low else [0.0, 1.0])
        return out


def avoided_lib() -> TagLib:
    return TagLib({f"{AVOIDED}_star": (GENERAL, 5000), "star_meadow": (GENERAL, 5000), "sunny_meadow": (GENERAL, 5000)})


async def test_meaning_hits_never_embeds_nor_returns_an_avoided_tag(config, db):
    config.embeddings_enabled = True
    emb = AvoidedClosestEmbedder()
    ctx = PlanContext(config, None, None, None, db, embedder=emb)
    assert await meaning_hits(ctx, avoided_lib(), "star light") == ["star_meadow"]
    assert all(AVOIDED not in t for t in emb.asked)      # not sent to the model either


async def test_concept_lines_never_offers_an_avoided_tag(config, db):
    ctx = PlanContext(config, None, None, None, db, tag_library=avoided_lib())
    assert await concept_lines(ctx, f"{AVOIDED} star", "general") == []


def test_plain_entries_leaves_out_avoided_entries():
    from app.wildcards.store import WildList
    lst = WildList("x/y", "x", "setting", "setting", True, ["quiet night", f"{AVOIDED} star", "warm lamp"])
    assert O.plain_entries(lst) == ["quiet night", "warm lamp"]


# ---------- cute words are suppressed at nsfw and explicit ----------

def cute_lib() -> TagLib:
    # "sweet" (unlike "cute", which tagstep.STOP already drops) reaches the keyword search, so it shows the suppression
    return TagLib({"sweet_picnic": (GENERAL, 5000), "picnic_blanket": (GENERAL, 5000)})


async def test_cute_words_bring_no_tags_at_nsfw_or_explicit(config, db):
    assert "cute" in load_concepts().skipped_at_high_rating
    assert "cute" not in load_concepts().for_rating("a cute picnic", "nsfw")      # the word itself is gone too
    ctx = PlanContext(config, None, None, None, db, tag_library=cute_lib())      # keywords only: embeddings off
    assert await concept_lines(ctx, "a sweet picnic", "general") == ["sweet picnic"]
    assert await concept_lines(ctx, "a sweet picnic", "nsfw") == []
    assert await concept_lines(ctx, "a sweet picnic", "explicit") == []


# ---------- a fresh install with no wildcards plans ----------

async def test_concept_lines_without_a_tag_library_is_empty(config, db):
    ctx = PlanContext(config, None, None, None, db)      # tmp data dir: no taglib folder
    assert ctx.taglib() is None
    assert await concept_lines(ctx, "a cozy evening indoors", "general") == []


async def test_offers_without_wildcards_is_none(config, db):
    ctx = PlanContext(config, None, None, None, db, wildcards=None)
    assert await O.build(ctx, "a cozy evening indoors", "", [], [], None, random.Random(1)) is None
