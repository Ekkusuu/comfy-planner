"""The content rules: loading, normalising, matching, blocking, failing closed, and the real rules file.

The pipeline tests use the synthetic rules of tests/content_probes.py (conftest.py loads them through the app's own loader). The last
section reads the real app/planning/content_rules.toml and builds every input from the file itself, so no real word is written here.
"""
import json
import logging
import re
from pathlib import Path

import pytest

from app.planning import content_safety as cs
from app.planning.content_safety import ContentRulesError, activate, check_content, matches_term, require_ready, term_sample
from app.planning.appearance import known
from app.planning.offers import plain_entries
from app.planning.scene import scene_issue
from app.planning.taglib import GENERAL, TagLib
from app.wildcards.store import WildList
from tests.content_probes import CSE_MESSAGE, CONTENT_TERMS as A, MODIFIERS as MOD, NUMBERS as NUM, OTHER_A, OTHER_B, RULES_TEXT, SUBJECT_TERMS as B, \
    ZZ_MESSAGE, write_rules
from tests.conftest import TAGWF, import_fixture
from tests.test_planning_flow import app  # noqa: F401  (the planning app fixture)

REAL_RULES = Path(cs.__file__).with_name("content_rules.toml")        # the app's own file, whatever conftest points the loader at
GROUP_A_LINE = "words = ['probea1', 'probea2', 'probea3', 'probea4', 'probea5', 'probea6', 'probea7s?', 'probea8\\w*']"


# ---------- loading and the self-test ----------

def test_the_rules_load_through_the_app_loader_and_the_self_test(probe_rules):
    assert activate(probe_rules) and [r.code for r in require_ready()] == ["cse", "zz"]
    assert cs.codes() == ("cse", "old_cse", "zz")


def test_one_rule_added_to_the_data_blocks_everywhere_with_no_code_change():
    hit = check_content([f"{OTHER_A} in a {OTHER_B}"])
    assert hit["code"] == "zz" and hit["message"] == ZZ_MESSAGE and hit["severity"] == "blocking" and hit["field"] == "prompts"
    assert check_content([OTHER_A]) is None and check_content([OTHER_B, "a quiet street"]) is None
    assert check_content([f"({OTHER_A}:1.4), ({OTHER_B})"])["code"] == "zz"           # the same normalisation applies to every rule
    assert check_content([OTHER_A, "a street", OTHER_B])["code"] == "zz"                # words in different texts count together
    assert scene_issue(f"{OTHER_A} {OTHER_B}")[0]["code"] == "scene_zz" and scene_issue(OTHER_A) == []       # its groups are not avoided alone
    assert not matches_term(OTHER_A) and not matches_term(OTHER_B)


def test_the_first_rule_wins_and_each_rule_has_one_fixed_message():
    assert check_content([f"{A[0]} {B[0]} {OTHER_A} {OTHER_B}"])["message"] == CSE_MESSAGE
    assert check_content([f"{A[3]} {B[2]}"])["message"] == check_content([f"({A[4]}:1.2) {NUM[0]} years old"])["message"] == CSE_MESSAGE
    assert scene_issue(B[0])[0]["message"] == scene_issue(f"{A[0]} {B[1]}")[0]["message"] == CSE_MESSAGE


@pytest.mark.parametrize("text,fires", [
    (f"{A[0]} {B[0]}", True), (f"{A[0]}", False), (f"{B[0]}", False),
    (f"{A[6]}s {B[0]}", True), (f"{A[0]} {B[6]} blue", False), (f"{A[0]} {B[6]}", True),         # an optional letter; a look-ahead exception
    (f"{A[0]} {MOD[0]} girl", True), (f"{A[0]} {MOD[1]} boy", True), (f"{A[0]} {MOD[0]}", False),
    (f"{A[0]} {NUM[1]} years old", True), (f"{A[0]} age: {NUM[0]}", True), (f"{A[0]} 18 years old", False), (f"{A[0]} 30 yrs old", False),
    (f"{A[0].upper()} {B[0].title()}", True), (f"x{A[0]} {B[0]}", False), (f"{A[0]}x {B[0]}", False),
])
def test_words_match_whole_words_ignoring_case(text, fires):
    assert (check_content([text]) is not None) == fires


