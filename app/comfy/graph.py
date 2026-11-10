"""Fill an API-format graph from one image's recipe: prompts, settings, seed, LoRA chain, switches."""
import copy

from app.comfy.brackets import parses_weights, protect_literal_brackets
from app.fields import merge_negative

LORA_CLASSES = {"LoraLoader": ("model", "clip"), "LoraLoaderModelOnly": ("model",)}   # class -> pass-through inputs
RECIPE_TO_SLOT = ("seed", "steps", "cfg", "sampler", "scheduler", "denoise", "width", "height")


class GraphError(Exception):
    """The recipe can't be applied to this workflow."""


def set_slot(graph: dict, slot: dict | None, value):
    """Write a literal at a slot (replacing any link there)."""
    if slot and value is not None:
        graph[slot["node"]]["inputs"][slot["input"]] = value


def replace_links(graph: dict, old: list, new):
    """Point every input that reads `old` ([node, slot]) at `new` instead."""
    if new is None:
        return
    for node in graph.values():
        for name, value in node["inputs"].items():
            if value == old:
                node["inputs"][name] = new


def remove_lora_loaders(graph: dict, loader_ids: list[str]):
    """Delete existing LoRA loaders, reconnecting their consumers to what fed them."""
    for nid in loader_ids:
        node = graph.pop(nid, None)
        if node is None:
            continue
        for slot, input_name in enumerate(LORA_CLASSES[node["class_type"]]):
            replace_links(graph, [nid, slot], node["inputs"].get(input_name))


def inject_loras(graph: dict, slots: dict, loras: list[dict]):
    """Rebuild the LoRA chain: model loader -> LoraLoaderModelOnly x N -> every former consumer."""
    remove_lora_loaders(graph, slots.get("lora_loaders", []))
    if not loras:
        return
    if not slots.get("model"):
        raise GraphError("This workflow has no detected model loader slot, so LoRAs can't be added.")
    source = [slots["model"]["node"], slots["model"]["output"]]
    consumers = [(nid, name) for nid, node in graph.items() for name, v in node["inputs"].items() if v == source]
    prev = source
    for i, lora in enumerate(loras, 1):
        nid = f"lora_{i}"
        graph[nid] = {"class_type": "LoraLoaderModelOnly", "_meta": {"title": f"LoRA {i}"},
                      "inputs": {"model": prev, "lora_name": lora["name"], "strength_model": float(lora.get("weight", 1.0))}}
        prev = [nid, 0]
    for nid, name in consumers:
        graph[nid]["inputs"][name] = prev


def apply_switches(graph: dict, slots: dict):
    """Prompt enhancer and LoRA switches go off (our code does both jobs). Other switches are left alone."""
    for sw in slots.get("switches", []):
        if sw["role"] in ("prompt_enhancer", "lora"):
            set_slot(graph, sw, False)


def apply_hires(graph: dict, slots: dict, recipe: dict):
    """Hires on: set its seed/steps/denoise. Off: send the output from the base-pass decode."""
    hires = slots.get("hires")
    if not hires:
        return
    if recipe.get("hires", True):
        if recipe.get("seed") is not None:
            set_slot(graph, hires["seed"], recipe["seed"] + hires["seed_offset"])
        set_slot(graph, hires["steps"], recipe.get("hires_steps"))
        set_slot(graph, hires["denoise"], recipe.get("hires_denoise"))
        set_slot(graph, hires["cfg"], recipe.get("hires_cfg"))
    elif hires.get("base_decode"):
        for out in slots["outputs"]:
            graph[out["node"]]["inputs"][out["input"]] = [hires["base_decode"], 0]


def apply_input_images(graph: dict, slots: dict, images: list[str]):
    """Set LoadImage slots. The first input image of an edit workflow is required; the others are optional when the
    workflow allows it (see drop_image_node): an extra image you didn't give is taken out of the graph, so the
    workflow's own example picture is never used by mistake."""
    for i, slot in enumerate(slots.get("input_images", [])):
        if i >= len(images) or not images[i]:
            if i == 0:
                raise GraphError("This is an image-edit workflow: the job needs an input image.")
            drop_image_node(graph, slot["node"], slot.get("label") or f"input image {i + 1}")
            continue
        set_slot(graph, slot, images[i])


def drop_image_node(graph: dict, node_id: str, label: str):
    """Remove a LoadImage node and the links to it. Only links into list-style inputs (names like `images.image_2`,
    which ComfyUI grows as images are added) can be dropped; any other input needs the image, so that's an error."""
    links = [(nid, key) for nid, node in graph.items() for key, value in node.get("inputs", {}).items()
             if isinstance(value, list) and len(value) == 2 and str(value[0]) == str(node_id)]
    needed = [key for _, key in links if "." not in key]
    if needed:
        raise GraphError(f"This workflow needs {label}: it feeds '{needed[0]}', which can't be left empty.")
    for nid, key in links:
        del graph[nid]["inputs"][key]
    graph.pop(node_id, None)


def negative_text(slots: dict, recipe: dict, safety_negative: list[str]) -> str:
    """The negative prompt of one image as stored: the recipe's (or the workflow's own) plus the safety tags. Not escaped."""
    base = recipe.get("negative")
    if base is None:
        base = (slots.get("defaults") or {}).get("negative") or ""
    return merge_negative(base, safety_negative)


def build_graph(api_graph: dict, slots: dict, recipe: dict, safety_negative: list[str]) -> dict:
    """Return a copy of `api_graph` filled from `recipe` (plain values, one image).

    recipe keys: prompt, negative, seed, steps, cfg, sampler, scheduler, denoise, width, height,
    loras [{name, weight}], hires, hires_steps, hires_denoise, hires_cfg, input_images [filenames]
    """
    graph = copy.deepcopy(api_graph)
    inject_loras(graph, slots, recipe.get("loras") or [])
    apply_switches(graph, slots)
    # brackets that are only brackets are escaped as the last step, and only where the encoder parses weights
    protect = protect_literal_brackets if parses_weights(graph, slots) else (lambda text: text)

    if recipe.get("prompt") is not None:
        if not slots.get("positive"):
            raise GraphError("No positive prompt slot is set for this workflow.")
        set_slot(graph, slots["positive"], protect(recipe["prompt"]))
    for name in RECIPE_TO_SLOT:
        set_slot(graph, slots.get(name), recipe.get(name))

    if slots.get("negative"):
        set_slot(graph, slots["negative"], protect(negative_text(slots, recipe, safety_negative)))

    apply_hires(graph, slots, recipe)
    if slots.get("kind") == "edit":
        apply_input_images(graph, slots, recipe.get("input_images") or [])
    return graph
