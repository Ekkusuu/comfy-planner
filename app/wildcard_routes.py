"""API routes for wildcard lists: import a folder, list the lists, read one, switch lists or folders on/off and
change their role. Every list also reports what it serves (which scene slots its entries feed, how that was decided, how sure it is)."""
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import slot_entry_texts
from app.planning.offers import plain_entries
from app.planning.semantic import warm_texts
from app.planning.serving import digest, serve_now
from app.wildcards.ideas import MAX_IDEA_ENTRY
from app.wildcards.store import WildcardError


class ImportBody(BaseModel):
    path: str


def register_wildcard_routes(app):
    def store(request: Request):
        return request.app.state.wildcards

    @app.exception_handler(WildcardError)
    async def _wildcard_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def reading(request: Request, lists):
        """What each list serves, from the vectors already stored (this never starts the embeddings server)."""
        return serve_now(lists, request.app.state.config, getattr(request.app.state, "embedder", None), plain_entries)

    def overview(request: Request) -> dict:
        st, config = store(request), request.app.state.config
        found = st.lists()
        served = reading(request, found)
        lists = [{**l.summary(), "serves": served[l.name].summary()} for l in found]
        return {"enabled": config.wildcards_enabled, "roles": st.roles(), "slot_roles": config.wildcards_slot_roles, "max_entry": MAX_IDEA_ENTRY,
                "folder": str(st.root), "sources": sorted({l["source"] for l in lists if l["source"]}), "lists": lists}

    @app.post("/api/wildcards/import")
    async def import_wildcards(request: Request, body: ImportBody):
        """Copy a wildcard folder into the app (the folder itself is only read). Importing it again replaces the copy and keeps your settings."""
        result = store(request).import_folder(body.path)
        found = store(request).lists()
        embedder = getattr(request.app.state, "embedder", None)
        if embedder is not None:        # read the new entries in the background; the Library shows what they serve once that is done
            embedder.warm([*warm_texts([l for l in found if l.enabled and l.entries]), *request.app.state.config.wildcards_slot_about.values(), *slot_entry_texts(request.app.state.config)])
        return {"imported": {**result, "serves": digest(reading(request, found))}, **overview(request)}

    @app.get("/api/wildcards")
    async def list_wildcards(request: Request):
        return overview(request)

    @app.patch("/api/wildcard-folders/{folder}")
    async def patch_folder(request: Request, folder: str, patch: dict):
        """Switch every list of a folder on or off (`enabled`) and/or give them a role (`role`, "" = the folder's default)."""
        store(request).update_folder(folder, patch.get("enabled"), patch.get("role"))
        return overview(request)

    @app.get("/api/wildcards/{name:path}")
    async def get_wildcard(request: Request, name: str):
        found = store(request).get(name)
        return {**found.summary(), "entries": found.entries}

    @app.patch("/api/wildcards/{name:path}")
    async def patch_wildcard(request: Request, name: str, patch: dict):
        found = store(request).update(name, patch.get("enabled"), patch.get("role"))
        return found.summary()
