import json

import pytest

from app.comfy.convert import ConvertError, check_runnable, convert, is_api_format, map_widgets
from app.comfy.slots import detect_slots
from tests.conftest import ALL_FIXTURES, FIXTURES, KREA, QWEN, TAGWF, TAGWF_LORA, TAGWF_MISSING, load_workflow


def test_tag_widgets_and_dropped_nodes(graphs):
    g = graphs[TAGWF]
    assert {n["class_type"] for n in g.values()} == {
        "CLIPLoader", "VAELoader", "CLIPTextEncode", "UNETLoader", "ImageUpscaleWithModel", "KSampler", "VAEEncode",
        "SaveImage", "EmptyLatentImage", "VAEDecode", "UpscaleModelLoader", "ImageScaleBy",
        "PreviewImage"}   # no MarkdownNote; PreviewImage stays, like in ComfyUI's own export
    main = g["41"]["inputs"]
    # control_after_generate ("randomize") must not shift the following widgets
    assert (main["seed"], main["steps"], main["cfg"], main["sampler_name"], main["scheduler"], main["denoise"]) == \
           (20838, 30, 5, "er_sde", "simple", 1)
    assert g["47"]["inputs"]["seed"] == 21838 and g["47"]["inputs"]["denoise"] == 0.6
    assert g["47"]["inputs"]["latent_image"] == ["46", 0]
    assert g["49"]["inputs"]["images"] == ["45", 0]


def test_krea_subgraph_expanded(graphs):
    g = graphs[KREA]
    assert "30:3" in g and "30" not in g                    # subgraph node replaced by prefixed inner nodes
    assert g["29"]["inputs"]["images"] == ["30:8", 0]       # outer SaveImage rewired to inner VAEDecode
    ks = g["30:3"]["inputs"]
    assert (ks["steps"], ks["cfg"], ks["sampler_name"], ks["scheduler"]) == (8, 1, "euler", "simple")
    assert ks["seed"] == 45862206397178                     # value passed in from the subgraph node's widgets
    assert g["30:5"]["inputs"]["width"] == ["49", 0]        # linked from ResolutionSelector outside the subgraph
    assert g["30:10"]["inputs"]["unet_name"] == "nl_diffusion_int8.safetensors"
    assert g["30:19"]["inputs"]["value"].startswith("A lighthouse on a cliff")
    assert g["30:16"]["inputs"]["sampling_mode.temperature"] == 0.7   # dynamic combo expanded


def test_lora_workflow_keeps_loader(graphs):
    assert graphs[TAGWF_LORA]["52"]["class_type"] == "LoraLoaderModelOnly"


def test_qwen_edit_converts(graphs):
    g = graphs[QWEN]
    assert g["470"]["inputs"]["image"] == "example.png"     # LoadImage's upload companion value skipped
    assert g["459:474"]["inputs"]["images.image_1"] == ["470", 0]
    assert "472" not in g                                    # ImageCompare dropped


def test_runnable_checks(graphs, oi):
    assert check_runnable(graphs[KREA], oi) == []
    assert check_runnable(graphs[TAGWF], oi) == []
    missing = {m["value"] for m in check_runnable(graphs[TAGWF_MISSING], oi)}
    assert "missing_unet_00012_.safetensors" in missing and "upscaler_4x_a.pth" in missing
    assert {m["input"] for m in check_runnable(graphs[QWEN], oi)} == {"unet_name", "clip_name", "vae_name"}


def test_fixtures_runnable_status(graphs, oi):
    runnable = {n: not check_runnable(graphs[n], oi) for n in ALL_FIXTURES}
    assert runnable == {KREA: True, TAGWF: True, TAGWF_LORA: True, TAGWF_MISSING: False, QWEN: False}


def test_api_format_passthrough(oi):
    api = {"1": {"class_type": "VAELoader", "inputs": {"vae_name": "x"}}}
    assert is_api_format(api)
    assert convert(api, oi).graph is api
    assert not is_api_format(load_workflow(TAGWF))


def node(nid, kind, inputs=None, widgets=None, mode=0, outputs=None):
    return {"id": nid, "type": kind, "mode": mode, "inputs": inputs or [], "widgets_values": widgets or [],
            "outputs": outputs or []}


