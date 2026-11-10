"""Step: the LLM chooses which wildcard lists feed ideas to the prompt writer.

It sees a compact catalog of the user's lists (grouped by the part of the picture each serves, name, number of entries and a few sample
entries each) and the brief. Code then validates the reply (`ideas.choose`: only enabled lists, none that serve only slots the brief already fixes)
and fills any remaining places by phrase and keyword matches and open slots. If the LLM can't answer, those rules decide alone."""
import logging
from collections import Counter
from itertools import zip_longest

from app.config import Config
from app.planning.artists import wants_artists
from app.planning.context import PlanContext, block
from app.planning.offers import served, served_slots
from app.planning.semantic import related_lists
from app.wildcards.ideas import given_roles, phrase_matches
from app.wildcards.store import WildList

log = logging.getLogger("planning")
# (samples per list, characters per sample), tried in this order until the catalog fits `wildcards_catalog_chars`.
# If even the last one is too big, the roles with the most lists lose their samples first.
SAMPLE_LEVELS = [(4, 40), (3, 32), (2, 28)]
MAX_REASON_CHARS = 120
MAX_NEAR = 6        # lists marked "close in meaning" in the catalog: the best of each role, at most this many


def samples_of(l: WildList, count: int, width: int) -> list[str]:
    """`count` entries spread evenly over the list (the same every time), each cut to `width` characters. Entries that are only
    syntax (`__ref__`, `{a|b}`) are skipped; a list of nothing else gets none."""
    plain = [e for e in l.entries if "__" not in e and "{" not in e]
    if not plain or count < 1:
        return []
    n = min(count, len(plain))
    picked = dict.fromkeys(plain[i * len(plain) // n] for i in range(n))
    return [e if len(e) <= width else e[:width].rstrip() + "…" for e in picked]


def render_catalog(lists: list[WildList], count: int, width: int, bare_roles: set[str], marks: dict[str, str] | None = None,
                   near: set[str] | None = None, labels: dict[str, str] | None = None) -> str:
    """One heading per group, one line per list: `name (entries): sample | sample`. The group is the list's label (`labels`: the slots it
    serves) or its role. A list in `marks` ({name: phrase}) has an entry that the request contains, and says so; a list in `near` is close
    in meaning to the request (search by meaning)."""
    marks, near, labels = marks or {}, near or set(), labels or {}
    by_role: dict[str, list[WildList]] = {}
    for l in lists:
        by_role.setdefault(labels.get(l.name, l.role), []).append(l)
    out = []
    for role in sorted(by_role):
        out.append(f"[{role}]")
        for l in sorted(by_role[role], key=lambda x: x.name):
            samples = [] if role in bare_roles else samples_of(l, count, width)
            out.append(f"{l.name} ({len(l.entries)})" + (": " + " | ".join(samples) if samples else "") + (f"  ← has “{marks[l.name]}”" if l.name in marks else "  ← close in meaning to the request" if l.name in near else ""))
    return "\n".join(out)


def build_catalog(lists: list[WildList], config: Config, marks: dict[str, str] | None = None,
                  near: set[str] | None = None, labels: dict[str, str] | None = None) -> tuple[str, list[WildList]]:
    """(the catalog text, the lists in it) within `wildcards_catalog_chars`: shorter samples first, then two per list, then no
    samples for the roles with the most lists. A library so big that even the bare names don't fit loses lists, taking
    turns between roles so every role stays represented."""
    budget = config.wildcards_catalog_chars
    for count, width in SAMPLE_LEVELS:
        text = render_catalog(lists, count, width, set(), marks, near, labels)
        if len(text) <= budget:
            return text, lists
    count, width = SAMPLE_LEVELS[-1]
    group = lambda l: (labels or {}).get(l.name, l.role)
    sizes = Counter(group(l) for l in lists)
    bare: set[str] = set()
    for role in sorted(sizes, key=lambda r: -sizes[r]):
        bare.add(role)
        text = render_catalog(lists, count, width, bare, marks, near, labels)
        if len(text) <= budget:
            return text, lists
    by_role = {role: sorted((l for l in lists if group(l) == role), key=lambda l: l.name) for role in sizes}
    turns = [l for group in zip_longest(*by_role.values()) for l in group if l]       # one list of each role in turn
    for keep in range(len(turns), 0, -1):
        text = render_catalog(turns[:keep], count, width, bare, marks, near, labels)
        if len(text) <= budget:
            return text, turns[:keep]
    return "", []


def best_per_role(related: list[tuple[WildList, float]], limit: int) -> set[str]:
    """Names of the first (best) related list of each role, at most `limit` of them."""
    seen: dict[str, str] = {}
    for l, _ in related:
        seen.setdefault(l.role, l.name)
    return set(list(seen.values())[:limit])


def picking_lists(ctx: PlanContext) -> bool:
    """Is the LLM step wanted at all? Not when wildcards are off, none are imported, or no lists are offered per prompt."""
    c = ctx.config
    return bool(ctx.wildcards and c.wildcards_enabled and c.wildcards_llm_picks_lists and c.wildcards_ideas_per_prompt > 0
                and any(l.enabled and l.entries for l in ctx.wildcards.lists()))


def card_summary(card) -> dict:
    return {"family": card.family, "prompt_style": card.prompt_style.kind, "purpose": card.purpose[:200], "best_for": card.best_for[:120]}


async def pick_wildcards(ctx: PlanContext, card, brief: dict, given_text: str) -> tuple[list[tuple[str, str, str]], list]:
    """([(list name, reason, scene slot)], []) - the LLM's choice, best first (the slot is "" when it said "any"); [] when it can't answer or nothing is on offer. `brief` is what
    the LLM is shown ({request, job, constraints}); `given_text` is what decides which slot roles are already fixed.
    The second value is the (empty) issue list of a planning step."""
    c = ctx.config
    lists = [l for l in ctx.wildcards.lists() if l.enabled and l.entries]
    sv = await served(ctx, lists)                           # what each list serves, read from its entries
    given = given_roles(given_text, lists, c, served_slots(sv))
    given_slots = {c.wildcards_role_slots.get(r, "") for r in given} - {""}
    offered = [l for l in lists if not (sv[l.name].slots and set(sv[l.name].slots) <= given_slots)]     # a slot the brief fixes is dropped by code anyway
    if not offered:
        return [], []
    asked = " ".join(str(x) for v in brief.values() for x in (v if isinstance(v, list) else [v]))
    offered = [l for l in offered if l.role != "artist" or wants_artists(asked)]    # no artist list unless one was asked for
    marks = {l.name: phrase for l, phrase in phrase_matches(offered, asked)}      # lists with an entry the request contains
    # lists close in meaning: asked about the job itself, not the whole request (its "2 images of" and other jobs' details are noise)
    about = " ".join([str(brief.get("job") or ""), *map(str, brief.get("constraints") or []), str(brief.get("change_requested") or "")]).strip()
    near = best_per_role(await related_lists(ctx, offered, about or asked) or [], MAX_NEAR)
    labels = {l.name: " · ".join(sv[l.name].slots) for l in offered if sv[l.name].slots}       # "camera · gaze": a mixed list is shown as what it serves
    text, shown = build_catalog(offered, c, marks, near, labels)
    names = [l.name for l in shown]
    if not names:
        return [], []
    slots = [*c.wildcards_slot_about, "any"]
    inputs = {"brief": brief, "card": block("workflow card", card_summary(card)), "catalog": block("wildcard lists", text),
              "max_lists": c.wildcards_ideas_per_prompt, "slots": ", ".join(slots)}
    reply = await ctx.llm.call("pick_wildcards", inputs, {"list_name": names, "slot": slots}, {"picks": {"maxItems": c.wildcards_ideas_per_prompt}})
    if reply.get("needs_attention"):
        log.warning("pick_wildcards needs attention, choosing lists by phrase and keyword only: %s", reply["error"])
        return [], []
    picks, seen = [], set()
    for item in reply["picks"]:
        if item["list_name"] in names and item["list_name"] not in seen:
            seen.add(item["list_name"])
            picks.append((item["list_name"], item["reason"].strip()[:MAX_REASON_CHARS], item.get("slot") if item.get("slot") in c.wildcards_slot_about else ""))
    return picks, []
