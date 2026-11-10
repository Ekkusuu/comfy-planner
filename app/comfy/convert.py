"""Convert a ComfyUI UI-format workflow to API format.

Needs ComfyUI's /object_info to know the widget order of each node.
Subgraphs are expanded into flat nodes with ids like "30:10".
"""
from collections import deque
from dataclasses import dataclass, field

INPUT_NODE = -10      # id of the subgraph's input pseudo-node
OUTPUT_NODE = -20     # id of the subgraph's output pseudo-node
NOTE_TYPES = {"Note", "MarkdownNote"}
PREVIEW_TYPES = {"PreviewImage", "ImageCompare"}   # kept like ComfyUI's export; dropped only if this ComfyUI lacks them
WIDGET_PRIMITIVES = {"INT", "FLOAT", "STRING", "BOOLEAN"}
UPLOAD_FLAGS = ("image_upload", "video_upload", "audio_upload")
EXPORT_HINT = "Open the workflow in ComfyUI, use Workflow -> Export (API), and import that file instead."


class ConvertError(Exception):
    """The workflow uses something the converter can't handle."""


@dataclass
class Converted:
    graph: dict
    warnings: list[str] = field(default_factory=list)


def is_api_format(wf: dict) -> bool:
    """API format is {node_id: {class_type, inputs}} with no 'nodes' list."""
    if not isinstance(wf, dict) or "nodes" in wf or not wf:
        return False
    return all(isinstance(v, dict) and "class_type" in v for v in wf.values())


def extract_notes(wf: dict) -> list[dict]:
    """Text of Note / MarkdownNote nodes (top level), for later card building."""
    notes = []
    for node in wf.get("nodes", []):
        if node.get("type") in NOTE_TYPES and node.get("widgets_values"):
            notes.append({"title": node.get("title") or node["type"], "text": node["widgets_values"][0]})
    return notes


# ---------- widget mapping ----------

def is_widget(spec: list) -> bool:
    """True if this object_info input is drawn as a widget (value in widgets_values)."""
    kind = spec[0]
    opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    if isinstance(kind, list) or kind in ("COMBO", "COMFY_DYNAMICCOMBO_V3"):
        return True
    return kind in WIDGET_PRIMITIVES and not opts.get("forceInput")


def input_order(info: dict) -> tuple[dict, list[str]]:
    """All required+optional input specs and their order."""
    inputs = {**info["input"].get("required", {}), **info["input"].get("optional", {})}
    order = info.get("input_order")
    names = (order.get("required", []) + order.get("optional", [])) if order else list(inputs)
    return inputs, names


def extra_values(name: str, spec: list, top_level: bool) -> int:
    """Number of hidden companion widget values after this widget."""
    opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    if spec[0] == "INT" and (opts.get("control_after_generate")
                             or (top_level and name in ("seed", "noise_seed") and opts.get("control_after_generate") is not False)):
        return 1
    return 1 if any(opts.get(flag) for flag in UPLOAD_FLAGS) else 0


def default_value(spec: list):
    """What the ComfyUI frontend fills in for a widget that has no saved value (None if it has no default)."""
    opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    if "default" in opts:
        return opts["default"]
    options = spec[0] if isinstance(spec[0], list) else opts.get("options")
    return options[0] if options and isinstance(options[0], str) else None


def map_widgets(info: dict, values: list) -> dict:
    """widgets_values list -> {input_name: value} following object_info order.

    Widgets past the end of the saved list (the workflow predates the input) get their default, like the frontend does.
    """
    inputs, names = input_order(info)
    out: dict = {}
    queue = deque(values)

    def take(specs: dict, order: list[str], prefix: str):
        for name in order:
            spec = specs.get(name)
            if spec is None or not is_widget(spec):
                continue
            value = queue.popleft() if queue else default_value(spec)
            if value is not None:
                out[prefix + name] = value
            if spec[0] == "COMFY_DYNAMICCOMBO_V3":
                option = next((o for o in spec[1]["options"] if o["key"] == value), None)
                if option:
                    sub = {**option["inputs"].get("required", {}), **option["inputs"].get("optional", {})}
                    take(sub, list(sub), f"{prefix}{name}.")
            for _ in range(extra_values(name, spec, top_level=not prefix)):
                if queue:
                    queue.popleft()

    take(inputs, names, "")
    return out


