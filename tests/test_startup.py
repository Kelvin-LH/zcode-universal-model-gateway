"""Startup helpers: first-run config bootstrap."""

from __future__ import annotations

from gateway.__main__ import ensure_config


def test_ensure_config_creates_from_example(tmp_path, monkeypatch):
    (tmp_path / "config.example.yaml").write_text("providers: {}\n", encoding="utf-8")
    target = tmp_path / "config.yaml"
    monkeypatch.chdir(tmp_path)
    ensure_config(str(target))
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "providers: {}\n"


def test_ensure_config_does_not_overwrite(tmp_path, monkeypatch):
    (tmp_path / "config.example.yaml").write_text("providers: {}\n", encoding="utf-8")
    target = tmp_path / "config.yaml"
    target.write_text("# user config\nproviders: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    ensure_config(str(target))
    assert target.read_text(encoding="utf-8").startswith("# user config")


def test_ensure_config_falls_back_to_bundled_example(tmp_path, monkeypatch):
    """No example next to the target: the bundled one is used instead.

    This is what makes a portable exe work: config.yaml is created beside the
    executable from the example shipped inside the bundle.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "config.example.yaml").write_text("providers: {}\n", encoding="utf-8")
    workdir = tmp_path / "app"
    workdir.mkdir()

    monkeypatch.setattr(
        "gateway.__main__.example_config_path", lambda: bundle / "config.example.yaml"
    )
    monkeypatch.chdir(workdir)
    ensure_config(str(workdir / "config.yaml"))
    assert (workdir / "config.yaml").read_text(encoding="utf-8") == "providers: {}\n"


def test_ensure_config_without_any_example_is_noop(tmp_path, monkeypatch):
    """Nothing to copy from: stay quiet and leave the file absent."""
    monkeypatch.setattr(
        "gateway.__main__.example_config_path", lambda: tmp_path / "nope.yaml"
    )
    workdir = tmp_path / "app"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    ensure_config(str(workdir / "config.yaml"))  # must not raise
    assert not (workdir / "config.yaml").exists()
