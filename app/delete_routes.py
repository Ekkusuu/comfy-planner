"""API routes for deleting LoRAs, workflows and wildcard lists with their files. Each item has a GET that only
describes what a delete would remove, and a DELETE that does it."""
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.library.deleting import Blocked, DeleteError
from app.library.trash import TrashError


class DeleteChoices(BaseModel):
    cancel_jobs: bool = False                                   # also cancel the queued jobs that use the item
    models: list[str] = Field(default_factory=list, max_length=50)      # workflow only: the ids of the model files to delete as well


def register_delete_routes(app):
    def deleter(request: Request):
        return request.app.state.deleter

    @app.exception_handler(Blocked)
    async def _blocked(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(DeleteError)
    async def _delete_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(TrashError)
    async def _trash_error(request, exc):
        done = f" {len(exc.moved)} file(s) before it were already moved to the Recycle Bin; delete again to finish." if exc.moved else ""
        return JSONResponse({"detail": f"{exc} Nothing was changed in the app.{done}"}, status_code=409)

    @app.get("/api/loras/{key:path}/delete-plan")
    async def lora_delete_plan(request: Request, key: str):
        return deleter(request).lora_plan(key)

    @app.delete("/api/loras/{key:path}/file")
    async def delete_lora(request: Request, key: str, body: DeleteChoices = DeleteChoices()):
        return await deleter(request).delete_lora(key, body.cancel_jobs)

    @app.get("/api/workflows/{wf_id}/delete-plan")
    async def workflow_delete_plan(request: Request, wf_id: str):
        return deleter(request).workflow_plan(wf_id)

    @app.delete("/api/workflows/{wf_id}")
    async def delete_workflow(request: Request, wf_id: str, body: DeleteChoices = DeleteChoices()):
        return await deleter(request).delete_workflow(wf_id, body.models, body.cancel_jobs)

    @app.get("/api/wildcards/{name:path}/delete-plan")
    async def wildcard_delete_plan(request: Request, name: str):
        return deleter(request).wildcard_plan(name)

    @app.delete("/api/wildcards/{name:path}")
    async def delete_wildcard(request: Request, name: str):
        return deleter(request).delete_wildcard(name)
