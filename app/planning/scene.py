"""The scene plan: one LLM call before the prompts turn a job into visible, drawable things.

The plan is the answer to vague words ("lewd", "complex"): each slot says what is seen, `must_keep` locks the
user's own concrete words, `rating` sets the least content rating and `detail_level` the smallest number of
content tags the prompt must have. Code then enforces all of that in tagcheck.py; the plan itself is only
offered to the prompt writer.

Content safety: a plan that the content rules block stops the job, exactly like a prompt does. A plan at a high content level
always speaks of adults: code adds the word when the model forgot it.
"""
import logging
import random
import re

from app.fields import make_issue
from app.llm.client import LlmTaskError
from app.planning.concepts import RATINGS, load_concepts, rating_max, rating_rank
from app.planning.context import block
from app.planning.intent import grounded, tokens
from app.planning.look import look_for, look_note
from app.planning.content_safety import check_content
from app.planning.tagcheck import VAGUE_WORDS, phrases_of, restates
from app.wildcards.ideas import IdeaSet, idea_record

log = logging.getLogger("planning.scene")

SLOTS = ("subjects", "body", "outfit", "pose", "action", "expression", "camera", "gaze", "setting", "lighting", "mood")
TEXT_KEYS = (*SLOTS, "must_keep")
DETAIL_LEVELS = ("normal", "high", "very_high")
DETAIL_FLOOR = {"high": 25, "very_high": 35}
VERY_HIGH = re.compile(r"\b(complex|as complex as possible|lots? of detail|very detailed|highly detailed|intricate|elaborate|"
                       r"lots? of tags?|many tags|a lot of tags)\b", re.I)
META_WORDS = {"image", "images", "picture", "pictures", "photo", "photos", "prompt", "prompts", "make", "use", "using",
              "lora", "loras", "high", "quality", "best", "adult", "adults", "woman", "women", "man", "men", "girl",
              "girls", "boy", "boys", "person", "people", "sure", "way"}
KEEP_STOP = {"a", "an", "the", "of", "in", "on", "at", "with", "and", "or", "to", "for", "from", "by", "is", "are", "very",
             "really", "some", "any", "this", "that", "these", "those", "it", "its", "as", "into", "over", "under",
             "multiple", "possible", "lots", "lot", "beside", "near", "about", "atop", "upon", "her", "his", "their", "its", "my", "your", "our"}
# A tweak is about the scene when it names one of these: then the stored plan is made again instead of reused.
TWEAK_WORDS = {
    "outfit": "dress skirt shirt blouse top bra panties lingerie underwear stockings thighhighs pants shorts coat jacket robe cloak armour armor "
              "uniform kimono yukata swimsuit bikini gown vest suit tie gloves hat helmet boots shoes heels scarf choker collar apron "
              "red blue black white green pink purple yellow brown grey gray gold silver",
    "pose": "pose standing sitting kneel lying lean crouch squat walk run jump dance legs crossed bending stretching straddling riding",
    "action": "hold holding touch grab reach drink eat read write fight swing strike block cast kiss hug undress dress wait talk laugh cook pour sip point wave rest",
    "expression": "smile smirk smug grin blush tear lips eyes angry surprised shocked serious calm seductive teasing playful embarrassed shy determined frown",
    "camera": "close-up closeup portrait framing upper full body cowboy wide shot above below behind dutch angle low high pov view",
    "gaze": "gaze gazing look looking stare staring glance glancing eye contact viewer away",
    "setting": "room bedroom hotel bed couch sofa chair table window street alley forest castle gate battlefield cafe kitchen bathroom garden park beach field mountain temple ruins "
               "indoors outdoors background scenery city town night rain",
    "lighting": "light lighting sunlight moonlight backlight rim glow shadow neon candle fire lamp spotlight dark bright golden dramatic",
    "body": "hair eyes face freckles tattoo muscular slim curvy voluptuous hourglass bare nude naked topless breasts cleavage thighs hips waist abs",
    "detail": "detail detailed complex intricate elaborate",
}


def one_line(value) -> str:
    return " ".join(str(value or "").split())


def scene_text(scene: dict | None) -> str:
    """Everything the plan says, for safety checks and the keep check."""
    if not scene:
        return ""
    parts = [one_line(scene.get(key)) for key in TEXT_KEYS]
    if isinstance(scene.get("must_keep"), list):
        parts.extend(one_line(x) for x in scene["must_keep"])
    return " . ".join(p for p in parts if p)


