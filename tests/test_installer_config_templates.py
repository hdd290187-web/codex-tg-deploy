from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import tomllib


ROOT = Path(__file__).resolve().parents[1]


def installer_config_python() -> str:
    source = (ROOT / 'installer/install.sh').read_text(encoding='utf-8')
    marker = "PROXY_RESULT=\"$TMP_DIR/proxy-result.json\" \"$FINAL_RELEASE/.venv/bin/python\" - 2>\"$TMP_DIR/config-install.log\" <<'PY'\n"
    start = source.index(marker) + len(marker)
    end = source.index('\nPY\n', start)
    return source[start:end]


def test_installer_merges_deployment_values_into_both_templates(tmp_path: Path) -> None:
    home = tmp_path / 'codex-home'
    target_env = tmp_path / 'codex-tg.env'
    claim_path = tmp_path / 'claim.json'
    proxy_path = tmp_path / 'proxy.json'
    claude_path = tmp_path / 'claude-settings.json'
    claim_path.write_text(json.dumps({
        'telegram_bot_token': '123456:A' * 5,
        'allowed_user_id': 7,
        'allowed_chat_id': 9,
        'model': 'relay-codex-model',
        'reasoning_effort': 'high',
        'plan_reasoning_effort': 'xhigh',
        'gateway_version': 'test',
        'release_base_url': 'https://center.invalid/releases',
        'base_url': 'https://codex.invalid/v1',
        'api_key': 'codex-secret',
        'claude_model': 'claude-sonnet-4-6',
        'claude_effort': 'xhigh',
        'claude_config_template': json.dumps({
            'permissions': {'defaultMode': 'bypassPermissions'},
            'modelSettings': {'effortLevel': 'low'},
            'mcpServers': {},
        }),
    }), encoding='utf-8')
    proxy_path.write_text(json.dumps({'network': 'DIRECT', 'kind': 'none', 'proxy_url': ''}), encoding='utf-8')
    env = {
        **os.environ,
        'CLAIM_FILE': str(claim_path),
        'PROXY_RESULT': str(proxy_path),
        'CODEX_HOME_DIR': str(home),
        'TARGET_ENV': str(target_env),
        'RELEASE_PUBLIC_KEY': str(tmp_path / 'release.pub'),
    }
    block = installer_config_python().replace(
        "pathlib.Path('/opt/codex-tg/data/claude/settings.json')",
        f'pathlib.Path({str(claude_path)!r})',
    )
    result = subprocess.run([
        str(ROOT / '.venv/bin/python'), '-c', block,
    ], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    codex = tomllib.loads((home / 'config.toml').read_text(encoding='utf-8'))
    assert codex['model_provider'] == 'codex_tg'
    assert codex['model'] == 'relay-codex-model'
    assert codex['model_providers']['codex_tg']['base_url'] == 'https://codex.invalid/v1'
    claude = json.loads(claude_path.read_text(encoding='utf-8'))
    assert claude['model'] == 'claude-sonnet-4-6'
    assert claude['effortLevel'] == 'xhigh'
    assert claude['modelSettings'] == {}
    assert claude['permissions']['defaultMode'] == 'bypassPermissions'
