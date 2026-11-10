"""Cached downscaled copies of generated images (data/thumbs/), for the gallery grid and workflow covers."""
import os
import warnings
from pathlib import Path

from PIL import Image

from app.config import Config

MIN_SIZE, MAX_SIZE, DEFAULT_SIZE = 64, 1024, 512


def clamp_size(size: int) -> int:
    return max(MIN_SIZE, min(MAX_SIZE, size))


def thumb_path(config: Config, image_id: int, size: int) -> Path:
    return config.thumbs_dir / f"{image_id}-{size}.webp"


def ensure_thumb(config: Config, image_id: int, source: Path, size: int) -> Path:
    """Path of the thumbnail (longest edge `size`), made now if it's missing or older than the source file
    (a retried image reuses its file name). Blocking: call it from a thread."""
    target = thumb_path(config, image_id, size)
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    write_thumb(source, target, size)
    return target


class BadImage(Exception):
    """The bytes or file aren't a picture this app accepts (not an image, too large in pixels, an unsupported format)."""


def write_thumb(source, target: Path, size: int, formats: tuple[str, ...] | None = None):
    """Decode `source` (a path or a file object) with Pillow and write a WebP of it, longest edge `size`, to `target`.
    The original bytes are never kept. With `formats` (Pillow format names), anything else is refused as BadImage;
    every decode problem becomes BadImage too. Blocking: call it from a thread."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)     # a huge pixel count is an error, not a warning
            with Image.open(source) as img:
                if formats is not None and img.format not in formats:
                    raise BadImage(f"Not a {'/'.join(formats)} picture ({img.format}).")
                img.thumbnail((size, size), Image.LANCZOS, reducing_gap=2.0)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA" if "A" in img.mode or "transparency" in img.info else "RGB")
                img.save(tmp, "WEBP", quality=82, method=4)
    except BadImage:
        tmp.unlink(missing_ok=True)
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        tmp.unlink(missing_ok=True)
        raise BadImage(f"Couldn't read the picture ({type(e).__name__}).") from e
    os.replace(tmp, target)


def remove_thumbs(config: Config, image_id: int):
    for path in config.thumbs_dir.glob(f"{image_id}-*.webp"):
        path.unlink(missing_ok=True)
