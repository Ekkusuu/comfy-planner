"""Build the contradiction evidence from the public Danbooru co-occurrence counts. Run it by hand, once:

    uv run python scripts/build_contradictions.py PATH/danbooru_tags_cooccurrence.csv
    uv run python scripts/build_contradictions.py PATH/danbooru_tags_cooccurrence.csv --tags PATH/danbooru_tags.csv

Get the data yourself (the app never downloads it): the Hugging Face dataset `newtextdoc1111/danbooru-tag-csv` (MIT licence) has
`danbooru_tags_cooccurrence.csv` (about 100 MB: pairs of tags seen together at least 100 times) and `danbooru_tags.csv` (about 1.5 MB:
how often each tag is used). Put both in a new folder such as data/cooccurrence/ (data/ is not committed). The files are only
read as CSV text: nothing from them is run or imported, malformed rows are skipped and counted.

The result, data/cooccurrence/contradictions.json, is a small file of tag pairs with their numbers, limited to the tag library's
general tags. Without it the planner simply skips the contradiction check. See app/planning/contradictions.py for what the numbers mean."""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.planning import contradictions as c  # noqa: E402
from app.planning.taglib import GENERAL, load_taglib  # noqa: E402

MAX_INPUT_BYTES = 150_000_000


def check_size(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"{path} is not a file.")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise SystemExit(f"{path} is bigger than {MAX_INPUT_BYTES} bytes; stopped.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cooccurrence", type=Path, help="danbooru_tags_cooccurrence.csv")
    ap.add_argument("--tags", type=Path, help="danbooru_tags.csv (default: next to the co-occurrence file)")
    ap.add_argument("--taglib", type=Path, default=ROOT / "data" / "taglib", help="the tag library folder (limits the tags to its general pool)")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / c.DATA_FOLDER / c.DERIVED_FILE)
    args = ap.parse_args()
    tags_path = args.tags or args.cooccurrence.with_name("danbooru_tags.csv")
    check_size(args.cooccurrence)
    check_size(tags_path)

    started = time.monotonic()
    with tags_path.open(encoding="utf-8", newline="") as f:
        counts, bad_tags = c.read_tag_counts(f)
    lib = load_taglib(args.taglib)
    if lib is None:
        print(f"note: no tag library in {args.taglib}; every tag used at least {c.MIN_TAG_COUNT} times is kept")
        vocabulary = None
    else:
        vocabulary = [t for t in lib.pool() if lib.tags[t][0] == GENERAL]
    judged = set(counts) if vocabulary is None else set(vocabulary)
    wanted = {t for t in judged | set(c.PERSON_TAGS) | {c.SOLO_TAG} if t in counts}
    wanted |= {t for t, n in counts.items() if n >= c.CONTEXT_MIN_COUNT}
    with args.cooccurrence.open(encoding="utf-8", newline="") as f:
        adj, bad_pairs = c.read_pair_counts(f, wanted)
    print(f"read {len(counts)} tag counts ({bad_tags} malformed rows) and pairs of {len(adj)} tags ({bad_pairs} malformed rows) in {time.monotonic() - started:.0f} s")

    derived = c.derive(counts, adj, vocabulary)
    c.write_derived(derived, args.out)
    size = args.out.stat().st_size
    print(f"pictures estimated: {derived.images}, solo: {derived.solo}")
    print(f"wrote {args.out}: {len(derived.pairs)} pairs over {len(derived.counts)} tags, {size / 1e6:.2f} MB, in {time.monotonic() - started:.0f} s total")


if __name__ == "__main__":
    main()
