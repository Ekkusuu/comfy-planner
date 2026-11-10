"""The request separator `---`: what counts as one, and that every part is planned as a request of its own."""
import pytest

from app.planning.separator import parse_request, split_request
from tests.test_batches import distinct_briefs
from tests.test_planning_flow import app  # noqa: F401  (the planning app fixture)


@pytest.mark.parametrize("text,parts", [
    ("a cat --- a dog", ["a cat", "a dog"]),                                          # inline
    ("a cat\n---\na dog", ["a cat", "a dog"]),                                        # on its own line
    ("a cat\n-----------\na dog\n---\n\na bird", ["a cat", "a dog", "a bird"]),       # longer, several
    ("  ---  a cat ---  ", ["a cat"]),                                                # at the ends: no empty parts
    ("---", []),
    ("a cat", ["a cat"]),
    ("a well-known knight, a --- b", ["a well-known knight, a", "b"]),
    ("a well-known knight", ["a well-known knight"]),                                 # hyphenated words
    ("a---b and c --- d", ["a---b and c", "d"]),                                      # touching text: not a separator
    ("a ---b c--- d", ["a ---b c--- d"]),
    ("a -- b - c", ["a -- b - c"]),                                                   # fewer than three
    ("a {red|blue}--- b", ["a {red|blue}--- b"]),                                     # touches a wildcard token
    ("a ---{red|blue} b", ["a ---{red|blue} b"]),
    ("a __color/palette__--- b", ["a __color/palette__--- b"]),
    ("a {red --- blue|green} cat --- b", ["a {red --- blue|green} cat", "b"]),         # inside a token
    ("a {red|blue} --- __color/palette__ dog", ["a {red|blue}", "__color/palette__ dog"]),
    ("a cat\r\n---\r\na dog", ["a cat", "a dog"]),
    ("a cat — a dog", ["a cat — a dog"]),                                             # an em dash is not a separator
])
def test_split_request(text, parts):
    assert split_request(text) == parts


def part_of(body) -> str:
    return body["messages"][1]["content"].split("## Request\n")[1].split("\n\n## Limits")[0]


def test_every_part_is_planned_as_a_request_of_its_own(app):
    def planner(schema, body):
        part = part_of(body)
        return {"jobs": [{"description": part.upper(), "count": 2, "variety": "variations", "constraints": [], "issues": [], "reason": "fits"}]}
    app.llama.scripts["plan_request"] = planner
    request = "2 cats in a garden\n---\n2 dogs in the snow --- 2 birds"
    plan = app.plan(request, on="tagwf")
    parts = ["2 cats in a garden", "2 dogs in the snow", "2 birds"]
    assert [part_of(b) for b in app.llama.of("plan_request")] == parts                 # three planner calls, each sees one part only
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert plan["status"] == "done" and plan["request"] == request                      # the batch keeps the whole request
    assert [j["description"] for j in jobs] == [p.upper() for p in parts] and [j["meta"]["request"] for j in jobs] == parts
    assert {j["batch_id"] for j in jobs} == {plan["id"]} and {j["status"] for j in jobs} == {"ready"}
    assert "--- " not in app.llama.of("plan_request")[0]["messages"][1]["content"]


def test_a_part_may_ask_for_many_unique_images_itself(app):
    def planner(schema, body):
        part = part_of(body)
        many = "unique" in part
        return {"jobs": [{"description": part, "count": 6 if many else 1, "variety": "unique" if many else "variations", "constraints": [],
                          "issues": [], "reason": "fits"}]}
    app.llama.scripts["plan_request"] = planner
    app.llama.scripts["write_briefs"] = distinct_briefs
    plan = app.plan("a portrait of a queen --- 6 unique cyberpunk scenes --- a still life", on="tagwf")
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert len(jobs) == 1 + 6 + 1 and [j["count"] for j in jobs] == [1] * 8
    assert [j["meta"]["request"] for j in jobs] == ["a portrait of a queen"] + ["6 unique cyberpunk scenes"] * 6 + ["a still life"]
    assert len(app.llama.of("write_briefs")) == 1 and {j["batch_id"] for j in jobs} == {plan["id"]}


def test_a_part_that_fails_is_visible_and_the_other_parts_go_on(app):
    def planner(schema, body):
        if "broken" in part_of(body):
            return {"jobs": "nonsense"}
        return {"jobs": [{"description": part_of(body), "count": 2, "variety": "variations", "constraints": [], "issues": [], "reason": "fits"}]}
    app.llama.scripts["plan_request"] = planner
    plan = app.plan("two cats --- a broken part --- two dogs", on="tagwf")
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert plan["status"] == "done" and len(jobs) == 3
    good = [j for j in jobs if j["status"] == "ready"]
    bad = [j for j in jobs if j["status"] == "draft"]
    assert [j["description"] for j in good] == ["two cats", "two dogs"]
    assert len(bad) == 1 and bad[0]["description"] == "a broken part" and "could not understand" in bad[0]["error"] and bad[0]["images"] == []
    assert {j["batch_id"] for j in jobs} == {plan["id"]}


def test_when_every_part_fails_the_plan_fails_and_the_text_can_be_tried_again(app):
    app.llama.scripts["plan_request"] = {"jobs": "nonsense"}
    plan = app.plan("one --- two", on="tagwf")
    assert plan["status"] == "failed" and "could not understand" in plan["error"] and app.client.get("/api/jobs").json() == []