def test_reroute_primitive_muted_and_bypass(oi):
    """Reroute is resolved through, PrimitiveNode is inlined, muted nodes dropped, bypassed nodes passed through."""
    nodes = [
        node(1, "VAELoader", widgets=["shared_vae.safetensors"], outputs=[{"name": "VAE", "type": "VAE", "links": [1]}]),
        node(2, "Reroute", inputs=[{"name": "", "type": "VAE", "link": 1}], outputs=[{"name": "", "type": "VAE", "links": [2]}]),
        node(3, "VAEDecode", inputs=[{"name": "vae", "type": "VAE", "link": 2}]),
        node(4, "PrimitiveNode", widgets=[1234], outputs=[{"name": "INT", "type": "INT", "links": [3]}]),
        node(5, "EmptyLatentImage", inputs=[{"name": "width", "type": "INT", "link": 3, "widget": {"name": "width"}}],
             widgets=[512, 512, 1]),
        node(6, "Note", widgets=["hello"]),
        node(7, "VAELoader", widgets=["x"], mode=2),
        node(8, "UNETLoader", widgets=["nl_diffusion_int8.safetensors", "default"],
             outputs=[{"name": "MODEL", "type": "MODEL", "links": [4]}]),
        node(9, "LoraLoaderModelOnly", mode=4, inputs=[{"name": "model", "type": "MODEL", "link": 4}], widgets=["a", 1],
             outputs=[{"name": "MODEL", "type": "MODEL", "links": [5]}]),
        node(10, "ModelSamplingAuraFlow", inputs=[{"name": "model", "type": "MODEL", "link": 5}]),
    ]
    links = [[1, 1, 0, 2, 0, "VAE"], [2, 2, 0, 3, 0, "VAE"], [3, 4, 0, 5, 0, "INT"],
             [4, 8, 0, 9, 0, "MODEL"], [5, 9, 0, 10, 0, "MODEL"]]
    oi = {**oi, "ModelSamplingAuraFlow": {"input": {"required": {"model": ["MODEL"]}}, "input_order": {"required": ["model"]},
                                         "output": ["MODEL"]}}
    g = convert({"nodes": nodes, "links": links}, oi).graph
    assert g["3"]["inputs"]["vae"] == ["1", 0]               # through the Reroute
    assert g["5"]["inputs"]["width"] == 1234                 # PrimitiveNode value inlined
    assert set(g) == {"1", "3", "5", "8", "10"}              # Note, Reroute, PrimitiveNode, muted, bypassed removed
    assert g["10"]["inputs"]["model"] == ["8", 0]            # bypass passes the model through


def test_unsupported_node_message(oi):
    wf = {"nodes": [node(1, "SomeCustomNode", widgets=[1])], "links": []}
    with pytest.raises(ConvertError) as err:
        convert(wf, oi)
    assert "node 1" in str(err.value) and "SomeCustomNode" in str(err.value) and "Export (API)" in str(err.value)


def test_map_widgets_skips_control_after_generate(oi):
    assert map_widgets(oi["KSampler"], [5, "fixed", 20, 7, "euler", "normal", 1]) == {
        "seed": 5, "steps": 20, "cfg": 7, "sampler_name": "euler", "scheduler": "normal", "denoise": 1}


def test_missing_widget_value_gets_default(oi):
    """A workflow saved before an input existed has no value for it; the frontend (and so Export) fills the default."""
    values = ["", 512, "on", 0.7, 64, 0.95, 0.05, 1.05, 0, 0, False, True]   # TextGenerate without the later "mtp"
    assert map_widgets(oi["TextGenerate"], values)["mtp"] == "auto"
    assert map_widgets(oi["TextGenerate"], values[:3])["max_length"] == 512


# ---------- equality with ComfyUI's own "Export (API)" ----------

def strip_meta(graph: dict) -> dict:
    return {nid: {"class_type": n["class_type"], "inputs": n["inputs"]} for nid, n in graph.items()}


def load_export(name: str) -> dict:
    return json.loads((FIXTURES / "api_export" / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", [KREA, TAGWF])
def test_converted_graph_equals_comfyui_export(graphs, name):
    """Same node ids, classes and inputs (literals and [source, output] links) as ComfyUI's Export (API).

    Seeds and prompt text are compared too: the export was made without touching the saved values.
    If a fixture is re-exported after editing in the UI, those fields are what to expect to differ.
    """
    assert strip_meta(graphs[name]) == strip_meta(load_export(name))


@pytest.mark.parametrize("name", [KREA, TAGWF])
def test_slots_same_on_export_and_converted(graphs, oi, name):
    exported = convert(load_export(name), oi).graph      # API format passes straight through
    assert detect_slots(exported) == detect_slots(graphs[name])
