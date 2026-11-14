"""Verified native Claude artifacts, shared by Installer and the updater."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
from subprocess import run
import urllib.request
from pathlib import Path

OFFICIAL = 'https://downloads.claude.ai/claude-code-releases'


def fetch(url: str, environment: dict | None = None) -> bytes:
    if environment is not None:
        result = run(['curl', '--fail', '--silent', '--show-error', '--location', '--retry', '2',
                      '--max-time', '60', url], env=environment, capture_output=True, timeout=200)
        if result.returncode:
            raise RuntimeError('Claude 官方元数据下载失败，请检查安装网络；旧版本保留')
        return result.stdout
    request = urllib.request.Request(url, headers={'User-Agent': 'codex-tg/dual-engine'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=60) as response:
        return response.read()


def latest_official(environment: dict | None = None) -> tuple[dict, str]:
    version = fetch(OFFICIAL + '/stable', environment).decode().strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise RuntimeError('invalid Claude version')
    manifest = json.loads(fetch(f'{OFFICIAL}/{version}/manifest.json', environment))
    assets = {}
    for arch, name in [('x86_64', 'linux-x64'), ('aarch64', 'linux-arm64')]:
        info = manifest['platforms'][name]
        assets[arch] = {'file': f'{name}/claude', 'sha256': info['checksum'], 'size': info['size']}
    return {'version': version, 'assets': assets}, f'{OFFICIAL}/{version}'


def prepare(metadata: dict, base_url: str, root: Path, environment: dict | None = None) -> Path:
    version = metadata['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise RuntimeError('invalid Claude version')
    arch = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(platform.machine(), platform.machine())
    item = metadata['assets'][arch]
    name = item['file']
    if Path(name).is_absolute() or '..' in Path(name).parts or not re.fullmatch(r'[a-zA-Z0-9./_-]+', name):
        raise RuntimeError('invalid Claude asset name')
    digest = item['sha256']
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise RuntimeError('invalid Claude digest')
    target = root / 'versions' / version
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
        # A previous interrupted install or manual chmod must not leave a
        # verified binary unusable by the systemd service.
        target.chmod(0o755)
        return target
    temporary = target.with_name(f'.{version}.{os.getpid()}')
    try:
        request = urllib.request.Request(f'{base_url}/{name}', headers={'User-Agent': 'codex-tg/dual-engine'})
        hasher = hashlib.sha256()
        total = 0
        if environment is not None:
            result = run(['curl', '--fail', '--silent', '--show-error', '--location', '--retry', '2',
                          '--max-time', '600', '--max-filesize', str(512 * 1024 * 1024),
                          f'{base_url}/{name}', '-o', str(temporary)], env=environment, capture_output=True, timeout=650)
            if result.returncode:
                raise RuntimeError('Claude 官方文件下载失败，旧版本保留')
            with temporary.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    hasher.update(chunk)
                    total += len(chunk)
        else:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=60) as response, temporary.open('xb') as output:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 512 * 1024 * 1024:
                        raise RuntimeError('Claude download exceeds limit')
                    hasher.update(chunk)
                    output.write(chunk)
        if hasher.hexdigest() != digest or (item.get('size') and total != item['size']):
            raise RuntimeError('Claude SHA-256 or size mismatch')
        temporary.chmod(0o755)
        probe = run([str(temporary), '--version'], text=True, capture_output=True, timeout=15, check=True)
        if not probe.stdout.startswith(version + ' '):
            raise RuntimeError('Claude version mismatch')
        help_text = run([str(temporary), '--help'], text=True, capture_output=True, timeout=15, check=True).stdout
        if not all(flag in help_text for flag in ('--effort', '--session-id', '--resume', '--permission-mode', '--dangerously-skip-permissions')):
            raise RuntimeError('Claude CLI lacks required session/effort support')
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def activate(target: Path, root: Path) -> None:
    link = root / f'.current-{os.getpid()}'
    link.unlink(missing_ok=True)
    link.symlink_to(target.resolve())
    link.replace(root / 'current')


def install_bundled(release: Path, base_url: str, root: Path) -> Path:
    metadata = json.loads((release / 'CLAUDE.json').read_text())
    target = prepare(metadata, base_url, root)
    activate(target, root)
    return target