def test_parts_share_the_cap_and_what_does_not_fit_is_left_out_visibly(app):
    app.state.config.max_images_per_request = 5

    def planner(schema, body):
        return {"jobs": [{"description": part_of(body), "count": 3, "variety": "variations", "constraints": [], "issues": [], "reason": "fits"}]}
    app.llama.scripts["plan_request"] = planner
    plan = app.plan("part one --- part two --- part three --- part four", on="tagwf")
    jobs = [app.job(i) for i in plan["job_ids"]]
    ready = [j for j in jobs if j["status"] == "ready"]
    assert [j["count"] for j in ready] == [3, 2] and "Only 2 of the 3" in ready[1]["issues"][0]["message"]
    left = [j for j in jobs if j["status"] == "draft"]
    assert len(left) == 1 and "Left out" in left[0]["error"] and "2 parts" in left[0]["error"]
    assert len(app.llama.of("plan_request")) == 2                                      # no planner call for parts that can't fit


def test_a_request_of_only_separators_is_refused(app):
    assert app.client.post("/api/plan", json={"request": " --- \n---", "workflow_id": app.ids["tagwf"]}).status_code == 400


@pytest.mark.parametrize("text,shared,parts", [
    ("a cat --- a dog", "", ["a cat", "a dog"]),                                      # none
    ("watercolor === a cat --- a dog", "watercolor", ["a cat", "a dog"]),             # inline
    ("use lora x\n=====\na cat\n---\na dog", "use lora x", ["a cat", "a dog"]),      # on its own line, longer
    ("watercolor === a cat", "watercolor", ["a cat"]),                                # one part is fine
    ("a cat ===", "", ["a cat"]),                                                     # nothing to apply it to: it is the request
    ("=== a cat", "", ["a cat"]),
    ("===", "", []),
    ("a==b --- {x === y|z}", "", ["a==b", "{x === y|z}"]),                            # not separators
])
def test_parse_request(text, shared, parts):
    assert parse_request(text) == (shared, parts)


def test_the_general_instruction_reaches_every_job_of_every_part(app):
    def planner(schema, body):
        return {"jobs": [{"description": "SOME PICTURE", "count": 1, "variety": "variations", "constraints": [], "issues": [], "reason": "fits"}]}
    app.llama.scripts["plan_request"] = planner
    plan = app.plan("use the ink lora === a cat --- a dog", on="tagwf")
    assert len(app.llama.of("plan_request")) == 2
    for call in app.llama.of("plan_request"):
        assert "For every image of this request: use the ink lora" in call["messages"][1]["content"]
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert plan["status"] == "done" and len(jobs) == 2
    assert all(j["meta"]["constraints"] == ["use the ink lora"] for j in jobs)
    assert [j["meta"]["request"] for j in jobs] == ["a cat (for all images: use the ink lora)", "a dog (for all images: use the ink lora)"]
    for call in app.llama.of("pick_loras"):
        assert "use the ink lora" in call["messages"][1]["content"]


def test_a_wildcard_the_llm_made_up_is_taken_out_and_the_job_stays_runnable(app):
    invented = "__lewd_wildcards__"
    def planner(schema, body):
        return {"jobs": [{"description": f"Cute girls with complex details and {invented}", "count": 1, "variety": "variations", "constraints": [], "issues": [], "reason": "fits"}]}
    app.llama.scripts["plan_request"] = planner
    plan = app.plan("cute girls, a lot of tags from the lewd wildcards", on="tagwf")
    job = app.job(plan["job_ids"][0])
    assert invented not in job["description"] and job["description"] == "Cute girls with complex details"
    assert invented not in str(job["fields"]["prompts"]["value"])
    assert not [i for i in job["issues"] if i["severity"] == "blocking"] and job["status"] == "ready"


@pytest.mark.parametrize("text,typed,expected", [
    ("a, __made_up__, b", "", "a, b"),
    ("__made_up__, a", "", "a"),
    ("a and __made_up__", "", "a"),
    ("a, __mine__, b", "my __mine__ one", "a, __mine__, b"),       # typed by the user: theirs, also if it is wrong
    ("a, {x|y}, b", "", "a, {x|y}, b"),                            # a choice names no list
    ("plain, text", "", "plain, text"),
])
def test_drop_invented_refs(text, typed, expected):
    from app.wildcards.expand import drop_invented_refs
    assert drop_invented_refs(text, typed, None) == expected


@pytest.mark.parametrize("count,subjects,request_text,fixed", [
    (5, {"girls": 5}, "five girls at a beach", True),
    (3, {"people": 3}, "3 friends in a park", True),
    (5, {"girls": 5}, "5 images of five girls", False),            # it says images
    (4, {"girls": 1}, "4 of a girl", False),                       # the count isn't the people
    (3, {"girls": 3}, "3 portraits of three sisters", False),
    (1, {"girls": 1}, "a girl", False),
    (4, None, "four cabins", False),                               # no people: left to the LLM
])
def test_people_are_not_images(count, subjects, request_text, fixed):
    from app.planning.planner import people_not_images
    assert people_not_images(count, subjects, request_text) is fixed


def test_a_style_lora_needs_to_be_named():
    from types import SimpleNamespace as NS
    from app.planning.loras import names_this_lora as named
    card = NS(trigger_words=["ink7cm_Paint"], style_tags=["@tidewalker"])
    file = "ink7cm-paint-anima-base-epoch-05.safetensors"
    assert not named(file, card, [], "a mixed group of friends in a park, anime style")
    assert named(file, card, [], "use the ink7cm paint look")
    assert named(file, card, [], "a girl in @tidewalker style")
    assert named(file, card, ["inkwork"], "some inkwork of a castle")
