"""Read a .safetensors file's JSON header (metadata and tensor names) without loading any weights."""
import hashlib
import json
import re
import struct
from collections import Counter
from pathlib import Path

MAX_HEADER = 64 * 1024 * 1024    # real headers are a few MB; refuse anything absurd
CHUNK = 4 * 1024 * 1024
KEEP_KEYS = ("modelspec.title", "modelspec.architecture", "modelspec.resolution", "modelspec.date", "ss_base_model_version",
             "ss_sd_model_name", "ss_output_name", "ss_network_module", "ss_network_dim", "ss_network_alpha",
             "ss_num_train_images", "ss_epoch", "ss_steps", "ss_learning_rate", "ss_training_comment")


MAX_TAG_WORDS = 5                         # a booru tag is a few words; longer pieces are sentences cut at their commas
SENTENCE_END = re.compile(r"\.(\s|$)")     # "... plain background. on the left", "... in her hand."


class SafetensorsError(Exception):
    """Not a readable safetensors file."""


def read_header(path: Path | str) -> dict:
    """The parsed JSON header: {tensor name: {...}, "__metadata__": {str: str}}. Reads 8 bytes + the header only."""
    path = Path(path)
    try:
        with path.open("rb") as f:
            raw = f.read(8)
            if len(raw) < 8:
                raise SafetensorsError(f"{path.name} is too short to be a safetensors file.")
            (length,) = struct.unpack("<Q", raw)
            if length > MAX_HEADER or length > path.stat().st_size - 8:
                raise SafetensorsError(f"{path.name} has an invalid safetensors header length ({length}).")
            return json.loads(f.read(length))
    except (OSError, ValueError) as e:
        raise SafetensorsError(f"Could not read {path.name}: {e}") from e


def sha256_file(path: Path | str) -> str:
    """SHA256 of the whole file, streamed in chunks (this is the hash Civitai indexes)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _json_or_text(value):
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def is_caption_text(piece: str) -> bool:
    """Is this piece of ss_tag_frequency a bit of a sentence caption rather than a tag? Captions are split at their commas
    when the file is trained, so a piece is caption text when it ends a sentence (". " inside or "." at the end) or has more than 5 words."""
    return bool(SENTENCE_END.search(piece)) or len(piece.split()) > MAX_TAG_WORDS


def summarize(header: dict, top_tags: int = 40, max_candidates: int = 120) -> dict:
    """A short, plain digest of the header for the card builder (the raw tag counts can be huge)."""
    meta = header.get("__metadata__") or {}
    tensors = [k for k in header if k != "__metadata__"]
    prefixes = Counter(k.split(".")[0] for k in tensors)

    tags: Counter = Counter()
    caption_starts: Counter = Counter()
    datasets: list[str] = []
    for name, counts in (_json_or_text(meta.get("ss_tag_frequency", "")) or {}).items():
        if isinstance(counts, dict):
            for tag, n in counts.items():
                if not isinstance(n, int) or not tag.strip():
                    continue
                if not is_caption_text(tag):
                    tags[tag] += n
                elif tag.startswith("@") and len(first := tag.split()[0].rstrip(".,;:")) > 1:
                    caption_starts[first] += n          # "@cedar. aria vale" starts with the word "@cedar"
            head, _, rest = name.partition("_")
            datasets.append(rest if head.isdigit() and rest else name)
    at_tags = [t for t, _ in tags.most_common() if t.startswith("@")]
    return {
        "has_metadata": bool(meta),
        "info": {k: meta[k] for k in KEEP_KEYS if meta.get(k) not in (None, "", "None")},
        "tensor_count": len(tensors),
        "tensor_prefixes": [p for p, _ in prefixes.most_common(5)],
        "tensor_name_sample": sorted(tensors)[:4],
        "top_tags": [t for t, _ in tags.most_common(top_tags)],
        "style_tags": at_tags[:max_candidates],                 # "@artist" style selectors, if the LoRA has them
        "caption_starts": [t for t, _ in caption_starts.most_common(max_candidates)],   # "@word" a caption begins with: a trigger word, not a style
        "dataset_names": datasets[:max_candidates],             # training folder names ("10_mychar" -> "mychar")
    }
