"""Pydantic models that validate each planning task's reply. Keep each in step with config/schemas/<task>.json.

Properties the planner leaves out of a call (locked or already decided fields) are simply missing from the reply, so
the fields of the settings, prompts and tweak replies are optional.
"""
from typing import Literal

from pydantic import BaseModel, Field

from app.library.card_models import LoraKind
from app.llm.client import llm_task

# the scene aspects a request can ask to be different from image to image (intent.vary)
AXES = ("camera", "gaze", "pose", "outfit", "setting", "lighting", "expression", "body", "mood")


class Subjects(BaseModel):
    """Who is in the pictures. A number is how many; -1 means the request doesn't say (code then forces nothing)."""
    girls: int = Field(default=-1, ge=-1)
    boys: int = Field(default=-1, ge=-1)
    others: int = Field(default=-1, ge=-1)         # people who are neither girls nor boys (never animals)
    people: int = Field(default=-1, ge=-1)         # how many people when the request gives a number but not who they are
    no_humans: bool = False


class PlannedJob(BaseModel):
    picture_given: bool = True                     # false: the request does not say what the pictures show (code then has one written, planner.py)
    description: str = Field(min_length=1)
    count: int = Field(ge=1)
    variety: Literal["variations", "unique"]       # same scene in several framings, or every image its own idea
    subjects: Subjects = Field(default_factory=Subjects)
    constraints: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    reason: str = ""


class MentionVerdict(BaseModel):
    """Whether the user wants the LoRA they wrote with @ (the numbered mention): one verdict per mention, so none is left to a guess."""
    mention: int = Field(ge=1)
    wanted: bool


class RequestIntent(BaseModel):
    """What the whole request asks for besides the pictures themselves. Names are what the user wrote; code resolves
    them against the real LoRA cards, so every field is a claim that code checks. All defaults mean "nothing special"."""
    rating: Literal["general", "sensitive", "nsfw", "explicit"] = "general"     # the content level the request asks for
    rating_limit: bool = False      # the request sets an upper limit on the content level ("nothing explicit", "keep it tasteful"): an intensifier ("very nsfw") then raises nothing
    mentions: list[MentionVerdict] = Field(default_factory=list)   # for each LoRA written with @ ("the chosen LoRA [1]"): wanted or not
    use_loras: list[str] = Field(default_factory=list)          # other LoRAs the user wants on the images, as written
    skip_loras: list[str] = Field(default_factory=list)         # LoRAs the user does not want
    skip_kinds: list[LoraKind] = Field(default_factory=list)    # kinds of LoRA the user does not want at all ("no style loras")
    per_lora_kind: Literal["none", "character", "style", "concept", "clothing", "pose", "other"] = "none"   # "N images per <kind> LoRA"
    per_lora_words: str | None = None       # the words of the request that ask for it, copied; a group the request does not say is dropped (intent.py)
    images_per_lora: int | None = Field(default=None, ge=1)    # with a group: how many images for each LoRA of it; the job's count is read only when this is missing
    per_lora_tags: list[str] = Field(default_factory=list)      # the user's own LoRA tags that narrow that group
    per_lora_limit: int = Field(default=0, ge=0)                # only the first N members of the group; 0 = all
    extra_loras: Literal["default", "none", "as_fit"] = "default"   # other LoRAs: the usual rule / none / the planner may add what fits
    extra_kinds: list[LoraKind] = Field(default_factory=list)   # the kinds of LoRA the planner may add (empty: any)
    vary: list[Literal["camera", "gaze", "pose", "outfit", "setting", "lighting", "expression", "body", "mood"]] = Field(default_factory=list)   # aspects to change between images


@llm_task("plan_request")
class PlanReply(BaseModel):
    jobs: list[PlannedJob] = Field(min_length=1)
    intent: RequestIntent = Field(default_factory=RequestIntent)


class LoraPick(BaseModel):
    lora: str
    weight: float
    reason: str = ""


class LoraLeftOut(BaseModel):
    lora: str
    reason: str = ""


@llm_task("pick_loras")
class LoraPickReply(BaseModel):
    loras: list[LoraPick] = Field(default_factory=list)
    reason: str = ""
    left_out: list[LoraLeftOut] = Field(default_factory=list)      # candidates the picker looked at and did not use, with why (when it may add LoRAs freely)


class SceneReply(BaseModel):
    """The scene plan (plan_scene): the concrete scene before the prompt is written."""
    subjects: str = ""
    body: str = ""
    outfit: str = ""
    pose: str = ""
    action: str = ""
    expression: str = ""
    camera: str = ""
    gaze: str = ""
    setting: str = ""
    lighting: str = ""
    mood: str = ""
    detail_level: Literal["normal", "high", "very_high"] = "normal"
    rating: Literal["general", "sensitive", "nsfw", "explicit"] = "general"
    must_keep: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)        # what the user wants absent from the picture (it goes to the negative prompt)


@llm_task("plan_scene")
class ScenePlanReply(SceneReply):
    pass


class PromptItem(BaseModel):
    prompt: str = Field(min_length=1)
    variation: str = ""


@llm_task("write_prompts")
class PromptsReply(BaseModel):
    prompts: list[PromptItem] | None = None
    negative_additions: list[str] | None = None
    reason: str = ""


@llm_task("pick_settings")
class SettingsReply(BaseModel):
    steps: int | None = None
    cfg: float | None = None
    sampler: str | None = None
    scheduler: str | None = None
    size: str | None = None
    seed_strategy: str | None = None
    hires: bool | None = None
    hires_steps: int | None = None
    hires_denoise: float | None = None
    reasons: dict[str, str] = Field(default_factory=dict)


class LoraWeight(BaseModel):
    lora: str
    weight: float


@llm_task("apply_tweak")
class TweakReply(BaseModel):
    prompts: list[str] | None = None
    negative: str | None = None
    lora_weights: list[LoraWeight] | None = None
    steps: int | None = None
    cfg: float | None = None
    sampler: str | None = None
    scheduler: str | None = None
    size: str | None = None
    hires: bool | None = None
    seed_strategy: str | None = None
    reason: str = ""


class WildcardPick(BaseModel):
    list_name: str
    reason: str = ""
    slot: str = ""          # the scene slot the list's entries fill; "" or "any": the list's role decides (offers.py)


@llm_task("pick_wildcards")
class WildcardPickReply(BaseModel):
    picks: list[WildcardPick] = Field(default_factory=list)


@llm_task("write_briefs")
class BriefsReply(BaseModel):
    briefs: list[str] = Field(min_length=1)
    subjects: list[Subjects] = Field(default_factory=list)        # one per brief, in the same order


@llm_task("repair_tags")
class RepairReply(BaseModel):
    prompts: list[str] = Field(min_length=1)
