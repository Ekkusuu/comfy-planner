"""Request understanding: the LLM reads the request and says what it asks for besides the pictures; this module checks
every claim against real data and turns it into something the pipeline can run.

The LLM's reply (`models.RequestIntent`) holds names *as the user wrote them*, kinds from a fixed list, a content level and a few
flags. Code never reads the request's English. It only:
  * resolves each written LoRA name against the cards (`resolve_name`: exact, word by word with prefixes, or a close spelling), and says
    so on the job when a name is unknown or fits several LoRAs - it never guesses;
  * lists the members of "N images per <kind> LoRA" from the cards (approved, or drafts when Settings allow), for the workflow's
    family, minus what the user ruled out, and says who was left out and why;
  * hands the rest (content level, extra-LoRA freedom, what must differ between images) on as plain data.
An `@name` the LLM did not classify counts as "use" (the old rule), so an empty reply behaves as before intent existed.
"""
import difflib
import re
from dataclasses import dataclass, field
from typing import get_args

from app.fields import make_issue
from app.library.card_models import LoraCard, LoraKind
from app.planning.concepts import load_concepts
from app.planning.mentions import readable, stem

MARKER = re.compile(r"^(?:the\s+)?(?:chosen\s+)?lora\s*[#\[(]?\s*(\d+)\s*[\])]?$", re.I)
KIND_WORDS = frozenset(w for k in get_args(LoraKind) for w in (k, k + "s"))      # "style", "styles": a kind of LoRA, not one LoRA
GENERIC = frozenset("lora loras the a an of my that this".split()) | KIND_WORDS     # words that never tell one LoRA from another
NOT_A_NAME = "it names no LoRA"
A_KIND = "it is a kind of LoRA, not one LoRA"           # the next two need no message: the LLM wrote something that was never a LoRA name
NOT_WRITTEN = "the request does not say it"
FUZZY_MIN = 0.82                # spelling similarity (difflib ratio) from which a misspelt name still counts as a match
TIE = 0.05                      # two best matches closer than this are ambiguous


def tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w]


def quoted_count(words: str | None) -> int:
    """The one number a quote states in digits ("3 images per style lora" -> 3), or 0 when it states none or several. A digit inside a
    name or a marker ("v6", "[1]") is not a count."""
    found = {int(n) for n in re.findall(r"(?<![\w\[])\d+(?![\w\]])", words or "")}
    return found.pop() if len(found) == 1 else 0


def token_hit(query: str, word: str) -> bool:
    """The query word is the name word, or one is the start of the other (four letters or more): "cedar" finds "cedarv2"."""
    return query == word or (min(len(query), len(word)) >= 4 and (word.startswith(query) or query.startswith(word)))


@dataclass
class Known:
    """One LoRA a written name can mean: the file and every name people may call it by."""
    file: str
    names: list[list[str]]      # each as a list of lower-case words


def known_loras(ctx) -> list[Known]:
    """Every LoRA the app knows, usable or not (so a named LoRA that cannot be used gets a reason, not "unknown")."""
    out: dict[str, Known] = {}
    for row in ctx.cards.of_kind("lora"):
        card = LoraCard.model_validate(row["card"])
        civitai = card.civitai.name if card.civitai and card.civitai.name else ""
        names = [stem(row["key"]), readable(row["key"]), civitai, *(w.lstrip("@") for w in card.trigger_words)]
        out[row["key"]] = Known(row["key"], [t for n in names if (t := tokens(n))])
    for file in ctx.lora_files:
        out.setdefault(file, Known(file, [t for t in [tokens(stem(file))] if t]))
    return list(out.values())


def grounded(quote: str, request: set[str]) -> bool:
    """Is the quote the LLM gives for a claim really in the request? Its words (not counting the little ones) are all found there."""
    words = [w for w in tokens(quote) if len(w) > 2]
    return bool(words) and all(any(token_hit(w, r) for r in request) for w in words)


