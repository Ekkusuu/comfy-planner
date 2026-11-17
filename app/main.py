"""FastAPI app: JSON API for workflows, jobs, the queue, images and progress events."""
import asyncio
import logging
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.comfy.client import ComfyClient, ComfyError, ComfyUnreachable
from app.comfy.desktop import desktop_launch_args
from app.batch_routes import register_batch_routes
from app.card_routes import register_card_routes
from app.delete_routes import register_delete_routes
from app.mcp_server import mount_mcp, serve_mcp
from app.plan_routes import register_plan_routes
from app.wildcard_routes import register_wildcard_routes
from app.setup.routes import make_setup_manager, register_setup_routes
from app import settings
from app.config import CONFIG_PATH, ROOT, Config, load_config, slot_entry_texts
from app.db import Database
from app.embeddings import Embedder
from app.rerank import Reranker
from app.events import EventBus
from app.gpu import GpuLease
from app.library.card_builder import CardBuilder
from app.library.card_models import FAMILIES, LORA_KINDS, LoraCard
from app.library.cards import CardStore
from app.library.deleting import Deleter
from app.library.civitai import CivitaiClient, get_token
from app.library.lora_covers import LoraCovers
from app.library.lora_extras import LoraExtras
from app.library.loras import LoraScanner
from app.library.workflows import WorkflowError, WorkflowLibrary
from app.logs import close_logging, setup_logging
from app.llm.client import LlmClient
from app.llm.server import LlamaServer
from app.planning import content_safety
from app.planning.context import PlanContext, unload_idle_tag_data
from app.planning.loras import lora_text
from app.planning.runner import PlanRunner
from app.planning.semantic import warm_texts
from app.queue.groups import Groups
from app.queue.jobs import ImageBusy, JobError, JobStore
from app.queue.worker import QueueWorker
from app.thumbs import DEFAULT_SIZE, clamp_size, ensure_thumb
from app.updates import UpdateChecker, project_url
from app.version import __version__
from app.wildcards.store import WildcardStore


class ImportRequest(BaseModel):
    path: str


class JobCreate(BaseModel):
    workflow_id: str
    description: str = ""
    count: int = 1
    fields: dict = Field(default_factory=dict)
    queue: bool = False     # put it straight in the queue


class MoveRequest(BaseModel):
    delta: int              # -1 = one place earlier, +1 = one place later


class OrderRequest(BaseModel):
    job_ids: list[int]


class CoverRequest(BaseModel):
    image_id: int | None = None     # None = back to the newest image


class RetryRequest(BaseModel):
    mode: str = "failed"    # failed | all


