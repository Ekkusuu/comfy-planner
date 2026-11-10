"""Search by meaning in the planner, on top of app/embeddings.py. Every function returns None (or its input
unchanged) when meaning search isn't available, so the callers fall back to keyword ranking.

Wildcard lists: a list's score for a brief is the average of two cosines, the brief against the list's name and role ("background
ambience (setting)") and the mean of the brief's 3 closest entries ("moody" is close to the entry "melancholy" of a list that is
not called anything like it). A list is *related* when its score reaches `floor` and is within `margin` of the best list's score.
Related lists are ranked by reciprocal rank fusion of the meaning score and the keyword (BM25) score.
Reranking (`rerank_blend`): the best few candidates of a meaning search are scored again by the reranker (app/rerank.py), and the two scores,
each scaled 0..1 over those candidates, are blended; only their order among themselves changes.
LoRA cards: the brief against each card's text; a card counts as pointed at when it reaches `lora_floor`.
Liked examples: the 👍 prompts most similar to the brief."""
import logging
import re
from collections import defaultdict

from app.embeddings import dot
from app.planning.context import PlanContext
from app.planning.rank import Bm25
from app.wildcards.ideas import doc_text
from app.wildcards.store import WildList

log = logging.getLogger("planning.embeddings")
ENTRY_CHARS = 120           # an entry is cut here (the same as when its vector is stored)
MAX_ENTRIES = 600           # entries embedded per list; a longer list is sampled evenly
IMMEDIATE = 300             # up to this many missing texts are embedded now; more are embedded in the background
TOP_ENTRIES = 3
QUERY_CHARS = 500


def list_name_text(l: WildList) -> str:
    """The list as a short phrase: "background ambience add (setting)"."""
    return f"{re.sub(r'[/_-]+', ' ', l.name)} ({l.role})"


