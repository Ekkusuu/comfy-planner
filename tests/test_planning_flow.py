"""Planning end to end with a fake llama-server (HTTP-level, so real prompts, schemas and reply models are used)."""
import json
import time
from itertools import count

import httpx
import pytest
from fastapi.testclient import TestClient

from app.library.card_models import CardSettings, Hires, LoraCard, PixelBudget, PromptStyle, Range, Size, WorkflowCard
from app.main import create_app
from tests.content_probes import CSE_MESSAGE, CONTENT_TERMS as CONTENT, MODIFIERS as MOD, NUMBERS as NUM, SUBJECT_TERMS as SUBJ
from tests.conftest import FIXTURES, KREA, TAGWF, TAGWF_LORA, TAGWF_MISSING
from tests.test_cards import STYLE_LORA
from tests.test_gpu import FakeServer, Recorder

KREA_LORA = "krea2_inkbrush.safetensors"
REQUEST = "3 images of a girl in a yellow raincoat in a neon city at night with the style LoRA, and 2 of her in a cafe"
serial = count(1)


def generate(schema: dict, key: str = ""):
    """Some value that fits a JSON schema (enough for the fake server's default answers)."""
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {k: generate(v, k) for k, v in schema.get("properties", {}).items() if k in schema.get("required", [])}
    if kind == "array":
        return [generate(schema["items"], key) for _ in range(schema.get("minItems", 0))]
    if kind == "integer":
        return schema.get("minimum", 1)
    if kind == "number":
        return float(schema.get("minimum", 1))
    if kind == "boolean":
        return False
    n = next(serial)
    return f"alpha{n} beta{n} gamma{n} delta{n} epsilon{n}" if key == "prompt" else f"{key or 'text'} {n}"


class FakeLlama:
    """The llama-server chat API. `scripts[task]` is a reply dict, a function (schema, body) -> dict, or a list of
    those used one per call (the last one repeats). Without a script the reply is generated from the schema."""

    def __init__(self, rec):
        self.rec, self.requests, self.scripts = rec, [], {}

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content)
        task = body["response_format"]["json_schema"]["name"]
        schema = body["response_format"]["json_schema"]["schema"]
        self.requests.append((task, body))
        self.rec.calls.append(f"llm:{task}")
        script = self.scripts.get(task)
        if isinstance(script, list):
            script = script[min(sum(1 for t, _ in self.requests if t == task) - 1, len(script) - 1)]
        reply = script(schema, body) if callable(script) else (script if script is not None else generate(schema))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]})

    def of(self, task):
        return [b for t, b in self.requests if t == task]

    def schema_of(self, task, n=-1):
        return self.of(task)[n]["response_format"]["json_schema"]["schema"]


def anima_card(**kw) -> WorkflowCard:
    return WorkflowCard(family="Anima", purpose="Anime characters with tag prompts.", best_for="characters", use_full_notes=False,
                        prompt_style=PromptStyle(kind="tags", quality_prefix="masterpiece, best quality", rules=["tags only"]),
                        settings=CardSettings(cfg=Range(rec=5, min=4, max=6), steps=Range(rec=30, min=20, max=40), samplers=["er_sde", "euler"],
                                              schedulers=["simple"], sizes=[Size(w=896, h=1152, label="base"), Size(w=1024, h=1024, label="square"), Size(w=1152, h=896, label="landscape"), Size(w=1344, h=768, label="wide")],
                                              pixel_budget=PixelBudget(min=900_000, max=1_200_000),
                                              hires=Hires(steps=Range(rec=10, min=8, max=20), denoise=Range(rec=0.6, min=0.4, max=0.7))),
                        warnings=["Keep CFG under 6."], examples=["masterpiece, best quality, 1girl, solo, rain"], **kw)


def krea_card() -> WorkflowCard:
    return WorkflowCard(family="Krea-2", purpose="Photoreal images from sentences.", best_for="photos",
                        prompt_style=PromptStyle(kind="sentences", quality_prefix=""),
                        settings=CardSettings(cfg=Range(rec=1, min=1, max=1), steps=Range(rec=8, min=8, max=8), samplers=["euler"], schedulers=["simple"]),
                        warnings=["CFG must stay at 1."], examples=["A cinematic wide shot of a street at night."])


