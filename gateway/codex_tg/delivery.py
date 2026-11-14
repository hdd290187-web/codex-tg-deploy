"""Authenticated stdio MCP file sending; no per-task prompt injection."""
from __future__ import annotations

import hmac
import json
import os
import sys
import time
import shlex
from pathlib import Path

from .artifacts import Artifacts
from .credentials import Credentials
from .store import Store

TOOLS = [
    {'name': 'telegram_send_file',
     'description': 'Send existing VPS files to the current Telegram user when they ask to receive/download them. '
                    'Accepts absolute file paths from any normal working directory; no delivery directory is required. '
                    'Queues immutable copies for background upload, with large-file splitting and retries. '
                    'QUEUED is not a delivery receipt; use telegram_file_status to check. Do not send unrequested or credential files.',
     'inputSchema': {'type': 'object', 'properties': {'paths': {'type': 'array', 'items': {'type': 'string'},
                        'minItems': 1, 'maxItems': 40}}, 'required': ['paths'], 'additionalProperties': False}},
    {'name': 'telegram_file_status', 'description': 'Check Telegram file delivery receipts for this task; reports queued, sending, delivered or failed.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
]


def call_tool(data: Path, database: Path, token: str, engine: str, chat_id: int, user_id: int,
              name: str, arguments: dict) -> dict:
    context = json.loads((data / 'delivery-context.json').read_text())
    if not token or not hmac.compare_digest(str(context.get('token', '')), token) or context.get('engine') != engine:
        raise ValueError('File tool has no active authorized task')
    if context.get('expires_at', time.time() + 1) < time.time():
        raise ValueError('File tool context expired')
    store = Store(database)
    task = store.task(int(context['task_id']), chat_id)
    if not task or task['status'] != 'RUNNING' or task['engine'] != engine or task['user_id'] != user_id:
        raise ValueError('File tool task is not authorized or no longer running')
    if name == 'telegram_file_status':
        with store.connect() as conn:
            rows = conn.execute('SELECT id,name,size,status,error FROM artifacts WHERE task_id=? AND chat_id=? ORDER BY id',
                                (task['id'], chat_id)).fetchall()
        return {'files': [dict(row) for row in rows]}
    if name != 'telegram_send_file':
        raise ValueError('Unknown file tool')
    paths = arguments.get('paths')
    if not isinstance(paths, list) or not 1 <= len(paths) <= 40 or not all(isinstance(p, str) for p in paths):
        raise ValueError('paths must contain 1-40 absolute file paths')
    protected = Credentials(data / 'credentials.json').secrets()
    env_path = Path('/etc/codex-tg.env')
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                if any(n in key for n in ('TOKEN', 'KEY', 'PROXY_URL')):
                    protected.extend(shlex.split(value))
    artifacts = Artifacts(data, store, lambda: protected)
    files = []
    for path in dict.fromkeys(paths):
        try:
            record = artifacts.register(Path(path), chat_id, task['id'])
            item = store.artifact(record, chat_id)
            files.append({'id': record, 'name': item['name'], 'status': item['status'], 'size': item['size']})
        except (OSError, ValueError) as exc:
            # Never echo arbitrary file bytes, API responses, or credential paths.
            files.append({'path': path, 'status': 'REJECTED', 'reason': str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    return {'files': files, 'receipt': 'Only DELIVERED means Telegram confirmed receipt. READY/SENDING are pending.'}


def serve(data: Path, database: Path, engine: str, chat_id: int, user_id: int):
    token = os.environ.get('CODEX_TG_DELIVERY_TOKEN', '')
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if 'id' not in request:
                continue
            method = request.get('method')
            if method == 'initialize':
                result = {'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'),
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'codex-tg-files', 'version': '1.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': TOOLS}
            elif method == 'tools/call':
                params = request.get('params', {})
                try:
                    value = call_tool(data, database, token, engine, chat_id, user_id, params.get('name'), params.get('arguments') or {})
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
                except Exception as exc:
                    result = {'content': [{'type': 'text', 'text': str(exc) if isinstance(exc, ValueError) else 'File tool failed: ' + type(exc).__name__}], 'isError': True}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
        except (ValueError, KeyError, TypeError):
            if isinstance(request, dict) and 'id' in request:
                print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32602, 'message': 'Invalid request'}}), flush=True)


if __name__ == '__main__':
    serve(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))