class UiFiles(StaticFiles):
    """The built UI. Files in assets/ have a content hash in their name and can be cached for good; everything
    else (index.html, icons) must be re-checked each time, so a rebuilt UI shows up on the next open."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        immutable = path.replace("\\", "/").startswith("assets/")   # Starlette passes OS-style paths
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return response


def create_app(config: Config | None = None, comfy=None, start_worker: bool = True, llm_server=None,
               llm_transport=None, civitai_transport=None, config_path: Path = CONFIG_PATH, embedding_server=None,
               embedding_transport=None, rerank_server=None, rerank_transport=None, update_transport=None) -> FastAPI:
    """Build the app. Tests pass a fake `comfy` client, start_worker=False, a fake llm_server, fake HTTP transports and a
    config_path in a temp folder."""
    config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.data_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(config.data_dir / "logs")
        content_safety.activate()       # load the content rules (a broken rule is skipped; see content_safety.py)
        logging.getLogger("app").info("Comfy Planner started (LLM: %s, ComfyUI: %s)", config.llm_model, config.comfy_url)
        app.state.config = config
        app.state.config_path = config_path
        app.state.db = Database(config.db_path)
        app.state.comfy = comfy or ComfyClient(config.comfy_url)
        app.state.events = EventBus()
        app.state.config_lock = asyncio.Lock()
        app.state.library = WorkflowLibrary(app.state.db, config, app.state.comfy)
        app.state.wildcards = WildcardStore(app.state.db, config)
        app.state.cards = CardStore(app.state.db, config)
        app.state.store = JobStore(app.state.db, config, app.state.library, app.state.wildcards, app.state.cards)
        app.state.groups = Groups(app.state.store)
        app.state.worker = QueueWorker(app.state.store, app.state.library, app.state.comfy, config, app.state.events)
        app.state.llm_server = llm_server or LlamaServer(config)
        app.state.llm = LlmClient(config, llm_transport)
        app.state.lease = GpuLease(app.state.worker, app.state.comfy, app.state.llm_server, config)
        app.state.worker.before_image = app.state.lease.free_for_images
        app.state.scanner = LoraScanner(app.state.db, config, app.state.comfy)
        app.state.civitai = CivitaiClient(config.cache_dir, transport=civitai_transport)
        app.state.updates = UpdateChecker(update_transport)
        app.state.extras = LoraExtras(app.state.db)
        app.state.covers = LoraCovers(app.state.db, config, app.state.scanner, app.state.civitai, app.state.extras, app.state.events,
                                      background=start_worker)
        app.state.deleter = Deleter(app.state.db, config, app.state.scanner, app.state.library, app.state.cards, app.state.extras,
                                    app.state.wildcards, app.state.store, app.state.events)
        app.state.builder = CardBuilder(app.state.cards, app.state.library, app.state.scanner, app.state.civitai,
                                        app.state.llm, app.state.lease, app.state.comfy, app.state.events, config)
        app.state.embedder = Embedder(config, embedding_server, embedding_transport)
        app.state.reranker = Reranker(config, rerank_server, rerank_transport)
        app.state.setup = make_setup_manager(app)       # first-run setup: the install job (app/setup)
        app.state.planner = PlanRunner(PlanContext(config, app.state.llm, app.state.library, app.state.cards, app.state.db, wildcards=app.state.wildcards,
                                                   embedder=app.state.embedder, reranker=app.state.reranker),
                                       app.state.store, app.state.lease, app.state.comfy, app.state.events, config, app.state.db)
        tag_data_task = None
        if start_worker:
            app.state.worker.start()
            warm_embeddings(app)
            tag_data_task = asyncio.create_task(unload_idle_tag_data())      # drops the tag data after 10 minutes without use
        async with serve_mcp(app):      # agents' access at /mcp (app/mcp_server.py)
            yield
        if tag_data_task is not None:
            tag_data_task.cancel()
        try:
            await app.state.setup.close()
            await app.state.embedder.close()
            await app.state.reranker.close()
            await app.state.planner.close()
            await app.state.builder.close()
            await app.state.covers.close()
            await app.state.lease.close()
            await app.state.llm_server.stop()      # never leave the LLM holding the GPU
            await app.state.worker.stop()
            await app.state.llm.close()
            await app.state.civitai.close()
            await app.state.comfy.close()
            app.state.db.close()
        finally:
            close_logging()

    app = FastAPI(title="Comfy Planner", lifespan=lifespan)
    register_routes(app)
    register_card_routes(app)
    register_plan_routes(app)
    register_batch_routes(app)
    register_delete_routes(app)       # before the wildcard routes: their {name:path} would swallow .../delete-plan
    register_wildcard_routes(app)
    register_setup_routes(app)
    mount_mcp(app)                   # before the UI, which is mounted at /
    dist = ROOT / "ui" / "dist"
    if dist.is_dir():
        app.mount("/", UiFiles(directory=dist, html=True), name="ui")
    return app


def warm_embeddings(app: FastAPI):
    """In the background, embed the wildcard lists and LoRA cards that aren't yet (the first time takes a minute or two on the
    CPU), so planning finds them ready. Nothing happens with search by meaning off or the model file missing."""
    st, c = app.state, app.state.config
    if not (c.embeddings_enabled and c.embeddings_model.is_file()):
        return
    lists = [l for l in st.wildcards.lists() if l.enabled and l.entries] if c.wildcards_enabled else []
    tags = st.extras.tags_by_key()
    loras = [lora_text(row["key"], LoraCard.model_validate(row["card"]), tags.get(row["key"], ())) for row in st.cards.of_kind("lora")]
    st.embedder.warm([*warm_texts(lists), *c.wildcards_slot_about.values(), *slot_entry_texts(c), *loras])


def register_routes(app: FastAPI):
    def S(request: Request):
        return request.app.state

    @app.exception_handler(WorkflowError)
    async def _workflow_error(request, exc):
        return _error(422, str(exc))

    @app.exception_handler(JobError)
    async def _job_error(request, exc):
        return _error(400, str(exc))

    @app.exception_handler(ImageBusy)
    async def _image_busy(request, exc):
        return _error(409, str(exc))

    @app.exception_handler(ComfyUnreachable)
    async def _unreachable(request, exc):
        return _error(503, str(exc))

    @app.exception_handler(ComfyError)
    async def _comfy_error(request, exc):
        return _error(502, str(exc))

    @app.exception_handler(KeyError)
    async def _not_found(request, exc):
        return _error(404, f"Not found: {exc.args[0]}")

    # ---------- health / ComfyUI ----------

    @app.get("/api/health")
    async def health(request: Request):
        st = S(request)
        comfy = {"url": st.config.comfy_url, "reachable": False, "version": None, "error": None}
        try:
            stats = await st.comfy.system_stats()
            comfy.update(reachable=True, version=stats.get("system", {}).get("comfyui_version"))
        except ComfyError as e:
            comfy["error"] = str(e)
        return {"app": "ok", "version": __version__, "comfy": comfy, "queue": st.worker.state()}

    @app.get("/api/update-check")
    async def update_check(request: Request):
        """Ask GitHub for the latest release (once an hour at most; the answer is kept in memory). Only runs when the user
        asks: the Settings button, or the UI's check at start when `check_updates_at_start` is on. A problem comes back as
        {current, error} with status 200, so the UI can show it as text."""
        st = S(request)
        return await st.updates.check(st.config.update_repo, __version__)

    @app.post("/api/comfy/start")
    async def start_comfy(request: Request):
        """Start ComfyUI. A Comfy Desktop install is started directly with Desktop's own arguments (shared models
        folder etc.); otherwise run `comfy launch --background`, falling back to the install's venv Python."""
        install = Path(S(request).config.comfy_install_dir)
        desktop_args = desktop_launch_args(install)
        out = b""
        if not desktop_args:
            cli = shutil.which(S(request).config.comfy_cli) or S(request).config.comfy_cli
            try:
                proc = await asyncio.create_subprocess_exec(cli, "launch", "--background", stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(proc.communicate(), 120)
            except (OSError, asyncio.TimeoutError) as e:
                out, proc = f"{e}".encode(), None
            if proc and proc.returncode == 0:
                return {"exit_code": 0, "output": out.decode(errors="replace")[-2000:]}
        # Start ComfyUI directly with its own venv Python (comfy-cli's own env can be broken).
        python = install / ".venv" / "Scripts" / "python.exe"
        if not python.exists():
            raise HTTPException(500, f"Can't start ComfyUI: {python} doesn't exist. {out.decode(errors='replace')[-500:]}")
        port = S(request).config.comfy_url.rsplit(":", 1)[-1].strip("/") or "8188"
        subprocess.Popen([str(python), "main.py", "--port", port, *desktop_args], cwd=install,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         # CREATE_NO_WINDOW: a hidden console the venv's python child inherits, so no terminal pops up
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        how = "with Comfy Desktop's model folders" if desktop_args else "(comfy launch failed)"
        return {"exit_code": 0, "output": f"Started ComfyUI with {python} {how}."}

    @app.get("/api/comfy/workflows")
    async def comfy_saved_workflows(request: Request):
        """Workflow files saved in ComfyUI's user folder (read-only listing, for the import screen)."""
        folder = Path(S(request).config.comfy_install_dir) / "user" / "default" / "workflows"
        return [{"name": p.stem, "path": str(p)} for p in sorted(folder.glob("*.json")) if not p.name.startswith(".")] if folder.is_dir() else []

    @app.post("/api/comfy/upload")
    async def upload_input_image(request: Request, filename: str):
        """Upload an input image (raw request body) to ComfyUI; returns the name to use in a job's input_image."""
        data = await request.body()
        if not data:
            raise HTTPException(400, "Empty body.")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name) or "image.png"
        return {"name": await S(request).comfy.upload_image(f"cp_{uuid.uuid4().hex[:8]}_{safe}", data)}

    # ---------- workflows ----------

    @app.get("/api/workflows")
    async def list_workflows(request: Request):
        return S(request).library.list()

    @app.post("/api/workflows/recheck")
    async def recheck_workflows(request: Request):
        """Re-check every workflow's models and nodes against the running ComfyUI."""
        return await S(request).library.recheck()

    @app.post("/api/workflows/import")
    async def import_workflow(request: Request, body: ImportRequest):
        return await S(request).library.import_path(body.path)

    @app.get("/api/workflows/{wf_id}")
    async def get_workflow(request: Request, wf_id: str):
        return S(request).library.get(wf_id)

    @app.get("/api/workflows/{wf_id}/graph")
    async def get_workflow_graph(request: Request, wf_id: str):
        return S(request).library.get_graph(wf_id)

    @app.put("/api/workflows/{wf_id}/slots")
    async def put_slots(request: Request, wf_id: str, slots: dict):
        return S(request).library.update_slots(wf_id, slots)

    @app.put("/api/workflows/{wf_id}/cover")
    async def put_cover(request: Request, wf_id: str, body: CoverRequest):
        return S(request).library.set_cover(wf_id, body.image_id)

    # ---------- jobs ----------

    async def optional_object_info(st):
        try:
            return await st.comfy.object_info()
        except ComfyError:
            return None   # ComfyUI down: skip the sampler/LoRA name checks

    @app.get("/api/comfy/options")
    async def comfy_options(request: Request):
        """Sampler, scheduler and LoRA names ComfyUI offers (read from /object_info), for the job editor."""
        oi = await optional_object_info(S(request))
        if not oi:
            return {"available": False, "samplers": [], "schedulers": [], "loras": []}

        def options(node: str, name: str) -> list:
            return oi.get(node, {}).get("input", {}).get("required", {}).get(name, [[]])[0]

        return {"available": True, "samplers": options("KSampler", "sampler_name"),
                "schedulers": options("KSampler", "scheduler"), "loras": options("LoraLoaderModelOnly", "lora_name")}

    def config_view(st) -> dict:
        c = st.config
        return {"version": __version__, "update_repo": c.update_repo, "project_url": project_url(c.update_repo), "check_updates_at_start": c.check_updates_at_start,
                "config_file": str(st.config_path), "comfy_url": c.comfy_url, "comfy_install_dir": c.comfy_install_dir,
                "comfy_cli": c.comfy_cli, "host": c.host, "port": c.port, "data_dir": str(c.data_dir),
                "images_dir": str(c.images_dir), "workflows_dir": str(c.workflows_dir),
                "max_images_per_job": c.max_images_per_job, "max_images_per_request": c.max_images_per_request,
                "hires_default": c.hires_default,
                "max_loras": c.max_loras, "safety_negative": c.safety_negative,
                "strip_from_default_negative": c.strip_from_default_negative,
                "llm_model": c.llm_model, "llm_models": settings.model_status(c), "llm_binary": settings.shown(c.llm_binary),
                "llm_binary_found": c.llm_binary.is_file(),
                "embeddings_enabled": c.embeddings_enabled, "embeddings_model_found": c.embeddings_model.is_file(), "reranker_enabled": c.reranker_enabled, "reranker_model_found": c.reranker_model.is_file(), "llm_url": c.llm_url, "llm_ctx_size": c.llm_ctx_size,
                "llm_gpu_layers": c.llm_gpu_layers, "llm_enable_thinking": c.llm_enable_thinking,
                "llm_temperature": c.llm_temperature, "llm_max_tokens": c.llm_max_tokens,
                "llm_start_timeout": c.llm_start_timeout, "llm_stop_timeout": c.llm_stop_timeout,
                "llm_keep_loaded_seconds": c.llm_keep_loaded_seconds,
                "llm_extra_args": c.llm_extra_args, "allow_draft_cards": c.allow_draft_cards,
                "wildcards_enabled": c.wildcards_enabled, "wildcards_llm_picks_lists": c.wildcards_llm_picks_lists, "wildcards_ideas_per_prompt": c.wildcards_ideas_per_prompt,
                "wildcards_entries_per_list": c.wildcards_entries_per_list, "wildcards_max_idea_entries": c.wildcards_max_idea_entries,
                "civitai_token_found": get_token() is not None,    # only whether it exists, never the value
                "card_families": FAMILIES, "lora_kinds": LORA_KINDS}

    @app.get("/api/config")
    async def get_config(request: Request):
        """Current settings. host, port and data_dir are bound by the launcher and only shown."""
        return config_view(S(request))

    @app.put("/api/config")
    async def put_config(request: Request, changes: dict):
        """Change settings: only the keys sent are touched. Written to config/app.toml (comments kept) and applied now."""
        st = S(request)
        try:
            clean = settings.validate(changes, st.config)
        except settings.SettingsError as e:
            return JSONResponse({"detail": str(e), "errors": e.errors}, status_code=422)
        async with st.config_lock:
            await asyncio.to_thread(settings.write, st.config_path, clean)
            changed = settings.apply(st.config, st.config_path)
            if "comfy_url" in changed:
                await st.comfy.set_url(st.config.comfy_url)
        return {**config_view(st), "changed": changed}

    @app.post("/api/system/open-models-folder")
    async def open_models_folder():
        """Open the project's models folder in File Explorer (made first if missing). Takes no input: the folder is
        always ROOT/models, so the client cannot choose what opens. Not offered to MCP agents."""
        if sys.platform != "win32":
            raise HTTPException(501, "Opening the models folder is only supported on Windows. Open the models folder in the project by hand.")
        folder = ROOT / "models"
        try:
            await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(os.startfile, str(folder))
        except OSError as e:
            raise HTTPException(500, f"Couldn't open the models folder: {e}")
        return {"opened": True}

    @app.post("/api/jobs")
    async def create_job(request: Request, body: JobCreate):
        st = S(request)
        job = st.store.create_job(body.workflow_id, body.count, body.fields, body.description, body.queue,
                                  await optional_object_info(st))
        st.worker.wake()
        return job

    @app.get("/api/jobs")
    async def list_jobs(request: Request, status: str | None = None):
        return S(request).store.list_jobs(status)

    @app.get("/api/jobs/{job_id}")
    async def get_job(request: Request, job_id: int):
        return S(request).store.get_job(job_id)

    @app.patch("/api/jobs/{job_id}")
    async def patch_job(request: Request, job_id: int, patch: dict):
        st = S(request)
        return st.store.update_fields(job_id, patch, await optional_object_info(st))

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(request: Request, job_id: int):
        S(request).store.delete_job(job_id)
        return {"deleted": job_id}

    @app.post("/api/jobs/{job_id}/queue")
    async def queue_job(request: Request, job_id: int):
        st = S(request)
        job = st.store.queue_job(job_id)
        st.worker.wake()
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, job_id: int):
        return await S(request).worker.cancel_job(job_id)

    @app.get("/api/jobs/{job_id}/preview")
    async def get_job_preview(request: Request, job_id: int):
        """The newest step preview of the image this job is generating now (JPEG or PNG); 404 when there is none."""
        data = S(request).worker.preview_for(job_id)
        if data is None:
            raise HTTPException(404, "No preview.")
        return Response(data, media_type="image/png" if data.startswith(b"\x89PNG") else "image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/jobs/{job_id}/clear")
    async def clear_job(request: Request, job_id: int):
        """Hide a finished job from the Queue page; its images stay in the Gallery."""
        return S(request).store.clear_job(job_id)

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(request: Request, job_id: int, body: RetryRequest = RetryRequest()):
        st = S(request)
        job = st.store.retry_job(job_id, body.mode)
        st.worker.wake()
        return job

    @app.post("/api/jobs/{job_id}/move")
    async def move_job(request: Request, job_id: int, body: MoveRequest):
        S(request).store.move_job(job_id, body.delta)
        return S(request).store.list_jobs("queued")

    # ---------- queue ----------

    @app.get("/api/queue")
    async def get_queue(request: Request):
        st = S(request)
        return {**st.worker.state(), "jobs": [j for j in st.store.list_jobs() if j["status"] in ("queued", "running")]}

    @app.put("/api/queue/order")
    async def set_order(request: Request, body: OrderRequest):
        S(request).store.set_order(body.job_ids)
        return S(request).store.list_jobs("queued")

    @app.post("/api/queue/pause")
    async def pause_queue(request: Request):
        S(request).worker.pause()
        return S(request).worker.state()

    @app.post("/api/queue/resume")
    async def resume_queue(request: Request):
        S(request).worker.resume()
        return S(request).worker.state()

    # ---------- images ----------

    @app.get("/api/images")
    async def list_images(request: Request, limit: int = 100, offset: int = 0, job_id: int | None = None):
        return S(request).store.list_images(limit, offset, job_id)

    @app.get("/api/images/{image_id}")
    async def get_image(request: Request, image_id: int):
        return S(request).store.get_image(image_id)

    @app.get("/api/images/{image_id}/file")
    async def get_image_file(request: Request, image_id: int):
        st = S(request)
        image = st.store.get_image(image_id)
        path = st.config.images_dir / image["file_path"] if image["file_path"] else None
        if not path or not path.is_file():
            raise HTTPException(404, "Image file not found.")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/images/{image_id}/thumb")
    async def get_image_thumb(request: Request, image_id: int, size: int = DEFAULT_SIZE):
        """Downscaled WebP (longest edge `size`, 64 to 1024), cached under data/thumbs."""
        st = S(request)
        image = st.store.get_image(image_id)
        path = st.config.images_dir / image["file_path"] if image["file_path"] else None
        if not path or not path.is_file():
            raise HTTPException(404, "Image file not found.")
        return FileResponse(await asyncio.to_thread(ensure_thumb, st.config, image_id, path, clamp_size(size)),
                            media_type="image/webp")

    @app.delete("/api/images/{image_id}")
    async def delete_image(request: Request, image_id: int):
        S(request).store.delete_image(image_id)
        return {"deleted": image_id}

    # ---------- progress stream ----------

    @app.get("/api/events")
    async def events(request: Request):
        """Server-sent events: job/image status, step progress, queue state (pause / ComfyUI down)."""
        st = S(request)
        queue = st.events.subscribe()

        async def stream():
            try:
                yield _sse({"type": "queue", "queue": st.worker.state()})
                while not await request.is_disconnected():
                    try:
                        yield _sse(await asyncio.wait_for(queue.get(), 15))
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                st.events.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _error(status: int, message: str):
    return JSONResponse({"detail": message}, status_code=status)


app = create_app()