class App:
    """A running app with imported workflows, approved cards and a fake LLM."""

    def __init__(self, client, llama, rec, ids):
        self.client, self.llama, self.rec, self.ids = client, llama, rec, ids
        self.state = client.app.state

    def plan(self, request=REQUEST, on="anima", **kw):
        """Plan on the workflow `on` (a key of self.ids): the user always chooses it."""
        plan = self.client.post("/api/plan", json={"request": request, "workflow_id": self.ids[on], **kw}).json()
        return self.wait(plan["id"])

    def wait(self, plan_id, status=("done", "failed", "cancelled"), tries=400):
        for _ in range(tries):
            plan = self.client.get(f"/api/plans/{plan_id}").json()
            if plan["status"] in status:
                return plan
            time.sleep(0.02)
        raise AssertionError(f"plan never finished: {plan}")

    def job(self, job_id):
        return self.client.get(f"/api/jobs/{job_id}").json()

    def script_plan(self, *jobs):
        self.llama.scripts["plan_request"] = {"jobs": [{"constraints": [], "issues": [], "reason": "fits", "variety": "variations", **j} for j in jobs]}

    def two_jobs(self):
        """The planner splits the request into two jobs (plan them on the Anima workflow)."""
        self.script_plan({"description": "a girl in a yellow raincoat in a neon city at night", "count": 3,
                          "constraints": ["use the style LoRA", "neon city at night"]},
                         {"description": "the same girl in a cozy cafe", "count": 2, "constraints": ["warm light"]})


@pytest.fixture
def app(config, comfy, tmp_path, monkeypatch):
    config.safety_negative = []          # the app adds no content tags of its own; planning tests start from that
    config.strip_from_default_negative = ["nsfw", "explicit", "sensitive", "nude"]
    rec = Recorder()
    llama = FakeLlama(rec)
    flask = create_app(config, comfy=comfy, start_worker=False, llm_server=FakeServer(rec), llm_transport=httpx.MockTransport(llama))
    with TestClient(flask) as client:
        worker = client.app.state.worker
        hold, release = worker.hold, worker.release

        async def recording_hold():
            rec.calls.append("worker.hold")
            await hold()

        def recording_release():
            rec.calls.append("worker.release")
            release()
        worker.hold, worker.release = recording_hold, recording_release     # the lease uses the real queue worker
        ids = {}
        for key, name in (("tagwf", TAGWF), ("anima", TAGWF_LORA), ("krea", KREA), ("original", TAGWF_MISSING)):
            ids[key] = client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{name}.json")}).json()["id"]
        cards = client.app.state.cards
        for key in ("tagwf", "anima"):
            cards.save_built("workflow", ids[key], anima_card()); cards.approve("workflow", ids[key])
        cards.save_built("workflow", ids["krea"], krea_card()); cards.approve("workflow", ids["krea"])
        cards.save_built("lora", STYLE_LORA, LoraCard(file=STYLE_LORA, family="Anima", kind="style", what_it_does="Applies one of many artist styles.",
                                                      trigger_words=["@style_name"], style_tags=["@bluejay", "@lensfox", "@quietmoss"],
                                                      weight=Range(rec=1.0, min=0.5, max=1.2)))
        cards.save_built("lora", KREA_LORA, LoraCard(file=KREA_LORA, family="Krea-2", kind="style", what_it_does="Dark ink brush style.",
                                                     trigger_words=["monochrome ink wash style"], weight=Range(rec=0.8, min=0.5, max=1.2)))
        cards.approve("lora", STYLE_LORA); cards.approve("lora", KREA_LORA)
        yield App(client, llama, rec, ids)


def settings_reply(**over):
    def reply(schema, body):
        out = generate(schema)
        reasons = {k: f"because {k.removeprefix('why_')}" for k in out.get("reasons", {})}
        return {**out, **({"reasons": reasons} if "reasons" in out else {}), **over}
    return reply


# ---------- the example request ----------

