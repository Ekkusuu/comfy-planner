"""The first-run setup routes (/api/setup/...): status, finding ComfyUI, the install job, and choosing a language model."""
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import settings
from app.setup import status
from app.setup.comfy_detect import find_installs, resolve_install_dir
from app.setup.downloader import DownloadError
from app.setup.installer import SetupManager
from app.setup.models import check_model_file, check_model_name


class ComfyChoice(BaseModel):
    url: str | None = None
    install_dir: str | None = None


class ModelChoice(BaseModel):
    name: str
    path: str


async def save_settings(st, changes: dict) -> list[str]:
    """Save settings the same way PUT /api/config does: validate, write config/app.toml (comments kept), apply now.
    Raises settings.SettingsError for a value that isn't allowed."""
    clean = settings.validate(changes, st.config)
    async with st.config_lock:
        await asyncio.to_thread(settings.write, st.config_path, clean)
        changed = settings.apply(st.config, st.config_path)
        if "comfy_url" in changed:
            await st.comfy.set_url(st.config.comfy_url)
    return changed


def make_setup_manager(app: FastAPI) -> SetupManager:
    """The install job for this app (the tests replace it with one that talks to a fake server)."""
    return SetupManager(app.state.config, lambda changes: save_settings(app.state, changes))


def _invalid(errors: dict[str, str]) -> JSONResponse:
    return JSONResponse({"detail": "; ".join(errors.values()), "errors": errors}, status_code=422)


def register_setup_routes(app: FastAPI):
    def S(request: Request):
        return request.app.state

    async def status_view(st) -> dict:
        items = st.setup.overlay(await status.compute(st.config, st.comfy))
        folder = Path(st.config.comfy_install_dir) if st.config.comfy_install_dir else None
        return {"items": items, "needs_setup": status.needs_setup(items, st.config),
                "total_bytes": sum(i.get("size_bytes", 0) for i in items if i["state"] in ("missing", "error")),
                "install": st.setup.snapshot(), "models_folder": "models",
                "comfy": {"url": st.config.comfy_url, "install_dir": st.config.comfy_install_dir,
                          "install_dir_ok": bool(folder) and resolve_install_dir(folder) == folder}}

    @app.get("/api/setup/status")
    async def setup_status(request: Request):
        """Every part the app needs with its state (ok, missing, installing, error), the install job's progress, and whether setup should open by itself."""
        return await status_view(S(request))

    @app.get("/api/setup/comfy")
    async def setup_comfy(request: Request):
        """ComfyUI folders found in the usual places, for the setup page to offer."""
        st = S(request)
        return {"url": st.config.comfy_url, "install_dir": st.config.comfy_install_dir, "installs": await asyncio.to_thread(find_installs)}

    @app.post("/api/setup/comfy")
    async def setup_set_comfy(request: Request, body: ComfyChoice):
        """Save ComfyUI's address and/or install folder (a portable download's outer folder is accepted too)."""
        st = S(request)
        changes: dict = {}
        if body.url is not None:
            changes["comfy_url"] = body.url
        if body.install_dir is not None:
            folder = body.install_dir.strip().strip('"').strip()
            if folder:
                found = resolve_install_dir(Path(folder))
                if not found:
                    return _invalid({"comfy_install_dir": "That folder doesn't look like ComfyUI: main.py isn't in it. Choose the folder that holds main.py."})
                folder = str(found)
            changes["comfy_install_dir"] = folder
        try:
            await save_settings(st, changes)
        except settings.SettingsError as e:
            return _invalid(e.errors)
        return await status_view(st)

    @app.post("/api/setup/install")
    async def setup_install(request: Request):
        """Download and install every missing part in the background. Poll /api/setup/status for progress."""
        st = S(request)
        try:
            started = st.setup.start()
        except DownloadError as e:
            raise HTTPException(409, str(e))
        if not started:
            raise HTTPException(409, "The install is already running.")
        return await status_view(st)

    @app.post("/api/setup/install/cancel")
    async def setup_install_cancel(request: Request):
        """Stop the install. What was downloaded is kept, so the next try continues from there."""
        st = S(request)
        st.setup.cancel()
        return await status_view(st)

    @app.post("/api/setup/llm-model")
    async def setup_llm_model(request: Request, body: ModelChoice):
        """Use a GGUF model file from the computer (nothing is copied or downloaded): it is added to [llm.models] and made the current model."""
        st = S(request)
        errors: dict[str, str] = {}
        name = path = None
        try:
            name = check_model_name(body.name)
        except ValueError as e:
            errors["name"] = str(e)
        try:
            path = check_model_file(body.path)
        except ValueError as e:
            errors["path"] = str(e)
        if errors:
            return _invalid(errors)
        models = {n: settings.shown(Path(p)) for n, p in st.config.llm_models.items()}
        models[name] = settings.shown(path)
        try:
            await save_settings(st, {"llm_models": models, "llm_model": name})
        except settings.SettingsError as e:
            return _invalid(e.errors)
        return await status_view(st)
