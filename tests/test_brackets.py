"""Literal brackets: escaped only at the last moment, only where the encoder parses weights."""
import pytest

from app.comfy.brackets import parses_weights, protect_literal_brackets as protect
from app.comfy.graph import build_graph
from tests.conftest import KREA, QWEN, TAGWF, TAGWF_LORA

CASES = [
    # (text, expected)
    ("((Eevee)) with ((tan body)) and (fluffy tail),", "((Eevee)) with ((tan body)) and (fluffy tail),"),    # the user's style lists: emphasis
    ("girl ((red hair))", "girl ((red hair))"),                                # doubled: nobody writes a qualifier like that
    ("@marlow (blue sedge)", r"@marlow \(blue sedge\)"),
    ("aria vale (starlight), 1girl", r"aria vale \(starlight\), 1girl"),
    ("(red scarf:1.3), 1girl", "(red scarf:1.3), 1girl"),                     # a number: a real weight
    ("1girl, (red scarf), forest", "1girl, (red scarf), forest"),             # bare at the start of a tag: emphasis
    ("(red scarf), 1girl", "(red scarf), 1girl"),                              # start of text
    ("1girl,\n(red scarf)", "1girl,\n(red scarf)"),                            # start of a line
    ("(aria vale (starlight):1.2)", r"(aria vale \(starlight\):1.2)"),     # inner literal inside a weight
    ("((red scarf))", "((red scarf))"),                                        # nested emphasis
    ("((red scarf:1.2))", "((red scarf:1.2))"),
    ("(red scarf (silk):1.5), 1girl", r"(red scarf \(silk\):1.5), 1girl"),
    (r"artist \(artist\), name", r"artist \(artist\), name"),                  # already escaped: unchanged
    (r"(a:1.2) \(b\)", r"(a:1.2) \(b\)"),
    ("a girl (with a scarf) walking", r"a girl \(with a scarf\) walking"),    # an aside in a sentence
    ("name (qualifier: text)", r"name \(qualifier: text\)"),                  # a colon, but no number
    ("1girl, (smile", r"1girl, \(smile"),                                      # unbalanced
    ("1girl, smile)", r"1girl, smile\)"),
    (":)", r":\)"),
    ("1girl, ()", r"1girl, \(\)"),                                             # empty
    ("(a, b:1.2), c (d)", r"(a, b:1.2), c \(d\)"),
    ("1girl, name (x), (emphasis), name2 (y)", r"1girl, name \(x\), (emphasis), name2 \(y\)"),
    ("no brackets here", "no brackets here"),
    ("", ""),
]


@pytest.mark.parametrize("text,expected", CASES)
def test_protect_cases(text, expected):
    assert protect(text) == expected


@pytest.mark.parametrize("text,_", CASES)
def test_protect_is_idempotent(text, _):
    once = protect(text)
    assert protect(once) == once


def test_none_stays_none():
    assert protect(None) is None


# ---------- which workflows parse ----------

def test_family_table(graphs):
    assert parses_weights(graphs[TAGWF]) and parses_weights(graphs[TAGWF_LORA])
    assert not parses_weights(graphs[KREA])           # CLIPLoader type krea2
    assert not parses_weights(graphs[QWEN])           # TextEncodeQwenImage21


def test_unknown_encoder_parses():
    graph = {"1": {"class_type": "CLIPLoader", "inputs": {"clip_name": "x", "type": "some_new_type"}},
             "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "x", "clip": ["1", 0]}}}
    assert parses_weights(graph)
    graph["1"]["inputs"]["type"] = "krea2"
    assert not parses_weights(graph)
    assert parses_weights({})


def test_lora_clip_passthrough_is_followed():
    graph = {"1": {"class_type": "CLIPLoader", "inputs": {"clip_name": "x", "type": "krea2"}},
             "3": {"class_type": "LoraLoader", "inputs": {"clip": ["1", 0], "model": ["9", 0]}},
             "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "x", "clip": ["3", 1]}}}
    assert not parses_weights(graph)


# ---------- in the graph ----------

TEXT = r"@marlow (blue sedge), (red scarf:1.3), aria vale (starlight)"


def test_anima_graph_gets_escaped_text_and_negative(graphs, slot_maps):
    g = build_graph(graphs[TAGWF], slot_maps[TAGWF], {"prompt": TEXT, "negative": "blurry (bad hands)"}, [])
    assert g["6"]["inputs"]["text"] == r"@marlow \(blue sedge\), (red scarf:1.3), aria vale \(starlight\)"
    assert g["7"]["inputs"]["text"] == r"blurry \(bad hands\)"


def test_krea_and_qwen_graphs_keep_the_text_as_it_is(graphs, slot_maps):
    k = build_graph(graphs[KREA], slot_maps[KREA], {"prompt": TEXT}, [])
    assert k[slot_maps[KREA]["positive"]["node"]]["inputs"][slot_maps[KREA]["positive"]["input"]] == TEXT
    q = build_graph(graphs[QWEN], slot_maps[QWEN], {"prompt": TEXT, "negative": "x (y)", "input_images": ["a.png"]}, [])
    pos, neg = slot_maps[QWEN]["positive"], slot_maps[QWEN]["negative"]
    assert q[pos["node"]]["inputs"][pos["input"]] == TEXT and q[neg["node"]]["inputs"][neg["input"]] == "x (y)"
    assert "\\" not in str(q[pos["node"]]["inputs"])


def test_recipe_and_stored_text_are_not_changed(graphs, slot_maps):
    recipe = {"prompt": TEXT, "negative": "blurry (bad hands)"}
    build_graph(graphs[TAGWF], slot_maps[TAGWF], recipe, [])
    assert recipe == {"prompt": TEXT, "negative": "blurry (bad hands)"}
