from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx
import tomlkit
from cryptography.hazmat.primitives import serialization
from .config_audit import validate_claude_document
from .config_files import atomic_write, config_target, redact_config
from .activity import acquire_activity


class ConfigSyncError(RuntimeError):
    pass


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _paths(base: Path, engine: str) -> tuple[Path, Path]:
    if engine == 'codex':
        return Path(os.getenv('CODEX_HOME', '/root/.codex')) / 'config.toml', base / 'config-template-state/codex.toml'
    return base / 'claude/settings.json', base / 'config-template-state/claude.json'


MISSING = object()


def three_way(base: dict, current: dict, template: dict) -> tuple[dict, list[dict], list[dict]]:
    changes, conflicts = [], []
    def merge(old, local, new, path):
        if all(isinstance(v, dict) or v is MISSING for v in (old, local, new)) and new is not MISSING and local is not MISSING:
            result = {}
            for key in sorted(set(old if isinstance(old, dict) else {}) | set(local) | set(new)):
                v = merge(old.get(key, MISSING) if isinstance(old, dict) else MISSING,
                          local.get(key, MISSING), new.get(key, MISSING), (*path, key))
                if v is not MISSING:
                    result[key] = v
            return result
        info = {'path': '.'.join(path), 'keys': list(path)}
        if local == old and new != old:
            changes.append({**info, 'action': 'delete' if new is MISSING else 'update', 'source': 'template'})
            return copy.deepcopy(new) if new is not MISSING else MISSING
        if new == old or local == new:
            return copy.deepcopy(local) if local is not MISSING else MISSING
        conflicts.append({**info, 'local': None if local is MISSING else local,
                          'template': None if new is MISSING else new, 'deleted': new is MISSING,
                          'base': None if old is MISSING else old})
        return copy.deepcopy(local) if local is not MISSING else MISSING
    return merge(base, current, template, ()), changes, conflicts


