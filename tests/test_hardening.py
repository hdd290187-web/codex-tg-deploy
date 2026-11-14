from __future__ import annotations

import asyncio

import pytest

from codex_tg.config_sync import ConfigSync
from codex_deploy.telegram import TelegramAPI
from test_dual_engine import gateway, message, configure


@pytest.mark.asyncio
async def test_binding_requires_exact_challenge(monkeypatch):
    class API(TelegramAPI):
        async def call(self, method, payload=None):
            return [{'update_id': 4, 'message': {'text': '/start wrong', 'chat': {'type': 'private', 'id': 9}, 'from': {'id': 7}}},
                    {'update_id': 5, 'message': {'text': '/start correct', 'chat': {'type': 'private', 'id': 9}, 'from': {'id': 7}}}]
    api = API('123456:A' * 5, None)
    assert await api.poll_start(0, 'correct') == (6, 7, 9)
    api.call = lambda *args, **kwargs: asyncio.sleep(0, result=[{'update_id': 7, 'message': {'text': '/start', 'chat': {'type': 'private', 'id': 9}, 'from': {'id': 7}}}])
    assert await api.poll_start(0, 'correct') == (8, None, None)


def test_export_redacts_headers_and_urls(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'home'))
    path = tmp_path / 'home/config.toml'
    path.parent.mkdir()
    path.write_text('model="m"\n[http_headers]\nAuthorization="Bearer real-header-secret"\n[env]\nCUSTOM="sk-test-value-12345678901234567890"\n')
    output = ConfigSync(tmp_path, 'https://center.invalid/releases', tmp_path / 'missing').export('codex')
    text = output.read_text()
    assert 'real-header-secret' not in text and 'sk-test-value' not in text and '[REDACTED]' in text


@pytest.mark.asyncio
async def test_inbox_replay_is_idempotent_for_task_admission(tmp_path):
    g = gateway(tmp_path / 'data')
    configure(g, 'codex')
    g.store.set_thread(9, 'thread')
    g.store.register_thread(9, 'thread')
    update = {'update_id': 100, 'message': message('same task', 100)}
    g.inbox.receive([update])
    g.inbox.receive([update])
    original = g.telegram.send
    async def no_network(*args, **kwargs):
        return [1]
    g.telegram.send = no_network
    # Exercise the same durable row twice through the ordinary admission path.
    msg = dict(update['message'])
    msg['_inbox_update_id'] = 100
    await g.handle_message(msg)
    await g.handle_message(msg)
    assert g.store.queue_count() == 1
    g.telegram.send = original
