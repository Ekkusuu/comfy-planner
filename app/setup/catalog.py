"""What the first-run setup can install: each pinned file with its address, size and SHA-256.

Every hash was checked against the bytes of a real download. Where each one comes from:
  llama.cpp zips   the "digest" field GitHub's release API gives each asset (GET /repos/ggml-org/llama.cpp/releases/tags/b11960)
  embeddings model Hugging Face's own LFS hash for the file (the same value as the file the app was developed with)
  tag counts       Hugging Face's LFS hashes for the two dataset files (the same values as the files the contradiction data was built from)
To move to another release, change the tag, names, sizes and hashes together, and nothing else."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Asset:
    name: str           # the file name on disk
    url: str
    size: int           # exact size in bytes
    sha256: str         # lower-case hex


LLAMA_TAG = "b11960"
LLAMA_RELEASE = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}"
# CUDA 12.4 build: the same CUDA 12 generation as the files the app was developed with (cudart64_12.dll, cublas64_12.dll).
LLAMA_ZIP = Asset(f"llama-{LLAMA_TAG}-bin-win-cuda-12.4-x64.zip", f"{LLAMA_RELEASE}/llama-{LLAMA_TAG}-bin-win-cuda-12.4-x64.zip",
                  279_763_224, "8d9d819261de10cc2f03921b8b79c779c5c458e21edb60c170ee30a7d462d684")
# The CUDA runtime libraries that go with it, so the computer needs no CUDA toolkit of its own.
CUDART_ZIP = Asset("cudart-llama-bin-win-cuda-12.4-x64.zip", f"{LLAMA_RELEASE}/cudart-llama-bin-win-cuda-12.4-x64.zip",
                   391_443_627, "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6")
CUDART_FILES = {"cudart64_12.dll", "cublas64_12.dll", "cublasLt64_12.dll"}      # the only files taken from the second zip
# Where things go, relative to the app folder.
LLAMA_FOLDER = Path("tools") / "llama.cpp" / LLAMA_TAG
LLAMA_BINARY = LLAMA_FOLDER / "llama-server.exe"

# Pinned to a commit of the Hugging Face repository, so the address always gives these exact bytes.
EMBEDDINGS_MODEL = Asset("embeddinggemma-300M-Q8_0.gguf",
                         "https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/0f741b5a6585bd53aeb15cd1372c56f2a0f65e12/embeddinggemma-300M-Q8_0.gguf",
                         333_590_944, "b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63")
EMBEDDINGS_FOLDER = Path("models") / "embeddings" / "embeddinggemma-300m"

# The public tag counts that scripts/build_contradictions.py reads (dataset commit fdf2772...).
_COUNTS = "https://huggingface.co/datasets/newtextdoc1111/danbooru-tag-csv/resolve/fdf2772213f13d46bff60fc5ebd876e1a811a053"
TAG_COUNTS = Asset("danbooru_tags.csv", f"{_COUNTS}/danbooru_tags.csv", 1_565_830,
                   "a48e1e63b81e8e4fc3091c660fd763e9d05e19beea3b98d1ee78d00ed10ac9d3")
COOCCURRENCE = Asset("danbooru_tags_cooccurrence.csv", f"{_COUNTS}/danbooru_tags_cooccurrence.csv", 103_263_669,
                     "29bf95d3dd2f4b1038ac26b3fee371ebbe67d9b622b32d274c9486dbde56077a")
# What scripts/update_taglib.py fetches (tag list and Danbooru's implications); not pinned by hash, the script checks the shape instead.
TAGLIB_ESTIMATE = 8_000_000

# The tag library's files, and the file the contradiction data is written to (see app/planning/contradictions.py).
TAGLIB_FILES = ("tags.csv", "implications.json")
CONTRADICTIONS_FILE = "contradictions.json"


def taglib_folder(data_dir: Path) -> Path:
    return data_dir / "taglib"


def contradictions_folder(data_dir: Path) -> Path:
    return data_dir / "cooccurrence"


def llama_member_ok(name: str) -> bool:
    """Whether a file of the llama.cpp zip is taken: a plain program, library or licence file at the top of the zip, nothing else."""
    if "/" in name or "\\" in name or name.startswith(".") or not name:
        return False
    low = name.lower()
    return low.endswith((".exe", ".dll")) or low.startswith("license")