def set_path(value, path, item, delete=False):
    node = value
    for key in path[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    if delete:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = item


def render_toml(text, value):
    document = tomlkit.parse(text)
    def update(node, desired):
        for key in list(node):
            if key not in desired:
                del node[key]
        for key, item in desired.items():
            if isinstance(item, dict) and key in node and hasattr(node[key], 'keys'):
                update(node[key], item)
            elif key not in node or node[key] != item:
                node[key] = item
    update(document, value)
    return tomlkit.dumps(document)


class ConfigSync:
    def __init__(self, data_dir: Path, release_base: str, public_key: Path):
        self.data_dir, self.release_base, self.public_key = data_dir, release_base.rstrip('/'), public_key
        self.state_file = data_dir / 'config-template-state/state.json'
        self.lock = asyncio.Lock()

    def endpoint(self) -> str:
        return os.getenv('CODEX_TG_CONFIG_TEMPLATE_URL', '').strip().rstrip('/') or self.release_base.rsplit('/releases', 1)[0] + '/config-templates/latest.json'

    def _state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _write_state(self, value):
        atomic_write(self.state_file, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())

    @staticmethod
    def parse(engine, text):
        try:
            value = tomlkit.loads(text).unwrap() if engine == 'codex' else json.loads(text)
            if not isinstance(value, dict):
                raise ValueError()
        except Exception:
            raise ConfigSyncError(f'{engine} 配置格式无效') from None
        if engine == 'claude':
            error = validate_claude_document(value)
            if error:
                raise ConfigSyncError(error)
        return value

    @staticmethod
    def render(engine, value):
        return tomlkit.dumps(value) if engine == 'codex' else json.dumps(value, ensure_ascii=False, indent=2) + '\n'

    def local(self, engine):
        path = config_target(_paths(self.data_dir, engine)[0])
        return path, path.read_text('utf-8') if path.exists() else ''

    def status(self, engine):
        path, text = self.local(engine)
        base_path = _paths(self.data_dir, engine)[1]
        base_text = base_path.read_text() if base_path.exists() else ''
        state = self._state().get(engine, {})
        try:
            changed = self.parse(engine, text or ('' if engine == 'codex' else '{}')) != self.parse(engine, base_text or ('' if engine == 'codex' else '{}'))
        except ConfigSyncError:
            changed = True
        return {'engine': engine, 'path': str(path), 'template_version': state.get('version'),
                'local_hash': digest(text), 'base_hash': digest(base_text), 'local_modified': changed,
                'conflicts': state.get('conflicts', []), 'pending_version': state.get('pending_version')}

    async def fetch(self):
        # Two GETs can straddle an admin save; retry the signature pair once.
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8), trust_env=False) as client:
                response, signature = await asyncio.gather(client.get(self.endpoint()), client.get(self.endpoint() + '.sig'))
            response.raise_for_status()
            signature.raise_for_status()
            if len(response.content) > 3 * 1024 * 1024 or len(signature.content) != 64:
                raise ConfigSyncError('配置清单大小异常')
            try:
                serialization.load_pem_public_key(self.public_key.read_bytes()).verify(signature.content, response.content)
                payload = json.loads(response.content)
                break
            except Exception:
                if attempt:
                    raise ConfigSyncError('配置模板签名校验失败') from None
        if payload.get('format') != 1:
            raise ConfigSyncError('配置模板格式版本无效')
        for engine in ('codex', 'claude'):
            item = payload.get(engine, {})
            if not isinstance(item.get('content'), str) or digest(item['content']) != item.get('sha256'):
                raise ConfigSyncError(f'{engine} 配置模板哈希不匹配')
            version = item.get('version', payload.get('version'))
            if not re.fullmatch(r'\d+', str(version)):
                raise ConfigSyncError('配置模板版本格式无效')
            item['version'] = str(version)
            self.parse(engine, item['content'])
        payload.setdefault('version', str(sum(int(payload[e]['version']) for e in ('codex', 'claude'))))
        return payload

    async def sync(self, strategy=None, activate=None):
        if strategy not in {None, 'local', 'template'}:
            raise ConfigSyncError('同步策略无效')
        async with self.lock:
            payload = await self.fetch()
            activity = acquire_activity(self.data_dir, exclusive=True)
            if activity is None:
                raise ConfigSyncError('Agent、文件交付或维护正在执行，请稍后同步')
            with activity:
                return await self._apply(payload, strategy, activate)

    async def _apply(self, payload, strategy, activate):
        state = self._state()
        result = {'version': str(payload['version']), 'engines': {}, 'conflicts': []}
        writes = {}
        for engine in ('codex', 'claude'):
            path, local_text = self.local(engine)
            base_path = _paths(self.data_dir, engine)[1]
            # A first sync has no evidence that local fields came from a
            # template: retain them and ask on conflict, never delete them.
            empty = '' if engine == 'codex' else '{}'
            base_text = base_path.read_text() if base_path.exists() else empty
            template_text = payload[engine]['content']
            version = str(payload[engine].get('version', payload['version']))
            if int(version) < int(state.get(engine, {}).get('version') or 0):
                raise ConfigSyncError('拒绝回放旧模板；中心回滚必须发布新版本')
            merged, changes, conflicts = three_way(self.parse(engine, base_text), self.parse(engine, local_text or empty), self.parse(engine, template_text))
            if conflicts and strategy is None:
                # Conflict state stores paths only, never local secrets.
                state.setdefault(engine, {}).update(pending_version=version, conflicts=[c['path'] for c in conflicts])
                result['conflicts'] += [{'engine': engine, 'path': c['path']} for c in conflicts]
                continue
            if strategy == 'template':
                for c in conflicts:
                    set_path(merged, c['keys'], c['template'], c['deleted'])
            rendered = render_toml(local_text, merged) if engine == 'codex' else self.render(engine, merged)
            self.parse(engine, rendered)
            if rendered != local_text:
                writes[path] = rendered.encode()
            writes[base_path] = template_text.encode()
            state[engine] = {'version': version, 'conflicts': [], 'updated_at': int(time.time()), 'base_hash': digest(template_text)}
            result['engines'][engine] = {'changes': changes, 'conflicts': [c['path'] for c in conflicts]}
        if result['conflicts']:
            self._write_state(state)
            return result  # all engines are applied together only after resolution
        writes[self.state_file] = (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode()
        originals = {p: p.read_bytes() if p.exists() else None for p in writes}
        try:
            for path, raw in writes.items():
                if originals[path] is not None:
                    atomic_write(path.with_name(path.name + f'.before-sync-{time.time_ns()}-{uuid.uuid4().hex[:8]}'), originals[path])
                atomic_write(path, raw)
            if activate:
                await activate()
        except BaseException:
            for path, raw in originals.items():
                if raw is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, raw)
            if activate:
                await activate()
            raise
        return result

    def export(self, engine):
        _, text = self.local(engine)
        value = self.parse(engine, text or ('' if engine == 'codex' else '{}'))
        output = self.render(engine, redact_config(value))
        path = self.data_dir / f'agent-config-{engine}-export-{uuid.uuid4().hex}.txt'
        atomic_write(path, output.encode())
        return path
