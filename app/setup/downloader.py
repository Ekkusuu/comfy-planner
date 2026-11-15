"""Download one pinned file: resumable, size- and SHA-256-checked, only from allowed hosts. Also extracts the files wanted from a zip.

The address of every file is pinned in catalog.py, and the bytes are only kept when their SHA-256 matches. Redirects are
followed by hand so that every hop is checked against the allowed hosts: a download starts at GitHub or Hugging Face, and
those redirect to their own download servers (listed below), never anywhere else."""
import asyncio
import hashlib
import os
import zipfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.setup.catalog import Asset

# Where a download may start.
START_HOSTS = {"github.com", "huggingface.co"}
# Where those hosts send the file from (release assets, and Hugging Face's storage).
REDIRECT_HOSTS = {"objects.githubusercontent.com", "release-assets.githubusercontent.com"}
REDIRECT_SUFFIXES = (".huggingface.co", ".hf.co")
MAX_REDIRECTS = 5
CHUNK = 1 << 20
USER_AGENT = "ComfyPlanner-setup/1.0"


class DownloadError(Exception):
    """Something went wrong; the message is plain words for the person using the app."""


class Cancelled(Exception):
    """The person pressed Cancel. The partly downloaded file is kept so a later try can continue."""


def host_allowed(url: str, first: bool) -> bool:
    """Only https, and only the hosts above (`first`: the address the download starts at; otherwise a redirect hop)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        return False
    if host in START_HOSTS:
        return True
    return not first and (host in REDIRECT_HOSTS or host.endswith(REDIRECT_SUFFIXES))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


class Downloader:
    """Downloads assets into `folder`. `transport` and `allow` exist for the tests (a fake server, and a policy that accepts it)."""

    def __init__(self, folder: Path, transport: httpx.AsyncBaseTransport | None = None, allow: Callable[[str, bool], bool] = host_allowed):
        self.folder = folder
        self.transport = transport
        self.allow = allow

    async def fetch(self, asset: Asset, on_progress: Callable[[int, int], None], cancel: asyncio.Event) -> Path:
        """Make sure `folder/asset.name` exists with the right bytes and return it. A file already there is checked and kept when right.
        A partly downloaded `<name>.part` is continued; a finished download whose hash is wrong is deleted."""
        self.folder.mkdir(parents=True, exist_ok=True)
        final = self.folder / asset.name
        part = final.with_name(final.name + ".part")
        if final.is_file():
            if final.stat().st_size == asset.size and await asyncio.to_thread(sha256_file, final) == asset.sha256:
                on_progress(asset.size, asset.size)
                return final
            final.unlink()
        have = part.stat().st_size if part.is_file() else 0
        if have > asset.size:
            part.unlink()
            have = 0
        on_progress(have, asset.size)
        if have < asset.size:
            await self._transfer(asset, part, have, on_progress, cancel)
        if part.stat().st_size != asset.size or await asyncio.to_thread(sha256_file, part) != asset.sha256:
            part.unlink(missing_ok=True)
            raise DownloadError(f"{asset.name} did not arrive intact (its checksum is wrong), so it was deleted. Try again.")
        os.replace(part, final)
        return final

    async def _transfer(self, asset: Asset, part: Path, have: int, on_progress, cancel: asyncio.Event):
        timeout = httpx.Timeout(30, read=60)
        try:
            async with httpx.AsyncClient(transport=self.transport, follow_redirects=False, timeout=timeout) as client:
                url = asset.url
                for hop in range(MAX_REDIRECTS + 1):
                    if not self.allow(url, hop == 0):
                        raise DownloadError(f"Refused to download from {urlsplit(url).hostname or 'an unknown address'}: it is not one of the allowed download sites.")
                    headers = {"User-Agent": USER_AGENT, **({"Range": f"bytes={have}-"} if have else {})}
                    async with client.stream("GET", url, headers=headers) as res:
                        if res.status_code in (301, 302, 303, 307, 308) and res.headers.get("location"):
                            url = urljoin(url, res.headers["location"])
                            continue
                        await self._save(asset, part, have, res, on_progress, cancel)
                        return
                raise DownloadError("The download site sent the request around in circles.")
        except httpx.HTTPError as e:
            raise DownloadError(f"The connection to the download site was lost ({type(e).__name__}). Press Retry to continue where it stopped.") from e

    async def _save(self, asset: Asset, part: Path, have: int, res: httpx.Response, on_progress, cancel: asyncio.Event):
        if res.status_code == 206:
            start = res.headers.get("content-range", "").removeprefix("bytes ").split("-")[0]
            if start != str(have):
                raise DownloadError("The download site resumed from the wrong place. Delete the partly downloaded file and try again.")
            mode = "ab"
        elif res.status_code == 200:
            have, mode = 0, "wb"           # the site sent everything again (it ignored the resume request)
        else:
            raise DownloadError(f"The download site answered {res.status_code} {res.reason_phrase or ''}".strip() + ". Try again later.")
        with part.open(mode) as f:
            async for chunk in res.aiter_bytes(CHUNK):
                if cancel.is_set():
                    raise Cancelled()
                have += len(chunk)
                if have > asset.size:
                    f.close()
                    part.unlink(missing_ok=True)
                    raise DownloadError(f"{asset.name} is bigger than expected, so the download was stopped and deleted.")
                f.write(chunk)
                on_progress(have, asset.size)


def extract_zip(zip_path: Path, folder: Path, wanted: Callable[[str], bool], max_bytes: int = 3 << 30) -> list[str]:
    """Take only the files `wanted` accepts out of the zip, flat into `folder` (each one is written under a temporary name, then moved
    into place). Anything else in the zip, any path with folders in it, and a zip that would unpack to more than `max_bytes` are refused.
    Returns the names written."""
    folder.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = [i for i in z.infolist() if not i.is_dir() and wanted(i.filename)]
            if sum(i.file_size for i in infos) > max_bytes:
                raise DownloadError("The archive would unpack to far more than expected, so it was not unpacked.")
            for info in infos:
                target = folder / info.filename
                tmp = target.with_name(target.name + ".tmp")
                with z.open(info) as src, tmp.open("wb") as dst:
                    while block := src.read(CHUNK):
                        dst.write(block)
                os.replace(tmp, target)
                written.append(info.filename)
    except PermissionError as e:
        raise DownloadError("A file could not be replaced because it is in use. Close the app's language model (stop planning) and try again.") from e
    except zipfile.BadZipFile as e:
        raise DownloadError("The downloaded archive could not be opened.") from e
    return written
