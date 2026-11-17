"""API routes for the knowledge library: workflow cards and LoRA cards."""
import asyncio

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.library.civitai import MAX_IMAGE_BYTES
from app.library.cards import CardError
from app.thumbs import BadImage

KINDS = {"workflows": "workflow", "loras": "lora"}


class TagChange(BaseModel):
    keys: list[str] = Field(max_length=2000)            # LoRA files
    add: list[str] = Field(default_factory=list, max_length=20)
    remove: list[str] = Field(default_factory=list, max_length=20)


class CoverChoice(BaseModel):
    image_id: int                                       # a finished gallery image


def register_card_routes(app):
    def S(request: Request):
        return request.app.state

    def kind_of(name: str) -> str:
        if name not in KINDS:
            raise HTTPException(404, f"Unknown card type: {name}")
        return KINDS[name]

    async def view(st, kind: str, key: str) -> dict:
        """One card with what the editor needs next to it: the live workflow, or the LoRA file."""
        row = st.cards.get(kind, key)
        out = {"kind": kind, "key": key, "status": row["status"] if row else None, "card": row["card"] if row else None,
               "pending": row["pending"] if row else None, "updated_at": row["updated_at"] if row else None,
               "build": st.builder.builds.get((kind, key))}
        if kind == "workflow":
            wf = st.library.get(key)
            out["workflow"] = {k: wf[k] for k in ("id", "title", "source_path", "kind", "runnable", "missing", "warnings", "slots", "cover_image_id")}
        else:
            out["file"] = next((f for f in await lora_entries(st) if f["key"] == key), None)
        return out

    async def lora_entries(st, refresh: bool = False) -> list[dict]:
        """Every LoRA file and every LoRA card, with how they relate: new (no card yet), ok, changed (the file's
        hash differs from the one the card was built from), missing (ComfyUI lists it, no file found) or removed."""
        files = {f.name: f for f in await st.scanner.scan(refresh)}
        cards = {c["key"]: c for c in st.cards.of_kind("lora")}
        extras, used = st.extras.all(), st.extras.used_counts()
        entries = []
        for key in sorted({*files, *cards}, key=str.lower):
            f, row = files.get(key), cards.get(key)
            if f is None:
                state = "removed"
            elif f.path is None:
                state = "missing"
            elif row is None:
                state = "new"
            else:
                state = "changed" if row["card"].get("sha256") != f.sha256 else "ok"
            entries.append({"key": key, "size": f.size if f else 0, "modified": f.mtime_ns // 10**9 if f else 0, "sha256": f.sha256 if f else "", "state": state,
                            "status": row["status"] if row else None, "card": row["card"] if row else None,
                            "pending": row["pending"] if row else None, "updated_at": row["updated_at"] if row else None,
                            "build": st.builder.builds.get(("lora", key)),
                            "user_tags": extras[key]["user_tags"] if key in extras else [], "used": used.get(key, 0),
                            "cover": st.covers.info(key, f.path if f else None, f.sha256 if f else "", extras.get(key))})
        st.covers.schedule(list(files.values()))     # Civitai previews for the LoRAs without a cover: in the background
        return entries

    def workflow_entries(st) -> list[dict]:
        cards = {c["key"]: c for c in st.cards.of_kind("workflow")}
        out = []
        for wf in st.library.list():
            row = cards.get(wf["id"])
            out.append({"key": wf["id"], "title": wf["title"], "kind": wf["kind"], "runnable": wf["runnable"],
                        "status": row["status"] if row else None, "has_pending": bool(row and row["pending"]),
                        "family": row["card"]["family"] if row else None, "needs_attention": row["card"]["needs_attention"] if row else [],
                        "best_for": row["card"].get("best_for", "") if row else "",
                        "quality_prefix": row["card"]["prompt_style"]["quality_prefix"] if row else "",
                        "purpose": row["card"].get("purpose", "") if row else "",
                        "build": st.builder.builds.get(("workflow", wf["id"]))})
        return out

    # ---------- lists ----------

    @app.get("/api/cards/builds")
    async def builds(request: Request):
        """Latest build state of every card built since the app started (stage: queued, waiting, loading, reading, writing, done, failed)."""
        return S(request).builder.status()

    @app.get("/api/cards/workflows")
    async def list_workflow_cards(request: Request):
        return workflow_entries(S(request))

    @app.get("/api/cards/loras")
    async def list_lora_cards(request: Request):
        return await lora_entries(S(request))

    @app.post("/api/cards/loras/rescan")
    async def rescan_loras(request: Request):
        """Look at the LoRA folders again: new, changed and removed files show up in each entry's `state`. LoRAs whose Civitai
        preview failed before are asked about again."""
        S(request).covers.forget_misses()
        return await lora_entries(S(request), refresh=True)

    # ---------- the user's tags and covers ----------

    async def known_lora(st, key: str):
        if key not in {*await st.scanner.names(), *(c["key"] for c in st.cards.of_kind("lora"))}:
            raise HTTPException(404, f"Unknown LoRA: {key}")

    def cover_info(st, key: str) -> dict:
        path = st.scanner.resolve(key)
        return st.covers.info(key, path, st.scanner.hashes.cached(path) if path else "", st.extras.get(key))

    @app.post("/api/loras/tags")
    async def change_lora_tags(request: Request, body: TagChange):
        """Add and remove the user's own tags on several LoRAs at once. Only the tags change: not the card, not its status."""
        st = S(request)
        known = {*await st.scanner.names(), *(c["key"] for c in st.cards.of_kind("lora"))}
        unknown = [k for k in body.keys if k not in known]
        if unknown:
            raise HTTPException(404, f"Unknown LoRA: {unknown[0]}")
        return {"tags": st.extras.change_tags(body.keys, body.add, body.remove)}

    @app.get("/api/loras/{key:path}/cover")
    async def get_lora_cover(request: Request, key: str):
        """The LoRA's cover as a small WebP (see app/library/lora_covers.py for where it comes from)."""
        path = await asyncio.to_thread(S(request).covers.serve_path, key)
        if path is None:
            raise HTTPException(404, "This LoRA has no cover.")
        return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.put("/api/loras/{key:path}/cover")
    async def choose_lora_cover(request: Request, key: str, body: CoverChoice):
        st = S(request)
        await known_lora(st, key)
        st.covers.choose_image(key, body.image_id)
        return cover_info(st, key)

    @app.post("/api/loras/{key:path}/cover/upload")
    async def upload_lora_cover(request: Request, key: str):
        """The picture as the raw request body. It is decoded and kept as our own small copy, not as it came."""
        st = S(request)
        await known_lora(st, key)
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(413, "That picture is larger than 20 MB.")
        await asyncio.to_thread(st.covers.choose_upload, key, bytes(data))
        return cover_info(st, key)

    @app.delete("/api/loras/{key:path}/cover")
    async def reset_lora_cover(request: Request, key: str):
        """Back to the automatic cover."""
        st = S(request)
        await known_lora(st, key)
        st.covers.go_automatic(key)
        return cover_info(st, key)

    # ---------- one card ----------

    @app.get("/api/cards/{kind}/{key:path}")
    async def get_card(request: Request, kind: str, key: str):
        return await view(S(request), kind_of(kind), key)

    @app.put("/api/cards/{kind}/{key:path}")
    async def put_card(request: Request, kind: str, key: str, card: dict):
        st, k = S(request), kind_of(kind)
        st.cards.save_edit(k, key, card)
        return await view(st, k, key)

    async def act(request: Request, kind: str, key: str, action: str) -> dict:
        st, k = S(request), kind_of(kind)
        getattr(st.cards, action)(k, key)
        return await view(st, k, key)

    @app.post("/api/cards/{kind}/{key:path}/build")
    async def build_card(request: Request, kind: str, key: str):
        """Start writing a draft (or a re-generated version of an existing card). Runs in the background; follow the
        `card` events on /api/events or GET /api/cards/builds."""
        st, k = S(request), kind_of(kind)
        return st.builder.request(k, key)

    @app.post("/api/cards/{kind}/{key:path}/approve")
    async def approve_card(request: Request, kind: str, key: str):
        return await act(request, kind, key, "approve")

    @app.post("/api/cards/{kind}/{key:path}/accept")
    async def accept_card(request: Request, kind: str, key: str):
        return await act(request, kind, key, "accept_pending")

    @app.post("/api/cards/{kind}/{key:path}/reject")
    async def reject_card(request: Request, kind: str, key: str):
        return await act(request, kind, key, "reject_pending")

    @app.exception_handler(BadImage)
    async def _bad_image(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(CardError)
    async def _card_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)
