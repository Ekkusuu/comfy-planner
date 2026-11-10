"""The tag workflow with a hires pass ("Tag T2I hires - reference", converted to `tests/fixtures/tag-t2i-hires.api.json`) takes any number of LoRAs, and the removed style-LoRA
workflow is gone for good while its history stays.

`tests/fixtures/tag-t2i-hires.api.json` is the app's converted copy of the kept workflow."""
import json

import pytest
from fastapi.testclient import TestClient

from app.comfy.graph import build_graph
from app.comfy.slots import detect_slots, is_link
from app.main import create_app
from tests.conftest import FIXTURES, TAGWF, TAGWF_LORA, import_fixture
from tests.test_planning_flow import anima_card

SAFETY = ["nsfw", "nude", "nudity"]
GRAPH = json.loads((FIXTURES / "tag-t2i-hires.api.json").read_text(encoding="utf-8"))
SLOTS = detect_slots(GRAPH)
SAMPLERS = ("41", "47")                          # base pass, hires pass
STYLE_LORA_FILE = "model_styles_anima-base1_v6.safetensors"


def build(loras=None, **recipe):
    return build_graph(GRAPH, SLOTS, {"prompt": "a cat", "seed": 7, **recipe, **({"loras": loras} if loras is not None else {})}, SAFETY)


def loaders(graph):
    return sorted((n for n, d in graph.items() if d["class_type"] == "LoraLoaderModelOnly"), key=lambda n: int(n.split("_")[1]))


def assert_no_dangling_links(graph):
    for nid, node in graph.items():
        for name, value in node["inputs"].items():
            if is_link(value):
                assert value[0] in graph, f"{nid}.{name} points at {value[0]}, which is not in the graph"


# ---------- the workflow settings stay as they are ----------

def test_workflow_settings_are_unchanged():
    g = GRAPH
    assert (g["41"]["inputs"]["sampler_name"], g["41"]["inputs"]["scheduler"], g["41"]["inputs"]["steps"], g["41"]["inputs"]["cfg"], g["41"]["inputs"]["denoise"]) == ("er_sde", "simple", 30, 4, 1)
    assert (g["47"]["inputs"]["sampler_name"], g["47"]["inputs"]["scheduler"], g["47"]["inputs"]["steps"], g["47"]["inputs"]["cfg"], g["47"]["inputs"]["denoise"]) == ("euler", "normal", 10, 5.5, 0.8)
    assert g["42"]["inputs"]["model_name"] == "upscaler_4x_a.safetensors" and g["44"]["inputs"]["scale_by"] == 0.45      # 4 x 0.45 = 1.8x
    assert SLOTS["problems"] == [] and SLOTS["hires"]["sampler_node"] == "47" and SLOTS["model"] == {"node": "1", "output": 0}


def test_no_style_lora_or_style_tag_is_built_into_the_workflow():
    text = json.dumps([GRAPH, SLOTS]).lower()
    assert "model_styles" not in text and "@tidewalker" not in text and "@" not in text
    assert not [n for n, d in GRAPH.items() if "lora" in d["class_type"].lower()]
    assert SLOTS["lora_loaders"] == [] and SLOTS["defaults"]["loras"] == []


# ---------- none, one or several LoRAs ----------

def test_with_no_lora_there_is_no_loader_and_the_samplers_read_the_model_loader():
    g = build()
    assert loaders(g) == [] and all(g[s]["inputs"]["model"] == ["1", 0] for s in SAMPLERS)
    assert_no_dangling_links(g)
    assert g == build(loras=[])


def test_one_lora_feeds_both_sampling_stages_at_its_own_weight():
    g = build([{"name": STYLE_LORA_FILE, "weight": 0.6}])
    assert loaders(g) == ["lora_1"]
    assert g["lora_1"]["inputs"] == {"model": ["1", 0], "lora_name": STYLE_LORA_FILE, "strength_model": 0.6}
    assert all(g[s]["inputs"]["model"] == ["lora_1", 0] for s in SAMPLERS)
    assert_no_dangling_links(g)


def test_several_loras_are_chained_and_the_last_one_feeds_every_sampler():
    wanted = [("a.safetensors", 1.0), ("b.safetensors", 0.4), ("c.safetensors", 0.7), ("d.safetensors", 0.25)]
    g = build([{"name": n, "weight": w} for n, w in wanted])
    assert loaders(g) == ["lora_1", "lora_2", "lora_3", "lora_4"]
    assert [(g[n]["inputs"]["lora_name"], g[n]["inputs"]["strength_model"]) for n in loaders(g)] == wanted
    assert g["lora_1"]["inputs"]["model"] == ["1", 0]
    assert [g[f"lora_{i}"]["inputs"]["model"] for i in (2, 3, 4)] == [["lora_1", 0], ["lora_2", 0], ["lora_3", 0]]
    assert all(g[s]["inputs"]["model"] == ["lora_4", 0] for s in SAMPLERS)
    assert_no_dangling_links(g)
    assert GRAPH["41"]["inputs"]["model"] == ["1", 0]            # the stored graph itself is never changed


