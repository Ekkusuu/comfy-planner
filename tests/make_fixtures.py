"""Keep tests/fixtures neutral.

The workflow fixtures in tests/fixtures are synthetic: graph shapes that ComfyUI's common nodes make (a subgraph text-to-image workflow, a
tag-prompt workflow with a hires pass, the same with a LoRA loader, a workflow whose model files are not installed, an image-edit
workflow), with neutral titles, model file names and notes. They are maintained by hand and have no upstream.

What this script rebuilds is `object_info.json`: it asks a running ComfyUI for its node classes, keeps only the classes the fixtures use
(plus a few extra ones), and runs `neutralise` over the result so that no model file name of the machine it ran on is stored: every model
file name that is not one of the neutral names below becomes `other_model_<n>` with its own extension.

    uv run python tests/make_fixtures.py
"""
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"
EXTRA_CLASSES = ["KSampler", "KSamplerAdvanced", "LoraLoader", "LoraLoaderModelOnly"]

# The model file names the fixtures and tests use; everything else a live install lists is renamed.
NEUTRAL_MODELS = {
    "nl_diffusion_int8.safetensors", "nl_text_encoder.safetensors", "shared_vae.safetensors", "shared_vae (1).safetensors",
    "edit_diffusion_int8.safetensors", "edit_text_encoder.safetensors", "edit_vae_bf16.safetensors", "edit_prompt_enhancer_int8.safetensors",
    "tagmodel_base_v14.safetensors", "tagmodel_text_v14.safetensors", "tagmodel_photo_v1.safetensors", "small_text_encoder.safetensors",
    "missing_unet_00012_.safetensors", "upscaler_4x_a.safetensors", "upscaler_4x_b.safetensors", "upscaler_4x_a.pth",
    "krea2_inkbrush.safetensors", "model_styles_anima-base1_v6.safetensors",
    "mesh_model_a_int8.safetensors", "mesh_model_b_int8.safetensors", "mesh_model_c_int8.safetensors",
    "mesh_shape_vae_bf16.safetensors", "mesh_texture_vae_bf16.safetensors",
}
MODEL_FILE = re.compile(r"[\w .()+\-/]+\.(safetensors|pth|ckpt|pt|gguf)", re.I)


def neutralise(value, renamed: dict | None = None):
    """`value` (any JSON structure) with every model file name outside NEUTRAL_MODELS replaced by `other_model_<n>.<ext>`, keys included."""
    renamed = {} if renamed is None else renamed
    if isinstance(value, str):
        if MODEL_FILE.fullmatch(value) and value not in NEUTRAL_MODELS:
            ext = value.rsplit(".", 1)[1]
            return renamed.setdefault(value, f"other_model_{len(renamed) + 1}.{ext}")
        return value
    if isinstance(value, list):
        return [neutralise(v, renamed) for v in value]
    if isinstance(value, dict):
        return {neutralise(k, renamed): neutralise(v, renamed) for k, v in value.items()}
    return value


def classes_used() -> set[str]:
    """The node classes of the workflow fixtures (UI format, subgraphs included) and the API-format ones."""
    used = set(EXTRA_CLASSES)

    def walk(node):
        if isinstance(node, dict):
            kind = node.get("type") or node.get("class_type")
            if isinstance(kind, str):
                used.add(kind)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for path in FIXTURES.rglob("*.json"):
        if path.name != "object_info.json":
            walk(json.loads(path.read_text(encoding="utf-8")))
    return used


async def main():
    from app.comfy.client import ComfyClient
    from app.config import load_config

    cfg = load_config()
    comfy = ComfyClient(cfg.comfy_url)
    oi = await comfy.object_info()
    await comfy.close()
    trimmed = neutralise({k: oi[k] for k in sorted(classes_used()) if k in oi})
    (FIXTURES / "object_info.json").write_text(json.dumps(trimmed), encoding="utf-8")
    print(f"{len(trimmed)} node classes, {(FIXTURES / 'object_info.json').stat().st_size // 1024} KB")


if __name__ == "__main__":
    asyncio.run(main())
