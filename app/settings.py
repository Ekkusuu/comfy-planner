"""Editing config/app.toml from the UI: validate a change, write it keeping the file's comments and layout, reload.

The file is the source of truth; `apply` then copies the freshly loaded values into the live Config so the rest of the
app (which holds that same object) sees them without a restart. host, port and data_dir are bound by the launcher and
are never written here."""
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import tomlkit

from app.config import ROOT, Config, load_config

# config field -> (toml table, toml key). llm_models is handled on its own ([llm.models]).
KEYS = {
    "comfy_url": ("comfy", "url"), "comfy_install_dir": ("comfy", "install_dir"), "comfy_cli": ("comfy", "cli"),
    "max_images_per_job": ("jobs", "max_images_per_job"), "max_images_per_request": ("jobs", "max_images_per_request"),
    "hires_default": ("jobs", "hires_default"),
    "max_loras": ("jobs", "max_loras"), "safety_negative": ("jobs", "safety_negative"),
    "strip_from_default_negative": ("jobs", "strip_from_default_negative"),
    "allow_draft_cards": ("cards", "allow_draft_cards"), "check_updates_at_start": ("app", "check_updates_at_start"),
    "wildcards_enabled": ("wildcards", "enabled"), "wildcards_ideas_per_prompt": ("wildcards", "ideas_per_prompt"),
    "wildcards_llm_picks_lists": ("wildcards", "llm_picks_lists"), "embeddings_enabled": ("embeddings", "enabled"), "embeddings_model": ("embeddings", "model"),
    "wildcards_entries_per_list": ("wildcards", "entries_per_list"), "wildcards_max_idea_entries": ("wildcards", "max_idea_entries"),
    "llm_binary": ("llm", "binary"), "llm_model": ("llm", "model"), "llm_ctx_size": ("llm", "ctx_size"),
    "llm_gpu_layers": ("llm", "gpu_layers"), "llm_enable_thinking": ("llm", "enable_thinking"),
    "llm_temperature": ("llm", "temperature"), "llm_max_tokens": ("llm", "max_tokens"),
    "llm_start_timeout": ("llm", "start_timeout"), "llm_stop_timeout": ("llm", "stop_timeout"),
    "llm_keep_loaded_seconds": ("llm", "keep_loaded_seconds"),
    "llm_extra_args": ("llm", "extra_args"),
}
EDITABLE = set(KEYS) | {"llm_models"}
INT_RANGES = {"max_images_per_job": (1, 100), "max_images_per_request": (1, 2000), "max_loras": (0, 10), "llm_ctx_size": (512, 262144),
              "llm_gpu_layers": (0, 999), "llm_max_tokens": (64, 65536),
              "wildcards_ideas_per_prompt": (0, 10), "wildcards_entries_per_list": (1, 30), "wildcards_max_idea_entries": (0, 1000)}
FLOAT_RANGES = {"llm_temperature": (0, 2), "llm_start_timeout": (10, 3600), "llm_stop_timeout": (1, 300), "llm_keep_loaded_seconds": (0, 3600)}
BOOLS = {"check_updates_at_start", "hires_default", "allow_draft_cards", "llm_enable_thinking", "wildcards_enabled", "wildcards_llm_picks_lists", "embeddings_enabled"}
LISTS = {"safety_negative", "strip_from_default_negative", "llm_extra_args"}
PATHS = {"comfy_cli", "llm_binary", "embeddings_model"}          # non-empty text


