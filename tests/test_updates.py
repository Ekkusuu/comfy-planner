"""The update check: comparing versions, and GET /api/update-check with GitHub mocked (no network)."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import updates
from app.main import create_app
from app.mcp_server import PROGRAM_SETTINGS
from app.version import __version__


@pytest.mark.parametrize("latest,current,newer", [
    ("0.2.0", "0.1.0", True),
    ("v0.2.0", "0.1.0", True),             # a leading v is ignored
    ("0.1.10", "0.1.9", True),             # numbers, not text
    ("1.0.0", "0.9.9", True),
    ("0.1.0", "0.1.0", False),
    ("v0.1.0", "0.1.0", False),
    ("0.0.9", "0.1.0", False),
    ("0.2.0-rc.1", "0.1.0", False),        # a pre-release is not offered
    ("0.1.0", "0.1.0-rc.1", True),         # but the final release is newer than its own pre-release
    ("nonsense", "0.1.0", False),
    ("0.2.0", "nonsense", False),
])
def test_is_newer(latest, current, newer):
    assert updates.is_newer(latest, current) is newer


def release(tag="v9.9.9", **extra):
    return {"tag_name": tag, "html_url": f"https://github.com/Ekkusuu/comfy-planner/releases/tag/{tag}", "body": "What is new", **extra}


def github(status=200, body=None, raises=None):
    """A mocked GitHub. `seen` collects the requests."""
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if raises:
            raise raises
        return httpx.Response(status, json=body if body is not None else release())
    handler.seen = seen
    return handler


@pytest.fixture
def make_client(config, comfy, tmp_path):
    """Builds clients whose update check talks to the given mocked GitHub; they are closed after the test."""
    opened = []

    def make(handler, repo=None):
        if repo is not None:
            config.update_repo = repo
        client = TestClient(create_app(config, comfy=comfy, start_worker=False, update_transport=httpx.MockTransport(handler),
                                       config_path=tmp_path / "app.toml"))     # never the real config/app.toml
        client.__enter__()
        opened.append(client)
        return client
    yield make
    for client in opened:
        client.__exit__(None, None, None)


def test_a_newer_release_is_reported(make_client):
    handler = github()
    answer = make_client(handler).get("/api/update-check").json()
    assert answer["newer"] is True and answer["latest"] == "9.9.9" and answer["current"] == __version__
    assert answer["url"] == "https://github.com/Ekkusuu/comfy-planner/releases/tag/v9.9.9"
    assert answer["notes"] == "What is new" and answer["checked_at"]
    sent = handler.seen[0]
    assert str(sent.url) == "https://api.github.com/repos/Ekkusuu/comfy-planner/releases/latest"
    assert sent.headers["user-agent"] == "comfy-planner-update-check"
    assert "authorization" not in sent.headers


def test_the_same_version_is_not_newer(make_client):
    answer = make_client(github(body=release(f"v{__version__}"))).get("/api/update-check").json()
    assert answer["newer"] is False and answer["latest"] == __version__


def test_an_older_release_is_not_newer(make_client):
    assert make_client(github(body=release("v0.0.1"))).get("/api/update-check").json()["newer"] is False


@pytest.mark.parametrize("flag", ["prerelease", "draft"])
def test_a_prerelease_or_draft_is_never_newer(make_client, flag):
    answer = make_client(github(body=release("v9.9.9", **{flag: True}))).get("/api/update-check").json()
    assert answer["newer"] is False


def test_a_prerelease_tag_is_never_newer(make_client):
    assert make_client(github(body=release("v9.9.9-beta.1"))).get("/api/update-check").json()["newer"] is False


def test_notes_are_cut_to_500_characters(make_client):
    answer = make_client(github(body=release(body="x" * 2000))).get("/api/update-check").json()
    assert len(answer["notes"]) == 500


def test_a_release_page_outside_github_is_not_linked(make_client):
    answer = make_client(github(body=release(html_url="https://evil.example/download"))).get("/api/update-check").json()
    assert answer["url"] == "https://github.com/Ekkusuu/comfy-planner/releases"


@pytest.mark.parametrize("status,words", [(404, "no release"), (403, "limiting"), (429, "limiting"), (500, "error")])
def test_github_problems_come_back_as_a_message(make_client, status, words):
    res = make_client(github(status=status, body={"message": "x"})).get("/api/update-check")
    answer = res.json()
    assert res.status_code == 200 and words in answer["error"].lower() and answer["current"] == __version__
    assert "latest" not in answer and "newer" not in answer


def test_offline_and_timeout_come_back_as_a_message(make_client):
    offline = make_client(github(raises=httpx.ConnectError("no network"))).get("/api/update-check").json()
    assert "internet" in offline["error"]
    slow = make_client(github(raises=httpx.ReadTimeout("slow"))).get("/api/update-check").json()
    assert "in time" in slow["error"]


def test_an_unreadable_answer_is_a_message(make_client):
    assert "not understood" in make_client(github(body=["not", "a", "release"])).get("/api/update-check").json()["error"]
    assert "not a version" in make_client(github(body=release("nightly"))).get("/api/update-check").json()["error"]


def test_an_empty_repo_turns_checking_off_without_any_request(make_client):
    handler = github()
    answer = make_client(handler, repo="").get("/api/update-check").json()
    assert "turned off" in answer["error"] and handler.seen == []


def test_a_malformed_repo_is_refused_without_any_request(make_client):
    handler = github()
    answer = make_client(handler, repo="../../etc").get("/api/update-check").json()
    assert "owner/name" in answer["error"] and handler.seen == []


def test_the_answer_is_kept_for_an_hour_and_errors_are_not(make_client, monkeypatch):
    handler = github()
    client = make_client(handler)
    client.get("/api/update-check")
    client.get("/api/update-check")
    assert len(handler.seen) == 1                                  # the second answer came from memory
    clock = updates.time.monotonic() + updates.CACHE_SECONDS + 1
    monkeypatch.setattr(updates.time, "monotonic", lambda: clock)
    client.get("/api/update-check")
    assert len(handler.seen) == 2                                  # an hour later it asks again

    failing = github(status=500)
    client = make_client(failing)
    client.get("/api/update-check")
    client.get("/api/update-check")
    assert len(failing.seen) == 2                                  # a failure is retried at once


def test_the_start_check_is_a_setting_the_user_changes_but_an_agent_does_not(make_client):
    client = make_client(github())
    assert client.get("/api/config").json()["check_updates_at_start"] is False
    assert client.put("/api/config", json={"check_updates_at_start": "yes"}).status_code == 422
    assert client.put("/api/config", json={"check_updates_at_start": True}).json()["check_updates_at_start"] is True
    assert "check_updates_at_start" in PROGRAM_SETTINGS         # update_settings (MCP) refuses it
