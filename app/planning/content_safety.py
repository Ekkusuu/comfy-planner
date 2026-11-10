"""The content safety filter: a fixed set of rules, loaded once at startup, applied the same way on every surface.

The rules are data (content_rules.toml, next to this file); this module is the one mechanism that reads and applies them. A rule has a
code, a message and groups of words; a text is blocked when every group of a rule matches it. Callers use only this API:

    check_content(texts)   a blocking issue for the first rule that the texts trigger, or None
    matches_term(text)     does one entry (a tag, an offer, an option) match a group that is avoided on its own?

Weights and brackets hide nothing: every rule runs on the text as typed, with ComfyUI's weight syntax removed, and with the brackets
turned into spaces (`_readings`). The check is deliberately blunt - it looks at words - so it can block an innocent text that uses
words of every group; rewrite the text and it passes.

The protection is always on. There is no setting, option or environment variable for it, and the rules file is part of the app, not
of config/. If the file is missing, unreadable or malformed, or fails its self-test, `check_content` and `require_ready` raise
ContentRulesError and the app refuses to plan or queue: it never runs unfiltered. Neither the log nor an issue ever holds matched text.
"""
import logging
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.fields import make_issue

log = logging.getLogger("content_safety")

RULES_PATH = Path(__file__).with_name("content_rules.toml")
REQUIRED_CODES = ("cse",)       # rules the file must define; the app does not run without them

AGE = re.compile(r"\b(\d{1,2})[\s-]*(?:years?|yrs?|y)[\s-]*old\b|\bage[ds]?\s*:?\s*(\d{1,2})\b", re.I)
WEIGHT_NUMBER = re.compile(r":\s*-?\d*\.?\d+\s*(?=\\?\))")      # the ":1.3" of "(x:1.3)", also of an escaped "(x:1.3\)"
BRACKET = re.compile(r"\\?[()]")                                 # a bracket, escaped or not


class ContentRulesError(Exception):
    """The rules are not usable. The message is meant for the user and never holds a rule's words."""


def _readings(text: str) -> list[str]:
    """The ways a text can be read once ComfyUI has handled its weight syntax: as typed; with brackets, backslashes and weight numbers
    removed (`(17:1.3) years old` -> `17 years old`, `nu(de:1.2)` -> `nude`); and with the brackets turned into spaces (so `(word)` still
    stands apart from the word before it). The rules run on all of them, so a weight can hide nothing."""
    bare = WEIGHT_NUMBER.sub("", text)
    return [text, BRACKET.sub("", bare), BRACKET.sub(" ", bare)]


def _age_below_18(text: str) -> bool:
    """The text states an age under 18 ("15 years old", "age: 12"). Structural, so it works in any language that writes the number."""
    return any(int(a or b) < 18 for a, b in AGE.findall(text))


CHECKS: dict[str, Callable[[str], bool]] = {"age_below_18": _age_below_18}      # the named checks a rule's group can refer to


@dataclass(frozen=True)
class Group:
    name: str
    words: tuple[str, ...]
    pattern: re.Pattern
    check: Callable[[str], bool] | None = None
    avoid_alone: bool = False

    def matches(self, text: str) -> bool:
        return bool(self.pattern.search(text)) or (self.check is not None and self.check(text))


@dataclass(frozen=True)
class Rule:
    code: str
    message: str
    groups: tuple[Group, ...]
    old_codes: tuple[str, ...] = ()

    def fires(self, text: str) -> bool:
        return all(group.matches(text) for group in self.groups)


_rules: tuple[Rule, ...] | None = None
_alone: tuple[Group, ...] = ()      # the groups of the active rules that are avoided on their own
_failure = "The content rules were not loaded."


# ---------- loading ----------

