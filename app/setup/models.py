"""Checking a language model file the person points at (a GGUF file, the format llama.cpp loads)."""
import os
from pathlib import Path

from app.config import ROOT

GGUF_MAGIC = b"GGUF"       # the first four bytes of every GGUF file


def check_model_file(text: str, root: Path = ROOT) -> Path:
    """The file `text` names, if it exists and starts with the GGUF marker. A relative path starts in the app folder.
    Raises ValueError with a message for the person otherwise."""
    raw = text.strip().strip('"').strip("'").strip()
    if not raw:
        raise ValueError("Enter the path of a .gguf model file.")
    path = Path(os.path.expandvars(raw)).expanduser()
    if not path.is_absolute():
        path = root / path
    if path.is_dir():
        raise ValueError("That is a folder. Enter the path of the .gguf file inside it.")
    if not path.is_file():
        raise ValueError("That file doesn't exist. Check the path, or put the file in the models folder first.")
    try:
        with path.open("rb") as f:
            magic = f.read(len(GGUF_MAGIC))
    except OSError as e:
        raise ValueError(f"That file can't be read: {e.strerror or e}.") from e
    if magic != GGUF_MAGIC:
        raise ValueError("That isn't a GGUF model file. Models for llama.cpp are .gguf files.")
    return path


def check_model_name(text: str) -> str:
    """A name for the model (any text up to 60 characters; it is how the model is listed in Settings)."""
    name = " ".join(text.split())
    if not name:
        raise ValueError("Give the model a name.")
    if len(name) > 60 or not name.isprintable():
        raise ValueError("Use a shorter name, up to 60 letters or numbers.")
    return name