def test_example_request_makes_two_jobs_on_the_chosen_workflow_inside_one_lease(app):
    app.two_jobs()
    app.llama.scripts["pick_loras"] = {"loras": [{"lora": STYLE_LORA, "weight": 1.0, "reason": "the style LoRA you asked for"}], "reason": "asked for it"}
    app.llama.scripts["pick_settings"] = settings_reply(steps=99, cfg=9.0, sampler="bogus", size="1024x1536", hires=True, hires_steps=15, hires_denoise=0.9)
    plan = app.plan()
    assert plan["status"] == "done" and plan["kind"] == "plan" and len(plan["job_ids"]) == 2 and plan["seconds"] >= 0
    anima, cafe = (app.job(i) for i in plan["job_ids"])

    assert (anima["status"], anima["count"], anima["workflow_id"]) == ("ready", 3, app.ids["anima"])
    assert (cafe["status"], cafe["count"], cafe["workflow_id"]) == ("ready", 2, app.ids["anima"])             # every job uses the workflow the user chose
    for job, n in ((anima, 3), (cafe, 2)):
        prompts = job["fields"]["prompts"]["value"]
        assert len(prompts) == n == len(set(prompts)) == len(job["images"]) == len(job["fields"]["prompts"]["items"])
        assert [i["recipe"]["prompt"] for i in job["images"]] == prompts
        assert job["issues"] == [] and job["meta"]["request"] == REQUEST
        assert job["meta"]["workflow"] == {"source": "locked", "reason": "You chose this workflow"}

    # Anima: prefix and a style tag are guaranteed; LoRA weight, steps, cfg, sampler, size and hires are clamped to the card
    assert all(p.startswith("masterpiece, best quality, ") and any(t in p for t in ("@bluejay", "@lensfox", "@quietmoss")) for p in anima["fields"]["prompts"]["value"])
    f = anima["fields"]
    assert f["loras"]["value"] == [{"name": STYLE_LORA, "weight": 1.0, "reason": "the style LoRA you asked for"}] and f["loras"]["source"] == "auto"
    assert (f["steps"]["value"], f["cfg"]["value"]) == (40, 6.0) and "clamped" in f["steps"]["note"] and "clamped" in f["cfg"]["note"]
    assert f["sampler"]["value"] in ("er_sde", "euler") and "not available" in f["sampler"]["note"]
    assert (f["width"]["value"], f["height"]["value"]) != (1024, 1536) and f["width"]["value"] % 64 == 0 and "not allowed" in f["width"]["note"]
    assert f["hires"]["value"] is True and (f["hires_steps"]["value"], f["hires_denoise"]["value"]) == (15, 0.7)
    assert f["steps"]["reason"] == "because steps" and f["scheduler"]["value"] == "simple" and f["scheduler"]["reason"].startswith("The card allows only")
    assert all(x["source"] == "auto" and x["reason"] for x in f.values())
    assert f["negative"]["value"].startswith("lowres") and f["seed_strategy"]["value"] in ("random", "fixed", "increment")

    # the whole run was one lease: the LLM started once, every call happened inside, and it stopped before the queue continued
    calls = app.rec.calls
    assert calls[0] == "worker.hold" and calls[1] == "server.start" and calls[-2:] == ["server.stop", "worker.release"]
    assert calls.count("server.start") == 1 and all(c.startswith("llm:") for c in calls[2:-2])
    assert [c for c in calls[2:-2] if c == "llm:plan_request"] == ["llm:plan_request"]
    assert calls[2] == "llm:plan_request" and calls.count("llm:write_prompts") == 2 and calls.count("llm:pick_settings") == 2


def test_a_card_that_pins_the_settings_is_obeyed_on_krea(app):
    app.llama.scripts["pick_loras"] = {"loras": [{"lora": KREA_LORA, "weight": 2.0, "reason": "ink look"}, {"lora": KREA_LORA, "weight": 0.7, "reason": "again"}], "reason": "ok"}
    app.llama.scripts["pick_settings"] = settings_reply(steps=99, cfg=9.0, sampler="bogus")
    app.script_plan({"description": "a cozy cabin interior with the darkbrush LoRA", "count": 4, "constraints": ["with the darkbrush LoRA"]})
    cabin = app.job(app.plan(on="krea")["job_ids"][0])
    assert (cabin["status"], cabin["count"], cabin["workflow_id"]) == ("ready", 4, app.ids["krea"])
    # the card pins steps, cfg, sampler and scheduler, so code sets them; the LoRA weight is clamped, duplicates dropped
    k = cabin["fields"]
    assert (k["steps"]["value"], k["cfg"]["value"], k["sampler"]["value"]) == (8, 1.0, "euler") and k["steps"]["reason"] == "The card allows only 8."
    assert k["loras"]["value"] == [{"name": KREA_LORA, "weight": 1.2, "reason": "ink look"}] and "clamped to 1.2" in k["loras"]["note"]
    assert all(p.startswith("monochrome ink wash style, ") for p in k["prompts"]["value"])                      # trigger word added
    assert "negative" not in k and "width" not in k                                                               # this workflow has neither slot
    assert cabin["images"][0]["recipe"]["loras"][0]["weight"] == 1.2
    assert set(app.llama.of("pick_settings")[0]["response_format"]["json_schema"]["schema"]["properties"]) == {"seed_strategy", "reasons"}


