"""Locks on single prompts (build step 4): stored on the `prompts` field as `locks`, kept verbatim by a re-plan."""
from app.fields import prompt_locks, settle_prompts
from app.planning import locks
from tests.test_planning_flow import app  # noqa: F401  (the fixture: a running app with a fake LLM)

KEPT_REPLY = {"prompts": [{"prompt": "close up face portrait, glowing eyes, hood up", "variation": "close up"},
                          {"prompt": "full body from behind under a transparent umbrella", "variation": "behind"}],
              "negative_additions": [], "reason": "two new angles"}


def planned_anima(app):
    app.script_plan({"description": "a girl in a raincoat", "count": 3})
    return app.job(app.plan(on="tagwf")["job_ids"][0])


def user_message(body):
    return body["messages"][1]["content"]


# ---------- helpers ----------

def test_prompt_lock_helpers():
    whole = {"value": ["a", "b"], "source": "locked", "reason": ""}
    assert prompt_locks(whole) == [True, True] and prompt_locks({"value": ["a"], "source": "auto"}) == [False] and prompt_locks(None) == []
    part = settle_prompts({"value": ["a", "b", "c"], "source": "auto", "locks": [True, False], "items": [{"reason": "x"}]})
    assert part["locks"] == [True, False, False] and part["source"] == "auto" and len(part["items"]) == 3     # padded to the prompts
    assert settle_prompts({"value": ["a", "b"], "source": "auto", "locks": [True, True]})["source"] == "locked"
    assert settle_prompts({"value": ["a", "b"], "source": "locked"})["locks"] == [True, True]
    assert settle_prompts(part, [False, False, False])["source"] == "auto"
    assert "items" not in settle_prompts({"value": ["a"], "source": "auto"})


def test_kept_prompts_are_only_the_ones_locked_one_by_one():
    fields = {"prompts": {"value": ["a", "b", " ", "d"], "source": "auto", "locks": [False, True, True, True], "items": [{"reason": "r0"}, {"reason": "r1"}]}}
    assert locks.kept_prompts(fields, 4) == {1: {"text": "b", "reason": "r1"}, 3: {"text": "d", "reason": "Locked by you"}}      # empty ones are not kept
    assert locks.kept_prompts(fields, 2) == {1: {"text": "b", "reason": "r1"}}                                                  # beyond the count: ignored
    assert locks.kept_prompts({"prompts": {"value": ["a"], "source": "locked"}}, 1) == {} and locks.kept_prompts({}, 3) == {}   # whole-field locks are handled by `locked`


# ---------- through the API ----------

def test_locking_single_prompts_keeps_the_field_partly_auto_and_all_of_them_locks_it(app):
    job = planned_anima(app)
    one = app.client.post(f"/api/jobs/{job['id']}/locks", json={"prompts": [1]}).json()["fields"]["prompts"]
    assert one["locks"] == [False, True, False] and one["source"] == "auto" and one["value"] == job["fields"]["prompts"]["value"]
    assert len(one["items"]) == 3 and one["items"][0]["reason"] == job["fields"]["prompts"]["items"][0]["reason"]       # reasons survive
    all3 = app.client.post(f"/api/jobs/{job['id']}/locks", json={"prompts": [0, 2]}).json()["fields"]["prompts"]
    assert all3["locks"] == [True, True, True] and all3["source"] == "locked"                  # every prompt locked = the field is locked
    back = app.client.post(f"/api/jobs/{job['id']}/locks", json={"prompts": [0], "locked": False}).json()["fields"]["prompts"]
    assert back["locks"] == [False, True, True] and back["source"] == "auto"
    whole = app.client.post(f"/api/jobs/{job['id']}/locks", json={"fields": ["prompts"], "locked": False}).json()["fields"]["prompts"]
    assert whole["locks"] == [False] * 3 and whole["source"] == "auto"
    assert app.client.post(f"/api/jobs/{job['id']}/locks", json={"prompts": [3]}).status_code == 400
    assert app.client.post(f"/api/jobs/{job['id']}/locks", json={"prompts": [-1]}).status_code == 400


def test_replan_keeps_locked_prompts_verbatim_and_writes_only_the_others(app):
    job = planned_anima(app)
    mine = job["fields"]["prompts"]["value"][1]
    app.client.post(f"/api/jobs/{job['id']}/locks", json={"prompts": [1]})
    app.llama.scripts["write_prompts"] = KEPT_REPLY
    n = len(app.llama.requests)
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/replan").json()["id"])["status"] == "done"
    after = app.job(job["id"])
    prompts = after["fields"]["prompts"]
    assert prompts["value"][1] == mine and prompts["locks"] == [False, True, False] and prompts["source"] == "auto"
    assert "close up face portrait" in prompts["value"][0] and "full body from behind" in prompts["value"][2]
    assert prompts["value"][0].startswith("masterpiece, best quality, ")                       # the new ones still get the card's prefix
    assert prompts["items"][0]["reason"] == "close up" and prompts["items"][1]["reason"] == job["fields"]["prompts"]["items"][1]["reason"]
    assert [i["recipe"]["prompt"] for i in after["images"]] == prompts["value"] and after["status"] == "ready"
    write = [b for t, b in app.llama.requests[n:] if t == "write_prompts"]
    assert len(write) == 1
    schema = write[0]["response_format"]["json_schema"]["schema"]["properties"]["prompts"]
    assert schema["minItems"] == schema["maxItems"] == 2                                       # the locked one counts towards N
    told = user_message(write[0])
    assert mine in told and "Prompts already written" in told                                  # the model sees it, so it can write something different


