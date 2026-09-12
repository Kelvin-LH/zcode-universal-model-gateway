"""Startup helpers: first-run config bootstrap."""

from __future__ import annotations

from gateway.__main__ import ensure_config


def test_ensure_config_creates_from_example(tmp_path, monkeypatch):
    (tmp_path / "config.example.yaml").write_text("providers: {}\n", encoding="utf-8")
    target = tmp_path / "config.yaml"
    monkeypatch.chdir(tmp_path)
    ensure_config("config.yaml")
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "providers: {}\n"


def test_ensure_config_does_not_overwrite(tmp_path, monkeypatch):
    (tmp_path / "config.example.yaml").write_text("providers: {}\n", encoding="utf-8")
    target = tmp_path / "config.yaml"
    target.write_text("# user config\nproviders: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    ensure_config("config.yaml")
    assert target.read_text(encoding="utf-8").startswith("# user config")


def test_ensure_config_without_example_is_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ensure_config("config.yaml")  # must not raise
    assert not (tmp_path / "config.yaml").exists()