def match_score(query: list[str], name: list[str]) -> float:
    """How well the written words fit one name of a LoRA, 0 to 1. All the query's words found among the name's words is a match
    (fewer extra words in the name is better); a close spelling of the whole name also counts."""
    if not query or not name:
        return 0.0
    if query == name:
        return 1.0
    hits = sum(1 for q in query if any(token_hit(q, w) for w in name))
    if hits == len(query):
        return max(0.6, 0.95 - 0.03 * (len(name) - len(query)))
    ratio = difflib.SequenceMatcher(None, " ".join(query), " ".join(name)).ratio()
    return ratio * 0.85 if ratio >= FUZZY_MIN else 0.0


def resolve_name(written: str, known: list[Known], mentions: list[str], request: set[str] | None = None) -> tuple[str | None, str]:
    """(the LoRA file, "") or (None, why not) for a name as the user wrote it. "LoRA 2" is the second `@` mention. With `request` (the
    words of the request) a name none of whose words the request says was made up by the LLM and is refused (NOT_WRITTEN)."""
    text = (written or "").strip().lstrip("@").strip()
    if m := MARKER.match(text):
        n = int(m.group(1))
        return (mentions[n - 1], "") if 1 <= n <= len(mentions) else (None, f"there is no mention number {n}")
    whole = tokens(stem(text) if text.lower().endswith((".safetensors", ".ckpt", ".pt")) else text)
    if request is not None and not any(token_hit(w, r) for w in whole for r in request):
        return None, NOT_WRITTEN
    exact = [k.file for k in known if any(whole == n for n in k.names)]
    if len(exact) == 1:         # a LoRA whose whole name is "style" is still found by it
        return exact[0], ""
    words = [w for w in whole if w not in GENERIC]
    if not words:
        return None, A_KIND if any(w in KIND_WORDS for w in whole) else NOT_A_NAME
    scored = sorted(((max(match_score(words, name) for name in k.names), k.file) for k in known if k.names), reverse=True)
    if not scored or scored[0][0] <= 0:
        return None, "no LoRA is called that"
    best = scored[0][0]
    close = [f for s, f in scored if s >= best - TIE and s > 0]
    if len(close) > 1:
        return None, "it fits several LoRAs (" + ", ".join(readable(f) for f in close[:4]) + ")"
    return close[0], ""


def user_tag_matches(wanted: str, tag_names: list[str]) -> list[str]:
    """The user's own tags (real ones, from the LoRA catalogue) that the written tag means: same words, or one is part of the other."""
    words = tokens(wanted)
    return [t for t in tag_names if words and (tokens(t) == words or all(any(token_hit(q, w) for w in tokens(t)) for q in words))]


@dataclass
class Member:
    file: str
    kind: str


@dataclass
class Resolved:
    """The checked meaning of the request's intent. `use`/`skip` are LoRA files; `members` the files of the "per LoRA" group."""
    rating: str = "general"
    use: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    skip_kinds: list[str] = field(default_factory=list)
    group_kind: str = ""
    per: int = 0                # images for each member of the group (0: the reply did not say)
    members: list[str] = field(default_factory=list)
    left_out: list[tuple[str, str]] = field(default_factory=list)       # (LoRA, why) of group members that could not be used
    extras: str = "default"
    extra_kinds: list[str] = field(default_factory=list)
    vary: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)

    def meta(self, member: str = "", n: int = 0, of: int = 0) -> dict:
        """What a job keeps of the intent, in its meta (json). `member` is the group LoRA this job belongs to."""
        out = {"rating": self.rating, "skip": self.skip, "skip_kinds": self.skip_kinds, "extras": self.extras, "extra_kinds": self.extra_kinds, "vary": self.vary}
        if self.group_kind and member:
            out["group"] = {"kind": self.group_kind, "member": member, "n": n, "of": of}
        return out

    def summary(self, got: dict[str, int], per: int) -> str:
        """One plain sentence about a group, for the plan's message, from what was really planned: `got` is the images each member got.
        When every LoRA got the `per` images asked for the sentence says so; otherwise it says how many each got. Then who was left out."""
        if not self.group_kind:
            return ""
        names = ", ".join(readable(m) for m in self.members) or "no LoRA"
        if all(got.get(m, 0) == per for m in self.members):
            text = f"{per} {'image' if per == 1 else 'images'} for each {self.group_kind} LoRA: {names}"
        else:
            text = f"Images for each {self.group_kind} LoRA (asked {per}): " + ", ".join(f"{readable(m)} {got.get(m, 0) or 'none'}" for m in self.members)
        if self.left_out:
            text += ". Left out: " + "; ".join(f"{readable(f)} ({why})" for f, why in self.left_out)
        return text


