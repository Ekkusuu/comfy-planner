"""Build draft cards: code fills what it can read for certain, the LLM (task workflow_card / lora_card) writes the rest.

Builds run one after another under a single GPU lease, so the LLM is loaded once for a batch of cards.
"""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

from app.comfy.client import ComfyError
from app.config import Config
from app.db import now
from app.events import EventBus
from app.fields import remove_tags
from app.gpu import GpuLease
from app.library import civitai
from app.library.card_models import FAMILIES, LORA_KINDS, CivitaiInfo, CardSettings, Hires, LoraCard, LoraRef, PixelBudget, Range, Size, WorkflowCard
from app.library.cards import CardError, CardStore
from app.library.loras import LoraScanner
from app.library.safetensors_meta import SafetensorsError
from app.library.workflows import WorkflowLibrary
from app.llm.client import LlmClient, data_block
from app.llm.server import LlmServerError
from app.planning.taglib import TagLib, get_taglib

log = logging.getLogger("cards")

MODEL_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".bin")
MAX_TRIGGER_CANDIDATES = 150

WORKFLOW_LLM_FIELDS = ["purpose", "best_for", "family", "prompt_style", "warnings", "examples"]
LORA_LLM_FIELDS = ["what_it_does", "kind", "family", "weight", "usage_notes"]


# Cards carry no content-rating tags and no rules about them: the app doesn't filter content. Notes written by
# workflow authors often do ("score_9, safe, ..." or "keep the safety tags in the negative"), so code removes them.
RATING_TAGS = ["safe", "sensitive", "questionable", "nsfw", "explicit"]
SAFETY_LINE = re.compile(r"\b(nsfw|sfw|safety|explicit|nudity|nude)\b", re.I)


def drop_safety_wording(card):
    """Take rating tags out of the prompt prefix and examples, and drop rules, warnings, usage notes and note lines about them."""
    keep = lambda items: [x for x in items if not SAFETY_LINE.search(x)]
    if isinstance(card, WorkflowCard):
        card.prompt_style.quality_prefix = remove_tags(card.prompt_style.quality_prefix, RATING_TAGS) or ""
        card.prompt_style.rules, card.warnings = keep(card.prompt_style.rules), keep(card.warnings)
        card.examples = [remove_tags(x, RATING_TAGS) or x for x in card.examples]
        lines = (line for line in card.notes_full.split("\n") if not SAFETY_LINE.search(line))
        card.notes_full = "\n".join(re.sub(r"\b(?:" + "|".join(RATING_TAGS) + r"),\s*", "", line) for line in lines)
    else:
        card.usage_notes = keep(card.usage_notes)
    return card


@dataclass
class BuildRequest:
    kind: str
    key: str


# ---------- workflow cards ----------

def model_files(graph: dict) -> list[str]:
    """Model file names the workflow loads (checkpoints, diffusion models, text encoders, VAEs, upscalers), without LoRAs."""
    found: list[str] = []
    for node in graph.values():
        if "lora" in node["class_type"].lower():
            continue
        for value in node["inputs"].values():
            if isinstance(value, str) and value.lower().endswith(MODEL_EXTENSIONS) and value not in found:
                found.append(value)
    return found


def workflow_facts(wf: dict, graph: dict) -> dict:
    """Everything about the workflow that code can read for certain."""
    slots, defaults = wf["slots"], wf["slots"].get("defaults") or {}
    return {"title": wf["title"], "kind": wf["kind"], "model_files": model_files(graph),
            "has_hires": bool(slots.get("hires")), "input_images": [i["label"] for i in slots.get("input_images", [])],
            "current_values": {k: defaults.get(k) for k in ("steps", "cfg", "sampler", "scheduler", "width", "height", "denoise") if defaults.get(k) is not None},
            "current_hires_values": defaults.get("hires"),
            "workflow_loras": defaults.get("loras") or [],
            "switches": [{"title": s["title"], "role": s["role"], "default": s["default"]} for s in slots.get("switches", [])],
            "allowed_families": FAMILIES}