# ---------- scopes (top level graph and each subgraph instance) ----------

def _links_table(raw_links: list) -> dict:
    """{link_id: (origin_id, origin_slot, target_id, target_slot)} for list or dict links."""
    table = {}
    for link in raw_links:
        if isinstance(link, dict):
            table[link["id"]] = (link["origin_id"], link["origin_slot"], link["target_id"], link["target_slot"])
        else:
            table[link[0]] = tuple(link[1:5])
    return table


class Scope:
    """One graph level: the workflow itself or one subgraph instance."""

    def __init__(self, nodes, links, defs, prefix="", sg=None, parent=None, host=None):
        self.nodes = {n["id"]: n for n in nodes}
        self.links = _links_table(links)
        self.defs = defs
        self.prefix = prefix
        self.sg, self.parent, self.host = sg, parent, host
        self.values = self._boundary_values() if sg else {}
        self.children = {nid: Scope(self.defs[n["type"]]["nodes"], self.defs[n["type"]].get("links", []), defs,
                                    f"{prefix}{nid}:", self.defs[n["type"]], self, n)
                         for nid, n in self.nodes.items() if n["type"] in defs}

    def _boundary_values(self) -> dict:
        """Widget values the parent stored on the subgraph node, by subgraph input index."""
        values = self.host.get("widgets_values") or []
        widget_idx = [i for i, inp in enumerate(self.sg["inputs"]) if inp["type"] in (*WIDGET_PRIMITIVES, "COMBO")]
        return dict(zip(widget_idx, values))

    def resolve_link(self, link_id: int):
        """Follow a link to a real node output or a literal value."""
        origin, slot, _, _ = self.links[link_id]
        return self.resolve_origin(origin, slot)

    def resolve_origin(self, origin: int, slot: int):
        if origin == INPUT_NODE:
            return self.resolve_boundary(slot)
        if origin in self.children:
            child = self.children[origin]
            for link_id, (_, _, target, tslot) in child.links.items():
                if target == OUTPUT_NODE and tslot == slot:
                    return child.resolve_link(link_id)
            return None
        return ("node", f"{self.prefix}{origin}", slot)

    def resolve_boundary(self, idx: int):
        """A subgraph input: the parent's link if connected, else the parent's widget value."""
        name = self.sg["inputs"][idx]["name"]
        host_input = next((i for i in self.host.get("inputs", []) if i["name"] == name), None)
        if host_input and host_input.get("link") is not None:
            return self.parent.resolve_link(host_input["link"])
        return ("value", self.values[idx]) if idx in self.values else None


@dataclass
class Flat:
    fid: str
    node: dict
    scope: Scope

    @property
    def type(self) -> str:
        return self.node["type"]

    @property
    def mode(self) -> int:
        return self.node.get("mode") or 0

    def describe(self) -> str:
        title = self.node.get("title")
        return f'node {self.fid} ({self.type}{f", titled {title!r}" if title else ""})'


def _collect(scope: Scope, flat: dict):
    for nid, node in scope.nodes.items():
        if nid in scope.children:
            if (node.get("mode") or 0) in (2, 4):
                raise ConvertError(f"Subgraph node {scope.prefix}{nid} is muted or bypassed, which isn't supported. {EXPORT_HINT}")
            _collect(scope.children[nid], flat)
        else:
            flat[f"{scope.prefix}{nid}"] = Flat(f"{scope.prefix}{nid}", node, scope)


# ---------- conversion ----------

def _input_source(f: Flat, key):
    """Resolved source of a node's input by name or position (None if unlinked)."""
    inputs = f.node.get("inputs") or []
    entry = inputs[key] if isinstance(key, int) and key < len(inputs) else next((i for i in inputs if i["name"] == key), None)
    if entry is None or entry.get("link") is None:
        return None
    return f.scope.resolve_link(entry["link"])


def _passthrough_input(f: Flat, slot: int):
    """For bypassed nodes: the input that feeds output `slot` (same type first, then same index)."""
    outputs, inputs = f.node.get("outputs") or [], f.node.get("inputs") or []
    want = outputs[slot]["type"] if slot < len(outputs) else None
    linked = [i for i, inp in enumerate(inputs) if inp.get("link") is not None]
    for i in linked:
        if inputs[i].get("type") == want:
            return i
    return slot if slot in linked else None