def stored_scene(job: dict) -> dict | None:
    """The plan a job already has (a re-plan or a tweak reuses it), or None."""
    scene = (job.get("meta") or {}).get("scene")
    return scene if isinstance(scene, dict) and scene.get("rating") in RATINGS else None


def forced_detail(text: str, detail: str = "normal") -> str:
    """The smallest detail level the words ask for. "Complex" and "lots of detail" force very_high in code."""
    if VERY_HIGH.search(text or ""):
        return "very_high"
    return detail if detail in DETAIL_LEVELS else "normal"


def detail_floor(scene: dict | None) -> int:
    return DETAIL_FLOOR.get((scene or {}).get("detail_level", "normal"), 0)


def rating_of(scene: dict | None) -> str:
    rating = (scene or {}).get("rating")
    return rating if rating in RATINGS else "general"


def scene_words(scene: dict | None) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", scene_text(scene).lower()))


def tweak_touches_scene(instruction: str) -> bool:
    """Does the instruction name something a slot of the scene plan holds ("make her wear red", "darker")?
    "Surprise me" asks for new wildcard ideas, not for a facial expression."""
    text = re.sub(r"\bsurprise me\b|\bsomething new\b|\bnew look\b|\brandom\b", " ", (instruction or "").lower())
    words = re.findall(r"[a-z0-9']+", text)
    starts = {word[:4] for word in words if len(word) >= 4}
    return any((len(word) < 4 and word in words) or word[:4] in starts
               for group in TWEAK_WORDS.values() for word in group.split())


def clean_avoid(values: list[str], text: str) -> list[str]:
    """What the plan says the user wants absent: short, and really said in the words (the request, the job), so a plan cannot make up a ban."""
    said = set(tokens(text))
    out = []
    for value in values or []:
        phrase = one_line(value)
        if phrase and len(phrase) <= 40 and grounded(phrase, said) and phrase.lower() not in {x.lower() for x in out}:
            out.append(phrase)
    return out[:8]


def clean_must_keep(values: list[str], text: str, vague: set[str], avoid: list[str] | None = None) -> list[str]:
    """The user's concrete words the model copied. A long model phrase is cut into its concrete runs ("an adult
    woman in a kimono" -> "kimono"), each kept only when the request really says it, with the request's own casing and no
    vague, subject or meta word. The designer's example "rainy hotel room" stays one phrase."""
    out = []
    low = text.lower()
    bad = vague | META_WORDS | KEEP_STOP
    banned = {w for a in avoid or [] for w in tokens(a)}        # a wish to leave something out is not a word to keep ("without glasses")
    for value in values or []:
        phrase = one_line(value)
        if not phrase or banned & set(tokens(phrase)):
            continue
        at = low.find(phrase.lower())
        if at < 0:
            continue
        exact = text[at:at + len(phrase)]
        run: list[str] = []
        chunks: list[str] = []
        for word in exact.split():
            if word.lower() in bad or word.isdigit() or len(word) < 2:
                if run:
                    chunks.append(" ".join(run))
                    run = []
            else:
                run.append(word)
        if run:
            chunks.append(" ".join(run))
        for chunk in chunks:
            if len(chunk.split()) > 4:
                continue
            found = low.find(chunk.lower())
            if found < 0:
                continue
            copy = text[found:found + len(chunk)]
            if copy.lower() not in {x.lower() for x in out}:
                out.append(copy)
            if len(out) >= 12:
                return out
    return out


EMPTY_VALUES = {"none", "n/a", "no body", "no outfit", "no pose", "no action", "no expression", "no gaze", "no mood", "no humans"}


def clean_slots(scene: dict) -> dict:
    """No-human plans say so in subjects only: the body, outfit, pose, action, expression and mood slots are cleared,
    so no clothing or body tag (and no gaze) can be offered for a cabin. "none"/"n/a" in any slot becomes empty."""
    subjects = one_line(scene.get("subjects"))
    no_people = subjects.lower() in ("none", "n/a") or "no humans" in subjects.lower()
    for key in ("body", "outfit", "pose", "action", "expression", "gaze", "mood"):
        value = one_line(scene.get(key))
        if no_people or value.lower() in EMPTY_VALUES or value.lower().startswith("no "):
            scene[key] = ""
    if no_people and not subjects:
        scene["subjects"] = "no humans, scenery"
    return scene


