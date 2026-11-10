"""API routes for automatic planning: start a plan, re-plan, tweak, lock fields, and follow progress.

The user always chooses the workflow; the LLM only splits the request into jobs on it.
Planning is asynchronous: POST /api/plan answers at once with a plan record ({id, status, message, job_ids, ...}).
Poll GET /api/plans/{id}, or follow `plan` and `job` events on /api/events. Jobs are created as soon as the planner
has split the request (status "planning") and become "ready" (or "draft" if something blocks them) one by one.
"""
from fastapi import Request
from pydantic import BaseModel, Field


class PlanRequest(BaseModel):
    request: str
    workflow_id: str = ""                                   # required: the workflow you chose (an empty one is refused with a plain message)
    input_images: list[str] = Field(default_factory=list)   # names from POST /api/comfy/upload, for image-edit workflows


class ReplanRequest(BaseModel):
    job_ids: list[int] | None = None       # None: every job that is still ready or draft


class TweakRequest(BaseModel):
    instruction: str


class LockRequest(BaseModel):
    fields: list[str] = Field(default_factory=list)    # field names
    locked: bool = True
    prompts: list[int] | None = None       # lock or unlock single prompts by number (0 is the first) instead of all of them


def register_plan_routes(app):
    def S(request: Request):
        return request.app.state

    @app.post("/api/plan")
    async def plan(request: Request, body: PlanRequest):
        """Plan a request on the workflow you chose (it may become several jobs, all on that workflow). Answers 400 with a plain
        message if the workflow is missing, unknown, not runnable or has no usable card. Returns the plan record straight
        away; the work runs in the background."""
        return S(request).planner.plan(body.request, body.workflow_id, body.input_images)

    @app.get("/api/plans")
    async def list_plans(request: Request, limit: int = 20):
        return S(request).planner.recent(limit)

    @app.get("/api/plans/{plan_id}")
    async def get_plan(request: Request, plan_id: int):
        return S(request).planner.get(plan_id)

    @app.post("/api/plans/{plan_id}/cancel")
    async def cancel_plan(request: Request, plan_id: int):
        """Stop a planning run before its next job. What is planned stays; the jobs not reached become drafts you can re-plan."""
        return S(request).planner.cancel(plan_id)

    @app.post("/api/plan/replan")
    async def replan_many(request: Request, body: ReplanRequest = ReplanRequest()):
        """Re-plan several jobs (default: all that haven't been queued). Locked fields stay, the rest is planned again."""
        st = S(request)
        ids = body.job_ids if body.job_ids is not None else [j["id"] for j in st.store.list_jobs() if j["status"] in ("draft", "ready")]
        return st.planner.replan(ids)

    @app.post("/api/jobs/{job_id}/replan")
    async def replan_job(request: Request, job_id: int):
        return S(request).planner.replan([job_id])

    @app.post("/api/jobs/{job_id}/tweak")
    async def tweak_job(request: Request, job_id: int, body: TweakRequest):
        """Change a job with a short instruction ("darker", "fewer steps", "portrait"). Locked fields are not touched."""
        return S(request).planner.tweak(job_id, body.instruction)

    @app.post("/api/jobs/{job_id}/locks")
    async def lock_fields(request: Request, job_id: int, body: LockRequest):
        """Lock or unlock fields, or single prompts (`prompts`). A locked field or prompt keeps its value when the job is re-planned."""
        return S(request).store.set_locks(job_id, body.fields, body.locked, body.prompts)
