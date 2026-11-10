"""BM25 keyword ranking: plain Python, no extra model in VRAM. Used to shortlist LoRA cards for a job."""
import math
import re
from collections import Counter

STOPWORDS = frozenset("a an and are as at be by for from has have in is it of on or that the this to was with without using use "
                      "image images picture pictures lora loras workflow style of into its their her his she he they them".split())


def tokenize(text: str) -> list[str]:
    """Lowercase words and numbers, camelCase and file-name pieces split, a plural 's' dropped, stop words removed."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words if w not in STOPWORDS]


class Bm25:
    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(d)) for d in documents]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lengths) / len(self.docs)) if self.docs else 0.0
        df = Counter(term for d in self.docs for term in d)
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - c + 0.5) / (c + 0.5)) for term, c in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = set(tokenize(query))
        out = []
        for doc, length in zip(self.docs, self.lengths):
            score = 0.0
            for term in terms & doc.keys():
                tf = doc[term]
                score += self.idf[term] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / (self.avg or 1)))
            out.append(score)
        return out


def rank_scored(items: list, text_of, query: str, top: int = 15) -> list[tuple]:
    """[(item, score)] for the `top` items best matching `query`, best first. Ties (and a query matching nothing) keep the input order."""
    if not items:
        return []
    scores = Bm25([text_of(i) for i in items]).scores(query)
    order = sorted(range(len(items)), key=lambda i: (-scores[i], i))
    return [(items[i], scores[i]) for i in order[:top]]


def rank(items: list, text_of, query: str, top: int = 15) -> list:
    """The `top` items best matching `query`, best first."""
    return [item for item, _ in rank_scored(items, text_of, query, top)]
