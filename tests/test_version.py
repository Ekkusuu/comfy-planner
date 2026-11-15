"""The version number: one source (pyproject.toml), a VERSION file in an installed copy, and where the app shows it."""
import tomllib

from fastapi.testclient import TestClient

from app import version
from app.config import ROOT
from app.main import create_app


def test_version_file_wins_over_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    (tmp_path / "VERSION").write_text("2.3.4\n", encoding="utf-8")
    assert version.read_version(tmp_path) == "2.3.4"


def test_version_file_with_a_byte_order_mark_is_read(tmp_path):
    (tmp_path / "VERSION").write_bytes("﻿0.2.0".encode("utf-8"))
    assert version.read_version(tmp_path) == "0.2.0"


def test_pyproject_is_used_without_a_version_file(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0.0"\n', encoding="utf-8")
    assert version.read_version(tmp_path) == "1.0.0"


def test_a_garbled_version_file_falls_back_to_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    (tmp_path / "VERSION").write_text("not a version", encoding="utf-8")
    assert version.read_version(tmp_path) == "1.0.0"


def test_zero_when_nothing_can_be_read(tmp_path):
    assert version.read_version(tmp_path) == "0.0.0"
    (tmp_path / "pyproject.toml").write_text("this is not toml [", encoding="utf-8")
    assert version.read_version(tmp_path) == "0.0.0"


def test_the_checkout_version_is_the_one_in_pyproject():
    expected = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert version.read_version() == expected
    if not (ROOT / "VERSION").exists():
        assert version.__version__ == expected


def test_health_and_config_report_the_version(config, comfy):
    with TestClient(create_app(config, comfy=comfy, start_worker=False)) as client:
        assert client.get("/api/health").json()["version"] == version.__version__
        shown = client.get("/api/config").json()
        assert shown["version"] == version.__version__
        assert shown["project_url"] == "https://github.com/Ekkusuu/comfy-planner"
        assert shown["check_updates_at_start"] is False      # off unless the user turns it on
