"""Editing settings: PUT /api/config validates, writes config/app.toml with its comments intact, and applies at once."""

import pytest
from fastapi.testclient import TestClient

from app import settings
from app.config import CONFIG_PATH, load_config
from app.main import create_app

EXAMPLE_PATH = CONFIG_PATH.with_name("app.example.toml")      # the tracked template: tests never need the owner's own app.toml


@pytest.fixture
def toml_path(tmp_path):
    """A copy of the example settings file (CRLF, comments and all), with an install folder that exists."""
    path = tmp_path / "app.toml"
    (tmp_path / "comfy").mkdir()
    text = EXAMPLE_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
    start = text.index("install_dir = ")
    end = text.index("\r\n", start)
    path.write_bytes((text[:start] + f"install_dir = '{tmp_path / 'comfy'}'" + text[end:]).encode("utf-8"))
    return path


@pytest.fixture
def client(toml_path, comfy, tmp_path):
    config = load_config(toml_path)
    config.data_dir = tmp_path / "data"
    with TestClient(create_app(config, comfy=comfy, start_worker=False, config_path=toml_path)) as c:
        yield c


def comments(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith("#")]


def test_put_config_round_trip_keeps_comments_and_layout(client, toml_path):
    before = toml_path.read_bytes()
    res = client.put("/api/config", json={"max_images_per_job": 24, "hires_default": False, "llm_ctx_size": 16384,
                                          "safety_negative": ["blurry", " ugly ", "blurry"], "comfy_cli": "comfy-cli"})
    assert res.status_code == 200
    body = res.json()
    assert body["max_images_per_job"] == 24 and body["hires_default"] is False and body["safety_negative"] == ["blurry", "ugly"]
    assert set(body["changed"]) == {"max_images_per_job", "hires_default", "llm_ctx_size", "safety_negative", "comfy_cli"}

    after = toml_path.read_bytes()
    assert b"\r\n" in after and b"\n" not in after.replace(b"\r\n", b"")      # line endings kept
    assert comments(before.decode()) == comments(after.decode())
    assert "# hires pass on unless a job says otherwise" in after.decode() and "# command used by" in after.decode()
    reloaded = load_config(toml_path)
    assert reloaded.max_images_per_job == 24 and reloaded.hires_default is False and reloaded.llm_ctx_size == 16384
    assert reloaded.safety_negative == ["blurry", "ugly"] and reloaded.comfy_cli == "comfy-cli"
    # untouched lines are byte for byte the same
    assert after.count(b"\r\n") == before.count(b"\r\n")
    strip = next(line for line in before.split(b"\r\n") if line.startswith(b"strip_from_default_negative"))
    assert strip in after and b"max_loras = 3" in after                     # the copy follows config/app.example.toml


def test_images_per_request_has_a_default_and_can_be_changed(client, toml_path):
    assert client.get("/api/config").json()["max_images_per_request"] == 200          # the code default: the settings file doesn't need the line
    res = client.put("/api/config", json={"max_images_per_request": 500})
    assert res.status_code == 200 and res.json()["changed"] == ["max_images_per_request"]
    assert "max_images_per_request = 500" in toml_path.read_text(encoding="utf-8") and load_config(toml_path).max_images_per_request == 500


def test_unchanged_values_leave_the_file_untouched(client, toml_path):
    before = toml_path.read_bytes()
    cfg = client.get("/api/config").json()
    res = client.put("/api/config", json={"max_loras": cfg["max_loras"], "llm_ctx_size": cfg["llm_ctx_size"],
                                          "llm_start_timeout": cfg["llm_start_timeout"], "llm_binary": cfg["llm_binary"],
                                          "strip_from_default_negative": cfg["strip_from_default_negative"]})
    assert res.status_code == 200 and res.json()["changed"] == []
    assert toml_path.read_bytes() == before