@pytest.mark.parametrize("text,avoided", [(B[0], True), (f"a {B[2]} here", True), (A[0], False), (f"{B[6]} blue", False), (B[6], True),
                                          (f"{MOD[0]} girl", True), (f"{NUM[2]} year old", True), ("25 year old", False), ("", False)])
def test_the_entry_helper_matches_the_avoided_group_alone(text, avoided):
    assert matches_term(text) is avoided


def test_the_strict_reading_blocks_the_avoided_group_alone():
    assert check_content([B[0]]) is None
    assert check_content([B[0]], strict=True)["code"] == "cse" and check_content([A[0]], strict=True) is None
    assert check_content(["a street", f"a {B[1]}"], strict=True, field="job")["field"] == "job"


def test_group_matching_is_per_reading_not_across_unrelated_texts_only():
    assert check_content([f"{A[0]}, ({B[0]}:1.3)"]) is not None and check_content([f"{A[0]} . {B[1]}"]) is not None


# ---------- an entry that can never match is caught by the self-test ----------

def test_the_self_test_catches_an_entry_that_can_never_match(tmp_path):
    text = RULES_TEXT.replace("words = ['probec1', 'probec2']", "words = ['(?<=zz)yy']")
    assert not activate(write_rules(tmp_path, text))
    with pytest.raises(ContentRulesError):
        check_content(["a street"])


# ---------- failing closed ----------

BAD_FILES = {
    "empty": "",
    "no_rules": "title = 'x'\n",
    "not_toml": "[[rule\n",
    "missing_required_rule": RULES_TEXT.replace("code = 'cse'", "code = 'other'"),
    "empty_group": RULES_TEXT.replace("words = ['probec1', 'probec2']", "words = []"),
    "empty_group_of_required_rule": RULES_TEXT.replace(GROUP_A_LINE, "words = []"),
    "required_rule_with_one_group": RULES_TEXT.replace(f"[[rule.all_of]]\nname = 'a'\n{GROUP_A_LINE}\n", ""),
    "required_rule_without_an_avoided_group": RULES_TEXT.replace("avoid_alone = true\n", ""),
    "unknown_check": RULES_TEXT.replace("check = 'age_below_18'", "check = 'nope'"),
    "unknown_key": RULES_TEXT.replace("name = 'c'", "name = 'c'\nword = ['typo']"),
    "bad_pattern": RULES_TEXT.replace("words = ['probed1', 'probed2']", "words = ['probe(']"),
    "no_message": RULES_TEXT.replace("message = 'Blocked: test rule two.'\n", ""),
    "duplicate_code": RULES_TEXT.replace("code = 'zz'", "code = 'cse'"),
    "rule_without_groups": RULES_TEXT + "\n[[rule]]\ncode = 'yy'\nmessage = 'x'\nall_of = []\n",
    "blank_word": RULES_TEXT.replace("words = ['probed1', 'probed2']", "words = ['probed1', ' ']"),
}


@pytest.mark.parametrize("name", sorted(BAD_FILES))
def test_a_malformed_rules_file_leaves_no_rules_active(name, tmp_path):
    assert not activate(write_rules(tmp_path, BAD_FILES[name]))
    for call in (lambda: check_content(["a street"]), lambda: matches_term("a street"), require_ready, cs.codes):
        with pytest.raises(ContentRulesError, match="turned off|Planning and queueing"):
            call()