def test_loras_do_not_change_the_workflow_settings():
    g = build([{"name": "a.safetensors", "weight": 1.0}, {"name": "b.safetensors", "weight": 0.5}])
    for node in ("41", "47", "42", "44"):
        assert {k: v for k, v in g[node]["inputs"].items() if k not in ("model", "seed")} == {k: v for k, v in GRAPH[node]["inputs"].items() if k not in ("model", "seed")}


# ---------- the removed workflow stays gone, its history stays ----------

@pytest.fixture
def client(config, comfy):
    with TestClient(create_app(config, comfy=comfy, start_worker=False)) as c:
        yield c


def import_both(client):
    kept = client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{TAGWF}.json")}).json()
    old = client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{TAGWF_LORA}.json")}).json()
    return kept, old


def test_removed_workflow_is_gone_from_every_list_and_a_recheck_does_not_bring_it_back(client):
    kept, old = import_both(client)
    client.app.state.cards.save_built("workflow", old["id"], anima_card())
    api_copy = client.app.state.config.workflows_dir / f"{old['id']}.api.json"
    assert api_copy.is_file() and {w["id"] for w in client.get("/api/workflows").json()} == {kept["id"], old["id"]}

    assert client.app.state.library.remove(old["id"], kept["id"]) is True
    assert not api_copy.exists() and (FIXTURES / f"{TAGWF_LORA}.json").is_file()            # our copy goes, the file it came from is never touched
    assert [w["id"] for w in client.get("/api/workflows").json()] == [kept["id"]]
    assert client.get(f"/api/workflows/{old['id']}").status_code == 404
    assert [c["key"] for c in client.get("/api/cards/workflows").json()] == [kept["id"]]
    assert client.app.state.cards.get("workflow", old["id"]) is None

    assert [w["id"] for w in client.post("/api/workflows/recheck").json()] == [kept["id"]]            # the Library's re-check
    assert [w["id"] for w in client.get("/api/workflows").json()] == [kept["id"]]
    client.post("/api/workflows/import", json={"path": str(FIXTURES / f"{TAGWF}.json")})              # importing the kept one again adds nothing
    assert [w["id"] for w in client.get("/api/workflows").json()] == [kept["id"]]
    assert client.app.state.library.remove(old["id"], kept["id"]) is False                           # running it twice is harmless


def test_jobs_and_images_made_with_the_removed_workflow_still_list_with_prompts_and_seeds(client):
    kept, old = import_both(client)
    job = client.post("/api/jobs", json={"workflow_id": old["id"], "count": 2, "fields": {
        "prompts": ["@lensfox, a cat", "@tidewalker, a dog"], "seed": 99, "seed_strategy": "increment",
        "loras": [{"name": STYLE_LORA_FILE, "weight": 1.0}]}}).json()
    store = client.app.state.store
    for image in job["images"]:
        store.set_image(image["id"], "done", file_path=f"x/{job['id']}-{image['idx']}.png")
    before = client.get("/api/images").json()
    assert {i["recipe"]["workflow_id"] for i in before} == {old["id"]}

    client.app.state.library.remove(old["id"], kept["id"])

    jobs = client.get("/api/jobs").json()
    assert [(j["id"], j["status"], j["workflow_id"]) for j in jobs] == [(job["id"], "ready", kept["id"])]
    after = client.get("/api/images").json()
    assert [(i["id"], i["seed"], i["recipe"]["prompt"], i["recipe"]["seed"]) for i in after] == [(i["id"], i["seed"], i["recipe"]["prompt"], i["recipe"]["seed"]) for i in before]
    assert {i["recipe"]["workflow_title"] for i in after} == {old["title"]}                 # each image still says which workflow made it
    assert all(i["workflow_id"] == kept["id"] and i["recipe"]["loras"][0]["name"] == STYLE_LORA_FILE for i in after)
    assert client.get(f"/api/jobs/{job['id']}").json()["fields"]["prompts"]["value"] == ["@lensfox, a cat", "@tidewalker, a dog"]


def test_a_finished_job_of_the_removed_workflow_can_be_run_again_on_the_kept_one(client, comfy):
    kept, old = import_both(client)
    job = client.post("/api/jobs", json={"workflow_id": old["id"], "fields": {"prompts": ["a cat"], "loras": [{"name": STYLE_LORA_FILE, "weight": 0.8}]}}).json()
    store = client.app.state.store
    store.set_image(job["images"][0]["id"], "done", file_path="x/1.png")
    store.db.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job["id"],))
    client.app.state.library.remove(old["id"], kept["id"])
    again = client.post(f"/api/jobs/{job['id']}/retry", json={"mode": "all"}).json()
    assert again["status"] == "queued" and again["workflow_id"] == kept["id"]
    wf = client.app.state.library.get(again["workflow_id"])
    graph = build_graph(client.app.state.library.get_graph(wf["id"]), wf["slots"], again["images"][0]["recipe"], [])
    assert graph["lora_1"]["inputs"]["lora_name"] == STYLE_LORA_FILE and graph["lora_1"]["inputs"]["strength_model"] == 0.8


async def test_remove_needs_a_workflow_for_the_jobs_to_move_to(library):
    old = await import_fixture(library, TAGWF_LORA)
    with pytest.raises(KeyError):
        library.remove(old["id"], "no-such-workflow")
    assert library.get(old["id"])["id"] == old["id"]            # nothing was removed