def state_adults(scene: dict) -> dict:
    """A plan at the nsfw or explicit level always says that its people are adults; code adds the word when the model left it out."""
    if rating_rank(scene["rating"]) < rating_rank("nsfw"):
        return scene
    subjects = one_line(scene.get("subjects"))
    if subjects and "adult" not in subjects.lower() and "no humans" not in subjects.lower():
        scene["subjects"] = f"adult {subjects}"
    return scene


def scene_issue(text: str) -> list[dict]:
    """A blocking issue when the content rules block the plan, in the strict reading (the plan must not use a group's words at all).
    Its code is the rule's code with `scene_`: unlike the ones job.finalize recomputes, it stays on the job until the plan is made again."""
    hit = check_content([text], strict=True, field="job")
    return [{**hit, "code": f"scene_{hit['code']}"}] if hit else []


def options_record(options: IdeaSet | None, scene: dict | None) -> dict:
    """What the scene plan was offered and what it took, for the job's meta: {"offered": [{list, role, text}], "used": [text]}.
    `used` are the offered options that appear in the plan's own words. Empty when nothing was offered."""
    if not options or not options.options():
        return {}
    return idea_record(options.options(), scene_text(scene), options.lists_record())


def appearance_line(appearance: dict[str, list[str]] | None) -> str:
    """The drawn hair and eye options as the one `appearance` line of the options block (plain words, so a sentence card reads them too)."""
    options = [o for key in ("hair", "eyes") for o in (appearance or {}).get(key, [])]
    return "appearance (hair and eyes): " + " | ".join(options) if options else ""


def options_text(options: IdeaSet | None, appearance: dict[str, list[str]] | None) -> str:
    """The text of the scene plan's options block: the wildcard options, one line per list, then the appearance line."""
    lines = [*(options.options_text().splitlines() if options else []), appearance_line(appearance)]
    return "\n".join(line for line in lines if line)


def appearance_record(appearance: dict[str, list[str]] | None, scene: dict | None) -> dict:
    """What the plan was offered for the look and what it took, for the job's meta: {"offered": {"hair", "eyes"}, "used": [option]}.
    An option is used when the plan's body slot says it inside one phrase (`brown hair` in `long wavy brown hair`). Empty when none was offered."""
    if not appearance:
        return {}
    body = phrases_of(str((scene or {}).get("body") or ""))
    return {"offered": appearance, "used": [o for key in ("hair", "eyes") for o in appearance.get(key, []) if restates(o, body)]}


def used_appearance(record: dict, prompts: list[str]) -> list[str]:
    """The offered appearance options that a final prompt has, inside one tag or phrase of it."""
    phrases = [words for text in prompts for words in phrases_of(text)]
    return [o for key in ("hair", "eyes") for o in (record.get("offered") or {}).get(key, []) if restates(o, phrases)]