def test_changes_apply_without_a_restart(client, comfy, toml_path):
    live = client.app.state.config
    assert client.put("/api/config", json={"comfy_url": "http://127.0.0.1:9999/", "allow_draft_cards": True,
                                           "llm_model": "qwen"}).status_code == 200
    assert live.comfy_url == "http://127.0.0.1:9999" and live.allow_draft_cards is True and live.llm_model == "qwen"
    assert comfy.base_url == "http://127.0.0.1:9999"                        # the ComfyUI client was re-pointed
    assert client.get("/api/config").json()["llm_model"] == "qwen"


def test_llm_models_table_is_edited_in_place(client, toml_path):
    models = {m["name"]: m["path"] for m in client.get("/api/config").json()["llm_models"]}
    models["gemma"] = "models/other/gemma.gguf"
    res = client.put("/api/config", json={"llm_models": models})
    assert res.status_code == 200
    text = toml_path.read_text(encoding="utf-8")
    assert 'gemma = "models/other/gemma.gguf"' in text
    assert 'qwen = "models/llm/qwen-9b-instruct-q6_k.gguf"' in text   # unchanged, still relative
    assert load_config(toml_path).llm_models["gemma"].as_posix().endswith("models/other/gemma.gguf")


@pytest.mark.parametrize("patch,field", [
    ({"max_images_per_job": 0}, "max_images_per_job"),
    ({"max_images_per_job": 2.5}, "max_images_per_job"),
    ({"max_images_per_request": 0}, "max_images_per_request"),
    ({"max_loras": True}, "max_loras"),
    ({"hires_default": "yes"}, "hires_default"),
    ({"comfy_url": "localhost:8188"}, "comfy_url"),
    ({"comfy_install_dir": "C:/definitely/not/here"}, "comfy_install_dir"),
    ({"llm_temperature": 5}, "llm_temperature"),
    ({"llm_model": "llama"}, "llm_model"),
    ({"llm_models": {}}, "llm_models"),
    ({"safety_negative": "blurry"}, "safety_negative"),
    ({"host": "0.0.0.0"}, "host"),
    ({"port": 9000}, "port"),
    ({"data_dir": "elsewhere"}, "data_dir"),
    ({"nope": 1}, "nope"),
])
def test_invalid_changes_are_refused_and_nothing_is_written(client, toml_path, patch, field):
    before = toml_path.read_bytes()
    drafts = client.get("/api/config").json()["allow_draft_cards"]             # the copy follows config/app.example.toml
    res = client.put("/api/config", json={**patch, "allow_draft_cards": not drafts})    # a valid change alongside is not applied either
    assert res.status_code == 422
    body = res.json()
    assert field in body["errors"] and body["detail"]
    assert toml_path.read_bytes() == before and client.get("/api/config").json()["allow_draft_cards"] is drafts


def test_removing_the_chosen_model_is_refused():
    cfg = load_config(EXAMPLE_PATH)
    with pytest.raises(settings.SettingsError) as e:
        settings.validate({"llm_models": {"qwen": "models/q.gguf"}}, cfg)         # "gemma" is the chosen one
    assert "llm_models" in e.value.errors


def test_civitai_token_is_reported_but_never_returned(client, monkeypatch):
    monkeypatch.setenv("CIVITAI_API_TOKEN", "secret-value-123")
    res = client.get("/api/config")
    assert res.json()["civitai_token_found"] is True and "secret-value-123" not in res.text
    monkeypatch.setattr("app.main.get_token", lambda: None)
    assert client.get("/api/config").json()["civitai_token_found"] is False


def test_a_missing_settings_file_means_defaults_and_saving_creates_it(comfy, tmp_path):
    missing = tmp_path / "config" / "app.toml"
    missing.parent.mkdir()
    config = load_config(missing)                                   # no file: built-in defaults
    assert config.max_loras == 3 and config.comfy_install_dir == ""
    config.data_dir = tmp_path / "data"
    with TestClient(create_app(config, comfy=comfy, start_worker=False, config_path=missing)) as c:
        assert c.get("/api/config").status_code == 200
        res = c.put("/api/config", json={"max_loras": 2})
        assert res.status_code == 200
    assert missing.exists() and load_config(missing).max_loras == 2
