import pytest

from app.comfy.graph import GraphError, build_graph
from app.fields import merge_negative, remove_tags, seed_for_image
from tests.conftest import KREA, QWEN, TAGWF, TAGWF_LORA

SAFETY = ["nsfw", "nude", "nudity"]


def build(graphs, slot_maps, name, **recipe):
    return build_graph(graphs[name], slot_maps[name], recipe, SAFETY)


def lora_chain(g):
    return sorted(n for n, d in g.items() if d["class_type"] == "LoraLoaderModelOnly")


def test_fill_values_and_hires_seed(graphs, slot_maps):
    g = build(graphs, slot_maps, TAGWF, prompt="1girl, solo", negative="bad hands", seed=500, steps=22, cfg=4.5,
              sampler="euler", scheduler="normal", width=640, height=960, hires_steps=8, hires_denoise=0.5)
    ks = g["41"]["inputs"]
    assert (ks["seed"], ks["steps"], ks["cfg"], ks["sampler_name"], ks["scheduler"]) == (500, 22, 4.5, "euler", "normal")
    assert (g["5"]["inputs"]["width"], g["5"]["inputs"]["height"]) == (640, 960)
    assert g["6"]["inputs"]["text"] == "1girl, solo"
    assert g["47"]["inputs"]["seed"] == 1500                         # main seed + 1000
    assert (g["47"]["inputs"]["steps"], g["47"]["inputs"]["denoise"]) == (8, 0.5)
    assert g["49"]["inputs"]["images"] == ["45", 0]                  # saves the hires decode
    assert graphs[TAGWF]["41"]["inputs"]["seed"] == 20838             # original graph untouched


def test_hires_off_saves_base_decode(graphs, slot_maps):
    g = build(graphs, slot_maps, TAGWF, prompt="x", seed=1, hires=False)
    assert g["49"]["inputs"]["images"] == ["8", 0]


def test_lora_chain_counts_and_rewiring(graphs, slot_maps):
    assert lora_chain(build(graphs, slot_maps, TAGWF, prompt="x")) == []
    one = build(graphs, slot_maps, TAGWF, prompt="x", loras=[{"name": "a.safetensors", "weight": 0.8}])
    assert lora_chain(one) == ["lora_1"]
    assert one["lora_1"]["inputs"] == {"model": ["1", 0], "lora_name": "a.safetensors", "strength_model": 0.8}
    assert one["41"]["inputs"]["model"] == ["lora_1", 0] and one["47"]["inputs"]["model"] == ["lora_1", 0]  # both samplers
    three = build(graphs, slot_maps, TAGWF, prompt="x",
                  loras=[{"name": n, "weight": 1} for n in ("a", "b", "c")])
    assert lora_chain(three) == ["lora_1", "lora_2", "lora_3"]
    assert three["lora_2"]["inputs"]["model"] == ["lora_1", 0] and three["lora_3"]["inputs"]["model"] == ["lora_2", 0]
    assert three["41"]["inputs"]["model"] == ["lora_3", 0]


def test_existing_lora_loader_removed_and_rebuilt(graphs, slot_maps):
    assert graphs[TAGWF_LORA]["41"]["inputs"]["model"] == ["52", 0]
    none = build(graphs, slot_maps, TAGWF_LORA, prompt="x")
    assert "52" not in none and none["41"]["inputs"]["model"] == ["1", 0]
    mine = build(graphs, slot_maps, TAGWF_LORA, prompt="x", loras=[{"name": "mine.safetensors", "weight": 0.8}])
    assert "52" not in mine and lora_chain(mine) == ["lora_1"]
    assert mine["41"]["inputs"]["model"] == ["lora_1", 0] and mine["47"]["inputs"]["model"] == ["lora_1", 0]


def test_krea_lora_switch_and_enhancer_off(graphs, slot_maps):
    g = build(graphs, slot_maps, KREA, prompt="a lake", seed=7, loras=[{"name": "krea2_inkbrush.safetensors", "weight": 0.8}])
    assert g["30:24"]["inputs"]["value"] is False                    # prompt enhancer ("Refine Prompt?") off
    assert g["30:23"]["inputs"]["value"] is False                    # LoRA switch off: our chain is used instead
    assert "30:15" not in g and g["lora_1"]["inputs"]["model"] == ["30:10", 0]
    assert g["30:22"]["inputs"]["on_false"] == ["lora_1", 0]         # the switch's consumer sees the chain
    assert g["30:19"]["inputs"]["value"] == "a lake" and g["30:3"]["inputs"]["seed"] == 7


