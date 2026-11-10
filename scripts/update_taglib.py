"""Download the Danbooru tag library into data/taglib/. Run it by hand, once in a while:

    uv run python scripts/update_taglib.py

What it fetches (data only; nothing is ever executed or imported from it):
  1. the tag list: a CSV from the public dbr-e621-lists-archive on GitHub (Unlicense), one tag per row:
     name, category, count, "aliases". Categories: 0 general, 1 artist, 3 copyright, 4 character, 5 meta.
  2. Danbooru's tag implications (2girls -> multiple_girls) and a few "tag group" wiki pages (character counts, image
     composition ...), from Danbooru's public API: anonymous, no key, one request per second, a User-Agent of its own.

Everything goes into a new empty folder first and is checked (size limits, the shape of the rows); only then does it replace
data/taglib/. A failed run leaves the old library as it was. The planner never contacts Danbooru: it reads these files."""
import csv
import io
import json
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "data" / "taglib"
CSV_URL = "https://raw.githubusercontent.com/DraconicDragon/dbr-e621-lists-archive/main/tag-lists/danbooru/danbooru_2026-10-01_pt20-ia-dd.csv"
API = "https://danbooru.donmai.us"
# Danbooru asks automated clients to identify themselves. Optionally set COMFY_PLANNER_CONTACT (an email or URL) to add contact details.
_CONTACT = os.environ.get("COMFY_PLANNER_CONTACT", "").strip()
USER_AGENT = "ComfyPlanner-taglib/1.0 (personal local tool; one-off download of tag data" + (f"; {_CONTACT})" if _CONTACT else ")")
WIKI_PAGES = ["character_count"]       # tag_group:<name>
MAX_CSV_BYTES = 30_000_000
MAX_JSON_BYTES = 8_000_000
MAX_IMPLICATION_PAGES = 60
MIN_ROWS = 100_000
PAUSE = 1.1             # seconds between requests to Danbooru (it asks for about one per second)


def fetch(url: str, limit: int) -> bytes:
    """GET with a size limit: reads at most limit + 1 bytes and refuses a bigger answer."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise SystemExit(f"{url} is bigger than {limit} bytes; stopped.")
    return data


def check_tags(raw: bytes) -> int:
    """The number of good rows; stops the run if the file doesn't look like the tag list."""
    rows = bad = 0
    for row in csv.reader(io.StringIO(raw.decode("utf-8"))):
        rows += 1
        try:
            ok = len(row) >= 3 and row[0] and " " not in row[0] and int(row[1]) in (0, 1, 3, 4, 5) and int(row[2]) >= 0
        except ValueError:
            ok = False
        bad += not ok
    if rows < MIN_ROWS or bad > rows * 0.01:
        raise SystemExit(f"the tag list does not look right: {rows} rows, {bad} of them malformed; stopped.")
    return rows


def get_json(path: str, **query):
    url = f"{API}{path}?{urllib.parse.urlencode(query)}"
    time.sleep(PAUSE)
    return json.loads(fetch(url, MAX_JSON_BYTES))


def main():
    new = TARGET.parent / f"taglib.new-{int(time.time())}"
    new.mkdir(parents=True)
    try:
        print("tag list ...", flush=True)
        raw = fetch(CSV_URL, MAX_CSV_BYTES)
        rows = check_tags(raw)
        (new / "tags.csv").write_bytes(raw)
        print(f"  {rows} rows, {len(raw)} bytes")

        print("implications ...", flush=True)
        pairs = []
        for page in range(1, MAX_IMPLICATION_PAGES + 1):
            items = get_json("/tag_implications.json", **{"search[status]": "active", "limit": 1000, "page": page, "only": "antecedent_name,consequent_name"})
            if not isinstance(items, list):
                raise SystemExit("unexpected answer for the implications; stopped.")
            pairs += [[i["antecedent_name"], i["consequent_name"]] for i in items if isinstance(i.get("antecedent_name"), str) and isinstance(i.get("consequent_name"), str)]
            if len(items) < 1000:
                break
        if len(pairs) < 1000:
            raise SystemExit(f"only {len(pairs)} implications; stopped.")
        (new / "implications.json").write_text(json.dumps(pairs, ensure_ascii=False), encoding="utf-8")
        print(f"  {len(pairs)} implications")

        print("tag groups ...", flush=True)
        (new / "wiki").mkdir()
        for name in WIKI_PAGES:
            items = get_json("/wiki_pages.json", **{"search[title]": f"tag_group:{name}", "only": "title,body"})
            body = items[0]["body"] if isinstance(items, list) and items and isinstance(items[0].get("body"), str) else ""
            if not body:
                raise SystemExit(f"tag_group:{name} came back empty; stopped.")
            (new / "wiki" / f"{name}.txt").write_text(body[:300_000], encoding="utf-8")
            print(f"  tag_group:{name}: {len(body)} characters")

        (new / "MANIFEST.json").write_text(json.dumps({"downloaded": date.today().isoformat(), "tags_url": CSV_URL, "tag_rows": rows,
                                                       "implications": len(pairs), "wiki": WIKI_PAGES}, indent=1), encoding="utf-8")
        old = TARGET.parent / "taglib.old"
        shutil.rmtree(old, ignore_errors=True)
        if TARGET.exists():
            TARGET.rename(old)
        new.rename(TARGET)
        shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    print("done:", TARGET)
    for f in sorted(TARGET.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(TARGET)}  {f.stat().st_size:,} bytes")


if __name__ == "__main__":
    sys.exit(main())