def notes_text(wf: dict) -> str:
    return "\n\n".join(f"### {n['title']}\n{n['text'].strip()}" for n in wf["notes"] if n.get("text", "").strip())


def prefill_workflow(wf: dict, graph: dict) -> WorkflowCard:
    """A card with everything code knows: current values become the recommended ones, the rest stays empty."""
    slots, d = wf["slots"], wf["slots"].get("defaults") or {}
    width, height = d.get("width"), d.get("height")
    settings = CardSettings(cfg=Range(rec=d.get("cfg")), steps=Range(rec=d.get("steps")),
                            samplers=[d["sampler"]] if d.get("sampler") else [], schedulers=[d["scheduler"]] if d.get("scheduler") else [])
    if width and height:
        settings.sizes = [Size(w=width, h=height, label="workflow default")]
        settings.pixel_budget = PixelBudget(min=int(width * height * 0.5), max=int(width * height * 1.5))
    if slots.get("hires"):
        h = d.get("hires") or {}
        settings.hires = Hires(steps=Range(rec=h.get("steps")), denoise=Range(rec=h.get("denoise")))
    return WorkflowCard(kind=wf["kind"], models=model_files(graph), input_images=[i["label"] for i in slots.get("input_images", [])],
                        default_loras=[LoraRef(name=x["name"], weight=x.get("weight")) for x in d.get("loras") or [] if x.get("name")],
                        default_negative=d.get("negative") or "", notes_full=notes_text(wf), settings=settings)


def merge_workflow(base: WorkflowCard, fill: dict | None, wf: dict) -> WorkflowCard:
    """Put the LLM's answer on top of the pre-filled card; keep what code knows for certain."""
    card = base.model_copy(deep=True)
    d = wf["slots"].get("defaults") or {}
    if fill is None:   # the LLM gave nothing usable: keep the pre-filled card and say what is empty
        card.needs_attention = list(WORKFLOW_LLM_FIELDS)
        if d.get("positive"):
            card.examples = [d["positive"]]
        return card
    s = CardSettings.model_validate(fill["settings"])
    card.family, card.purpose, card.best_for = fill["family"], fill["purpose"], fill["best_for"]
    card.prompt_style, card.warnings, card.examples = type(card.prompt_style).model_validate(fill["prompt_style"]), fill["warnings"], fill["examples"]
    card.use_full_notes = fill["use_full_notes"]

    def keep_current(rng: Range, current) -> Range:
        """The range the LLM gave, widened to include the value the workflow uses now."""
        if isinstance(current, (int, float)):
            rng = Range(rec=rng.rec if rng.rec is not None else current, min=min(rng.min, current) if rng.min is not None else None,
                        max=max(rng.max, current) if rng.max is not None else None)
        return rng

    card.settings.cfg = keep_current(s.cfg, d.get("cfg"))
    card.settings.steps = keep_current(s.steps, d.get("steps"))
    card.settings.samplers = _first_then(base.settings.samplers, s.samplers)
    card.settings.schedulers = _first_then(base.settings.schedulers, s.schedulers)
    if wf["slots"].get("width") and wf["slots"].get("height"):     # an edit workflow takes its size from the input image: no size to list
        sizes = {(z.w, z.h): z for z in [*base.settings.sizes, *s.sizes]}
        card.settings.sizes = list(sizes.values())
    pb = s.pixel_budget
    if pb.min and pb.max and pb.min <= pb.max:
        card.settings.pixel_budget = pb
    if base.settings.hires is not None:
        h = s.hires or base.settings.hires
        card.settings.hires = Hires(steps=keep_current(h.steps, (d.get("hires") or {}).get("steps")),
                                    denoise=keep_current(h.denoise, (d.get("hires") or {}).get("denoise")))
    return card


def _first_then(known: list[str], more: list[str]) -> list[str]:
    return list(dict.fromkeys([*known, *more]))


# ---------- LoRA cards ----------