def test_safety_negative_merged_and_deduped(graphs, slot_maps):
    g = build(graphs, slot_maps, TAGWF, prompt="x", negative="Bad Hands, NSFW, (nude:1.2), blurry")
    tags = [t.strip() for t in g["7"]["inputs"]["text"].split(",")]
    assert tags == ["Bad Hands", "NSFW", "(nude:1.2)", "blurry", "nudity"]
    default = build(graphs, slot_maps, TAGWF, prompt="x")["7"]["inputs"]["text"]    # no negative given: workflow's own + safety
    assert default.startswith("lowres") and default.endswith("nudity") and default.count("nsfw") == 1


def test_merge_negative_keeps_weight_groups_and_handles_empty():
    assert merge_negative("", ["a", "b"]) == "a, b"
    assert merge_negative(None, []) == ""
    assert merge_negative("(worst quality, bad quality:1.2), x", ["x", "y"]) == "(worst quality, bad quality:1.2), x, y"


def test_seed_strategies():
    assert seed_for_image("fixed", 10, 3) == 10
    assert seed_for_image("increment", 10, 3) == 13
    assert seed_for_image("random", 10, 3) != seed_for_image("random", 10, 3)


def test_edit_workflow_needs_input_image(graphs, slot_maps):
    with pytest.raises(GraphError):
        build(graphs, slot_maps, QWEN, prompt="make it blue")
    g = build(graphs, slot_maps, QWEN, prompt="make it blue", input_images=["cp_abc_photo.png"])
    assert g["470"]["inputs"]["image"] == "cp_abc_photo.png"
    assert g["459:484"]["inputs"]["on_false"] == "make it blue" and g["459:484"]["inputs"]["switch"] is False
    assert "nsfw" in g["459:474"]["inputs"]["negative_prompt"]       # safety applied to the edit negative too


def test_missing_optional_image_is_taken_out_not_left_as_the_example():
    from app.comfy.graph import apply_input_images
    graph = {"1": {"class_type": "LoadImage", "inputs": {"image": "person.png"}},
             "2": {"class_type": "LoadImage", "inputs": {"image": "example_shirt.png"}},
             "3": {"class_type": "Encode", "inputs": {"images.image_1": ["1", 0], "images.image_2": ["2", 0]}},
             "4": {"class_type": "Batch", "inputs": {"images.image0": ["1", 0], "images.image1": ["2", 0]}}}
    slots = {"input_images": [{"node": "1", "input": "image"}, {"node": "2", "input": "image"}]}
    apply_input_images(graph, slots, ["mine.png"])
    assert "2" not in graph and graph["1"]["inputs"]["image"] == "mine.png"
    assert graph["3"]["inputs"] == {"images.image_1": ["1", 0]} and graph["4"]["inputs"] == {"images.image0": ["1", 0]}
    needed = {"1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
              "2": {"class_type": "LoadImage", "inputs": {"image": "b.png"}},
              "3": {"class_type": "Blend", "inputs": {"image1": ["1", 0], "image2": ["2", 0]}}}
    with pytest.raises(GraphError):
        apply_input_images(needed, slots, ["mine.png"])         # a plain input can't lose its image


def test_remove_tags_drops_listed_tags_only():
    text = "lowres,(worst quality, bad quality:1.2),censored,Watermark,blurry,(logo:1.1),"
    assert remove_tags(text, ["watermark", "blurry", "logo"]) == "lowres, (worst quality, bad quality:1.2), censored"
    assert remove_tags(None, ["nsfw"]) is None
    assert remove_tags("a, b", []) == "a, b"


def test_a_size_the_workflow_ignores_is_not_a_slot():
    """The Qwen edit workflow takes its size from the input image: the empty latent is only used when a switch is turned on."""
    from app.comfy.slots import size_is_bypassed
    g = {"1": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
         "2": {"class_type": "ComfySwitchNode", "inputs": {"switch": False, "on_false": ["3", 0], "on_true": ["1", 0]}},
         "3": {"class_type": "Other", "inputs": {}}}
    assert size_is_bypassed(g, "1")
    g["2"]["inputs"]["switch"] = True
    assert not size_is_bypassed(g, "1")
    g["2"]["inputs"]["switch"] = ["4", 0]
    g["4"] = {"class_type": "PrimitiveBoolean", "inputs": {"value": False}}
    assert size_is_bypassed(g, "1")
    g["5"] = {"class_type": "KSampler", "inputs": {"latent_image": ["1", 0]}}        # something else reads the size too
    assert not size_is_bypassed(g, "1")
