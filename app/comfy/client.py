"""Small async client for ComfyUI's HTTP + websocket API."""
import asyncio
import json
import logging
import struct
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import websockets

log = logging.getLogger("comfy")

# Binary websocket frames from ComfyUI (protocol.py, BinaryEventTypes): a 4-byte big-endian event type, then the payload.
PREVIEW_IMAGE = 1                # payload: 4-byte image format (1 JPEG, 2 PNG), then the image bytes
PREVIEW_IMAGE_WITH_METADATA = 4  # payload: 4-byte JSON length, the JSON (with prompt_id), then the image bytes
# The preview method asked for with each prompt (extra_data.preview_method). "taesd" decodes each step with the model's
# small preview decoder from models/vae_approx (lighttaew2_1 for Anima, Krea-2 and Qwen Image), which gives a real
# picture; a model without its decoder file falls back to ComfyUI's cheap latent-to-RGB colours. Previews never change
# the saved image. The server's own --preview-method default is "none".
PREVIEW_METHOD = "taesd"
# The reachability check (/system_stats) keeps its answer this long, and gives up connecting after a short time: on Windows a
# refused connection to a closed port takes about 2 seconds to report, and the UI, the queue and the setup page all ask often.
STATS_CACHE_SECONDS = 3.0
LOCAL_CONNECT_SECONDS = 0.5      # connecting to this machine is instant when ComfyUI is running
REMOTE_CONNECT_SECONDS = 3.0


class ComfyError(Exception):
    """ComfyUI answered with an error (bad graph, failed execution, ...)."""


class ComfyUnreachable(ComfyError):
    """ComfyUI isn't running or can't be reached."""


class ComfyInterrupted(ComfyError):
    """The running prompt was interrupted (cancel)."""


@dataclass
class OutputImage:
    filename: str
    subfolder: str = ""
    type: str = "output"


