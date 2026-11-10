"""Find the slots of an API-format graph: which node inputs hold prompt, seed, size, etc.

The result is a plain JSON dict the user can confirm or edit later.
A slot is {"node": id, "input": name}.
"""

SAMPLERS = {   # sampler class -> input names for each setting
    "KSampler": {"seed": "seed", "steps": "steps", "cfg": "cfg", "sampler": "sampler_name",
                 "scheduler": "scheduler", "denoise": "denoise"},
    "KSamplerAdvanced": {"seed": "noise_seed", "steps": "steps", "cfg": "cfg", "sampler": "sampler_name",
                         "scheduler": "scheduler", "denoise": None},
}
LORA_CLASSES = {"LoraLoader", "LoraLoaderModelOnly"}
OUTPUT_CLASSES = {"SaveImage", "SaveImageAdvanced"}
# text encode node class -> (positive input, negative input or None)
TEXT_NODES = {"CLIPTextEncode": ("text", None), "TextEncodeQwenImage21": ("prompt", "negative_prompt")}
SETTING_NAMES = ("seed", "steps", "cfg", "sampler", "scheduler", "denoise")


def is_link(value) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)


def ref(node_id: str, input_name: str) -> dict:
    return {"node": node_id, "input": input_name}


def title_of(node: dict) -> str:
    return (node.get("_meta") or {}).get("title") or node["class_type"]


def id_key(node_id: str) -> list:
    """Sort key so '9' < '10' and '30:5' < '30:10'."""
    return [int(p) if p.isdigit() else p for p in node_id.split(":")]


def ancestors(graph: dict, node_id: str, stop_at=()) -> set[str]:
    """All nodes feeding `node_id` (not including it). Nodes of `stop_at` classes are included but not expanded."""
    seen, todo = set(), [node_id]
    while todo:
        for value in graph[todo.pop()]["inputs"].values():
            if is_link(value) and value[0] in graph and value[0] not in seen:
                seen.add(value[0])
                if graph[value[0]]["class_type"] not in stop_at:
                    todo.append(value[0])
    return seen


def trace_text(graph: dict, node_id: str, name: str) -> dict | None:
    """Follow a text input upstream (through switches/previews) to the literal that holds the text."""
    value = graph[node_id]["inputs"].get(name)
    if not is_link(value):
        return ref(node_id, name) if name in graph[node_id]["inputs"] else None
    source = graph.get(value[0])
    if source is None:
        return None
    kind = source["class_type"]
    if kind == "PreviewAny":
        return trace_text(graph, value[0], "source")
    if kind == "ComfySwitchNode":
        return trace_text(graph, value[0], "on_false")   # the path used when the switch is off
    if kind.startswith("Primitive") and "value" in source["inputs"]:
        return trace_text(graph, value[0], "value")
    return None


def find_samplers(graph: dict) -> tuple[str | None, list[str]]:
    """(main sampler id, hires sampler ids). Main has no other sampler upstream."""
    samplers = sorted((n for n, d in graph.items() if d["class_type"] in SAMPLERS), key=id_key)
    mains = [s for s in samplers if not (ancestors(graph, s) & set(samplers))]
    main = mains[0] if mains else None
    return main, [s for s in samplers if main and main in ancestors(graph, s)]


def find_model_loader(graph: dict, sampler_id: str) -> str | None:
    """Walk the model input upstream to the node that loads the model."""
    value = graph[sampler_id]["inputs"].get("model")
    seen = set()
    while is_link(value) and value[0] in graph and value[0] not in seen:
        seen.add(value[0])
        node = graph[value[0]]
        nxt = node["inputs"].get("on_false" if node["class_type"] == "ComfySwitchNode" else "model")
        if not is_link(nxt):
            return value[0]
        value = nxt
    return None


