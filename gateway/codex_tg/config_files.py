"""Atomic local config files and conservative, credential-free exports."""
from __future__ import annotations
import copy
import os
import re
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def config_target(path: Path) -> Path:
    target = path.resolve(strict=path.is_symlink())
    if target.exists():
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8 * 1024 * 1024:
            raise ValueError('配置必须是小于 8 MiB 的独立普通文件')
        if os.geteuid() == 0 and (info.st_uid != 0 or info.st_mode & 0o022):
            raise ValueError('配置目标必须由 root 拥有且其他用户不可写')
    for parent in target.parents:
        if parent.exists() and parent.stat().st_mode & 0o002 and not parent.stat().st_mode & stat.S_ISVTX:
            raise ValueError('配置目录可被其他用户写入')
    return target


def atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def redact_config(value):
    # Unknown env/header/argument names can hold opaque credentials. Export
    # these containers conservatively; preserve env_key *names*, not values.
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            lower = re.sub(r'[^a-z]', '', key.lower())
            sensitive = lower != 'envkey' and (any(x in lower for x in (
                'key', 'token', 'password', 'secret', 'credential', 'authorization', 'cookie', 'auth'))
                or lower in {'env', 'headers', 'httpheaders', 'args', 'command'})
            result[key] = '[REDACTED]' if sensitive else redact_config(child)
        return result
    if isinstance(value, list):
        return [redact_config(v) for v in value]
    if isinstance(value, str):
        if re.search(r'(?i)bearer\s|basic\s|sk-|tvly-|gh[pousr]_|-----BEGIN|\d{6,15}:[\w-]{20,}', value):
            return '[REDACTED]'
        if '://' in value:
            try:
                url = urlsplit(value)
                return urlunsplit((url.scheme, url.hostname or '', url.path, '', ''))
            except ValueError:
                return '[REDACTED]'
    return value


def claude_snapshot(path: Path) -> tuple[dict, dict]:
    import json
    value = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError('Claude settings 根必须是对象')
    settings = copy.deepcopy(value)
    # Files remain intact. The per-turn snapshot applies the explicit Gateway
    # unattended contract, while ordinary official settings remain usable.
    settings['permissions'] = {'defaultMode': 'bypassPermissions'}
    settings['disableAllHooks'] = True
    settings.pop('hooks', None)
    settings.pop('apiKeyHelper', None)
    for key in ('allowedMcpServers', 'deniedMcpServers', 'maxEffortLevel', 'forceLoginMethod', 'forceLoginOrgUUID'):
        settings.pop(key, None)
    for item in settings.get('modelSettings', {}).values():
        if isinstance(item, dict):
            item.pop('maxEffortLevel', None)
    env = settings.get('env', {})
    if isinstance(env, dict):
        settings['env'] = {k: v for k, v in env.items() if not k.startswith(('ANTHROPIC_', 'CODEX_', 'TELEGRAM_', 'TAVILY_', 'CLAUDE_CODE_EFFORT', 'CLAUDE_CONFIG_DIR')) and k.lower() not in {'http_proxy','https_proxy','all_proxy'}}
    servers = settings.pop('mcpServers', {})
    if not isinstance(servers, dict):
        raise ValueError('mcpServers 必须是对象')
    return settings, servers
