from __future__ import annotations

import asyncio

import pytest

from test_dual_engine import configure, gateway


@pytest.mark.asyncio
async def test_codex_compact_uses_native_sdk_method_and_keeps_thread(tmp_path):
    g = gateway(tmp_path)
    configure(g, 'codex')
    g.store.set_thread(9, 'codex-thread-native')
    g.store.register_thread(9, 'codex-thread-native')
    calls = []
    class Thread:
        id = 'codex-thread-native'
        async def compact(self):
            calls.append('thread/compact/start')
        async def read(self, include_turns=False):
            return type('Response', (), {'thread': type('ThreadData', (), {'turns': []})()})()
    class Runtime:
        context_usage = {'used': 1200, 'window': 10000, 'total': 1300}
        async def get_thread(self, *_args, **_kwargs): return Thread()
        async def compact_thread(self, thread_id, *_args):
            calls.append((thread_id, 'native'))
            return {'supported': True, 'confirmed': True, 'before': {'used': 1200, 'window': 10000}, 'after': {'used': 400, 'window': 10000}}
    g.runtime = Runtime()
    await g.handle_command('/compact', '/compact', 9)
    assert calls == [('codex-thread-native', 'native')]
    assert g.store.thread_id(9) == 'codex-thread-native'
    assert '原生上下文压缩已完成' in g.telegram.sent[-1]
    assert '1200 / 10000' in g.telegram.sent[-1] and '400 / 10000' in g.telegram.sent[-1]


@pytest.mark.asyncio
async def test_compact_waits_for_running_task_and_claude_reports_unsupported(tmp_path):
    g = gateway(tmp_path)
    configure(g, 'claude')
    g.store.set_active_engine(9, 'claude')
    g.store.set_engine_session(9, 'claude', 'claude-session')
    g.store.set_claude_conversation_key(9, 'claude-session')
    g.store.enqueue(7, 9, 'claude-session', 'running', [])
    g.store.claim_next()
    await g.handle_command('/compact', '/compact', 9)
    assert g.store.get_meta('compact_pending') == '1'
    assert '排队' in g.telegram.sent[-1]
    g.store.fail(g.store.active()['id'], 'done')
    g.claude_runtime.compact = lambda _session: asyncio.sleep(0, result={'supported': False, 'reason': 'unsupported'})
    await g.perform_compact(9)
    assert g.telegram.sent[-1] == 'unsupported'