def _text(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContentRulesError(f"The content rules file is malformed ({what}).")
    return value


def _parse_group(raw, where: str) -> Group:
    if not isinstance(raw, dict) or set(raw) - {"name", "words", "check", "avoid_alone"}:
        raise ContentRulesError(f"The content rules file is malformed ({where}).")
    words = raw.get("words")
    if not isinstance(words, list) or not words or not all(isinstance(w, str) and w.strip() for w in words):
        raise ContentRulesError(f"The content rules file has an empty or malformed word group ({where}).")
    check = raw.get("check")
    if check is not None and check not in CHECKS:
        raise ContentRulesError(f"The content rules file names an unknown check ({where}).")
    if not isinstance(raw.get("avoid_alone", False), bool):
        raise ContentRulesError(f"The content rules file is malformed ({where}).")
    try:
        pattern = re.compile(r"\b(" + "|".join(words) + r")\b", re.I)
    except re.error:
        raise ContentRulesError(f"The content rules file has an entry that cannot be read ({where}).") from None
    return Group(str(raw.get("name") or where), tuple(words), pattern, CHECKS[check] if check else None, bool(raw.get("avoid_alone", False)))


def _parse_rules(raw: dict) -> tuple[Rule, ...]:
    entries = raw.get("rule")
    if not isinstance(entries, list) or not entries or set(raw) - {"rule"}:
        raise ContentRulesError("The content rules file has no rules.")
    rules = []
    for n, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or set(entry) - {"code", "message", "all_of", "old_codes"}:
            raise ContentRulesError(f"The content rules file is malformed (rule {n}).")
        code = _text(entry.get("code"), f"rule {n} code")
        groups = entry.get("all_of")
        if not isinstance(groups, list) or not groups:
            raise ContentRulesError(f"The content rules file is malformed (rule '{code}' has no groups).")
        old = entry.get("old_codes", [])
        if not isinstance(old, list) or not all(isinstance(c, str) and c for c in old):
            raise ContentRulesError(f"The content rules file is malformed (rule '{code}' old codes).")
        rules.append(Rule(code, _text(entry.get("message"), f"rule '{code}' message"),
                          tuple(_parse_group(g, f"rule '{code}' group {i}") for i, g in enumerate(groups, 1)), tuple(old)))
    if len({r.code for r in rules}) != len(rules):
        raise ContentRulesError("The content rules file repeats a rule code.")
    for code in REQUIRED_CODES:
        rule = next((r for r in rules if r.code == code), None)
        if rule is None:
            raise ContentRulesError(f"The content rules file is missing the required rule '{code}'.")
        if len(rule.groups) < 2 or not any(g.avoid_alone for g in rule.groups):
            raise ContentRulesError(f"The required rule '{code}' is incomplete.")
    return tuple(rules)


def load_rules(path: Path) -> tuple[Rule, ...]:
    """Read and check a rules file. Raises ContentRulesError for anything missing, unreadable or malformed."""
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise ContentRulesError("The content rules file is missing or cannot be read.") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise ContentRulesError("The content rules file is not valid.") from None
    return _parse_rules(raw)


def _strip_lookarounds(entry: str) -> str:
    out, i = [], 0
    while i < len(entry):
        if entry.startswith(("(?=", "(?!", "(?<=", "(?<!"), i):
            depth = 0
            while i < len(entry):
                if entry[i] == "\\":
                    i += 2
                    continue
                depth += (entry[i] == "(") - (entry[i] == ")")
                i += 1
                if depth == 0:
                    break
            continue
        out.append(entry[i])
        i += 1
    return "".join(out)


def term_sample(entry: str) -> str:
    """A plain text that an entry of a group matches, made from the entry itself (`word\\w*` -> `word`, `nipples?` -> `nipple`): what the
    self-test and the rule tests use, so that no word is written anywhere but in the rules file."""
    text = _strip_lookarounds(entry)
    text = re.sub(r"\\[wd][*+?]?", "", text)
    text = text.replace("\\s", " ").replace("\\b", "")
    text = re.sub(r"(.)\?", "", text)
    text = re.sub(r"[*+]", "", text)
    text = re.sub(r"[\\()\[\]{}]", "", text)
    return text.split("|")[0].strip()


def group_sample(group: Group) -> str | None:
    """The first sample, made from the group's own entries, that the group really matches (None if no entry yields one)."""
    for entry in group.words:
        sample = term_sample(entry)
        if sample and group.pattern.search(sample):
            return sample
    return None


def _self_test(rules: tuple[Rule, ...]) -> None:
    """Every rule must fire on a probe made from its own data - one sample from each group - in every reading, and a group avoided on its
    own must be found alone. Words are neither printed nor logged: a failure names only the rule."""
    for rule in rules:
        samples = [group_sample(g) for g in rule.groups]
        if not all(samples):
            raise ContentRulesError(f"The content rules self-test failed (rule '{rule.code}': a group has no usable entry).")
        probe = " . ".join(samples)
        wrapped = f"({samples[0]}:1.2) " + " ".join(f"({s})" for s in samples[1:])
        if not all(any(rule.fires(view) for view in _readings(text)) for text in (probe, wrapped)):
            raise ContentRulesError(f"The content rules self-test failed (rule '{rule.code}' does not fire).")
        if any(g.avoid_alone and not g.matches(s) for g, s in zip(rule.groups, samples)):
            raise ContentRulesError(f"The content rules self-test failed (rule '{rule.code}' group).")


def activate(path: Path | None = None) -> bool:
    """Load the rules (by default the file next to this module), run the self-test, and use them from now on. On any failure no rules are
    active: every check then raises ContentRulesError. Called once at startup; returns whether the filter is active."""
    global _rules, _alone, _failure
    try:
        rules = load_rules(path or RULES_PATH)
        _self_test(rules)
    except ContentRulesError as e:
        _rules, _alone, _failure = None, (), str(e)
        log.error("content rules are not active: %s", e)
        return False
    _rules, _failure = rules, ""
    _alone = tuple(g for rule in rules for g in rule.groups if g.avoid_alone)
    log.info("content rules active: %s", ", ".join(r.code for r in rules))
    return True


def require_ready() -> tuple[Rule, ...]:
    """The active rules, or ContentRulesError: planning and queueing call this before they do anything."""
    if _rules is None:
        raise ContentRulesError(f"{_failure} Planning and queueing are turned off until the file app/planning/content_rules.toml is restored "
                                f"and the app is restarted.")
    return _rules


# ---------- checking ----------

def check_content(texts: list[str], *, strict: bool = False, field: str = "prompts") -> dict | None:
    """A blocking issue (with the rule's fixed message and code) if the texts trigger a rule, else None. Each text is checked on its own and
    all of them together (a description can hold one group's words while the prompt holds another's). `strict` is for text that must
    not mention a group at all, such as a scene plan: it also triggers when a group marked `avoid_alone` matches one of the texts alone."""
    rules = require_ready()
    pieces = [t for t in texts if t]
    views = _readings(" . ".join(pieces))
    for rule in rules:
        if any(rule.fires(view) for view in views) or (strict and any(g.avoid_alone and g.matches(p) for g in rule.groups for p in pieces)):
            return make_issue(field, rule.message, code=rule.code)
    return None


def matches_term(text: str) -> bool:
    """Does one entry (a tag, an offered option, an appearance word) match a group that is avoided on its own? Such entries are never used."""
    if _rules is None:
        require_ready()
    return any(group.matches(text) for group in _alone)


def codes() -> tuple[str, ...]:
    """The issue codes of the rules, and the codes that earlier versions stored for them: issues a job's finalize recomputes every time."""
    return tuple(c for rule in require_ready() for c in (rule.code, *rule.old_codes))


def log_block(code: str, job_id: int | None) -> None:
    """The one log line of a block: the rule's code and the job. Never text."""
    log.warning("content rule %s blocked job %s", code, job_id)
