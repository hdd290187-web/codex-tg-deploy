from __future__ import annotations

import importlib.metadata as metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def codex_components() -> dict:
    from codex_cli_bin import bundled_codex_path
    runtime = Path(bundled_codex_path()).resolve()
    global_cli = shutil.which('codex')
    def version(path):
        try:
            result = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=10)
            return result.stdout.strip() if result.returncode == 0 else '不可执行'
        except (OSError, subprocess.TimeoutExpired):
            return '不可执行'
    result = {'python': sys.executable, 'sdk': metadata.version('openai-codex'),
              'runtime_package': metadata.version('openai-codex-cli-bin'), 'runtime_path': str(runtime),
              'runtime_version': version(runtime), 'cli_path': global_cli or '未安装',
              'cli_version': version(global_cli) if global_cli else '未安装'}
    resolved = str(Path(global_cli).resolve()) if global_cli else ''
    managed = False
    if global_cli:
        with Path(global_cli).open('rb') as source:
            managed = b'Codex TG managed' in source.read(512)
    result['cli_installation'] = ('Gateway 管理' if '/opt/codex-tg/' in resolved or managed else
                                  'npm' if 'node_modules' in resolved or '/.nvm/' in resolved else
                                  'native/手动' if global_cli else '未安装')
    result['paired'] = result['sdk'] == result['runtime_package'] and result['runtime_package'] in result['runtime_version']
    return result


if __name__ == '__main__':
    if sys.argv[1:2] == ['--exec-codex']:
        from codex_cli_bin import bundled_codex_path
        binary = str(bundled_codex_path())
        os.execv(binary, [binary, *sys.argv[2:]])
    else:
        print(json.dumps(codex_components(), ensure_ascii=False))