def clean_words(words) -> list[str]:
    """Trigger words as they should be stored: whitespace and stray `, . ; :` around a word removed ("@l7m3n," -> "@l7m3n"),
    empty ones and repeats (ignoring case) dropped. The inside of a word is never changed."""
    kept: dict[str, str] = {}
    for w in words:
        if isinstance(w, str) and (w := w.strip().strip(",.;:").strip()):
            kept.setdefault(w.lower(), w)
    return list(kept.values())


def find_word(word: str, text: str) -> str | None:
    """The word as spelled in the text, if it stands there as a whole word (ignoring case): "@cedar" is not found in "@cedars"
    or "x@cedar". The leading "@" counts as part of the word."""
    found = re.search(r"(?<![\w@])" + r"\s+".join(re.escape(part) for part in word.split()) + r"(?!\w)", text, re.I)
    return found.group(0) if found else None


def real_style_tags(tags: list[str], lib: TagLib | None) -> list[str]:
    """The "@..." tokens of a LoRA's training captions that are style selectors a prompt can use. A selector has a name in it (letters or
    digits: `@cedar`, `@marlow (blue sedge)`), and is not a tag of the tag library: `@_@` and `@ @` are the swirly-eyes face, the library
    knows it, and `@` alone names nothing. Without the library only the name test applies."""
    return [t for t in tags if re.search(r"[^\W_]", t.lstrip("@")) and not (lib is not None and lib.lookup(t) is not None)]


def lora_candidates(meta: dict, civ: dict | None) -> list[str]:
    """Words the LLM may choose trigger words from: Civitai's trained words, then the "@word" captions start with and
    names found in the file's training data."""
    words = [*(civ or {}).get("trained_words", []), *meta.get("caption_starts", []), *meta["style_tags"], *meta["dataset_names"], *meta["top_tags"][:15]]
    return clean_words(words)[:MAX_TRIGGER_CANDIDATES]


def accepted_trigger_words(asked: list[str], meta: dict, civ: dict | None) -> list[str]:
    """The LLM's trigger words that are really in the data: a candidate, or a whole word of the Civitai description or an example prompt."""
    candidates = {w.lower(): w for w in lora_candidates(meta, civ)}
    texts = [(civ or {}).get("description") or "", *(civ or {}).get("example_prompts", [])]
    accepted = []
    for word in clean_words(asked):
        spelled = candidates.get(word.lower()) or next((m for t in texts if (m := find_word(word, t))), None)
        if spelled:
            accepted.append(spelled)
    return clean_words(accepted)


def merge_lora(file_name: str, sha256: str, meta: dict, civ: dict | None, result: civitai.CivitaiResult, fill: dict | None,
               used_by: list[dict], lib: TagLib | None = None) -> LoraCard:
    card = LoraCard(file=file_name, sha256=sha256, tags=meta["top_tags"][:20], style_tags=real_style_tags(meta["style_tags"], lib),
                    civitai=CivitaiInfo(**{k: civ[k] for k in ("model_id", "version_id", "url", "name", "version_name", "base_model", "example_prompts")}) if civ else None,
                    metadata_only=civ is None, notice=_notice(result))
    weights = [u["weight"] for u in used_by if u.get("weight") is not None]
    if civ and clean_words(civ["trained_words"]):
        card.trigger_words = clean_words(civ["trained_words"])          # Civitai's own list is certain
    if fill is None:
        card.needs_attention = list(LORA_LLM_FIELDS)
        card.weight = Range(rec=weights[0]) if weights else Range()
        return card
    card.what_it_does, card.kind, card.family, card.usage_notes = fill["what_it_does"], fill["kind"], fill["family"], fill["usage_notes"]
    if not card.trigger_words:   # only words that really are in the file's training data or in what Civitai says
        card.trigger_words = accepted_trigger_words(fill["trigger_words"], meta, civ)
    card.weight = Range.model_validate(fill["weight"])
    if weights:
        card.weight = Range(rec=weights[0], min=min(card.weight.min or weights[0], weights[0]), max=max(card.weight.max or weights[0], weights[0]))
    return card