class ComfyClient:
    def __init__(self, base_url: str, client_id: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id or uuid.uuid4().hex
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=30)
        self._object_info: dict | None = None
        self._stats: tuple[float, bool, object] | None = None   # (when, answered, stats or the failure text) of the last check

    async def set_url(self, base_url: str):
        """Point at another ComfyUI address (a changed setting); what was cached from the old one is dropped."""
        old = self._http
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=30)
        self._object_info = None
        self._stats = None
        await old.aclose()

    async def close(self):
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            resp = await self._http.request(method, path, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            raise ComfyUnreachable(f"ComfyUI isn't reachable at {self.base_url}") from e
        return resp

    async def _json(self, method: str, path: str, **kw):
        resp = await self._request(method, path, **kw)
        if resp.status_code >= 400:
            raise ComfyError(f"ComfyUI {path} returned {resp.status_code}: {resp.text[:1000]}")
        return resp.json() if resp.content else {}

    # ---------- info ----------

    async def system_stats(self, fresh: bool = False) -> dict:
        """ComfyUI's /system_stats. The answer, or the 'not reachable' failure, is kept for STATS_CACHE_SECONDS unless `fresh`."""
        now = time.monotonic()
        if not fresh and self._stats and now - self._stats[0] < STATS_CACHE_SECONDS:
            _, answered, value = self._stats
            if answered:
                return value
            raise ComfyUnreachable(value)
        host = urlsplit(self.base_url).hostname or ""
        connect = LOCAL_CONNECT_SECONDS if host in ("127.0.0.1", "localhost", "::1") else REMOTE_CONNECT_SECONDS
        try:
            stats = await self._json("GET", "/system_stats", timeout=httpx.Timeout(10, connect=connect))
        except ComfyUnreachable as e:
            self._stats = (time.monotonic(), False, str(e))
            raise
        self._stats = (time.monotonic(), True, stats)
        return stats

    async def is_up(self, fresh: bool = False) -> bool:
        try:
            await self.system_stats(fresh)
            return True
        except ComfyError:
            return False

    async def object_info(self, refresh: bool = False) -> dict:
        """All node definitions; cached for the session."""
        if self._object_info is None or refresh:
            self._object_info = await self._json("GET", "/object_info", timeout=120)
        return self._object_info

    # ---------- running ----------

    async def queue_prompt(self, graph: dict, preview_method: str | None = None) -> str:
        """POST /prompt; returns the prompt_id. Raises ComfyError with ComfyUI's node errors.
        preview_method asks ComfyUI for step previews for this prompt only (its server default stays untouched)."""
        body = {"prompt": graph, "client_id": self.client_id}
        if preview_method:
            body["extra_data"] = {"preview_method": preview_method}
        resp = await self._request("POST", "/prompt", json=body)
        if resp.status_code >= 400:
            raise ComfyError(_explain_prompt_error(resp))
        return resp.json()["prompt_id"]

    async def history(self, prompt_id: str) -> dict | None:
        data = await self._json("GET", f"/history/{prompt_id}")
        return data.get(prompt_id)

    async def view(self, image: OutputImage) -> bytes:
        resp = await self._request("GET", "/view", params={"filename": image.filename, "subfolder": image.subfolder, "type": image.type})
        if resp.status_code >= 400:
            raise ComfyError(f"/view failed for {image.filename}: {resp.status_code}")
        return resp.content

    async def upload_image(self, name: str, data: bytes) -> str:
        """Upload into ComfyUI's input folder; returns the stored file name."""
        resp = await self._json("POST", "/upload/image", files={"image": (name, data, "application/octet-stream")},
                                data={"overwrite": "true"})
        return resp["name"]

    async def interrupt(self):
        await self._json("POST", "/interrupt")

    async def delete_queued(self, prompt_ids: list[str] | None = None):
        """Remove pending prompts from ComfyUI's queue (all of them if no ids are given)."""
        body = {"delete": prompt_ids} if prompt_ids else {"clear": True}
        await self._json("POST", "/queue", json=body)

    async def pending_ids(self) -> list[str]:
        data = await self._json("GET", "/queue")
        return [item[1] for item in data.get("queue_pending", [])]

    async def free(self, unload_models: bool = True):
        await self._json("POST", "/free", json={"unload_models": unload_models, "free_memory": True})

    # ---------- run one graph and wait ----------

    async def run(self, graph: dict, on_event: Callable[[dict], None] | None = None,
                  on_preview: Callable[[bytes], None] | None = None) -> list[OutputImage]:
        """Queue a graph, follow progress on the websocket, return the saved output images.

        on_event gets {"type": "queued"|"progress"|"executing", ...} dicts. With on_preview, ComfyUI is asked for step
        previews and on_preview gets the JPEG/PNG bytes of each one (nothing if ComfyUI sends none).
        """
        emit = on_event or (lambda e: None)
        ws_url = self.base_url.replace("http", "ws", 1) + f"/ws?clientId={self.client_id}"
        prompt_id = None
        try:
            async with websockets.connect(ws_url, max_size=None) as ws:
                prompt_id = await self.queue_prompt(graph, PREVIEW_METHOD if on_preview else None)
                emit({"type": "queued", "prompt_id": prompt_id})
                await self._follow(ws, prompt_id, emit, on_preview)
        except (OSError, websockets.WebSocketException) as e:
            if isinstance(e, ConnectionRefusedError) or not await self.is_up(fresh=True):
                raise ComfyUnreachable(f"ComfyUI isn't reachable at {self.base_url}") from e
            if prompt_id is None:
                raise ComfyError(f"Could not open the progress websocket: {e}") from e
            await self._poll_history(prompt_id)   # websocket dropped mid-run: fall back to polling
        return await self.outputs(prompt_id)

    async def _follow(self, ws, prompt_id: str, emit: Callable[[dict], None], on_preview: Callable[[bytes], None] | None = None):
        """Read websocket messages until our prompt finishes."""
        while True:
            raw = await ws.recv()
            if isinstance(raw, bytes):   # binary preview frame
                image = parse_preview_frame(raw, prompt_id) if on_preview else None
                if image:
                    try:
                        on_preview(image)
                    except Exception:
                        log.warning("preview frame ignored", exc_info=True)
                continue
            msg = json.loads(raw)
            kind, data = msg.get("type"), msg.get("data", {})
            if data.get("prompt_id") not in (None, prompt_id):
                continue
            if kind == "progress":
                emit({"type": "progress", "value": data["value"], "max": data["max"], "node": data.get("node")})
            elif kind == "executing":
                if data.get("node") is None:
                    return
                emit({"type": "executing", "node": data["node"]})
            elif kind == "execution_success":
                return
            elif kind == "execution_interrupted":
                raise ComfyInterrupted("Interrupted")
            elif kind == "execution_error":
                raise ComfyError(f'{data.get("node_type")}: {data.get("exception_message", "execution error")}'.strip())

    async def _poll_history(self, prompt_id: str, interval: float = 1.0):
        while True:
            entry = await self.history(prompt_id)
            if entry and entry.get("status", {}).get("completed") is not None:
                return
            await asyncio.sleep(interval)

    async def outputs(self, prompt_id: str) -> list[OutputImage]:
        """Saved (type=output) images of a finished prompt, in node order."""
        entry = await self.history(prompt_id)
        if not entry:
            raise ComfyError(f"No history entry for prompt {prompt_id}")
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            msgs = [m[1] for m in status.get("messages", []) if m[0] == "execution_error"]
            raise ComfyError(msgs[0].get("exception_message", "execution error") if msgs else "execution error")
        images = [OutputImage(i["filename"], i.get("subfolder", ""), i.get("type", "output"))
                  for out in entry.get("outputs", {}).values() for i in out.get("images", [])]
        return [i for i in images if i.type == "output"]


def parse_preview_frame(raw: bytes, prompt_id: str | None = None) -> bytes | None:
    """The JPEG or PNG bytes inside a binary preview frame, or None for anything else (other event types, short or
    damaged frames, a frame that belongs to another prompt). Never raises: a bad frame must not touch the job."""
    try:
        if len(raw) < 8:
            return None
        kind = struct.unpack(">I", raw[:4])[0]
        if kind == PREVIEW_IMAGE:
            image = raw[8:]                       # 4 bytes of image format, then the image
        elif kind == PREVIEW_IMAGE_WITH_METADATA:
            size = struct.unpack(">I", raw[4:8])[0]
            meta = json.loads(raw[8:8 + size])
            if prompt_id and meta.get("prompt_id") not in (None, prompt_id):
                return None
            image = raw[8 + size:]
        else:
            return None
        return image if image.startswith((b"\xff\xd8", b"\x89PNG")) else None
    except (ValueError, struct.error, AttributeError):
        return None


def _explain_prompt_error(resp: httpx.Response) -> str:
    """Readable message from a rejected /prompt (node_errors lists what's wrong)."""
    try:
        body = resp.json()
    except ValueError:
        return f"ComfyUI rejected the prompt ({resp.status_code}): {resp.text[:500]}"
    err = body.get("error", {})
    parts = [err.get("message", "prompt rejected")]
    for nid, node in (body.get("node_errors") or {}).items():
        for e in node.get("errors", []):
            parts.append(f'node {nid} ({node.get("class_type")}): {e.get("message")} {e.get("details", "")}'.strip())
    return "; ".join(parts)
