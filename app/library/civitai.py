"""Civitai lookup by file hash. Read-only: one GET per file, nothing that costs credits. The one download is a LoRA's
small preview picture from Civitai's image server (see fetch_image).

The API token (CIVITAI_API_TOKEN) is read from the environment and sent only to civitai.com. It is never logged,
stored or put in an error message, and never sent to the image server.
"""
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

HOST = "civitai.com"
CDN_HOST = "image.civitai.com"         # where preview pictures live; asked without the token
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 2                      # the image server answers with a redirect to its storage (blobs-b2.civitai.com)
REDIRECTS = (301, 302, 303, 307, 308)
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".gif")
API = f"https://{HOST}/api/v1"
TOKEN_VAR = "CIVITAI_API_TOKEN"
MAX_EXAMPLES = 5


def get_token() -> str | None:
    """The token from the process environment, else (Windows) the user-level value in HKCU\\Environment."""
    token = os.environ.get(TOKEN_VAR)
    if not token and sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                token = winreg.QueryValueEx(key, TOKEN_VAR)[0]
        except OSError:
            token = None
    return (token.strip() or None) if isinstance(token, str) else None


@dataclass
class ImageResult:
    """status: ok | refused | too_big | offline | error. `data` is the picture's raw bytes when ok (untrusted: decode it with Pillow)."""
    status: str
    data: bytes | None = None
    message: str = ""


@dataclass
class CivitaiResult:
    """status: ok | no_token | not_found | offline | error. `data` is Civitai's JSON when ok."""
    status: str
    data: dict | None = None
    message: str = ""


class CivitaiClient:
    def __init__(self, cache_dir: Path, token_getter=get_token, transport: httpx.AsyncBaseTransport | None = None):
        self.cache_dir = Path(cache_dir) / "civitai"
        self._token = token_getter
        self._http = httpx.AsyncClient(base_url=API, timeout=30, follow_redirects=False, transport=transport,
                                       event_hooks={"request": [_only_civitai]})

    async def close(self):
        await self._http.aclose()

    async def by_hash(self, sha256: str) -> CivitaiResult:
        """GET /model-versions/by-hash/{sha256}. Answers from the cache first (the response has no token in it)."""
        cached = self.cache_dir / f"{sha256}.json"
        if cached.is_file():
            return CivitaiResult("ok", json.loads(cached.read_text(encoding="utf-8")))
        token = self._token()
        if not token:
            return CivitaiResult("no_token", message=f"No {TOKEN_VAR} found, so Civitai wasn't asked.")
        try:
            resp = await self._http.get(f"/model-versions/by-hash/{sha256}", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            return CivitaiResult("offline", message=f"Couldn't reach Civitai ({type(e).__name__}).")
        if resp.status_code == 404:
            return CivitaiResult("not_found", message="Civitai doesn't know this file.")
        if resp.status_code != 200:
            return CivitaiResult("error", message=f"Civitai answered {resp.status_code}.")
        try:
            data = resp.json()
        except ValueError:
            return CivitaiResult("error", message="Civitai's answer wasn't JSON.")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return CivitaiResult("ok", data)

    async def fetch_image(self, url: str) -> ImageResult:
        """GET one preview picture, with no token, at most MAX_IMAGE_BYTES. Only https addresses on the image server are
        asked. It answers with a redirect to its storage host, so up to MAX_REDIRECTS redirects are followed, each one
        checked the same way; a redirect anywhere else is refused before any request is made."""
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            if parts.scheme != "https" or not is_image_host(parts.hostname):
                return ImageResult("refused", message=f"Only {CDN_HOST} (and its storage) is asked for pictures.")
            try:
                async with self._http.stream("GET", url) as resp:
                    if resp.status_code in REDIRECTS and resp.headers.get("location"):
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code != 200:
                        return ImageResult("error", message=f"The image server answered {resp.status_code}.")
                    if int(resp.headers.get("content-length") or 0) > MAX_IMAGE_BYTES:
                        return ImageResult("too_big", message="The picture is larger than the limit.")
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_IMAGE_BYTES:
                            return ImageResult("too_big", message="The picture is larger than the limit.")
                        chunks.append(chunk)
            except (httpx.HTTPError, ValueError) as e:
                return ImageResult("offline", message=f"Couldn't reach the image server ({type(e).__name__}).")
            return ImageResult("ok", b"".join(chunks))
        return ImageResult("error", message="The image server redirected too many times.")


def is_image_host(host: str | None) -> bool:
    """The image server, or the storage host it redirects to (blobs-b2.civitai.com): a subdomain of civitai.com, never civitai.com itself."""
    return bool(host) and (host == CDN_HOST or (host.endswith("." + HOST) and host != "www." + HOST))


async def _only_civitai(request: httpx.Request):
    """Last line of defence. Only civitai.com may get a request with credentials; the image server (and its storage) gets
    requests only without them; no other host gets any."""
    if request.url.host == HOST:
        return
    if is_image_host(request.url.host) and "authorization" not in request.headers:
        return
    raise httpx.RequestError("Refusing a request to a host other than civitai.com, or credentials sent to another host.")


def preview_urls(data: dict) -> list[str]:
    """Addresses of the still pictures in a version's reply, in Civitai's order: no videos, no other host.
    Pictures Civitai marks as showing a minor are left out."""
    urls = []
    for image in data.get("images") or []:
        url = image.get("url") if isinstance(image, dict) else None
        if not isinstance(url, str) or image.get("type", "image") != "image" or image.get("minor"):
            continue
        parts = urlsplit(url)
        if parts.scheme == "https" and parts.hostname == CDN_HOST and not parts.path.lower().endswith(VIDEO_EXTENSIONS):
            urls.append(url)
    return urls


def summarize(data: dict) -> dict:
    """What the card needs from Civitai's version JSON: ids, link, names, trained words, description, example prompts."""
    model = data.get("model") or {}
    examples: list[str] = []
    for image in data.get("images") or []:
        prompt = ((image.get("meta") or {}).get("prompt") or "").strip()
        if prompt and prompt not in examples:
            examples.append(prompt[:800])
        if len(examples) >= MAX_EXAMPLES:
            break
    model_id, version_id = data.get("modelId"), data.get("id")
    url = f"https://{HOST}/models/{model_id}" + (f"?modelVersionId={version_id}" if version_id else "") if model_id else ""
    return {"model_id": model_id, "version_id": version_id, "url": url,
            "name": model.get("name") or "", "version_name": data.get("name") or "", "type": model.get("type") or "",
            "base_model": data.get("baseModel") or "", "trained_words": [w for w in data.get("trainedWords") or [] if isinstance(w, str)],
            "description": strip_html(data.get("description") or ""), "example_prompts": examples}


def strip_html(text: str) -> str:
    text = re.sub(r"<\s*(br|/p|/li|/h\d)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()