class SettingsError(ValueError):
    """`errors` maps a setting name to what is wrong with it."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(errors.values()))


def validate(patch: dict, current: Config) -> dict:
    """The patch with every value checked and cleaned. Raises SettingsError listing all problems at once."""
    errors: dict[str, str] = {}
    out: dict = {}
    for name in patch:
        if name not in EDITABLE:
            errors[name] = "This setting can't be changed here." if name in {"host", "port", "data_dir"} else "Unknown setting."
    for name, value in patch.items():
        if name in errors:
            continue
        try:
            out[name] = _clean(name, value)
        except ValueError as e:
            errors[name] = str(e)
    models = out.get("llm_models", {n: str(p) for n, p in current.llm_models.items()})
    chosen = out.get("llm_model", current.llm_model)
    if ("llm_model" in out or "llm_models" in out) and chosen not in models and "llm_model" not in errors and "llm_models" not in errors:
        errors["llm_model" if "llm_model" in out else "llm_models"] = f"There is no model called '{chosen}'. Pick one of: {', '.join(models) or 'none'}."
    if errors:
        raise SettingsError(errors)
    return out


def _clean(name: str, value):
    if name == "comfy_url":
        url = _text(value, "Enter the address of ComfyUI, like http://127.0.0.1:8188.").rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("Use a full address starting with http:// or https://.")
        return url
    if name == "comfy_install_dir":
        folder = value.strip() if isinstance(value, str) else None
        if folder is None:
            raise ValueError("Enter a folder path.")
        if folder and not Path(folder).is_dir():
            raise ValueError("That folder doesn't exist.")
        return folder
    if name in PATHS:
        return _text(value, "This can't be empty.")
    if name == "llm_model":
        return _text(value, "Pick a model.")
    if name == "llm_models":
        if not isinstance(value, dict) or not value:
            raise ValueError("Keep at least one model.")
        return {_text(k, "A model needs a name."): _text(v, "Enter the path of the model file.") for k, v in value.items()}
    if name in INT_RANGES:
        lo, hi = INT_RANGES[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Enter a whole number.")
        if not lo <= value <= hi:
            raise ValueError(f"Enter a number from {lo} to {hi}.")
        return value
    if name in FLOAT_RANGES:
        lo, hi = FLOAT_RANGES[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Enter a number.")
        if not lo <= value <= hi:
            raise ValueError(f"Enter a number from {lo} to {hi}.")
        return float(value)
    if name in BOOLS:
        if not isinstance(value, bool):
            raise ValueError("Choose on or off.")
        return value
    if name in LISTS:
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError("Enter a list of text items.")
        seen: list[str] = []
        for item in (x.strip() for x in value):
            if item and item not in seen:
                seen.append(item)
        return seen
    raise ValueError("Unknown setting.")


def _text(value, empty_message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(empty_message)
    return value.strip()


def write(path: Path, changes: dict):
    """Put the changed values into the toml file, leaving comments, blank lines and key order alone."""
    raw = path.read_bytes().decode("utf-8") if path.exists() else ""
    doc = tomlkit.parse(raw)          # parsed from bytes-decoded text, so CRLF line endings survive
    for name, value in changes.items():
        if name == "llm_models":
            table = _table(doc, "llm", "models")
            for gone in [k for k in table if k not in value]:
                del table[gone]
            for k, v in value.items():
                if k not in table or not _same(table[k], v, True):
                    table[k] = v
            continue
        table_name, key = KEYS[name]
        table = _table(doc, table_name)
        if key in table and _same(table[key], value, name in ("llm_binary", "embeddings_model")):
            continue
        table[key] = value
    text = tomlkit.dumps(doc)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".app.toml.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _table(doc, *names):
    node = doc
    for n in names:
        if n not in node:
            node[n] = tomlkit.table()
        node = node[n]
    return node


def _plain(item):
    return item.unwrap() if hasattr(item, "unwrap") else item


def _same(old, new, is_path: bool) -> bool:
    """Equal values, or (for file paths) the same file written relative vs absolute."""
    old = _plain(old)
    if is_path and isinstance(old, str) and isinstance(new, str):
        return (ROOT / old).resolve() == (ROOT / new).resolve()
    return old == new


def apply(live: Config, path: Path) -> list[str]:
    """Reload the file and copy every editable value into the live Config (host, port and data_dir stay as launched). Returns the names that changed."""
    fresh = load_config(path)
    changed = []
    for name in sorted(EDITABLE):
        new = getattr(fresh, name)
        if getattr(live, name) != new:
            setattr(live, name, new)
            changed.append(name)
    return changed


def shown(path: Path) -> str:
    """A file path as the UI shows it: relative to the project folder when it lives inside it."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def model_status(cfg: Config) -> list[dict]:
    """Each configured LLM model with whether its file exists, for the UI."""
    return [{"name": n, "path": shown(p), "found": Path(p).is_file()} for n, p in cfg.llm_models.items()]

