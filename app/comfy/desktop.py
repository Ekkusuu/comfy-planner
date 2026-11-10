"""Launch arguments that Comfy Desktop uses for an install, so ComfyUI started by this app sees the same models.

Comfy Desktop keeps models, inputs and outputs in a shared folder (ComfyUI-Shared) and passes them to ComfyUI
on the command line. Without these arguments ComfyUI only sees the install's own models folder, and workflows
that use shared models look like they have missing files.
"""
import json
import os
import shlex
from pathlib import Path


def _desktop_install(install_dir: str | Path) -> tuple[Path, dict, dict] | None:
    """(Comfy Desktop's data folder, this install's entry, Desktop's settings), or None if it isn't a Desktop install."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    root = Path(appdata) / "Comfy Desktop"
    try:
        installs = json.loads((root / "installations.json").read_text(encoding="utf-8"))
        settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    install_dir = Path(install_dir).resolve()
    entry = next((i for i in installs if i.get("installPath") and Path(i["installPath"]).resolve() in (install_dir, install_dir.parent)), None)
    return (root, entry, settings) if entry else None


def model_dirs(install_dir: str | Path, folder: str) -> list[Path]:
    """Every folder ComfyUI reads `folder` models from (e.g. "loras"): the install's own, plus the shared
    folders listed in Comfy Desktop's extra-model-paths file. Folders that don't exist are left out."""
    install_dir = Path(install_dir)
    dirs = [install_dir / "models" / folder]
    found = _desktop_install(install_dir)
    if found:
        root, entry, _ = found
        yaml = root / "instance-model-paths" / f"{entry['id']}.yaml"
        if yaml.is_file():
            dirs += _yaml_model_dirs(yaml.read_text(encoding="utf-8", errors="replace"), folder)
    unique = {}
    for d in dirs:
        unique.setdefault(os.path.normcase(os.path.abspath(d)), d)
    return [d for d in unique.values() if d.is_dir()]


def _yaml_model_dirs(text: str, folder: str) -> list[Path]:
    """Read `base_path` and the `folder` entry from Desktop's extra-model-paths yaml (a flat file, so no yaml library)."""
    base, values, reading = None, [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if stripped.startswith("base_path:"):
            base = stripped.split(":", 1)[1].strip().strip("'\"")
        elif reading and line.startswith("    "):
            values.append(stripped)
        else:
            reading = False
            key, _, rest = stripped.partition(":")
            if key.strip("'\"") == folder:
                rest = rest.strip()
                if rest.startswith("|"):
                    reading = True
                elif rest:
                    values.append(rest.strip("'\""))
    return [Path(base) / v for v in values] if base else []


def desktop_launch_args(install_dir: str | Path) -> list[str]:
    """Extra ComfyUI arguments for `install_dir` (the folder with main.py), or [] if it isn't a Comfy Desktop install."""
    found = _desktop_install(install_dir)
    if not found:
        return []
    root, entry, settings = found
    args = []
    model_paths = root / "instance-model-paths" / f"{entry['id']}.yaml"
    if model_paths.is_file():
        args += ["--extra-model-paths-config", str(model_paths)]
    if settings.get("inputDir"):
        args += ["--input-directory", settings["inputDir"]]
    if settings.get("outputDir"):
        args += ["--output-directory", settings["outputDir"]]
    args += shlex.split(entry.get("launchArgs") or "", posix=False)
    return args