def test_the_planner_gets_only_the_chosen_workflow_and_cannot_pick_one(app):
    app.two_jobs()
    app.plan(on="krea")
    call = app.llama.of("plan_request")[0]
    schema = call["response_format"]["json_schema"]["schema"]["properties"]["jobs"]["items"]
    assert {"workflow_id", "workflow_name", "workflow_named_by_user"}.isdisjoint(schema["properties"]) and schema["properties"]["count"]["maximum"] == 200 and schema["properties"]["variety"]["enum"] == ["variations", "unique"]
    user = call["messages"][1]["content"]
    assert "Photoreal images from sentences." in user and "Anime characters with tag prompts." not in user            # only Krea's card
    for key in ("anima", "tagwf", "original"):
        assert app.ids[key] not in user and app.state.library.get(app.ids[key])["title"] not in user
    assert "Never choose, name or suggest a workflow" in call["messages"][0]["content"]
    jobs = app.client.get("/api/jobs").json()
    assert len(jobs) == 2 and {j["workflow_id"] for j in jobs} == {app.ids["krea"]}


def test_a_request_for_several_things_splits_into_jobs_that_all_use_the_one_workflow(app):
    app.script_plan({"description": "a girl in a yellow raincoat in a neon city", "count": 3, "reason": "the first scene"},
                    {"description": "the same girl in a cafe", "count": 2, "reason": "a second scene"},
                    {"description": "her portrait", "count": 4, "reason": "x"})
    plan = app.plan()
    jobs = [app.job(i) for i in plan["job_ids"]]
    assert [(j["count"], j["workflow_id"]) for j in jobs] == [(3, app.ids["anima"]), (2, app.ids["anima"]), (4, app.ids["anima"])]
    assert [j["meta"]["reason"] for j in jobs[:2]] == ["the first scene", "a second scene"] and len(app.llama.of("plan_request")) == 1


