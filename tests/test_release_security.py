from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_updater():
    path = ROOT / "gateway/bin/codex-tg-updater"
    loader = SourceFileLoader("codex_tg_updater", str(path))
    spec = importlib.util.spec_from_loader("codex_tg_updater", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_updater_rejects_archive_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("../../escape")
        payload = b"bad"
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
    updater = load_updater()
    with pytest.raises(RuntimeError, match="unsafe archive path"):
        updater.safe_extract(archive, tmp_path / "extract")
    assert not (tmp_path.parent / "escape").exists()


def test_published_release_has_unique_safe_entries() -> None:
    latest = json.loads((ROOT / "releases/latest.json").read_text("utf-8"))
    archive = ROOT / "releases" / latest["archive"]
    with tarfile.open(archive, "r:gz") as bundle:
        names = bundle.getnames()
        assert len(names) == len(set(names))
        assert not any(Path(name).is_absolute() or ".." in Path(name).parts for name in names)


def test_release_builder_excludes_bytecode(tmp_path: Path) -> None:
    path = ROOT / "release-tools/release.py"
    loader = SourceFileLoader("codex_tg_release_builder", str(path))
    spec = importlib.util.spec_from_loader("codex_tg_release_builder", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archive = tmp_path / "candidate.tar.gz"
    module.build_archive("9.9.9", archive)
    with tarfile.open(archive, "r:gz") as bundle:
        names = bundle.getnames()
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
