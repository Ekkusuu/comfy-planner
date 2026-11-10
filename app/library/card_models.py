"""Pydantic models for workflow and LoRA cards, and for what the LLM is asked to fill in.

The *Card models are what is stored (as JSON in SQLite) and edited. The *Fill models are the LLM's replies; their
JSON schemas are in config/schemas/. Keep each pair in step with its schema file.
"""
from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

from app.llm.client import llm_task

Family = Literal["Anima", "Krea-2", "Qwen-Image", "SDXL", "Pony", "Illustrious", "SD1.5", "Flux", "other"]
FAMILIES = list(get_args(Family))
LoraKind = Literal["character", "style", "concept", "clothing", "pose", "other"]
LORA_KINDS = list(get_args(LoraKind))
PromptKind = Literal["tags", "sentences", "mixed"]


class Range(BaseModel):
    rec: float | None = None
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def ordered(self):
        """Keep min <= rec <= max (widen the range to include the recommended value)."""
        if self.rec is not None:
            if self.min is not None and self.min > self.rec:
                self.min = self.rec
            if self.max is not None and self.max < self.rec:
                self.max = self.rec
        if self.min is not None and self.max is not None and self.min > self.max:
            self.min, self.max = self.max, self.min
        return self


class PromptStyle(BaseModel):
    kind: PromptKind = "mixed"
    quality_prefix: str = ""
    rules: list[str] = Field(default_factory=list)


class Size(BaseModel):
    w: int = Field(ge=64)
    h: int = Field(ge=64)
    label: str = ""


class PixelBudget(BaseModel):
    min: int | None = None
    max: int | None = None


class Hires(BaseModel):
    steps: Range = Field(default_factory=Range)
    denoise: Range = Field(default_factory=Range)


class CardSettings(BaseModel):
    cfg: Range = Field(default_factory=Range)
    steps: Range = Field(default_factory=Range)
    samplers: list[str] = Field(default_factory=list)
    schedulers: list[str] = Field(default_factory=list)
    sizes: list[Size] = Field(default_factory=list)
    pixel_budget: PixelBudget = Field(default_factory=PixelBudget)
    hires: Hires | None = None


class LoraRef(BaseModel):
    name: str
    weight: float | None = None


# ---------- stored cards ----------

class WorkflowCard(BaseModel):
    # written by the LLM (and edited by you)
    family: Family = "other"
    purpose: str = ""
    best_for: str = ""
    prompt_style: PromptStyle = Field(default_factory=PromptStyle)
    settings: CardSettings = Field(default_factory=CardSettings)
    warnings: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    use_full_notes: bool = False
    # read by code
    kind: Literal["t2i", "edit"] = "t2i"
    models: list[str] = Field(default_factory=list)          # model files the workflow loads
    input_images: list[str] = Field(default_factory=list)    # what each input image is for (edit workflows)
    default_loras: list[LoraRef] = Field(default_factory=list)
    default_negative: str = ""
    notes_full: str = ""
    # bookkeeping
    needs_attention: list[str] = Field(default_factory=list)   # fields the LLM couldn't fill
    built_with: str = ""
    built_at: str = ""


class CivitaiInfo(BaseModel):
    model_id: int | None = None
    version_id: int | None = None
    url: str = ""
    name: str = ""
    version_name: str = ""
    base_model: str = ""
    example_prompts: list[str] = Field(default_factory=list, max_length=5)


class LoraCard(BaseModel):
    file: str
    sha256: str = ""
    family: Family = "other"
    kind: LoraKind = "other"
    what_it_does: str = ""
    trigger_words: list[str] = Field(default_factory=list)
    style_tags: list[str] = Field(default_factory=list)      # "@artist" style selectors the file was trained with
    tags: list[str] = Field(default_factory=list)            # top training tags
    weight: Range = Field(default_factory=Range)
    usage_notes: list[str] = Field(default_factory=list)
    remove_from_negative: list[str] = Field(default_factory=list)   # set by you only: negative tags this LoRA works better without
    sets_look: bool | None = None                            # set by you only: the LoRA brings its own colours, light and rendering, so the planner adds none; None = by kind
    civitai: CivitaiInfo | None = None
    metadata_only: bool = True                                # True: built from the file alone, no Civitai data
    notice: str = ""                                          # why, when metadata_only
    needs_attention: list[str] = Field(default_factory=list)
    built_with: str = ""
    built_at: str = ""

    @model_validator(mode="after")
    def look_by_kind(self):
        """A card that says nothing about it (every card written before that setting existed) sets the look when it is a style LoRA, and not otherwise."""
        if self.sets_look is None:
            self.sets_look = self.kind == "style"
        return self

    @field_validator("remove_from_negative", mode="before")
    @classmethod
    def clean_words(cls, v):
        """Trimmed, no empty entries, no repeats."""
        return list(dict.fromkeys(w.strip() for w in v if isinstance(w, str) and w.strip())) if isinstance(v, list) else v


CARD_MODELS = {"workflow": WorkflowCard, "lora": LoraCard}


# ---------- LLM replies ----------

def _text(v):
    return v.strip() if isinstance(v, str) else v


@llm_task("workflow_card")
class WorkflowCardFill(BaseModel):
    purpose: str = Field(min_length=1)
    best_for: str = Field(min_length=1)
    family: Family
    prompt_style: PromptStyle
    settings: CardSettings
    warnings: list[str]
    examples: list[str] = Field(min_length=1, max_length=4)
    use_full_notes: bool

    _strip = field_validator("purpose", "best_for", mode="before")(_text)


@llm_task("lora_card")
class LoraCardFill(BaseModel):
    what_it_does: str = Field(min_length=1)
    kind: LoraKind
    family: Family
    trigger_words: list[str]
    weight: Range
    usage_notes: list[str]

    _strip = field_validator("what_it_does", mode="before")(_text)
