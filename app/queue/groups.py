"""Batches and gallery folders.

A batch is one request that gave 2 or more images: its jobs carry the plan's id in `jobs.batch_id`. The Gallery shows a batch as
one folder, and also a hand-made job with more than one image. The folder's key is "b<plan id>" for a batch and "j<job id>"
for a single job; images of any other job are shown on their own."""
from app.config import Config
from app.db import Database
from app.queue.jobs import JobError, JobStore

IMAGE_ROWS = "SELECT i.*, j.workflow_id FROM images i JOIN jobs j ON j.id = i.job_id"


def parse_key(key: str) -> tuple[str, int]:
    """("b" | "j", id) from a folder key; KeyError (404) for anything else."""
    if len(key) < 2 or key[0] not in "bj" or not key[1:].isdigit():
        raise KeyError(key)
    return key[0], int(key[1:])


def clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


class Groups:
    def __init__(self, store: JobStore):
        self.store, self.db, self.config = store, store.db, store.config

    # ---------- the gallery ----------

    def gallery(self, limit: int = 100, offset: int = 0) -> dict:
        """The gallery's top level, newest first: {"entries": [...], "total": entries, "images": finished images}.
        An entry is a single image ({"type": "image", **image row}) or a folder ({"type": "group", key, title, request, count,
        cover_image_id, cover_ratio, newest}); the cover is the first favourite of the folder, else its first image."""
        rows = self.db.rows("""SELECT i.id, i.job_id, i.idx, i.rating, i.finished_at, j.batch_id, j.count AS job_count
                               FROM images i JOIN jobs j ON j.id = i.job_id WHERE i.status = 'done'""")
        folders: dict[str, list[dict]] = {}
        singles = []
        for r in rows:
            key = f"b{r['batch_id']}" if r["batch_id"] is not None else f"j{r['job_id']}" if r["job_count"] > 1 else None
            (folders.setdefault(key, []) if key else singles).append(r)
        order = lambda r: (r["finished_at"] or "", r["id"])
        entries = [(max(map(order, members)), "group", key, members) for key, members in folders.items()]
        entries += [(order(r), "image", r["id"], r) for r in singles]
        entries.sort(key=lambda e: e[0], reverse=True)
        page = entries[offset:offset + limit]
        full = {r["id"]: r for r in self._images([e[2] for e in page if e[1] == "image"])}
        titles = self._titles([e[2] for e in page if e[1] == "group"])
        covers = {}
        for newest, kind, key, data in page:
            if kind == "group":
                first = sorted(data, key=lambda r: (r["job_id"], r["idx"]))
                covers[key] = next((r for r in first if r["rating"] == 1), first[0])     # the first favourite, else the first image
        shapes = {r["id"]: r["recipe"] for r in self._recipes([c["id"] for c in covers.values()])}
        out = []
        for newest, kind, key, data in page:
            if kind == "image":
                out.append({"type": "image", **full[key]})
                continue
            recipe = shapes[covers[key]["id"]]
            ratio = recipe["width"] / recipe["height"] if recipe.get("width") and recipe.get("height") else None
            out.append({"type": "group", "key": key, "count": len(data), "cover_image_id": covers[key]["id"], "cover_ratio": ratio,
                        "newest": newest[0], **titles[key]})
        return {"entries": out, "total": len(entries), "images": len(rows)}

    def get(self, key: str) -> dict:
        """One folder with its finished images in the order they were planned."""
        kind, number = parse_key(key)
        titles = self._titles([key])
        where, param = ("j.batch_id = ?", number) if kind == "b" else ("i.job_id = ?", number)
        images = self.db.rows(f"{IMAGE_ROWS} WHERE i.status = 'done' AND {where} ORDER BY i.job_id, i.idx", (param,))
        return {"key": key, "count": len(images), "images": images, **titles[key]}

    def delete(self, key: str) -> int:
        """Delete every finished image of a folder (files, rows, thumbnails). The jobs stay. Returns how many."""
        images = self.get(key)["images"]
        for image in images:
            self.store.delete_image(image["id"])
        return len(images)

    def _recipes(self, ids: list[int]) -> list[dict]:
        return self.db.rows(f"SELECT id, recipe FROM images WHERE id IN ({','.join('?' * len(ids))})", tuple(ids)) if ids else []

    def _images(self, ids: list[int]) -> list[dict]:
        return self.db.rows(f"{IMAGE_ROWS} WHERE i.id IN ({','.join('?' * len(ids))})", tuple(ids)) if ids else []

    def _titles(self, keys: list[str]) -> dict[str, dict]:
        """{key: {"title", "request"}}: a batch is named by the request that made it, a job by what it asked for."""
        out = {}
        for key in keys:
            kind, number = parse_key(key)
            if kind == "b":
                plan = self.db.row("SELECT request FROM plans WHERE id = ?", (number,))
                if plan is None:
                    raise KeyError(key)
                out[key] = {"title": clip(plan["request"], 120), "request": plan["request"]}
                continue
            job = self.db.row("SELECT description, fields, meta FROM jobs WHERE id = ?", (number,))
            if job is None:
                raise KeyError(key)
            prompts = (job["fields"].get("prompts") or {}).get("value") or []
            text = job["meta"].get("request") or job["description"] or (prompts[0] if prompts else "") or f"Job {number}"
            out[key] = {"title": clip(text, 120), "request": text}
        return out

    # ---------- a batch as a whole (Plan and Queue pages) ----------

    def jobs_of(self, batch_id: int, statuses: tuple[str, ...]) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        rows = self.db.rows(f"SELECT id, status, issues FROM jobs WHERE batch_id = ? AND status IN ({marks}) ORDER BY id", (batch_id, *statuses))
        if not self.db.row("SELECT 1 AS x FROM jobs WHERE batch_id = ? LIMIT 1", (batch_id,)):
            raise KeyError(batch_id)
        return rows

    def queue(self, batch_id: int) -> dict:
        """Put the batch's ready jobs in the queue, in order. Jobs with an open issue are skipped, and counted."""
        queued, skipped = 0, len(self.jobs_of(batch_id, ("draft", "planning")))
        for job in self.jobs_of(batch_id, ("ready",)):
            try:
                self.store.queue_job(job["id"])
                queued += 1
            except JobError:
                skipped += 1
        return {"queued": queued, "skipped": skipped}

    def clear(self, batch_id: int) -> int:
        """Take the batch's finished jobs off the Queue page."""
        jobs = self.jobs_of(batch_id, ("done", "failed", "cancelled"))
        for job in jobs:
            self.store.clear_job(job["id"])
        return len(jobs)

    def remove(self, batch_id: int) -> int:
        """Delete the batch's cards that were never queued. Returns how many."""
        jobs = self.jobs_of(batch_id, ("draft", "ready"))
        for job in jobs:
            self.store.delete_job(job["id"])
        return len(jobs)
