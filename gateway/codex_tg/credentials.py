from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def validate_url(value: str) -> str:
    value = value.strip()
    if any(c in value for c in '\r\n\x00'):
        raise ValueError('Base URL 含无效字符')
    p = urlsplit(value)
    if p.scheme not in {'https', 'http'} or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('Base URL 必须是 HTTP(S) 地址，不能含凭据、查询或片段')
    try:
        p.port
    except ValueError as exc:
        raise ValueError('Base URL 端口无效') from exc
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip('/'), '', ''))


def validate_key(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('API Key 格式无效')
    return value


class Credentials:
    """Versioned 0600 secret file, separate from the ordinary SQLite state.

    Immutable revisions let queued tasks retain credentials across rotations and
    restarts. Only opaque revision ids are stored in the tasks table.
    """
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text())
        else:
            self.data = {'active': {}, 'versions': {}}

    def current_ref(self, engine: str) -> str:
        return self.data['active'].get(engine, '')

    def get(self, engine: str, ref: str | None = None) -> dict:
        return dict(self.data['versions'].get(ref if ref is not None else self.current_ref(engine), {}))

    def save(self, engine: str, base_url: str, api_key: str, auth: str = 'api_key') -> str:
        revision = uuid.uuid4().hex
        self.data['versions'][revision] = {'engine': engine, 'base_url': base_url, 'api_key': api_key, 'auth': auth}
        self.data['active'][engine] = revision
        atomic_json(self.path, self.data)
        return revision

    def secrets(self) -> list[str]:
        return [v['api_key'] for v in self.data['versions'].values() if v.get('api_key')]


def clean_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.lower() in {'http_proxy', 'https_proxy', 'all_proxy'} or key.startswith(('CODEX_TG_', 'ANTHROPIC_', 'TAVILY_', 'TELEGRAM_', 'ALLOWED_')):
            env.pop(key, None)
    return env