async def plan_scene(ctx, job: dict, min_rating: str | None = None, options: IdeaSet | None = None,
                     rng: random.Random | None = None, appearance: dict[str, list[str]] | None = None,
                     planned: list[dict] | None = None) -> tuple[dict | None, list[dict]]:
    """(scene, issues). A failed call gives (None, warning) and planning goes on as before; a plan that the content rules block
    gives (None, blocking issue). `job` is the job view the prompt writer sees (description, request, constraints, subjects).
    `options` are random wildcard options for what the user left open: several per list, shown as data, and the
    model may take any, mix them or ignore them all; the user's own words still win. `rng` makes the tags found for the request's words a
    random subset (tagstep.concept_lines); without it they are the best ones. `appearance` are hair and eye options drawn for a request that
    leaves the look open (appearance.py): they are one more line of the options block, for the body slot. `planned` are short digests of the
    scenes already planned for other pictures of the same request (the newest ones): the model is asked to make this one clearly different.
    `job["look"]` (look.Look) is set when a LoRA of the job sets the picture's look: the writer is told to plan no light, colour scheme or lens effect, and
    code takes any that came back out of the slots unless the user's own words contain them.
    `min_rating` is the content level the request asks for. When the job says `rating_read` that level was read from the whole request (planner.py, with
    its limits: "sexy, nothing explicit"), and it is the minimum as it stands; otherwise the request's words raise it (concepts.toml `ratings`), which
    can not tell "explicit" from "nothing explicit"."""
    request = one_line(job.get("request")) or one_line(job.get("description"))
    focus = " ".join([one_line(job.get("description")), *map(one_line, job.get("constraints") or [])]).strip()
    user_words = "" if job.get("batch") else request        # one image of a big batch: its own description is the picture
    text = " ".join([user_words, focus])
    book = load_concepts()
    look = job.get("look")
    least = (min_rating or "general") if job.get("rating_read") else rating_max(min_rating or "general", book.rating_min(text))
    lib = ctx.taglib() if look else None
    from app.planning.tagstep import concept_lines      # imported here: tagstep imports this module
    concepts = await concept_lines(ctx, text, least, rng, (lambda t: look.is_look(t, lib)) if look else None)
    inputs = {"job": {"description": job.get("description", ""), "constraints": job.get("constraints") or [],
                      **({"subjects": job["subjects"]} if job.get("subjects") else {})},
              "request": focus or request,
              "user_words": user_words or "(one image of a big batch: the job above is the whole picture)",
              "min_rating": least,
              "concepts": "\n".join(concepts) or "(none)",
              "look": look_note(look),
              "options": block("wildcard options", options_text(options, appearance)) if options_text(options, appearance) else "(none)",
              "planned": block("scenes already planned", planned) if planned else "(none yet)"}
    try:
        reply = await ctx.llm.call("plan_scene", inputs, None, None)
    except (LlmTaskError, ValueError) as e:      # a broken task setup or a reply that could not be read
        log.warning("plan_scene failed: %s", e)
        return None, [make_issue("job", f"The scene plan could not be made ({e}); planning went on without it.", "warning", "llm_failed")]
    if reply.get("needs_attention"):
        log.warning("plan_scene needs attention: %s", reply["error"])
        return None, [make_issue("job", f"The scene plan could not be made ({reply['error']}); planning went on without it.", "warning", "llm_failed")]
    scene = {key: one_line(reply.get(key)) for key in SLOTS}
    scene["detail_level"] = forced_detail(text, reply.get("detail_level", "normal"))
    scene["rating"] = rating_max(least, reply.get("rating", "general"))
    scene["avoid"] = clean_avoid(reply.get("avoid") or [], text)
    scene["must_keep"] = clean_must_keep(reply.get("must_keep") or [], text, book.vague_words() | VAGUE_WORDS, scene["avoid"])
    scene = clean_slots(scene)
    if look and (gone := look.clean_scene(scene, " ".join([one_line(job.get("request")), *map(one_line, job.get("constraints") or [])]))):
        log.info("a LoRA sets the look: light and colour taken out of the scene plan's %s", ", ".join(gone))
    scene = state_adults(scene)
    issues = scene_issue(scene_text(scene))
    if issues:
        return None, issues
    return scene, []


async def replan_scene_for_tweak(ctx, job: dict, instruction: str) -> tuple[dict | None, list[dict]]:
    """A scene plan for a tweak that changes the picture itself: the request plus the change."""
    stored = stored_scene(job) or {}
    names = [x["name"] for x in ((job.get("fields") or {}).get("loras") or {}).get("value") or []]
    view = {"description": job.get("description", ""), "constraints": job.get("meta", {}).get("constraints") or [],
            "request": " ".join(x for x in [job.get("meta", {}).get("request", ""), "Change: " + instruction] if x.strip()),
            "subjects": job.get("meta", {}).get("subjects"), "look": look_for(ctx, names)}
    return await plan_scene(ctx, view, stored.get("rating"))


def scene_summary(scene: dict | None) -> str:
    """A short line for the job card: who, what, where, and the rating."""
    if not scene:
        return ""
    bits = [one_line(scene.get(key)) for key in ("subjects", "outfit", "pose", "setting", "lighting")]
    bits = [b for b in bits if b]
    return " · ".join(bits[:4])


__all__ = ["SLOTS", "DETAIL_FLOOR", "appearance_record", "detail_floor", "forced_detail", "options_record", "used_appearance", "plan_scene", "replan_scene_for_tweak", "rating_of",
           "scene_issue", "scene_summary", "scene_text", "stored_scene", "tweak_touches_scene"]