def _notice(result: civitai.CivitaiResult) -> str:
    if result.status == "ok":
        return ""
    return f"Built from the file's own metadata only. {result.message}".strip()


# ---------- the builder ----------

class CardBuilder:
    def __init__(self, cards: CardStore, library: WorkflowLibrary, scanner: LoraScanner, civ: civitai.CivitaiClient,
                 llm: LlmClient, lease: GpuLease, comfy, events: EventBus, config: Config):
        self.cards, self.library, self.scanner, self.civitai, self.llm = cards, library, scanner, civ, llm
        self.lease, self.comfy, self.events, self.config = lease, comfy, events, config
        self.pending: list[BuildRequest] = []
        self.builds: dict[tuple[str, str], dict] = {}     # latest state per card, for the UI
        self._runner: asyncio.Task | None = None

    # ---------- queueing ----------

    def request(self, kind: str, key: str) -> dict:
        """Queue a card build. Checks the card's subject exists first; the build itself runs in the background."""
        if kind == "workflow":
            self.library.get(key)    # KeyError if unknown
        elif kind != "lora":
            raise KeyError(kind)
        if any(r.kind == kind and r.key == key for r in self.pending) or self.builds.get((kind, key), {}).get("stage") in ("waiting", "loading", "reading", "writing"):
            return self.builds[(kind, key)]
        self.pending.append(BuildRequest(kind, key))
        state = self._set(kind, key, "queued", "Waiting to start")
        if self._runner is None:
            self._runner = asyncio.create_task(self._run())
        return state

    def status(self) -> list[dict]:
        return list(self.builds.values())

    def _set(self, kind: str, key: str, stage: str, message: str = "", **extra) -> dict:
        state = {"kind": kind, "key": key, "stage": stage, "message": message, **extra}
        self.builds[(kind, key)] = state
        self.events.publish({"type": "card", **state})
        return state

    async def close(self):
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)

    # ---------- the runner ----------

    async def _run(self):
        """Take the GPU once, build every queued card, give the GPU back."""
        try:
            def stage(name: str):
                text = {"waiting": "Waiting for the current image to finish", "loading": "Loading the LLM"}[name]
                for r in self.pending:
                    self._set(r.kind, r.key, name, text)
            async with self.lease.session(stage):
                while self.pending:
                    req = self.pending.pop(0)
                    await self._build_one(req)
        except asyncio.CancelledError:
            raise
        except (LlmServerError, ComfyError, OSError) as e:
            log.warning("card build could not start: %s", e)
            for r in self.pending:
                self._set(r.kind, r.key, "failed", str(e))
            self.pending.clear()
        except Exception as e:
            log.exception("card build crashed")
            for r in self.pending:
                self._set(r.kind, r.key, "failed", f"Unexpected error: {type(e).__name__}")
            self.pending.clear()
        finally:
            self._runner = None
            if self.pending:   # a request arrived while the GPU was being given back
                self._runner = asyncio.create_task(self._run())

    async def _build_one(self, req: BuildRequest):
        started = time.monotonic()
        try:
            self._set(req.kind, req.key, "reading", "Reading the " + ("workflow" if req.kind == "workflow" else "file"))
            card = await (self._workflow_card(req.key) if req.kind == "workflow" else self._lora_card(req.key))
            row = self.cards.save_built(req.kind, req.key, card)
            seconds = round(time.monotonic() - started, 1)
            self._set(req.kind, req.key, "done", "Needs attention: " + ", ".join(card.needs_attention) if card.needs_attention else "Draft written",
                      seconds=seconds, pending=row["pending"] is not None, needs_attention=card.needs_attention)
        except (CardError, KeyError, SafetensorsError, OSError) as e:
            self._set(req.kind, req.key, "failed", str(e.args[0] if isinstance(e, KeyError) else e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("card build failed")
            self._set(req.kind, req.key, "failed", f"Unexpected error: {type(e).__name__}")

    # ---------- workflow ----------

    async def _optional_object_info(self) -> dict | None:
        try:
            return await self.comfy.object_info()
        except ComfyError:
            return None

    async def _workflow_card(self, wf_id: str) -> WorkflowCard:
        wf, graph = self.library.get(wf_id), self.library.get_graph(wf_id)
        base = prefill_workflow(wf, graph)
        oi = await self._optional_object_info()
        options = lambda name: (oi or {}).get("KSampler", {}).get("input", {}).get("required", {}).get(name, [[]])[0]
        inputs = {"facts": workflow_facts(wf, graph),
                  "notes": data_block("workflow notes", notes_text(wf) or "(the workflow has no notes)"),
                  "defaults": data_block("saved prompts", json.dumps({"positive": (wf["slots"].get("defaults") or {}).get("positive"),
                                                                       "negative": base.default_negative}, ensure_ascii=False, indent=1))}
        self._set("workflow", wf_id, "writing", "Writing the card")
        result = await self.llm.call("workflow_card", inputs, {"family": FAMILIES, "samplers": options("sampler_name"), "schedulers": options("scheduler")})
        card = merge_workflow(base, None if result.get("needs_attention") else result, wf)
        if result.get("needs_attention"):
            log.warning("workflow_card for %s needs attention: %s", wf_id, result["error"])
        return self._stamp(card)

    # ---------- LoRA ----------

    async def _lora_card(self, name: str) -> LoraCard:
        file = next((f for f in await self.scanner.scan() if f.name == name), None)
        if file is None or file.path is None:
            raise CardError(f"{name} isn't in the LoRA folders any more. Rescan the LoRAs.")
        meta = self.scanner.metadata(file)
        lib = await asyncio.to_thread(get_taglib, self.config.data_dir / "taglib")      # in a thread: the first read takes about a second
        meta = {**meta, "style_tags": real_style_tags(meta["style_tags"], lib)}        # candidates and the card see only real selectors
        result = await self.civitai.by_hash(file.sha256)
        civ = civitai.summarize(result.data) if result.status == "ok" else None
        used_by = self._used_by(name)
        facts = {"file": file.name, "size_mb": round(file.size / 1e6), "allowed_families": FAMILIES, "allowed_kinds": LORA_KINDS,
                 "used_by_workflows": used_by, "candidate_trigger_words": lora_candidates(meta, civ),
                 "style_tags_total": len(meta["style_tags"]), "civitai_status": result.status}
        inputs = {"facts": facts,
                  "metadata": data_block("file metadata", json.dumps({k: v for k, v in meta.items() if k != "style_tags"}, ensure_ascii=False, indent=1)),
                  "civitai": data_block("Civitai", json.dumps(civ, ensure_ascii=False, indent=1) if civ else "(no Civitai data: " + result.message + ")")}
        self._set("lora", name, "writing", "Writing the card")
        fill = await self.llm.call("lora_card", inputs, {"family": FAMILIES, "kind": LORA_KINDS})
        if fill.get("needs_attention"):
            log.warning("lora_card for %s needs attention: %s", name, fill["error"])
        card = merge_lora(file.name, file.sha256, meta, civ, result, None if fill.get("needs_attention") else fill, used_by, lib)
        if (current := self.cards.get("lora", name)):
            card.remove_from_negative = current["card"].get("remove_from_negative", [])        # yours, not the LLM's: kept when the card is written again
            if current["card"].get("sets_look") is not None:
                card.sets_look = current["card"]["sets_look"]        # likewise; a card that never said keeps following its kind
        return self._stamp(card)

    def _used_by(self, lora: str) -> list[dict]:
        """Workflows whose own graph already uses this LoRA, with the strength they use (a certain hint for family and weight)."""
        out = []
        for row in self.library.list():
            wf = self.library.get(row["id"])
            for item in (wf["slots"].get("defaults") or {}).get("loras") or []:
                if item.get("name") == lora:
                    card = self.cards.get("workflow", wf["id"])
                    out.append({"workflow": wf["title"], "weight": item.get("weight"), "family": card["card"]["family"] if card else None})
        return out

    def _stamp(self, card):
        card.built_with, card.built_at = self.config.llm_model, now()
        return drop_safety_wording(card)
