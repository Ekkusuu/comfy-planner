from tests.conftest import KREA, QWEN, TAGWF, TAGWF_LORA


def test_tag_slots(slot_maps):
    s = slot_maps[TAGWF]
    assert s["kind"] == "t2i" and s["problems"] == []
    assert s["positive"] == {"node": "6", "input": "text"} and s["negative"] == {"node": "7", "input": "text"}
    assert s["seed"] == {"node": "41", "input": "seed"}            # main sampler = the one fed by EmptyLatentImage
    assert (s["width"], s["height"]) == ({"node": "5", "input": "width"}, {"node": "5", "input": "height"})
    assert s["model"] == {"node": "1", "output": 0}
    assert s["outputs"] == [{"node": "49", "input": "images"}]
    hires = s["hires"]
    assert hires["sampler_node"] == "47" and hires["base_decode"] == "8"
    assert hires["seed_offset"] == 1000                            # 21838 - 20838 in the graph
    assert s["defaults"]["seed"] == 20838 and s["defaults"]["hires"]["denoise"] == 0.6


def test_existing_lora_is_a_default_not_a_slot_of_the_graph(slot_maps):
    s = slot_maps[TAGWF_LORA]
    assert s["lora_loaders"] == ["52"]
    assert s["defaults"]["loras"][0]["name"].startswith("model_styles")


def test_krea_slots(slot_maps):
    s = slot_maps[KREA]
    assert s["positive"] == {"node": "30:19", "input": "value"}     # traced through switches and PreviewAny to the text
    assert s["negative"] is None                                    # negative is a zeroed-out positive
    assert s["hires"] is None and s["kind"] == "t2i"
    assert s["model"] == {"node": "30:10", "output": 0}
    assert s["lora_loaders"] == ["30:15"]
    roles = {sw["title"]: sw["role"] for sw in s["switches"]}
    assert roles == {"Boolean (Refine Prompt?)": "prompt_enhancer", "Boolean (Enable LoRA?)": "lora"}
    assert (s["width"], s["height"]) == ({"node": "30:5", "input": "width"}, {"node": "30:5", "input": "height"})


def test_qwen_edit_slots(slot_maps):
    s = slot_maps[QWEN]
    assert s["kind"] == "edit"
    assert s["input_images"] == [{"node": "470", "input": "image", "label": "input image 1"}]
    assert s["negative"] == {"node": "459:474", "input": "negative_prompt"}
    roles = {sw["node"]: sw["role"] for sw in s["switches"]}
    assert roles["459:484"] == "prompt_enhancer" and roles["459:468"] == "other"
