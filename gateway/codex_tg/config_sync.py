from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import tomlkit
from cryptography.hazmat.primitives import serialization

from .config_audit import validate_claude_document


class ConfigSyncError(RuntimeError):
    pass


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _paths(base: Path, engine: str) -> tuple[Path, Path]:
    if engine == 'codex':
        home = Path(os.getenv('CODEX_HOME', '/root/.codex'))
        return home / 'config.toml', base / 'config-template-state/codex.toml'
    home = base / 'claude'
    return home / 'settings.json', base / 'config-template-state/claude.json'


def _flatten(value: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    if not isinstance(value, dict):
        return {prefix: value}
    result: dict[tuple[str, ...], Any] = {}
    for key, item in value.items():
        result.update(_flatten(item, prefix + (str(key),)))
    return result


def _set(value: dict, path: tuple[str, ...], item: Any) -> None:
    node = value
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    if path:
        node[path[-1]] = copy.deepcopy(item)


def _delete(value: dict, path: tuple[str, ...]) -> None:
    node = value
    for key in path[:-1]:
        node = node.get(key, {})
        if not isinstance(node, dict):
            return
    if path:
        node.pop(path[-1], None)


def three_way(base: dict, current: dict, template: dict) -> tuple[dict, list[dict], list[dict]]:
    merged = copy.deepcopy(current)
    conflicts: list[dict] = []
    changes: list[dict] = []
    paths = set(_flatten(base)) | set(_flatten(current)) | set(_flatten(template))
    missing = object()
    for path in sorted(paths):
        old = _flatten(base).get(path, missing)
        local = _flatten(current).get(path, missing)
        new = _flatten(template).get(path, missing)
        if local == old and new != old:
            if new is missing:
                _delete(merged, path)
                action = 'delete'
            else:
                _set(merged, path, new)
                action = 'update'
            changes.append({'path': '.'.join(path), 'action': action, 'source': 'template'})
        elif new == old or local == new:
            continue
        elif local != old and new != old and local != new:
            conflicts.append({'path': '.'.join(path), 'local': None if local is missing else local,
                              'template': None if new is missing else new,
                              'base': None if old is missing else old})
    return merged, changes, conflicts


class ConfigSync:
    def __init__(self, data_dir: Path, release_base: str, public_key: Path):
        self.data_dir = data_dir
        self.release_base = release_base.rstrip('/')
        self.public_key = public_key
        self.state_file = data_dir / 'config-template-state/state.json'

    def endpoint(self) -> str:
        configured = os.getenv('CODEX_TG_CONFIG_TEMPLATE_URL', '').strip()
        if configured:
            return configured.rstrip('/')
        return self.release_base.rsplit('/releases', 1)[0] + '/config-templates/latest.json'

    def _state(self) -> dict:
        try:
            value = json.loads(self.state_file.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, value: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_file.with_name('.state-' + str(os.getpid()))
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(self.state_file)

    @staticmethod
    def parse(engine: str, text: str) -> dict:
        try:
            if engine == 'codex':
                value = tomlkit.loads(text).unwrap()
            else:
                value = json.loads(text)
        except Exception as exc:
            raise ConfigSyncError(f'{engine} 配置格式无效：{type(exc).__name__}') from None
        if not isinstance(value, dict):
            raise ConfigSyncError('配置根必须是对象')
        if engine == 'claude':
            error = validate_claude_document(value)
            if error:
                raise ConfigSyncError(error)
        return value

    @staticmethod
    def render(engine: str, value: dict) -> str:
        if engine == 'codex':
            return tomlkit.dumps(value)
        return json.dumps(value, ensure_ascii=False, indent=2) + '\n'

    def local(self, engine: str) -> tuple[Path, str]:
        path, _ = _paths(self.data_dir, engine)
        if not path.exists():
            return path, ''
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ConfigSyncError(f'{engine} 本地配置路径异常')
        return path, path.read_text('utf-8')

    def status(self, engine: str) -> dict:
        path, state_path = _paths(self.data_dir, engine)
        state = self._state().get(engine, {})
        local_text = path.read_text('utf-8') if path.exists() and path.is_file() else ''
        base_text = state_path.read_text('utf-8') if state_path.exists() else ''
        try:
            local_changed = self.parse(engine, local_text or '{}') != self.parse(engine, base_text or '{}') if base_text else False
        except ConfigSyncError:
            local_changed = True
        return {'engine': engine, 'path': str(path), 'template_version': state.get('version'),
                'local_hash': digest(local_text) if local_text else '',
                'base_hash': digest(base_text) if base_text else '',
                'local_modified': local_changed,
                'conflicts': state.get('conflicts', []), 'pending_version': state.get('pending_version')}

    async def fetch(self) -> dict:
        url = self.endpoint()
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8), trust_env=False) as client:
            response, signature = await __import__('asyncio').gather(client.get(url), client.get(url + '.sig'))
        response.raise_for_status()
        signature.raise_for_status()
        try:
            public = serialization.load_pem_public_key(self.public_key.read_bytes())
            public.verify(signature.content, response.content)
            payload = json.loads(response.content)
        except Exception as exc:
            raise ConfigSyncError(f'配置模板签名或内容校验失败：{type(exc).__name__}') from None
        if payload.get('format') != 1 or not re.fullmatch(r'\d+', str(payload.get('version', ''))):
            raise ConfigSyncError('配置模板版本格式无效')
        for engine in ('codex', 'claude'):
            if not isinstance(payload.get(engine), dict) or not isinstance(payload[engine].get('content'), str):
                raise ConfigSyncError(f'{engine} 配置模板缺失')
            if digest(payload[engine]['content']) != payload[engine].get('sha256'):
                raise ConfigSyncError(f'{engine} 配置模板哈希不匹配')
            self.parse(engine, payload[engine]['content'])
        return payload

    def _backup(self, path: Path) -> Path | None:
        if not path.exists():
            return None
        backup = path.with_name(path.name + f'.before-sync-{int(time.time())}')
        backup.write_bytes(path.read_bytes())
        backup.chmod(0o600)
        return backup

    async def sync(self, strategy: str | None = None) -> dict:
        payload = await self.fetch()
        state = self._state()
        result = {'version': str(payload['version']), 'engines': {}, 'conflicts': []}
        originals: dict[Path, bytes | None] = {}
        try:
            for engine in ('codex', 'claude'):
                path, base_path = _paths(self.data_dir, engine)
                local_text = path.read_text('utf-8') if path.exists() else ''
                base_text = base_path.read_text('utf-8') if base_path.exists() else local_text
                template_text = payload[engine]['content']
                base = self.parse(engine, base_text or '{}')
                local = self.parse(engine, local_text or '{}')
                template = self.parse(engine, template_text)
                merged, changes, conflicts = three_way(base, local, template)
                if conflicts and strategy not in {'local', 'template'}:
                    state.setdefault(engine, {})['pending_version'] = str(payload['version'])
                    state[engine]['conflicts'] = conflicts
                    result['conflicts'] += [{'engine': engine, **item} for item in conflicts]
                    continue
                if conflicts and strategy == 'template':
                    for conflict in conflicts:
                        path_parts = tuple(conflict['path'].split('.'))
                        value = conflict['template']
                        if value is None:
                            _delete(merged, path_parts)
                        else:
                            _set(merged, path_parts, value)
                    changes += [{'path': c['path'], 'action': 'conflict-template'} for c in conflicts]
                elif conflicts and strategy == 'local':
                    changes += [{'path': c['path'], 'action': 'conflict-local'} for c in conflicts]
                rendered = self.render(engine, merged)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                originals.setdefault(path, path.read_bytes() if path.exists() else None)
                originals.setdefault(base_path, base_path.read_bytes() if base_path.exists() else None)
                self._backup(path)
                temporary = path.with_name('.' + path.name + '.sync')
                temporary.write_text(rendered)
                temporary.chmod(0o600)
                temporary.replace(path)
                base_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                base_path.write_text(template_text)
                base_path.chmod(0o600)
                state[engine] = {'version': str(payload['version']), 'conflicts': [], 'updated_at': int(time.time()),
                                 'base_hash': digest(template_text)}
                result['engines'][engine] = {'changes': changes, 'conflicts': conflicts}
            self._write_state(state)
        except Exception:
            for path, original in originals.items():
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path.write_bytes(original)
                    path.chmod(0o600)
            raise
        return result

    def export(self, engine: str) -> Path:
        path, text = self.local(engine)
        value = self.parse(engine, text or '{}')
        sensitive = {'api_key', 'apiKey', 'token', 'password', 'secret', 'bot_token', 'authorization', 'env_key'}
        def redact(node):
            if isinstance(node, dict):
                return {k: ('[REDACTED]' if k in sensitive or any(x in k.lower() for x in ('token','secret','password','api_key')) else redact(v)) for k,v in node.items()}
            if isinstance(node, list):
                return [redact(x) for x in node]
            return node
        output = self.render(engine, redact(value))
        path = self.data_dir / f'agent-config-{engine}-export-{int(time.time())}.txt'
        path.write_text(output)
        path.chmod(0o600)
        return path