def test_prompt_writer_is_told_the_loras_trigger_words_and_gets_exactly_n(app):
    app.two_jobs()
    app.llama.scripts["pick_loras"] = {"loras": [{"lora": STYLE_LORA, "weight": 1.0, "reason": "r"}], "reason": "r"}
    app.plan()
    first = app.llama.of("write_prompts")[0]
    schema = first["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["prompts"]["minItems"] == schema["properties"]["prompts"]["maxItems"] == 3
    user = first["messages"][1]["content"]
    assert "<<<BEGIN DATA: workflow card>>>" in user and "@lensfox" in user and "masterpiece, best quality" in user and "3" in user
    picker = app.llama.of("pick_loras")[0]
    assert picker["response_format"]["json_schema"]["schema"]["properties"]["loras"]["items"]["properties"]["lora"]["enum"] == [STYLE_LORA]    # same family only


def test_settings_schema_has_runtime_enums_and_only_real_choices(app):
    app.two_jobs()
    app.plan()
    anima = next(b for b in app.llama.of("pick_settings"))["response_format"]["json_schema"]["schema"]["properties"]
    assert anima["sampler"]["enum"] == ["er_sde", "euler"] and "scheduler" not in anima                      # one scheduler allowed: code decides
    assert anima["size"]["enum"] == ["896x1152", "1024x1024", "1152x896", "1344x768"]
    assert {"steps", "cfg", "seed_strategy", "hires", "hires_steps", "hires_denoise"} <= set(anima)


# ---------- locks and re-planning ----------

def test_replan_keeps_locked_fields_and_replans_the_rest(app):
    app.two_jobs()
    plan = app.plan()
    anima_id = plan["job_ids"][0]
    before = app.job(anima_id)
    locked = app.client.post(f"/api/jobs/{anima_id}/locks", json={"fields": ["steps", "prompts"]}).json()
    assert {n for n, f in locked["fields"].items() if f["source"] == "locked"} == {"steps", "prompts"}
    assert app.client.post(f"/api/jobs/{anima_id}/locks", json={"fields": ["workflow"]}).status_code == 400        # the workflow is always yours: nothing to lock

    app.llama.scripts["pick_settings"] = settings_reply(cfg=4.0, steps=33, sampler="euler", size="896x1152")
    app.llama.scripts["pick_loras"] = {"loras": [], "reason": "not this time"}
    n_before = len(app.llama.requests)
    replan = app.client.post(f"/api/jobs/{anima_id}/replan").json()
    assert replan["kind"] == "replan" and replan["job_ids"] == [anima_id]
    assert app.wait(replan["id"])["status"] == "done"
    after = app.job(anima_id)
    assert after["fields"]["steps"]["value"] == before["fields"]["steps"]["value"] and after["fields"]["steps"]["source"] == "locked"      # never overwritten
    assert after["fields"]["prompts"]["value"] == before["fields"]["prompts"]["value"] and after["fields"]["prompts"]["source"] == "locked"
    assert after["fields"]["cfg"]["value"] == 4.0 and after["fields"]["cfg"]["source"] == "auto"                      # the rest was planned again
    assert after["fields"]["loras"]["value"] == [] and after["status"] == "ready" and len(after["images"]) == 3
    assert [i["recipe"]["prompt"] for i in after["images"]] == before["fields"]["prompts"]["value"]

    new = app.llama.requests[n_before:]
    assert [t for t, _ in new] == ["pick_loras", "write_prompts", "pick_settings"]
    only_negative = new[1][1]["response_format"]["json_schema"]["schema"]
    assert "prompts" not in only_negative["properties"] and "negative_additions" in only_negative["properties"]        # prompts are locked: only the negative is asked
    schema = new[2][1]["response_format"]["json_schema"]["schema"]
    assert "steps" not in schema["properties"] and "why_steps" not in schema["properties"]["reasons"]["properties"] and "cfg" in schema["properties"]
    told = new[2][1]["messages"][1]["content"]
    assert '"steps": ' + str(before["fields"]["steps"]["value"]) in told and "first_prompts" in told                    # locked values are given as context


def test_locking_everything_means_no_llm_call_at_all(app):
    app.two_jobs()
    job_id = app.plan()["job_ids"][1]
    names = [n for n in app.job(job_id)["fields"]]
    app.client.post(f"/api/jobs/{job_id}/locks", json={"fields": names})
    n = len(app.llama.requests)
    before = app.job(job_id)["fields"]
    assert app.wait(app.client.post(f"/api/jobs/{job_id}/replan").json()["id"])["status"] == "done"
    assert len(app.llama.requests) == n and app.job(job_id)["fields"] == before


def test_editing_a_field_locks_only_that_field_and_unlock_works(app):
    app.two_jobs()
    job_id = app.plan()["job_ids"][0]
    job = app.client.patch(f"/api/jobs/{job_id}", json={"cfg": 4.5}).json()
    assert job["fields"]["cfg"] == {**job["fields"]["cfg"], "value": 4.5, "source": "locked"}
    assert job["fields"]["steps"]["source"] == "auto" and job["fields"]["prompts"]["source"] == "auto" and len(job["images"]) == 3
    unlocked = app.client.post(f"/api/jobs/{job_id}/locks", json={"fields": ["cfg"], "locked": False}).json()
    assert unlocked["fields"]["cfg"]["source"] == "auto" and unlocked["fields"]["cfg"]["value"] == 4.5
    assert app.client.post(f"/api/jobs/{job_id}/locks", json={"fields": ["nope"]}).status_code == 400
    quiet = app.client.patch(f"/api/jobs/{job_id}", json={"steps": {"value": 25, "source": "auto"}}).json()
    assert quiet["fields"]["steps"]["source"] == "auto" and quiet["fields"]["steps"]["value"] == 25


def test_replan_all_defaults_to_every_unqueued_job_and_refuses_queued_ones(app):
    app.two_jobs()
    a, b = app.plan()["job_ids"]
    app.client.post(f"/api/jobs/{b}/queue")
    assert app.client.post(f"/api/jobs/{b}/replan").status_code == 400
    replan = app.client.post("/api/plan/replan").json()
    assert replan["job_ids"] == [a] and app.wait(replan["id"])["status"] == "done"
    assert app.client.post("/api/plan/replan", json={"job_ids": []}).status_code == 400


# ---------- near-duplicates ----------

def test_near_duplicate_prompts_are_retried_once_then_accepted(app):
    same = "a girl in a yellow raincoat standing in a neon city street at night with rain"
    dupes = {"prompts": [{"prompt": same, "variation": "v"}, {"prompt": same + ", wet", "variation": "v"}, {"prompt": same + ", smile", "variation": "v"}], "negative_additions": [], "reason": "r"}
    fixed = {"prompts": [{"prompt": "close up face portrait, glowing eyes, hood up", "variation": "close up"},
                         {"prompt": "full body from behind under a transparent umbrella", "variation": "behind"},
                         {"prompt": "sitting on steps eating ramen, steam, relaxed", "variation": "sitting"}], "negative_additions": ["blurry"], "reason": "r"}
    app.script_plan({"description": "a girl in a raincoat", "count": 3})
    app.llama.scripts["write_prompts"] = [dupes, fixed]
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert len(app.llama.of("write_prompts")) == 2 and "nearly identical" in app.llama.of("write_prompts")[1]["messages"][1]["content"]
    assert "close up face portrait" in job["fields"]["prompts"]["value"][0] and job["issues"] == []
    assert job["fields"]["negative"]["value"].endswith("blurry")

    app.llama.scripts["write_prompts"] = [dupes]                                    # the retry is just as bad: keep it, warn
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert len(app.llama.of("write_prompts")) == 4 and job["status"] == "ready"
    assert [i["code"] for i in job["issues"]] == ["similar_prompts"] and job["issues"][0]["severity"] == "warning" and job["issues"][0]["field"] == "prompts"


def test_wrong_number_of_prompts_blocks_the_job(app):
    app.script_plan({"description": "x", "count": 3})
    app.llama.scripts["write_prompts"] = {"prompts": [{"prompt": "only one", "variation": "v"}], "negative_additions": [], "reason": "r"}
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert job["status"] == "draft" and job["images"] == [] and any(i["severity"] == "blocking" and i["field"] == "prompts" for i in job["issues"])
    assert app.client.post(f"/api/jobs/{job['id']}/queue").status_code == 400


# ---------- the workflow is always the user's choice ----------

def post_plan(app, **body):
    return app.client.post("/api/plan", json={"request": "a cat", **body})


def test_plan_without_a_usable_workflow_is_refused_before_the_gpu_is_touched(app):
    held, n = app.rec.calls.count("worker.hold"), len(app.llama.requests)
    missing = post_plan(app)
    assert missing.status_code == 400 and missing.json()["detail"] == "Choose a workflow first."
    assert post_plan(app, workflow_id="  ").status_code == 400
    unknown = post_plan(app, workflow_id="no-such-workflow")
    assert unknown.status_code == 400 and "doesn't exist" in unknown.json()["detail"]
    broken = post_plan(app, workflow_id=app.ids["original"])                                  # a model is missing: not runnable
    assert broken.status_code == 400 and "can't run yet" in broken.json()["detail"]
    app.state.cards.db.execute("DELETE FROM cards WHERE kind = 'workflow' AND key = ?", (app.ids["krea"],))
    nocard = post_plan(app, workflow_id=app.ids["krea"])
    assert nocard.status_code == 400 and "no approved card" in nocard.json()["detail"] and "allow draft cards" in nocard.json()["detail"]
    app.state.cards.save_built("workflow", app.ids["krea"], krea_card())                      # a draft card is not enough...
    assert post_plan(app, workflow_id=app.ids["krea"]).status_code == 400
    app.state.config.allow_draft_cards = True                                                 # ...unless drafts are allowed
    ok = post_plan(app, workflow_id=app.ids["krea"])
    assert ok.status_code == 200 and app.wait(ok.json()["id"])["status"] == "done"
    assert app.rec.calls.count("worker.hold") == held + 1 and len(app.llama.requests) > n     # only the last one reached the GPU
    assert app.client.get("/api/plans").json()[-1]["kind"] == "plan" and len(app.client.get("/api/plans").json()) == 1


def test_an_edit_workflow_needs_its_image_and_a_text_one_ignores_images(app, monkeypatch):
    real = app.state.library.get
    monkeypatch.setattr(app.state.library, "get", lambda i: {**real(i), "kind": "edit", "slots": {**real(i)["slots"], "input_images": [{"label": "Photo"}]}} if i == app.ids["tagwf"] else real(i))
    refused = post_plan(app, workflow_id=app.ids["tagwf"])
    assert refused.status_code == 400 and "edits pictures you give it" in refused.json()["detail"]
    app.script_plan({"description": "make the coat red", "count": 1})
    done = app.wait(post_plan(app, workflow_id=app.ids["tagwf"], input_images=["cp_12345678_photo.png"]).json()["id"])
    assert done["status"] == "done"
    assert app.job(done["job_ids"][0])["fields"]["input_images"] == {**app.job(done["job_ids"][0])["fields"]["input_images"], "value": ["cp_12345678_photo.png"], "source": "locked"}
    assert "cp_12345678_photo.png" in app.llama.of("plan_request")[-1]["messages"][1]["content"]
    text_only = app.wait(post_plan(app, workflow_id=app.ids["anima"], input_images=["cp_12345678_photo.png"]).json()["id"])
    assert "input_images" not in app.job(text_only["job_ids"][0])["fields"] or not app.job(text_only["job_ids"][0])["fields"]["input_images"]["value"]


def test_changing_the_workflow_on_a_card_is_your_choice_and_a_replan_follows_it(app):
    app.script_plan({"description": "a cat", "count": 2})
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert job["meta"]["workflow"] == {"source": "locked", "reason": "You chose this workflow"}
    changed = app.client.patch(f"/api/jobs/{job['id']}", json={"workflow_id": app.ids["krea"]}).json()
    assert changed["workflow_id"] == app.ids["krea"] and changed["meta"]["workflow"] == {"source": "locked", "reason": "You chose this workflow"}
    assert app.wait(app.client.post(f"/api/jobs/{job['id']}/replan").json()["id"])["status"] == "done"
    done = app.job(job["id"])
    assert done["workflow_id"] == app.ids["krea"] and done["status"] == "ready" and len(done["fields"]["prompts"]["value"]) == 2
    assert app.llama.of("write_prompts")[-1]["messages"][1]["content"].count("Photoreal images from sentences.") >= 1     # planned around the new card


# ---------- the content rules ----------

def test_blocked_content_is_blocked_and_cannot_be_queued(app):
    app.script_plan({"description": f"an {CONTENT[3]} {CONTENT[0]} picture of a {SUBJ[2]}", "count": 1})
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert job["status"] == "draft" and job["images"] == [] and job["issues"][0]["code"] == "cse" and job["issues"][0]["message"] == CSE_MESSAGE
    refused = app.client.post(f"/api/jobs/{job['id']}/queue")
    assert refused.status_code == 400 and "open issue" in refused.json()["detail"]
    # a hand-made job gets the same check when it is queued
    manual = app.client.post("/api/jobs", json={"workflow_id": app.ids["tagwf"], "fields": {"prompts": [f"{CONTENT[5]}, {CONTENT[0]} {SUBJ[0]}"]}}).json()
    assert manual["status"] == "ready"
    assert app.client.post(f"/api/jobs/{manual['id']}/queue").status_code == 400


def test_normal_nude_prompts_for_adults_are_not_filtered(app):
    app.script_plan({"description": "a nude woman, studio portrait", "count": 1})
    app.llama.scripts["write_prompts"] = {"prompts": [{"prompt": "1girl, nude, adult woman, studio lighting", "variation": "v"}], "negative_additions": [], "reason": "r"}
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert job["status"] == "ready" and "nude" in job["fields"]["prompts"]["value"][0]
    assert "nude" not in job["fields"]["negative"]["value"]                                    # the app adds no content tags of its own


# ---------- failures ----------

def test_llm_failures_become_issues_not_crashes(app):
    app.script_plan({"description": "a cat", "count": 1})
    app.llama.scripts["pick_loras"] = {"loras": "not a list"}                              # invalid twice -> needs attention
    app.llama.scripts["pick_settings"] = {"steps": "many"}
    job = app.job(app.plan(on="tagwf")["job_ids"][0])
    assert job["status"] == "ready"                                                           # the card's recommended values fill in
    assert job["fields"]["loras"]["value"] == [] and job["fields"]["steps"]["value"] == 30 and job["fields"]["cfg"]["value"] == 5.0
    assert {i["code"] for i in job["issues"]} == {"llm_failed"} and all(i["severity"] == "warning" for i in job["issues"])


def test_request_the_planner_cannot_read_fails_the_plan_cleanly(app):
    app.llama.scripts["plan_request"] = {"jobs": []}
    plan = app.plan()
    assert plan["status"] == "failed" and "could not understand" in plan["error"] and app.client.get("/api/jobs").json() == []
    assert app.rec.calls[-2:] == ["server.stop", "worker.release"]
    assert app.client.post("/api/plan", json={"request": "   "}).status_code == 400


def test_llm_server_that_will_not_start_fails_the_plan_and_frees_the_queue(app):
    app.two_jobs()
    app.state.lease.server.fail_start = True
    plan = app.plan()
    assert plan["status"] == "failed" and "boom" in plan["error"]
    assert app.rec.calls[-1] == "worker.release" and not app.state.lease.active


def test_interrupted_planning_is_cleaned_up_on_restart(app):
    store = app.state.store
    wf = app.state.library.get(app.ids["tagwf"])
    row = store.create_planning_job("half done", 2, wf["id"], {}, 99)
    app.state.db.execute("INSERT INTO plans (id, kind, status, created_at) VALUES (99, 'plan', 'planning', 'now')")
    store.recover()
    job = store.get_job(row["id"])
    assert job["status"] == "draft" and "interrupted" in job["error"]
    assert app.client.get("/api/plans/99").json()["status"] == "failed"


# ---------- tweak ----------

def test_tweak_changes_only_the_asked_fields_and_never_locked_ones(app):
    app.script_plan({"description": "a girl in rain", "count": 2})
    job_id = app.plan(on="tagwf")["job_ids"][0]
    app.client.post(f"/api/jobs/{job_id}/locks", json={"fields": ["cfg"]})
    before = app.job(job_id)["fields"]
    app.llama.scripts["apply_tweak"] = {"steps": 200, "cfg": 5.5, "prompts": ["darker 1, moody", "darker 2, night"], "reason": "darker and quicker"}
    plan = app.client.post(f"/api/jobs/{job_id}/tweak", json={"instruction": "make it darker"}).json()
    assert plan["kind"] == "tweak" and app.wait(plan["id"])["status"] == "done"
    after = app.job(job_id)["fields"]
    schema = app.llama.schema_of("apply_tweak")
    assert "cfg" not in schema["properties"] and schema["properties"]["prompts"]["minItems"] == 2 and schema["properties"]["size"]["enum"][0] == "896x1152"
    assert "make it darker" in app.llama.of("apply_tweak")[0]["messages"][1]["content"]
    assert after["cfg"]["value"] == before["cfg"]["value"]                                    # locked: ignored even though the reply had it
    assert after["steps"]["value"] == 40 and after["steps"]["note"] == "asked 200, clamped to 40"
    assert after["prompts"]["value"] == ["masterpiece, best quality, darker 1, moody", "masterpiece, best quality, darker 2, night"]
    assert after["prompts"]["reason"] == "Tweak: darker and quicker"
    assert after["sampler"] == before["sampler"] and after["width"] == before["width"]
    assert [i["recipe"]["prompt"] for i in app.job(job_id)["images"]] == after["prompts"]["value"]
    assert app.client.post(f"/api/jobs/{job_id}/tweak", json={"instruction": " "}).status_code == 400



def test_a_lora_nobody_asked_for_is_dropped_unless_the_brief_points_at_it(app):
    krea_pick = {"loras": [{"lora": KREA_LORA, "weight": 0.8, "reason": "it is for this model"}], "reason": "same family"}
    app.llama.scripts["pick_loras"] = krea_pick
    app.script_plan({"description": "a cozy cabin interior", "count": 1})
    job = app.job(app.plan(on="krea")["job_ids"][0])
    assert job["fields"]["loras"]["value"] == [] and "nothing in the brief points at it" in job["fields"]["loras"]["note"]
    assert '"user_asked_for_a_lora": false' in app.llama.of("pick_loras")[0]["messages"][1]["content"]
    app.script_plan({"description": "a brush painting of a cabin", "count": 1})
    job = app.job(app.plan(on="krea")["job_ids"][0])
    assert [x["name"] for x in job["fields"]["loras"]["value"]] == [KREA_LORA]                 # "brush" is in the LoRA's card
    app.script_plan({"description": "a floral pattern", "count": 1})
    app.plan(on="krea")
    assert '"user_asked_for_a_lora": false' in app.llama.of("pick_loras")[-1]["messages"][1]["content"]     # "floral" is not "lora"


def test_a_loras_usage_notes_reach_the_picker_and_the_prompt_writer(app):
    notes = ["Put one @style tag right after the quality tags.", "Strength 1.0; 0.6 for a lighter touch.", "Mixing two styles works only loosely.",
             "Strong coloured lighting drowns the style."]
    row = app.state.cards.get("lora", STYLE_LORA)
    app.state.cards.save_edit("lora", STYLE_LORA, {**row["card"], "usage_notes": notes})
    app.llama.scripts["pick_loras"] = {"loras": [{"lora": STYLE_LORA, "weight": 1.0, "reason": "you asked for it"}], "reason": "asked"}
    app.script_plan({"description": "a girl in the rain", "count": 1, "constraints": ["use the style LoRA"]})
    plan = app.plan(on="tagwf")
    assert plan["status"] == "done" and app.job(plan["job_ids"][0])["fields"]["loras"]["value"][0]["name"] == STYLE_LORA
    pick = app.llama.of("pick_loras")[0]["messages"][1]["content"]
    write = app.llama.of("write_prompts")[0]["messages"][1]["content"]
    assert all(n in pick for n in notes[:3]) and notes[3] not in pick            # the picker sees a card's first three notes
    assert all(n in write for n in notes)                                       # the prompt writer sees all of them
