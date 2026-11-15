"""The app's version number, from one source: the `version` in pyproject.toml.

An installed copy has no pyproject.toml worth reading: scripts/build_release.ps1 writes the number into a VERSION file in the
program folder, and that file wins. A checkout (development) reads pyproject.toml. If neither can be read it is "0.0.0"."""
import re
import tomllib
from pathlib import Path

from app.config import ROOT

UNKNOWN = "0.0.0"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?$")


def read_version(root: Path = ROOT) -> str:
    """The version for the program folder `root`: its VERSION file, else its pyproject.toml, else "0.0.0"."""
    try:
        text = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
        if _SEMVER.match(text):
            return text
    except OSError:
        pass
    try:
        text = str(tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get("project", {}).get("version", ""))
        if _SEMVER.match(text):
            return text
    except (OSError, ValueError):
        pass
    return UNKNOWN


__version__ = read_version()
