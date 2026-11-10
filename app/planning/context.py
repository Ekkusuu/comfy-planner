"""What every planning step needs: config, the LLM client, the library and cards, and ComfyUI's lists."""
import json
import random
from collections import Counter
from dataclasses import dataclass, field

from app.config import Config
from app.db import Database
from app.embeddings import Embedder
from app.fields import split_tags, tag_key
from app.library.card_models import LoraCard, WorkflowCard
from app.library.cards import CardStore
from app.library.lora_extras import LoraExtras
from app.library.workflows import WorkflowLibrary
from app.llm.client import LlmClient, data_block
from app.planning import contradictions
from app.planning.contradictions import Contradictions, get_contradictions
from app.planning.taglib import TagLib, get_taglib, key
from app.rerank import Reranker
from app.wildcards.store import WildcardStore

MAX_LIKED = 5
LIKED_POOL = 200         # liked prompts considered when choosing the most similar ones
RECENT_JOBS = 12         # the newest jobs whose prompt tags are not offered again while other good tags exist (variety)
HABIT_SHARE = 0.5        # a tag in at least this share of the recent prompts is a habit (tagcheck.drop_habits)
HABIT_MIN_PROMPTS = 8    # ... but only when there are at least this many recent prompts to judge by


def derive_seed(seed: int, label: str) -> int:
    """A seed for one purpose of a job ("lists", "tags", "concepts") from the job's seed, so one number repeats a whole plan."""
    return random.Random(f"{seed}:{label}").randrange(2**32)


def derive_rng(seed: int, label: str) -> random.Random:
    """A local random generator for one purpose of a job. Never the global `random` state, so a job can be repeated."""
    return random.Random(f"{seed}:{label}")


def tag_name(tag: str) -> str:
    """A prompt tag as a plain lower-case name (no weight, underscores as spaces), for comparing with library tags."""
    return tag_key(tag).replace("_", " ").strip()