def entry_texts(l: WildList) -> list[str]:
    """The entries of a list worth embedding: plain text (no `__ref__` or `{a|b}` syntax), distinct, cut, at most MAX_ENTRIES."""
    plain = list(dict.fromkeys(e.strip()[:ENTRY_CHARS] for e in l.entries if e.strip() and "__" not in e and "{" not in e))
    if len(plain) > MAX_ENTRIES:
        plain = [plain[i * len(plain) // MAX_ENTRIES] for i in range(MAX_ENTRIES)]
    return plain


def clean_query(text: str) -> str:
    """The brief as the model should read it: without "2 images of" and the like (a count says nothing about the picture, and
    it shifts which lists look close)."""
    text = re.sub(r"\b(?:\d+|one|two|three|four|five|six|a couple of|a few|some)\s+(?:images?|pictures?|photos?|variations?|versions?|renders?)\b(?:\s+of\b)?", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()[:QUERY_CHARS]


def rrf(*rankings: list[int], k: int = 60) -> list[int]:
    """Reciprocal rank fusion: item indexes ordered by their combined rank in several rankings (best first)."""
    score: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for pos, i in enumerate(ranking):
            score[i] += 1 / (k + pos + 1)
    return sorted(score, key=lambda i: -score[i])


async def vectors_or_background(ctx: PlanContext, texts: list[str]):
    """Vectors for `texts`, or None. A few missing ones are computed now; many (a library not embedded yet) are computed in the
    background and this call gives up, so a plan never waits minutes for them."""
    embedder = ctx.embedder
    have = embedder.cached(texts)
    missing = [t for t, v in zip(texts, have) if v is None]
    if not missing:
        return have
    if len(missing) > IMMEDIATE:
        log.info("%d texts are not embedded yet; searching by keyword this time, embedding them in the background", len(missing))
        embedder.warm(missing)
        return None
    return await embedder.embed(texts)


def usable(ctx: PlanContext) -> bool:
    return ctx.embedder is not None and ctx.embedder.enabled


def unit_range(values: list[float]) -> list[float]:
    """The values scaled to 0..1 (their lowest to their highest); all 0.5 when they are equal."""
    low, high = min(values), max(values)
    return [(v - low) / (high - low) if high > low else 0.5 for v in values]


async def rerank_blend(ctx: PlanContext, query: str, texts: list[str], scores: list[float]) -> list[float]:
    """`scores` (the meaning search's, higher is better) with the best `reranker_pool` texts scored again by the reranker for this query. Over
    those texts both scores are scaled 0..1 and blended (`reranker_weight` for the reranker's), then put back on the range the old scores had there,
    so the texts after the pool stay behind it and the callers' floors and bonuses mean what they did. The scores unchanged when reranking isn't
    available."""
    reranker = ctx.reranker
    if reranker is None or not reranker.enabled or len(texts) < 2 or not query.strip():
        return scores
    top = sorted(range(len(texts)), key=lambda i: (-scores[i], i))[:max(ctx.config.reranker_pool, 0)]
    if len(top) < 2:
        return scores
    fresh = await reranker.scores(query, [texts[i] for i in top])
    if fresh is None:
        return scores
    old = [scores[i] for i in top]
    low, high, weight = min(old), max(old), ctx.config.reranker_weight
    blend = [weight * r + (1 - weight) * m for r, m in zip(unit_range(fresh), unit_range(old))]
    out = list(scores)
    for i, b in zip(top, blend):
        out[i] = low + (high - low) * b
    return out


async def related_lists(ctx: PlanContext, lists: list[WildList], query: str) -> list[tuple[WildList, float]] | None:
    """[(list, meaning score)] for the lists related to `query`, best first; None when meaning search isn't available."""
    query = clean_query(query)
    if not usable(ctx) or not lists or not query:
        return None
    texts = [t for l in lists for t in (list_name_text(l), *entry_texts(l))]
    vectors = await vectors_or_background(ctx, list(dict.fromkeys(texts)))
    q = await ctx.embedder.embed([query], query=True)
    if vectors is None or q is None:
        return None
    by_text = dict(zip(dict.fromkeys(texts), vectors))
    qv = q[0]
    scores = []
    for l in lists:
        name = dot(qv, by_text[list_name_text(l)])
        near = sorted((dot(qv, by_text[e]) for e in entry_texts(l)), reverse=True)[:TOP_ENTRIES]
        scores.append((name + sum(near) / len(near)) / 2 if near else name)
    order = sorted(range(len(lists)), key=lambda i: (-scores[i], i))
    best, c = scores[order[0]], ctx.config
    keywords = Bm25([doc_text(l) for l in lists]).scores(query)
    ok = [i for i in order if scores[i] >= c.embeddings_floor and scores[i] >= best - c.embeddings_margin]
    named = sorted((i for i in ok if keywords[i] > 0), key=lambda i: (-keywords[i], i))
    return [(lists[i], scores[i]) for i in rrf(ok, named)]


async def lora_similarities(ctx: PlanContext, texts: list[str], query: str) -> list[float] | None:
    """The meaning score of each card text against `query`, or None when meaning search isn't available."""
    query = clean_query(query)
    if not usable(ctx) or not texts or not query:
        return None
    vectors = await ctx.embedder.embed(texts)
    q = await ctx.embedder.embed([query], query=True)
    if vectors is None or q is None:
        return None
    return [dot(q[0], v) for v in vectors]


async def similar_prompts(ctx: PlanContext, prompts: list[str], query: str, top: int) -> list[str] | None:
    """The `top` prompts closest in meaning to `query`, closest first; None when meaning search isn't available."""
    query = clean_query(query)
    if not usable(ctx) or not prompts or not query:
        return None
    vectors = await ctx.embedder.embed(prompts)
    q = await ctx.embedder.embed([query], query=True)
    if vectors is None or q is None:
        return None
    order = sorted(range(len(prompts)), key=lambda i: (-dot(q[0], vectors[i]), i))
    return [prompts[i] for i in order[:top]]


def warm_texts(lists: list[WildList]) -> list[str]:
    """Everything `related_lists` embeds for these lists (for the warm-up at start)."""
    return list(dict.fromkeys(t for l in lists for t in (list_name_text(l), *entry_texts(l))))