def resolve(ctx, reply: dict | None, mentions: list[str], family: str, can_take_loras: bool = True, request: str | None = None) -> Resolved:
    """Check the LLM's intent against the cards. `mentions` are the LoRA files the request names with `@` (in order); `request` is the
    text the LLM read (a name it wrote that the text does not say is refused). Nothing is guessed: whatever cannot be resolved becomes
    an issue on the job, except what was never a LoRA name at all (a number, a kind of LoRA, junk), which is dropped without a word."""
    reply = reply or {}
    out = Resolved(rating=reply.get("rating") if reply.get("rating") in ("general", "sensitive", "nsfw", "explicit") else "general")
    if request and not reply.get("rating_limit"):       # "very nsfw", "extremely lewd": one level more than the plain word (concepts.toml), unless the request sets a limit
        out.rating = load_concepts().intensified(request, out.rating)
    known = known_loras(ctx)
    said = None if request is None else set(tokens(request))

    def resolved(names: list[str], purpose: str) -> list[str]:
        files = []
        for written in names or []:
            file, why = resolve_name(written, known, mentions, said)
            if file is None and (MARKER.match(written.strip().lstrip("@").strip()) or why in (A_KIND, NOT_WRITTEN)):
                continue        # a number the user never wrote, a kind, junk: the LLM made it up; there is nothing to tell the user
            if file is None:
                out.issues.append(make_issue("loras", f"You {purpose} the LoRA “{written}”, but {why}. It was ignored.", "warning", "planner_note"))
            elif file not in files:
                files.append(file)
        return files

    out.use = resolved(reply.get("use_loras"), "asked for")
    typed = list(out.use)           # the LoRAs the user wrote the names of (an @ mention is a verdict of its own)
    out.skip = resolved(reply.get("skip_loras"), "ruled out")
    verdicts = {v["mention"]: bool(v["wanted"]) for v in reply.get("mentions") or [] if isinstance(v, dict) and 1 <= v.get("mention", 0) <= len(mentions)}
    for n, file in enumerate(mentions, 1):      # the verdict on an @ mention is the LLM's reading of the words around it and wins over a name in the lists
        if n in verdicts:
            wanted, unwanted = (out.use, out.skip) if verdicts[n] else (out.skip, out.use)
            if file not in wanted:
                wanted.append(file)
            if file in unwanted:
                unwanted.remove(file)
    for file in mentions:           # an @ mention the LLM did not classify is a request to use it (the rule before intent existed)
        if file not in out.use and file not in out.skip:
            out.use.append(file)
    clash = [f for f in out.use if f in out.skip]
    if clash:
        out.issues.append(make_issue("loras", f"You both asked for and ruled out {', '.join(readable(f) for f in clash)}; it was left out.", "warning", "planner_note"))
        out.use = [f for f in out.use if f not in out.skip]
    out.skip_kinds = list(dict.fromkeys(reply.get("skip_kinds") or []))
    out.extras = reply.get("extra_loras") if reply.get("extra_loras") in ("default", "none", "as_fit") else "default"
    out.extra_kinds = list(dict.fromkeys(reply.get("extra_kinds") or []))
    out.vary = list(dict.fromkeys(reply.get("vary") or []))
    kind = reply.get("per_lora_kind") or "none"
    if kind != "none" and said is not None and reply.get("per_lora_words") is not None and not grounded(reply["per_lora_words"], said):
        kind = "none"       # a group the request does not say in words (the LLM read it into "use the JADE lora"): not a group
    kinds = {row["key"]: LoraCard.model_validate(row["card"]).kind for row in ctx.cards.of_kind("lora")}
    if kind != "none" and any(kinds.get(f) == kind for f in typed if f in out.use):
        kind = "none"       # the user names LoRAs of that kind: they pick which ones, so it is not "an image for each of them"
    if kind != "none":
        out.group_kind = kind
        out.per = quoted_count(reply.get("per_lora_words")) or max(0, int(reply.get("images_per_lora") or 0))      # the number the request's own words state wins over the reading of it
        if not can_take_loras:
            out.issues.append(make_issue("loras", "This workflow can't take LoRAs, so there is nothing to make an image for each LoRA with.", "warning", "planner_note"))
        else:
            members(ctx, out, kind, family, reply.get("per_lora_tags") or [], int(reply.get("per_lora_limit") or 0))
    return out