@dataclass
class PlanContext:
    config: Config
    llm: LlmClient
    library: WorkflowLibrary
    cards: CardStore
    db: Database
    oi: dict | None = None          # ComfyUI's /object_info, None when ComfyUI isn't reachable
    wildcards: WildcardStore | None = None
    embedder: Embedder | None = None    # search by meaning; None or disabled: keyword search only
    reranker: Reranker | None = None    # puts the best matches of the meaning search in better order; None, disabled or failing: their order stands
    rng: random.Random = field(default_factory=random.Random)    # picks the wildcard ideas offered to the LLM (tests seed it)
    tag_library: TagLib | None = None   # tests put a small library here; normally the one in data/taglib is read when first needed
    contradiction_data: Contradictions | None = None    # tests put evidence here; normally data/cooccurrence/contradictions.json is read when first needed

    def taglib(self) -> TagLib | None:
        """The Danbooru tag library, or None when it has not been downloaded: then no tag work is done at all."""
        return self.tag_library or get_taglib(self.config.data_dir / "taglib")

    def contradictions(self) -> Contradictions | None:
        """The evidence for contradicting tags, or None when the check is off (CONTRADICTIONS_ENABLED) or the data was never built: then that
        check is skipped quietly. Read from disk on first use only."""
        if self.contradiction_data is not None:
            return self.contradiction_data
        return get_contradictions(self.config.data_dir / contradictions.DATA_FOLDER) if contradictions.CONTRADICTIONS_ENABLED else None

    def idea_seed(self) -> int:
        """A fresh seed for one call's wildcard draw: every plan and every re-plan gets different ingredients."""
        return self.rng.randrange(2**32)

    def recent_tag_counts(self, limit: int = RECENT_JOBS) -> tuple[Counter, int]:
        """(in how many prompts each tag is used, how many prompts) over the newest `limit` jobs, tag names as `tag_name` writes
        them. Read from the stored jobs: nothing extra is kept."""
        counts: Counter = Counter()
        total = 0
        for row in self.db.rows("SELECT fields FROM jobs ORDER BY id DESC LIMIT ?", (limit,)):
            prompts = ((row.get("fields") or {}).get("prompts") or {}).get("value")
            for text in prompts if isinstance(prompts, list) else []:
                names = {name for tag in split_tags(str(text)) if (name := tag_name(tag))}
                if names:
                    counts.update(names)
                    total += 1
        return counts, total

    def recent_tags(self, limit: int = RECENT_JOBS) -> set[str]:
        """The tags used in the prompts of the newest `limit` jobs. Tag offers avoid these when enough other good tags exist."""
        return set(self.recent_tag_counts(limit)[0])

    def habit_tags(self, limit: int = RECENT_JOBS) -> dict[str, float]:
        """{tag: the share of the recent prompts that have it} for the tags that most of them have (HABIT_SHARE), with
        names written as `key` does (underscores). Empty while there are fewer than HABIT_MIN_PROMPTS recent prompts."""
        counts, total = self.recent_tag_counts(limit)
        if total < HABIT_MIN_PROMPTS:
            return {}
        return {key(name): n / total for name, n in counts.items() if n / total >= HABIT_SHARE}

    # ---------- ComfyUI lists ----------

    def options(self, node: str, name: str) -> list[str]:
        """Allowed values of a node input from /object_info ([] when unknown)."""
        return list((self.oi or {}).get(node, {}).get("input", {}).get("required", {}).get(name, [[]])[0])

    @property
    def samplers(self) -> list[str]:
        return self.options("KSampler", "sampler_name")

    @property
    def schedulers(self) -> list[str]:
        return self.options("KSampler", "scheduler")

    @property
    def lora_files(self) -> list[str]:
        return self.options("LoraLoaderModelOnly", "lora_name")

    # ---------- cards ----------

    def workflow_card(self, wf_id: str) -> WorkflowCard | None:
        row = next((c for c in self.cards.usable("workflow") if c["key"] == wf_id), None)
        return WorkflowCard.model_validate(row["card"]) if row else None

    def lora_choices(self) -> list[tuple[str, LoraCard]]:
        """LoRA cards the planner may use: usable cards whose file ComfyUI lists (when ComfyUI is reachable)."""
        listed = self.lora_files
        return [(c["key"], LoraCard.model_validate(c["card"])) for c in self.cards.usable("lora") if not listed or c["key"] in listed]

    def lora_keys(self) -> list[str]:
        """Every LoRA file the app knows, with a card or listed by ComfyUI, usable or not (what an `@` mention can name)."""
        return sorted({*(c["key"] for c in self.cards.of_kind("lora")), *self.lora_files})

    def lora_tags(self) -> dict[str, list[str]]:
        """{LoRA file: the user's own tags}, for the files that have any."""
        return LoraExtras(self.db).tags_by_key()

    def lora_card(self, name: str) -> LoraCard | None:
        row = self.cards.get("lora", name)
        return LoraCard.model_validate(row["card"]) if row else None

    def liked_examples(self, wf_id: str, limit: int = MAX_LIKED) -> list[str]:
        """Prompts of images rated 👍 for this workflow, newest first (max `limit`)."""
        rows = self.db.rows("SELECT i.recipe FROM images i JOIN jobs j ON j.id = i.job_id WHERE j.workflow_id = ? AND i.rating = 1 "
                            "ORDER BY i.finished_at DESC, i.id DESC LIMIT ?", (wf_id, limit))
        return [r["recipe"]["prompt"] for r in rows if r["recipe"].get("prompt")]

    async def liked_for(self, wf_id: str, brief: str) -> list[str]:
        """Up to MAX_LIKED liked prompts for this workflow: the ones closest in meaning to the brief when meaning search is on
        (out of the LIKED_POOL newest), otherwise the newest."""
        pool = self.liked_examples(wf_id, LIKED_POOL)
        if len(pool) > MAX_LIKED:
            from app.planning.semantic import similar_prompts      # semantic imports this module
            close = await similar_prompts(self, list(dict.fromkeys(pool)), brief, MAX_LIKED)
            if close:
                return close
        return pool[:MAX_LIKED]


def block(label: str, value) -> str:
    """JSON (or text) put into a marked data block, for card text and other third-party material."""
    return data_block(label, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1))


def job_view(job: dict) -> dict:
    """What the LLM is told about the job itself. The rest of the user's request is left out on purpose: in a request with
    several jobs it would mix up their details."""
    return {"description": job["description"], "images": job.get("total", job["count"]), "constraints": job.get("constraints", [])}
