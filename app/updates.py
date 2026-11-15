"""Asking GitHub whether a newer release of Comfy Planner exists.

Nothing here runs unless the user asks (the "Check for updates" button) or has turned on `check_updates_at_start`. The request
is one anonymous GET of the repository's latest release; it carries no personal data. The answer is kept in memory for an
hour so repeated clicks don't hit GitHub's rate limit. Errors are never kept, so the next click tries again."""
import re
import time
from datetime import datetime, timezone

import httpx

CACHE_SECONDS = 3600
TIMEOUT = 10.0
USER_AGENT = "comfy-planner-update-check"
REPO_PATTERN = re.compile(r"^(?!\.+/)[A-Za-z0-9_.-]+/(?!\.+$)[A-Za-z0-9_.-]+$")   # owner/name, never ".." parts
NOTES_CHARS = 500
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def project_url(repo: str) -> str:
    """The repository's page on GitHub, or "" when `repo` is empty or not owner/name."""
    repo = (repo or "").strip()
    return f"https://github.com/{repo}" if REPO_PATTERN.match(repo) else ""


def parse_version(text: str):
    """(major, minor, patch, prerelease) from "1.2.3", "v1.2.3" or "1.2.3-rc.1", else None."""
    m = _VERSION.match((text or "").strip())
    return (int(m[1]), int(m[2]), int(m[3]), m[4] or "") if m else None


def is_newer(latest: str, current: str) -> bool:
    """True when `latest` is a higher release than `current`. A pre-release (1.2.3-rc.1) is never offered as an update; a
    current version that is a pre-release is older than the same number without the suffix."""
    new, old = parse_version(latest), parse_version(current)
    if new is None or old is None or new[3]:
        return False
    return (new[:3], 1) > (old[:3], 0 if old[3] else 1)


class UpdateChecker:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport                 # tests pass httpx.MockTransport; the app uses the real network
        self._cache: dict[str, tuple[float, dict]] = {}   # repo -> (time stored, the release facts)

    async def check(self, repo: str, current: str) -> dict:
        """{current, latest, newer, url, notes, checked_at} or {current, error}. Never raises."""
        repo = (repo or "").strip()
        if not repo:
            return {"current": current, "error": "Update checking is turned off (update_repo is empty in config/app.toml)."}
        if not REPO_PATTERN.match(repo):
            return {"current": current, "error": "update_repo in config/app.toml must look like owner/name."}
        hit = self._cache.get(repo)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            release = hit[1]
        else:
            release = await self._fetch(repo)
            if "error" in release:
                return {"current": current, "error": release["error"]}
            self._cache[repo] = (time.monotonic(), release)
        return {"current": current, "latest": release["latest"], "newer": release["offered"] and is_newer(release["latest"], current),
                "url": release["url"], "notes": release["notes"], "checked_at": release["checked_at"]}

    async def _fetch(self, repo: str) -> dict:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT,
                                         headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}) as client:
                res = await client.get(f"https://api.github.com/repos/{repo}/releases/latest")
        except httpx.TimeoutException:
            return {"error": "GitHub did not answer in time. Try again later."}
        except httpx.HTTPError:
            return {"error": "Couldn't reach GitHub. Check your internet connection."}
        if res.status_code == 404:
            return {"error": f"No release was found for {repo}: the repository doesn't exist yet, or it has no published release."}
        if res.status_code in (403, 429):
            return {"error": "GitHub is limiting requests from this connection right now. Try again in a while."}
        if res.status_code != 200:
            return {"error": f"GitHub answered with an error ({res.status_code}). Try again later."}
        try:
            data = res.json()
            tag = str(data["tag_name"])
        except (ValueError, KeyError, TypeError):
            return {"error": "GitHub's answer was not understood."}
        if parse_version(tag) is None:
            return {"error": f"The latest release is called '{tag[:40]}', which is not a version number."}
        url = str(data.get("html_url") or "")
        if not url.startswith("https://github.com/"):       # only ever link to GitHub itself
            url = f"{project_url(repo)}/releases"
        # a draft or pre-release is shown but never counted as newer (GitHub's "latest" route doesn't return them anyway)
        return {"latest": tag.lstrip("v"), "offered": not (data.get("draft") or data.get("prerelease")), "url": url, "notes": str(data.get("body") or "")[:NOTES_CHARS].strip(),
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