def test_a_new_prompt_too_close_to_a_locked_one_is_retried_and_two_locked_ones_are_left_alone(app):
    job = planned_anima(app)
    first, second = job["fields"]["prompts"]["value"][:2]
    app.client.post(f"/api/jobs/{job['id']}/locks", json={"prompts": [0, 1]})
    same = {"prompts": [{"prompt": first, "variation": "same"}], "negative_additions": [], "reason": "r"}      # a copy of locked prompt 1
    good = {"prompts": [{"prompt": "sitting on steps eating ramen, steam, relaxed", "variation": "sitting"}], "negative_additions": [], "reason": "r"}
    replies = iter([same, good])
    app.llama.scripts["write_prompts"] = lambda schema, body: next(replies)
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/replan").json()["id"])["status"] == "done"
    sent = app.llama.of("write_prompts")[-2:]
    assert "nearly identical" in user_message(sent[1]) and "locked prompts" in user_message(sent[1])
    prompts = app.job(job["id"])["fields"]["prompts"]["value"]
    assert prompts[:2] == [first, second] and "ramen" in prompts[2]

    # two locked prompts being alike is not a reason to retry or to warn: only new prompts are judged
    app.client.post(f"/api/jobs/{job['id']}/locks", json={"prompts": [0, 1, 2], "locked": False})
    app.client.patch(f"/api/jobs/{job['id']}", json={"prompts": {"value": [first, first + ", wet", "x"], "source": "auto", "locks": [True, True, False]}})
    app.llama.scripts["write_prompts"] = good
    before = len(app.llama.of("write_prompts"))
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/replan").json()["id"])["status"] == "done"
    assert len(app.llama.of("write_prompts")) == before + 1 and app.job(job["id"])["issues"] == []


def test_all_prompts_locked_one_by_one_means_prompts_are_not_asked_for_at_all(app):
    job = planned_anima(app)
    app.client.post(f"/api/jobs/{job['id']}/locks", json={"prompts": [0, 1, 2]})
    n = len(app.llama.requests)
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/replan").json()["id"])["status"] == "done"
    write = [b for t, b in app.llama.requests[n:] if t == "write_prompts"]
    assert "prompts" not in write[0]["response_format"]["json_schema"]["schema"]["properties"]
    assert app.job(job["id"])["fields"]["prompts"]["value"] == job["fields"]["prompts"]["value"]


def test_failed_prompt_writing_does_not_lose_the_locked_prompts(app):
    job = planned_anima(app)
    mine = job["fields"]["prompts"]["value"][2]
    app.client.post(f"/api/jobs/{job['id']}/locks", json={"prompts": [2]})
    app.llama.scripts["write_prompts"] = {"prompts": [{"prompt": "only one", "variation": "v"}], "negative_additions": [], "reason": "r"}
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/replan").json()["id"])["status"] == "done"
    after = app.job(job["id"])
    assert after["status"] == "draft" and after["fields"]["prompts"]["value"] == ["", "", mine] and after["fields"]["prompts"]["locks"] == [False, False, True]
    assert any(i["field"] == "prompts" and i["severity"] == "blocking" for i in after["issues"])


def test_editing_a_prompt_by_hand_notes_it_and_keeps_the_other_reasons(app):
    job = planned_anima(app)
    old = job["fields"]["prompts"]
    new_text = old["value"][:]
    new_text[0] = "masterpiece, best quality, 1girl, my own words"
    patched = app.client.patch(f"/api/jobs/{job['id']}", json={"prompts": {"value": new_text, "source": "auto", "locks": [True, False, False], "reason": old["reason"]}}).json()
    p = patched["fields"]["prompts"]
    assert p["value"] == new_text and p["locks"] == [True, False, False] and p["source"] == "auto"
    assert p["items"][0]["reason"] == "Edited by you" and p["items"][1:] == old["items"][1:]
    assert [i["recipe"]["prompt"] for i in patched["images"]] == new_text
    whole = app.client.patch(f"/api/jobs/{job['id']}", json={"prompts": ["a", "b", "c"]}).json()["fields"]["prompts"]     # a plain list = set by hand = all locked
    assert whole["source"] == "locked" and whole["locks"] == [True, True, True]


def test_an_older_ui_prompt_locks_field_is_read_as_per_prompt_locks(app):
    job = planned_anima(app)
    prompts = {k: v for k, v in job["fields"]["prompts"].items() if k != "locks"}
    legacy = {"value": [True, False, False], "source": "auto", "reason": ""}
    patched = app.client.patch(f"/api/jobs/{job['id']}", json={"prompts": prompts, "prompt_locks": legacy}).json()
    assert patched["fields"]["prompts"]["locks"] == [True, False, False] and "prompt_locks" not in patched["fields"]


def test_tweak_leaves_locked_prompts_as_they_are(app):
    job = planned_anima(app)
    mine = job["fields"]["prompts"]["value"][0]
    app.client.post(f"/api/jobs/{job['id']}/locks", json={"prompts": [0]})
    tweak = {"prompts": ["rewritten one", "rewritten two", "rewritten three"], "reason": "darker"}
    app.llama.scripts["apply_tweak"] = lambda schema, body: {k: v for k, v in tweak.items() if k in schema["properties"]}
    done = app.wait(app.client.post(f"/api/jobs/{job['id']}/tweak", json={"instruction": "darker"}).json()["id"])
    assert done["status"] == "done"
    p = app.job(job["id"])["fields"]["prompts"]
    assert p["value"][0] == mine and "rewritten two" in p["value"][1] and "rewritten three" in p["value"][2] and p["locks"] == [True, False, False]
    assert "prompt_numbers_to_keep_as_they_are" in user_message(app.llama.of("apply_tweak")[-1])
