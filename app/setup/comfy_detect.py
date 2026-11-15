"""Find ComfyUI installs on this computer, so the setup can offer them. Only looks at a few well-known places; never scans the disk."""
import json
import os
from pathlib import Path


def is_comfyui_folder(folder: Path) -> bool:
    """The folder that holds ComfyUI's main.py (the one the app reads models and workflows from)."""
    return (folder / "main.py").is_file()


def resolve_install_dir(folder: Path) -> Path | None:
    """The ComfyUI folder for something the person typed: the folder itself, or the ComfyUI folder inside it (as in a portable download)."""
    for candidate in (folder, folder / "ComfyUI"):
        if is_comfyui_folder(candidate):
            return candidate
    return None


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _desktop_installs() -> list[Path]:
    """Installs that Comfy Desktop lists, plus the ones in its default folder."""
    found: list[Path] = []
    local, appdata = _env_path("LOCALAPPDATA"), _env_path("APPDATA")
    if appdata:
        try:
            for entry in json.loads((appdata / "Comfy Desktop" / "installations.json").read_text(encoding="utf-8")):
                if isinstance(entry, dict) and isinstance(entry.get("installPath"), str):
                    base = Path(entry["installPath"])
                    found += [base / "ComfyUI", base]
        except (OSError, ValueError):
            pass
    if local:
        root = local / "Comfy-Desktop" / "ComfyUI-Installs"
        try:
            for install in sorted(root.iterdir()):
                found += [install / "ComfyUI", install]
        except OSError:
            pass
    return found


def _portable_installs() -> list[Path]:
    """The usual places a portable download is unpacked."""
    home = _env_path("USERPROFILE") or Path.home()
    parents = [home, home / "Desktop", home / "Downloads", home / "Documents", home / "OneDrive" / "Desktop", Path("C:/"), Path("D:/")]
    names = ["ComfyUI_windows_portable", "ComfyUI"]
    found: list[Path] = []
    for parent in parents:
        for name in names:
            found += [parent / name / "ComfyUI", parent / name]
    return found


def find_installs() -> list[dict]:
    """[{path, kind}]: every ComfyUI folder found, Comfy Desktop first, without repeats."""
    seen: set[str] = set()
    out: list[dict] = []
    for kind, folders in (("Comfy Desktop", _desktop_installs()), ("Portable", _portable_installs())):
        for folder in folders:
            try:
                ok = is_comfyui_folder(folder)
                key = os.path.normcase(str(folder.resolve()))
            except OSError:
                continue
            if ok and key not in seen:
                seen.add(key)
                out.append({"path": str(folder), "kind": kind})
    return out
