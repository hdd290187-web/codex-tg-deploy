from __future__ import annotations

import json
from pathlib import Path

from codex_cli_bin import bundled_codex_path
from codex_deploy.app import default_config_template
from codex_tg.config_audit import audit_claude, audit_codex


ROOT = Path(__file__).resolve().parents[1]
CLAUDE = ROOT / 'releases/claude-2.1.283-linux-x64'


def test_default_codex_template_loads_in_bundled_runtime(tmp_path: Path) -> None:
    path = tmp_path / 'config.toml'
    path.write_text(default_config_template('codex'), encoding='utf-8')
    result = audit_codex(path, str(bundled_codex_path()))
    assert result.state == '正常'
    assert 'Responses' in result.detail


def test_claude_template_uses_official_model_settings_shape(tmp_path: Path) -> None:
    path = tmp_path / 'settings.json'
    path.write_text(default_config_template('claude'), encoding='utf-8')
    result = audit_claude(path, str(CLAUDE))
    assert result.state == '正常'
    assert 'bypassPermissions' in result.detail

    old = json.loads('{"modelSettings":{"effortLevel":"high"}}')
    path.write_text(json.dumps(old), encoding='utf-8')
    result = audit_claude(path, str(CLAUDE))
    assert result.state == '异常'
    assert 'modelSettings.effortLevel' in result.detail


def test_codex_audit_reports_ignored_newer_cli_keys(tmp_path: Path) -> None:
    path = tmp_path / 'config.toml'
    path.write_text(
        'model_provider = "fixture"\nmodel = "fixture"\nnewer_cli_only = true\n\n'
        '[model_providers.fixture]\nname = "Fixture"\nbase_url = "https://example.invalid/v1"\n',
        encoding='utf-8',
    )
    result = audit_codex(path, str(bundled_codex_path()))
    assert result.state == '警告'
    assert '忽略配置项' in result.detail