def members(ctx, out: Resolved, kind: str, family: str, tags: list[str], limit: int):
    """Fill `out.members` (and `left_out`, `issues`) for "N images per <kind> LoRA": every card of that kind and family that the planner may use,
    minus the ones the user ruled out, narrowed to the user's own tags when named, cut to `limit`. Order: by file name."""
    usable = {k for k, _ in ctx.lora_choices()}
    user_tags = ctx.lora_tags()
    all_tag_names = sorted({t for v in user_tags.values() for t in v})
    wanted: set[str] = set()
    for written in tags:
        found = user_tag_matches(written, all_tag_names)
        if not found:
            out.issues.append(make_issue("loras", f"You asked for LoRAs tagged “{written}”, but none of your LoRA tags matches. That tag was ignored.", "warning", "planner_note"))
        wanted.update(found)
    if kind in out.skip_kinds:      # "images for each style LoRA" and "no style LoRAs besides": the group is the explicit ask; the rule keeps other style LoRAs off the images
        out.skip_kinds = [k for k in out.skip_kinds if k != kind]
        out.extra_kinds = [k for k in out.extra_kinds if k != kind]
    cards = [(row["key"], LoraCard.model_validate(row["card"])) for row in ctx.cards.of_kind("lora")]
    chosen, left = [], []
    for file, card in sorted(cards, key=lambda x: x[0].lower()):
        if card.kind != kind or card.family != family:
            continue
        if file in out.skip:
            left.append((file, "you ruled it out"))
        elif wanted and not wanted.intersection(user_tags.get(file, [])):
            continue
        elif file not in usable:
            left.append((file, "its card is not approved yet" if not ctx.lora_files or file in ctx.lora_files else "ComfyUI does not list the file"))
        else:
            chosen.append(file)
    if limit and len(chosen) > limit:
        left += [(f, f"only {limit} asked for") for f in chosen[limit:]]
        chosen = chosen[:limit]
    out.members, out.left_out = chosen, left
    if not chosen:
        why = f" ({'; '.join(f'{readable(f)}: {w}' for f, w in left)})" if left else ""
        out.issues.append(make_issue("loras", f"You asked for images with each {kind} LoRA, but there is no {kind} LoRA this workflow ({family}) can use{why}.", "blocking", "lora_group"))


def intent_text(meta: dict | None) -> list[str]:
    """The free text of a job's stored intent, for the content check (it must see every field the LLM wrote). Names and tags only."""
    intent = (meta or {}).get("intent") or {}
    asked = intent.get("asked") or {}
    out = [*(asked.get("use_loras") or []), *(asked.get("skip_loras") or []), *(asked.get("per_lora_tags") or [])]
    return [str(x) for x in out if x]
