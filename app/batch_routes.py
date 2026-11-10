"""API routes for batches and gallery folders: the Gallery's top level, a folder, and the actions that apply to
a whole batch on the Plan and Queue pages."""
from fastapi import Request

from app.queue.jobs import JobError


def register_batch_routes(app):
    def S(request: Request):
        return request.app.state

    # ---------- gallery ----------

    @app.get("/api/gallery")
    async def gallery(request: Request, limit: int = 100, offset: int = 0):
        """Newest first: single images, and folders (a batch, or a job with several images) with a cover image and a count."""
        return S(request).groups.gallery(limit, offset)

    @app.get("/api/gallery/groups/{key}")
    async def gallery_group(request: Request, key: str):
        return S(request).groups.get(key)

    @app.delete("/api/gallery/groups/{key}")
    async def delete_gallery_group(request: Request, key: str):
        """Delete every finished image of a folder. The Gallery asks first; the jobs stay."""
        return {"deleted": S(request).groups.delete(key)}

    # ---------- a batch as a whole ----------

    @app.post("/api/batches/{batch_id}/queue")
    async def queue_batch(request: Request, batch_id: int):
        """Run all: queue the batch's ready jobs. Jobs with an open issue, or still planning, are skipped and counted."""
        st = S(request)
        result = st.groups.queue(batch_id)
        st.worker.wake()
        return result

    @app.post("/api/batches/{batch_id}/cancel")
    async def cancel_batch(request: Request, batch_id: int):
        """Cancel the batch's queued and running jobs (finished ones stay as they are)."""
        st = S(request)
        jobs = st.groups.jobs_of(batch_id, ("queued", "running"))
        for job in sorted(jobs, key=lambda j: j["status"] == "running"):      # queued ones first, so none can start in between
            await st.worker.cancel_job(job["id"])
        return {"cancelled": len(jobs)}

    @app.post("/api/batches/{batch_id}/clear")
    async def clear_batch(request: Request, batch_id: int):
        """Take the batch's finished jobs off the Queue page (its images stay in the Gallery)."""
        return {"cleared": S(request).groups.clear(batch_id)}

    @app.delete("/api/batches/{batch_id}")
    async def delete_batch(request: Request, batch_id: int):
        """Remove the batch's cards that were never queued."""
        n = S(request).groups.remove(batch_id)
        if not n:
            raise JobError("This batch has no cards left to remove: queued and finished jobs stay.")
        return {"deleted": n}