def find_size(graph: dict, sampler_id: str) -> tuple[dict | None, dict | None]:
    """width/height slots of the empty latent upstream of the main sampler."""
    for nid in sorted(ancestors(graph, sampler_id), key=id_key):
        inputs = graph[nid]["inputs"]
        if graph[nid]["class_type"].startswith("Empty") and "width" in inputs and "height" in inputs:
            return ref(nid, "width"), ref(nid, "height")
    return None, None


def size_is_bypassed(graph: dict, latent_id: str) -> bool:
    """Is the empty latent's size ignored? True when its only readers are switches that are off and take the size of the input
    image instead (the Qwen image edit workflow): setting a width and height there would change nothing."""
    readers = [d for d in graph.values() if any(is_link(v) and v[0] == latent_id for v in d["inputs"].values())]
    if not readers:
        return False
    for d in readers:
        control = d["inputs"].get("switch")
        if is_link(control) and control[0] in graph:
            control = graph[control[0]]["inputs"].get("value")
        linked = [k for k, v in d["inputs"].items() if is_link(v) and v[0] == latent_id]
        if d["class_type"] != "ComfySwitchNode" or control is not False or linked != ["on_true"]:
            return False
    return True


def switch_role(graph: dict, switch_id: str, boolean_title: str) -> str:
    """What a switch controls: the prompt enhancer, a LoRA, or something else (left alone)."""
    on_true = graph[switch_id]["inputs"].get("on_true")
    if is_link(on_true) and on_true[0] in graph:
        branch = {on_true[0], *ancestors(graph, on_true[0], stop_at=("ComfySwitchNode", "PreviewAny"))}
        classes = {graph[n]["class_type"] for n in branch}
        if "TextGenerate" in classes:
            return "prompt_enhancer"
        if classes & LORA_CLASSES:
            return "lora"
    return "lora" if "lora" in boolean_title.lower() else "other"


def find_switches(graph: dict) -> list[dict]:
    """Boolean switches (one slot per controlling literal); the most specific role wins."""
    rank = {"prompt_enhancer": 0, "lora": 1, "other": 2}
    found: dict[tuple, dict] = {}
    for nid in sorted((n for n, d in graph.items() if d["class_type"] == "ComfySwitchNode"), key=id_key):
        control = graph[nid]["inputs"].get("switch")
        if is_link(control) and control[0] in graph and graph[control[0]]["class_type"] == "PrimitiveBoolean":
            slot, title = ref(control[0], "value"), title_of(graph[control[0]])
        elif not is_link(control):
            slot, title = ref(nid, "switch"), title_of(graph[nid])
        else:
            continue
        role = switch_role(graph, nid, title)
        key = (slot["node"], slot["input"])
        if key not in found or rank[role] < rank[found[key]["role"]]:
            found[key] = {**slot, "title": title, "role": role, "default": graph[slot["node"]]["inputs"].get(slot["input"])}
    return list(found.values())


def find_prompts(graph: dict, sampler_id: str) -> tuple[dict | None, dict | None]:
    """Positive / negative text slots from what feeds the sampler's conditioning inputs."""
    inputs = graph[sampler_id]["inputs"]
    pos, neg = inputs.get("positive"), inputs.get("negative")
    positive = negative = None
    if is_link(pos) and graph[pos[0]]["class_type"] in TEXT_NODES:
        pos_name, neg_name = TEXT_NODES[graph[pos[0]]["class_type"]]
        positive = trace_text(graph, pos[0], pos_name)
        if is_link(neg) and neg[0] == pos[0] and neg_name:
            negative = trace_text(graph, pos[0], neg_name)
    if negative is None and is_link(neg) and neg[0] in graph and graph[neg[0]]["class_type"] in TEXT_NODES:
        negative = trace_text(graph, neg[0], TEXT_NODES[graph[neg[0]]["class_type"]][0])
    return positive, negative


def sampler_slots(graph: dict, sampler_id: str) -> dict:
    names = SAMPLERS[graph[sampler_id]["class_type"]]
    return {key: ref(sampler_id, name) if name else None for key, name in names.items()}