def _final_source(flat: dict, src, depth=0):
    """Resolve through muted / bypassed / reroute / primitive nodes."""
    while src is not None and src[0] == "node":
        if depth > 200:
            raise ConvertError("Loop detected while resolving links.")
        depth += 1
        f = flat.get(src[1])
        if f is None or f.mode == 2:
            return None
        if f.type == "PrimitiveNode":
            values = f.node.get("widgets_values") or []
            return ("value", values[0]) if values else None
        if f.type == "Reroute":
            src = _input_source(f, 0)
        elif f.mode == 4:
            idx = _passthrough_input(f, src[2])
            src = _input_source(f, idx) if idx is not None else None
        else:
            return src
    return src


def _is_dropped(f: Flat, object_info: dict) -> bool:
    if f.type in PREVIEW_TYPES and f.type not in object_info:
        return True
    return f.mode in (2, 4) or f.type in NOTE_TYPES | {"Reroute", "PrimitiveNode"}


def _convert_node(f: Flat, flat: dict, info: dict, warnings: list) -> dict:
    inputs: dict = {}
    values = f.node.get("widgets_values") or []
    if isinstance(values, dict):
        inputs.update(values)
    else:
        mapped = map_widgets(info, values)
        expected = len(mapped)
        if len(values) > expected + 2:   # a couple of unknown trailing UI-only values is normal
            warnings.append(f"{f.describe()}: {len(values)} widget values, only {expected} matched object_info")
        inputs.update(mapped)
    for entry in f.node.get("inputs") or []:
        if entry.get("link") is None:
            continue
        src = _final_source(flat, f.scope.resolve_link(entry["link"]))
        if src is None:
            inputs.pop(entry["name"], None)
        elif src[0] == "value":
            inputs[entry["name"]] = src[1]
        else:
            inputs[entry["name"]] = [src[1], src[2]]
    api = {"class_type": f.type, "inputs": inputs}
    if f.node.get("title"):
        api["_meta"] = {"title": f.node["title"]}
    return api


def convert(workflow: dict, object_info: dict) -> Converted:
    """UI-format workflow dict -> Converted(API graph, warnings). API input passes through."""
    if is_api_format(workflow):
        return Converted(workflow)
    if "nodes" not in workflow:
        raise ConvertError("This file is neither a ComfyUI UI-format nor API-format workflow.")

    defs = {sg["id"]: sg for sg in workflow.get("definitions", {}).get("subgraphs", [])}
    top = Scope(workflow["nodes"], workflow.get("links", []), defs)
    flat: dict[str, Flat] = {}
    _collect(top, flat)

    graph, warnings = {}, []
    for fid, f in flat.items():
        if _is_dropped(f, object_info):
            continue
        info = object_info.get(f.type)
        if info is None:
            raise ConvertError(f"{f.describe()} is not available in this ComfyUI (a custom node that isn't installed, "
                               f"or a UI-only node the converter doesn't know). {EXPORT_HINT}")
        graph[fid] = _convert_node(f, flat, info, warnings)
    return Converted(graph, warnings)


# ---------- runnable check ----------

def check_runnable(graph: dict, object_info: dict) -> list[dict]:
    """Missing files / nodes: combo values that aren't in object_info's option lists."""
    missing = []
    for nid, node in graph.items():
        info = object_info.get(node["class_type"])
        if info is None:
            missing.append({"node": nid, "class": node["class_type"], "input": None, "value": None, "problem": "node not installed"})
            continue
        specs = {**info["input"].get("required", {}), **info["input"].get("optional", {})}
        for name, value in node["inputs"].items():
            spec = specs.get(name)
            if spec is None or isinstance(value, list) or not isinstance(value, str):
                continue
            options = spec[0] if isinstance(spec[0], list) else (spec[1].get("options") if spec[0] == "COMBO" and len(spec) > 1 else None)
            if not options or value in options:
                continue
            if any(spec[1].get(flag) for flag in UPLOAD_FLAGS if len(spec) > 1):
                continue   # user-supplied input images aren't checked here
            missing.append({"node": nid, "class": node["class_type"], "input": name, "value": value, "problem": "file or option not found"})
    return missing
