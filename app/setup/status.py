"""What is set up and what is missing: one item per thing the app needs, for the setup page and the check at start-up."""
from pathlib import Path

from app import settings
from app.comfy.client import ComfyError
from app.config import Config
from app.setup import catalog
from app.setup.comfy_detect import is_comfyui_folder


def item(id: str, label: str, state: str, detail: str, size_bytes: int | None = None) -> dict:
    out = {"id": id, "label": label, "state": state, "detail": detail}
    if size_bytes is not None:
        out["size_bytes"] = size_bytes
    return out


def taglib_missing(config: Config) -> dict[str, int]:
    """The parts of the tag library that are not there yet, by name, with the download size of each."""
    missing: dict[str, int] = {}
    folder = catalog.taglib_folder(config.data_dir)
    if not all((folder / f).is_file() for f in catalog.TAGLIB_FILES):
        missing["taglib"] = catalog.TAGLIB_ESTIMATE
    if not (catalog.contradictions_folder(config.data_dir) / catalog.CONTRADICTIONS_FILE).is_file():
        missing["contradictions"] = catalog.TAG_COUNTS.size + catalog.COOCCURRENCE.size
    return missing


async def comfy_reachable(comfy) -> bool:
    try:
        await comfy.system_stats()
        return True
    except ComfyError:
        return False


async def compute(config: Config, comfy) -> list[dict]:
    """The status items, in the order the setup page shows them (the install state of the moment is added by the manager)."""
    reachable = await comfy_reachable(comfy)
    folder = Path(config.comfy_install_dir) if config.comfy_install_dir else None
    folder_ok = bool(folder) and is_comfyui_folder(folder)
    if reachable:
        comfy_item = item("comfyui", "ComfyUI", "ok", f"Running at {config.comfy_url}")
    elif folder_ok:
        comfy_item = item("comfyui", "ComfyUI", "missing", f"ComfyUI is installed but not running at {config.comfy_url}. Start it to use the app.")
    elif folder:
        comfy_item = item("comfyui", "ComfyUI", "missing", "The ComfyUI folder you set doesn't contain main.py. Choose the folder that does.")
    else:
        comfy_item = item("comfyui", "ComfyUI", "missing", "ComfyUI wasn't found. Choose its folder, or install ComfyUI first.")

    if config.llm_binary.is_file():
        server = item("llama_server", "Language model program (llama.cpp)", "ok", settings.shown(config.llm_binary))
    else:
        server = item("llama_server", "Language model program (llama.cpp)", "missing",
                      f"Runs the language model on your graphics card. llama.cpp {catalog.LLAMA_TAG} for Windows with CUDA.",
                      catalog.LLAMA_ZIP.size + catalog.CUDART_ZIP.size)

    found = [name for name, path in config.llm_models.items() if Path(path).is_file()]
    if found:
        note = "" if config.llm_model in found else f" (The chosen model, {config.llm_model}, has no file yet.)"
        model = item("llm_model", "Language model", "ok", f"{', '.join(found)}{note}")
    else:
        model = item("llm_model", "Language model", "missing", "Choose a GGUF model file on your computer. The app doesn't download one.")

    if not config.embeddings_enabled:
        embeddings = item("embeddings_model", "Search by meaning model", "ok", "Turned off in Settings.")
    elif config.embeddings_model.is_file():
        embeddings = item("embeddings_model", "Search by meaning model", "ok", settings.shown(config.embeddings_model))
    else:
        embeddings = item("embeddings_model", "Search by meaning model", "missing",
                          "A small model that finds wildcard lists and LoRAs by meaning. Runs on the processor.", catalog.EMBEDDINGS_MODEL.size)

    missing = taglib_missing(config)
    if missing:
        tags = item("tag_library", "Tag library", "missing", "Real tags and the tags that don't go together, so prompts use proper tag names.", sum(missing.values()))
    else:
        tags = item("tag_library", "Tag library", "ok", settings.shown(catalog.taglib_folder(config.data_dir)))
    return [comfy_item, server, model, embeddings, tags]


def needs_setup(items: list[dict], config: Config) -> bool:
    """Whether the app should open the setup by itself: a language model program or model is missing, or ComfyUI is neither
    running nor set up to be started (someone who already chose their ComfyUI folder is not nagged because ComfyUI is off)."""
    state = {i["id"]: i["state"] for i in items}
    comfy_unknown = state["comfyui"] != "ok" and not (config.comfy_install_dir and is_comfyui_folder(Path(config.comfy_install_dir)))
    return comfy_unknown or state["llama_server"] != "ok" or state["llm_model"] != "ok"