def read_default(graph: dict, slot: dict | None):
    """Literal value currently stored at a slot (None if linked or missing)."""
    if not slot:
        return None
    value = graph[slot["node"]]["inputs"].get(slot["input"])
    return None if is_link(value) else value


def detect_slots(graph: dict) -> dict:
    """Auto-detect all slots. `problems` lists anything the user must fix by hand."""
    problems: list[str] = []
    main, hires_ids = find_samplers(graph)
    slots: dict = {"version": 1, "positive": None, "negative": None, "width": None, "height": None, "hires": None,
                   "model": None, **{k: None for k in SETTING_NAMES}}
    if main is None:
        problems.append("no KSampler found")
    else:
        slots.update(sampler_slots(graph, main))
        slots["positive"], slots["negative"] = find_prompts(graph, main)
        slots["width"], slots["height"] = find_size(graph, main)
        if slots["width"] and size_is_bypassed(graph, slots["width"]["node"]):
            slots["width"] = slots["height"] = None        # the size follows the input image; nothing to set
            slots["size_follows_image"] = True
        loader = find_model_loader(graph, main)
        slots["model"] = {"node": loader, "output": 0} if loader else None
        if slots["positive"] is None:
            problems.append("positive prompt slot not found")
        if slots["model"] is None:
            problems.append("model loader not found (LoRAs can't be injected)")
        if hires_ids:
            slots["hires"] = _hires_slots(graph, main, hires_ids[0])

    slots["lora_loaders"] = sorted((n for n, d in graph.items() if d["class_type"] in LORA_CLASSES), key=id_key)
    slots["switches"] = find_switches(graph)
    ordered = sorted(graph, key=id_key)
    slots["outputs"] = [ref(n, "images") for n in ordered if graph[n]["class_type"] in OUTPUT_CLASSES]
    if not slots["outputs"]:
        problems.append("no SaveImage node found")
    loads = [n for n in ordered if graph[n]["class_type"] == "LoadImage"]
    slots["input_images"] = [{**ref(n, "image"), "label": title_of(graph[n]) if graph[n].get("_meta") else f"input image {i + 1}"}
                             for i, n in enumerate(loads)]
    slots["kind"] = "edit" if loads else "t2i"
    slots["problems"] = problems
    slots["defaults"] = _defaults(graph, slots)
    return slots


def _hires_slots(graph: dict, main: str, hires: str) -> dict:
    base_decode = next((n for n, d in graph.items() if d["class_type"].startswith("VAEDecode")
                        and d["inputs"].get("samples") == [main, 0]), None)
    out = {"sampler_node": hires, "base_decode": base_decode, **sampler_slots(graph, hires)}
    main_seed = graph[main]["inputs"].get(SAMPLERS[graph[main]["class_type"]]["seed"])
    hires_seed = read_default(graph, out["seed"])
    both_int = isinstance(main_seed, int) and isinstance(hires_seed, int)
    offset = hires_seed - main_seed if both_int else 1000
    out["seed_offset"] = offset if abs(offset) <= 100_000 else 1000   # unrelated random seeds -> default
    return out


def _defaults(graph: dict, slots: dict) -> dict:
    """Current literal values of the workflow, used when a job doesn't set a field."""
    names = (*SETTING_NAMES, "width", "height", "positive", "negative")
    out = {name: read_default(graph, slots[name]) for name in names}
    out["loras"] = [{"name": graph[n]["inputs"].get("lora_name"), "weight": graph[n]["inputs"].get("strength_model")}
                    for n in slots["lora_loaders"]]   # the workflow's own LoRAs: suggested defaults only
    if slots["hires"]:
        out["hires"] = {k: read_default(graph, slots["hires"][k]) for k in ("steps", "cfg", "sampler", "scheduler", "denoise")}
    return out