def test_a_missing_or_unreadable_rules_file_leaves_no_rules_active(tmp_path):
    assert not activate(tmp_path / "missing.toml")
    assert not activate(tmp_path)                                   # a folder cannot be read as a file
    (tmp_path / "bytes.toml").write_bytes(b"\xff\xfe\x00bad")
    assert not activate(tmp_path / "bytes.toml")
    with pytest.raises(ContentRulesError):
        require_ready()


def test_a_good_file_activates_again_after_a_bad_one(tmp_path, probe_rules):
    assert not activate(tmp_path / "missing.toml") and activate(probe_rules) and check_content([f"{A[0]} {B[0]}"])


def test_the_rules_and_logs_never_print_terms(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    activate(write_rules(tmp_path, BAD_FILES["unknown_key"]))
    activate(write_rules(tmp_path, RULES_TEXT.replace("words = ['probec1', 'probec2']", "words = ['(?<=zz)probec1']")))
    cs.log_block("cse", 7)
    text = caplog.text + str(pytest.raises(ContentRulesError, require_ready).value)
    assert "probe" not in text and "cse blocked job 7" in text


def test_nothing_in_settings_or_the_environment_switches_the_rules_off(monkeypatch, config):
    for name in ("CONTENT_RULES", "CONTENT_SAFETY", "SAFETY_OFF", "NO_FILTER"):
        monkeypatch.setenv(name, "0")
    assert check_content([f"{A[0]} {B[0]}"]) is not None
    assert not any(re.search(r"content|filter|rules", field, re.I) for field in vars(config))


def test_the_app_refuses_to_plan_or_queue_without_rules(app, tmp_path):
    ok = app.client.post("/api/jobs", json={"workflow_id": app.ids["tagwf"], "fields": {"prompts": ["a quiet street"]}}).json()
    assert ok["status"] == "ready"
    assert not activate(tmp_path / "missing.toml")
    for path, body in (("/api/jobs", {"workflow_id": app.ids["tagwf"], "fields": {"prompts": ["a quiet street"]}}),
                       ("/api/plan", {"request": "a quiet street", "workflow_id": app.ids["tagwf"]}),
                       (f"/api/jobs/{ok['id']}/queue", None), (f"/api/jobs/{ok['id']}/replan", None),
                       (f"/api/jobs/{ok['id']}/tweak", {"instruction": "darker"})):
        answer = app.client.post(path, json=body) if body is not None else app.client.post(path)
        assert answer.status_code == 503 and "Planning and queueing are turned off" in answer.json()["detail"], path
    assert app.job(ok["id"])["status"] == "ready"


# ---------- stored issues from earlier versions ----------

async def test_issues_with_an_earlier_code_still_block_and_are_recomputed(store, library):
    wf = await import_fixture(library, TAGWF)
    job = store.create_job(wf["id"], 1, {"prompts": ["a quiet street"]})
    old = {"field": "prompts", "severity": "blocking", "code": "old_cse", "message": "stored by an earlier version"}
    kept = {"field": "job", "severity": "blocking", "code": "scene_old_cse", "message": "stored by an earlier version"}
    store.db.execute("UPDATE jobs SET issues = ? WHERE id = ?", (json.dumps([old, kept]), job["id"]))
    with pytest.raises(Exception, match="open issue"):
        store.queue_job(job["id"])                                          # blocking by severity, whatever the code
    done = store.finalize(job["id"], wf, 1, store.get_job(job["id"])["fields"], store.get_job(job["id"])["issues"])
    assert [i["code"] for i in done["issues"]] == ["scene_old_cse"]            # the one finalize recomputes is dropped; the planner's stays
    assert done["status"] == "draft"


# ---------- the entry surfaces share the one helper ----------

def test_entry_surfaces_leave_out_what_the_helper_avoids():
    lst = WildList("x/y", "x", "setting", "setting", True, ["quiet night", f"{B[0]} star", f"{NUM[0]} year old", "warm lamp"])
    assert plain_entries(lst) == ["quiet night", "warm lamp"]
    lib = TagLib({"blue_hair": (GENERAL, 200_000), f"{B[0]}_hair": (GENERAL, 200_000)})
    assert known(lib, ["blue hair", f"{B[0]} hair"]) == ["blue hair"]


# ---------- the real rules file, read from the file itself ----------

def samples(group) -> list[str]:
    """One plain text per entry of a group, made from the file's own entries; each must be matched by its group."""
    out = [term_sample(entry) for entry in group.words]
    assert all(s and group.pattern.search(s) for s in out), "an entry of the real rules cannot match its own sample"
    return out


def test_the_real_file_loads_passes_its_self_test_and_defines_the_required_rule(real_rules):
    assert {r.code for r in real_rules} >= set(cs.REQUIRED_CODES)
    for rule in real_rules:
        assert rule.message and len(rule.groups) >= 2 and all(g.words for g in rule.groups)
    assert all(g.avoid_alone for g in next(r for r in real_rules if r.code == "cse").groups[-1:])
    assert cs.codes()[:1] == ("cse",)


def test_the_real_file_keeps_at_least_the_entries_it_shipped_with(real_rules):
    cse = next(r for r in real_rules if r.code == "cse")
    assert all(len(g.words) >= n for g, n in zip(cse.groups, (48, 39)))       # the entries it shipped with; more may be added, none dropped
    assert cse.groups[1].check is cs.CHECKS["age_below_18"]


def test_every_rule_of_the_real_file_blocks_every_entry_of_every_group(real_rules):
    for rule in real_rules:
        firsts = [samples(g)[0] for g in rule.groups]
        for n, group in enumerate(rule.groups):
            others = [s for i, s in enumerate(firsts) if i != n]
            for sample in samples(group):
                text = " ".join([sample, *others])
                assert check_content([text])["code"] == rule.code, rule.code
                assert check_content([sample, "a quiet street", *others])["code"] == rule.code      # across separate texts
                weighted = f"({sample}:1.3), " + ", ".join(f"({o}:1.1)" for o in others)
                assert check_content([weighted])["code"] == rule.code                              # a weight hides nothing
                assert check_content([f"\\({sample}\\), " + ", ".join(f"(({o}))" for o in others)])["code"] == rule.code


def test_every_rule_of_the_real_file_needs_all_of_its_groups(real_rules):
    for rule in real_rules:
        firsts = [samples(g)[0] for g in rule.groups]
        for n in range(len(firsts)):
            text = " ".join(s for i, s in enumerate(firsts) if i != n)
            hit = check_content([text])
            assert hit is None or hit["code"] != rule.code, f"{rule.code} fires without group {n}"


def test_every_avoided_group_of_the_real_file_is_found_alone_by_the_entry_helper_and_the_strict_reading(real_rules):
    for rule in real_rules:
        for group in (g for g in rule.groups if g.avoid_alone):
            for sample in samples(group):
                assert matches_term(sample) and matches_term(f"a {sample.upper()} here")
                assert check_content([sample], strict=True, field="job")["code"] == rule.code


def test_the_age_check_of_the_real_file_counts_ages_under_eighteen(real_rules):
    cse = next(r for r in real_rules if r.code == "cse")
    content = samples(cse.groups[0])[0]
    for age, blocked in ((5, True), (12, True), (17, True), (18, False), (25, False), (99, False)):
        assert (check_content([f"{content}, {age} years old"]) is not None) == blocked
        assert (check_content([f"{content}, ({age}:1.3) years old"]) is not None) == blocked
        assert matches_term(f"{age} years old") == blocked and matches_term(f"age: {age}") == blocked


def test_every_rule_of_the_real_file_has_a_plain_code_and_a_fixed_message(real_rules):
    for rule in real_rules:
        assert re.fullmatch(r"[a-z_]{2,16}", rule.code) and rule.message.startswith("Blocked:")
        assert not any(group.matches(rule.message) for group in rule.groups)       # the message itself holds no rule word
