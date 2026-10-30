"""bot_startup.log FileHandler is skipped under supervisor.py (KICKBOT_SUPERVISED=1)."""

import logging

import pytest

from core.logging_setup import setup_logging


@pytest.fixture
def restore_root():
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    yield
    for h in root.handlers:
        if h not in saved:
            h.close()
    root.handlers[:] = saved
    root.setLevel(level)


def _file_handlers():
    return [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]


def test_supervised_skips_bot_startup_log(tmp_path, monkeypatch, restore_root):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KICKBOT_SUPERVISED", "1")
    setup_logging("INFO")
    assert _file_handlers() == []
    assert not (tmp_path / "bot_startup.log").exists()


def test_standalone_keeps_bot_startup_log(tmp_path, monkeypatch, restore_root):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KICKBOT_SUPERVISED", raising=False)
    setup_logging("INFO")
    handlers = _file_handlers()
    assert len(handlers) == 1
    assert handlers[0].baseFilename == str(tmp_path / "bot_startup.log")
