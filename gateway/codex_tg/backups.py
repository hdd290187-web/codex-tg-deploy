"""Small encrypted configuration snapshots; task/files/session payloads stay in place."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .credentials import atomic_json
from .pre_instructions import PromptStore
from .store import Store

MAGIC = b'CTGCFG1\0'


class Backups:
    def __init__(self, data: Path, environment: Path = Path('/etc/codex-tg.env'), proxy: Path = Path('/etc/codex-tg-proxy')):
        self.data, self.environment, self.proxy = data, environment, proxy
        self.directory = data / 'backups'
        self.key_file = data / 'backup.key'

    def _key(self, create: bool = False) -> bytes:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        if os.geteuid() == 0:
            os.chown(self.directory, 0, 0)
        if create and not self.key_file.exists():
            fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(os.urandom(32))
                out.flush()
                os.fsync(out.fileno())
        if self.key_file.is_symlink():
            raise ValueError('备份密钥路径异常')
        self.key_file.chmod(0o600)
        if os.geteuid() == 0:
            os.chown(self.key_file, 0, 0)
        key = self.key_file.read_bytes()
        if len(key) != 32:
            raise ValueError('备份密钥长度无效')
        return key

    def create(self, reason: str = 'manual') -> Path:
        key = self._key(create=True)
        state = Store(self.data / 'gateway.db')
        with state.connect() as conn:
            conn.execute('BEGIN')
            tables = {name: [dict(row) for row in conn.execute(f'SELECT * FROM {name}')]
                      for name in ('chats', 'thread_counters', 'codex_prompt_state')}
            meta = [dict(row) for row in conn.execute("SELECT * FROM meta WHERE key LIKE 'endpoint_state:%'")]
            conn.commit()
        files = {}
        paths = {'credentials': self.data / 'credentials.json', 'inbox-key': self.data / 'inbox.key', 'codex-config': Path(os.getenv('CODEX_HOME', '/root/.codex')) / 'config.toml', 'claude-settings': self.data / 'claude/settings.json', 'config-state': self.data / 'config-template-state/state.json', 'prompt': self.data / 'codex-pre-instructions.txt',
                 'environment': self.environment}
        for name in ('config.yaml', 'subscription-url', 'providers/subscription.yaml', 'source.json'):
            paths['proxy/' + name] = self.proxy / name
        for name, path in paths.items():
            if path.is_file() and not path.is_symlink():
                raw = path.read_bytes()
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError('配置文件超过备份大小限制')
                files[name] = base64.b64encode(raw).decode()
            else:
                files[name] = None
        payload = json.dumps({'format': 1, 'created_at': int(time.time()), 'reason': reason,
                              'tables': tables, 'meta': meta, 'files': files}, ensure_ascii=False).encode()
        nonce = os.urandom(12)
        destination = self.directory / f'{time.strftime("%Y%m%d-%H%M%S", time.gmtime())}-{uuid.uuid4().hex[:8]}.ctgbackup'
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(MAGIC + nonce + AESGCM(key).encrypt(nonce, payload, MAGIC))
            output.flush()
            os.fsync(output.fileno())
        return destination

    def list(self) -> list[Path]:
        return sorted(self.directory.glob('*.ctgbackup'), reverse=True)[:10]

    def read(self, name: str) -> dict:
        if not re.fullmatch(r'\d{8}-\d{6}-[a-f0-9]{8}\.ctgbackup', name):
            raise ValueError('备份名称无效')
        path = self.directory / name
        if path.is_symlink() or path.stat().st_size > 48 * 1024 * 1024:
            raise ValueError('备份文件异常')
        raw = path.read_bytes()
        if not raw.startswith(MAGIC):
            raise ValueError('不是支持的加密备份')
        plaintext = AESGCM(self._key()).decrypt(raw[len(MAGIC):len(MAGIC) + 12], raw[len(MAGIC) + 12:], MAGIC)
        result = json.loads(plaintext)
        if result.get('format') != 1:
            raise ValueError('不支持此备份格式')
        return result

    def restore(self, name: str, chat_id: int, user_id: int) -> Path:
        saved = self.read(name)
        chats = saved['tables']['chats']
        if len(chats) != 1 or chats[0]['chat_id'] != chat_id or chats[0]['user_id'] != user_id:
            raise ValueError('只能恢复当前 Telegram 授权身份的配置')
        before = self.create('before-restore')
        try:
            self._restore(saved)
        except Exception:
            self._restore(self.read(before.name))
            raise
        return before

    def _restore(self, saved: dict) -> None:
        paths = {'credentials': self.data / 'credentials.json', 'inbox-key': self.data / 'inbox.key', 'codex-config': Path(os.getenv('CODEX_HOME', '/root/.codex')) / 'config.toml', 'claude-settings': self.data / 'claude/settings.json', 'config-state': self.data / 'config-template-state/state.json', 'prompt': self.data / 'codex-pre-instructions.txt',
                 'environment': self.environment}
        for name in ('config.yaml', 'subscription-url', 'providers/subscription.yaml', 'source.json'):
            paths['proxy/' + name] = self.proxy / name
        for name, encoded in saved['files'].items():
            if name not in paths:
                raise ValueError('备份包含未知配置项')
            path = paths[name]
            if encoded is None:
                path.unlink(missing_ok=True)
                continue
            raw = base64.b64decode(encoded, validate=True)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = path.with_name('.' + path.name + '.restore-' + uuid.uuid4().hex)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
        prompt = PromptStore(self.data / 'codex-pre-instructions.txt')
        prompt.save(prompt.read())
        store = Store(self.data / 'gateway.db')
        with store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for table in ('chats', 'thread_counters', 'codex_prompt_state'):
                allowed = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
                for row in saved['tables'][table]:
                    columns = [key for key in row if key in allowed]
                    primary = 'chat_id' if table == 'chats' else 'thread_id' if table == 'thread_counters' else 'id'
                    updates = ','.join(f'{c}=excluded.{c}' for c in columns if c != primary)
                    conn.execute(f'INSERT INTO {table}({",".join(columns)}) VALUES({",".join("?" for _ in columns)}) '
                                 f'ON CONFLICT({primary}) DO UPDATE SET {updates}', [row[c] for c in columns])
            for row in saved['meta']:
                if row['key'].startswith('endpoint_state:'):
                    conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (row['key'], row['value']))
            conn.execute('''UPDATE thread_counters SET last_task_number=MAX(last_task_number,
                COALESCE((SELECT MAX(thread_task_number) FROM tasks WHERE tasks.thread_id=thread_counters.thread_id),0))''')
            conn.commit()
        atomic_json(self.data / 'restore-status.json', {'restored_at': int(time.time()), 'state': 'RESTORED'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('/opt/codex-tg/data'))
    parser.add_argument('--reason', default='update')
    args = parser.parse_args()
    print(Backups(args.data).create(args.reason).name)


if __name__ == '__main__':
    main()
