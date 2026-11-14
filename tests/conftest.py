from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deployment-center"))
sys.path.insert(0, str(ROOT / "gateway"))


@pytest.fixture(autouse=True)
def isolate_codex_home(tmp_path, monkeypatch):
    home = tmp_path / 'isolated-codex-home'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
