"""Isolated real Codex package install, SDK smoke, switch and health rollback.

Only systemctl/health and the signed base wheelhouse location are substituted.
Nothing under /opt/codex-tg or /root/.local/bin is changed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    loader = SourceFileLoader('isolated_updater', str(ROOT / 'gateway/bin/codex-tg-updater'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    updater = importlib.util.module_from_spec(spec)
    loader.exec_module(updater)
    with tempfile.TemporaryDirectory(prefix='codex-update-live-audit-') as name:
        root = Path(name)
        data = root / 'data'
        data.mkdir()
        release = root / 'releases/fixture'
        shutil.copytree(ROOT / 'gateway', release, ignore=shutil.ignore_patterns('__pycache__'))
        (release / 'VERSION').write_text('2.2.0\n')
        (release / 'WHEELHOUSE.json').write_text('{}')
        (root / 'current').symlink_to(release)
        # Use a disposable old version to exercise a real 0.154 -> 0.162 upgrade.
        subprocess.run([sys.executable, '-m', 'venv', str(release / '.venv')], check=True)
        subprocess.run([str(release / '.venv/bin/python'), '-m', 'pip', 'install', '-q',
                        '-r', str(release / 'requirements.lock')], check=True)
        subprocess.run([str(release / '.venv/bin/python'), '-m', 'pip', 'install', '-q',
                        'openai-codex==0.154.0', 'openai-codex-cli-bin==0.154.0'], check=True)
        wheels = root / 'base-wheelhouse'
        subprocess.run([sys.executable, '-m', 'pip', 'download', '--quiet', '--only-binary=:all:',
                        '--dest', str(wheels), '-r', str(release / 'requirements.lock')], check=True)
        updater.ROOT, updater.CURRENT, updater.RELEASES = root, root / 'current', root / 'releases'
        updater.CLI_WRAPPER = root / 'bin/codex'
        updater.UPDATE_STATUS = data / 'update-status.json'
        updater.prepare_wheelhouse = lambda *_args: wheels
        updater.notify = lambda text: False
        updater.current_version = lambda: '2.2.0'
        real_run = subprocess.run
        calls = []
        def run(command, *args, **kwargs):
            if str(command[0]) == 'systemctl':
                calls.append([str(x) for x in command])
                return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
            return real_run(command, *args, **kwargs)
        updater.subprocess.run = run
        old_environment = dict(os.environ)
        os.environ.update(TELEGRAM_BOT_TOKEN='123456:fixture-no-network', ALLOWED_USER_ID='7', ALLOWED_CHAT_ID='9',
                          CODEX_TG_DATABASE=str(data / 'gateway.db'), CODEX_TG_RELEASE_BASE_URL='https://release.invalid',
                          CODEX_TG_MODEL='gpt-5.6-sol', CODEX_TG_UPLOAD_DIR=str(data / 'uploads'), CODEX_TG_LOG_DIR=str(root / 'logs'))
        for key in ('CODEX_TG_BASE_URL', 'CODEX_TG_API_KEY', 'CODEX_TG_TELEGRAM_PROXY_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'TAVILY_API_KEY'):
            os.environ.pop(key, None)
        sys.path.insert(0, str(release))
        from codex_tg.store import Store
        from codex_tg.pre_instructions import PromptStore
        store = Store(data / 'gateway.db')
        store.ensure_chat(7, 9)
        store.set_thread(9, 'preserved-thread')
        store.set_engine_session(9, 'claude', 'preserved-claude-session')
        prompt = PromptStore(data / 'codex-pre-instructions.txt')
        revision = prompt.save('  exact instructions\r\n')
        store.set_codex_prompt_state(enabled=True, updated_at=1234, length=22, sha256=revision)
        before = hashlib.sha256(prompt.path.read_bytes()).hexdigest()
        try:
            updater.wait_healthy = lambda _version: None
            result, _ = updater.update_codex()
            assert '0.154.0' in result and '0.162.1' in result
            version = real_run([str(updater.CLI_WRAPPER), '--version'], capture_output=True, text=True, check=True).stdout
            assert '0.162.1' in version
            health_calls = []
            def unhealthy_then_healthy(_version):
                health_calls.append(_version)
                if len(health_calls) == 1:
                    raise RuntimeError('isolated injected health failure')
            updater.wait_healthy = unhealthy_then_healthy
            # Reuse official downloaded wheels through pip's normal cache; all
            # candidate validation and filesystem switching still execute.
            try:
                updater.update_codex()
            except RuntimeError as exc:
                assert '已回滚且健康检查通过' in str(exc), str(exc)
            else:
                raise AssertionError('expected injected health failure')
            assert len(health_calls) == 2
            assert '0.162.1' in real_run([str(updater.CLI_WRAPPER), '--version'], capture_output=True, text=True, check=True).stdout
            assert before == hashlib.sha256(prompt.path.read_bytes()).hexdigest()
            assert store.thread_id(9) == 'preserved-thread'
            assert store.engine_session(9, 'claude') == 'preserved-claude-session'
            report = {'real_official_download_install': True, 'from_sdk': '0.154.0', 'to_sdk': '0.162.1',
                      'runtime_and_cli_match': True, 'sdk_local_app_server_smoke': True,
                      'atomic_switch': True, 'injected_health_failure_rollback': True, 'configuration_preserved': True,
                      'systemd': 'isolated stub, no production service stopped', 'systemctl_calls': calls}
            (ROOT / 'tests/artifacts/codex-update-live-audit.json').write_text(json.dumps(report, indent=2) + '\n')
            print('CODEX_UPDATE_AUDIT_OK ' + json.dumps(report))
        finally:
            updater.subprocess.run = real_run
            os.environ.clear()
            os.environ.update(old_environment)


if __name__ == '__main__':
    main()
