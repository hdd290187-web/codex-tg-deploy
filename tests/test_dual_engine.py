from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from codex_tg.app import Gateway
from codex_tg.claude_runtime import ClaudeRuntime, ClaudeRunResult
from codex_tg.credentials import Credentials, validate_url
from codex_tg.search import TavilyClient
from test_gateway import make_config, FakeTelegram


class Telegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.deleted = []

    async def delete_message(self, chat, message):
        self.deleted.append((chat, message))


def gateway(tmp_path):
    g = Gateway(make_config(tmp_path))
    g.store.ensure_chat(7, 9)
    g.telegram = Telegram()
    return g


def message(text, id=12):
    return {'from': {'id': 7}, 'chat': {'id': 9, 'type': 'private'}, 'message_id': id, 'text': text}


def configure(g, engine, key='fixture-api-key'):
    ref = g.credentials.save(engine, 'https://relay.invalid', key)
    g.engine_endpoints[engine] = g.credentials.get(engine)
    return ref


@pytest.mark.asyncio
async def test_switch_queue_snapshots_and_restart(tmp_path):
    g = gateway(tmp_path)
    codex = configure(g, 'codex')
    claude = configure(g, 'claude')
    g.store.set_thread(9, 'codex-original')
    g.store.register_thread(9, 'codex-original')
    await g.handle_message(message('first'))
    await g.handle_command('/engine', '/engine claude', 9)
    await g.handle_command('/model', '/model claude-custom', 9)
    await g.handle_command('/reasoning', '/reasoning xhigh', 9)
    await g.handle_message(message('second'))
    session = g.store.engine_session(9, 'claude')
    await g.handle_command('/engine', '/engine codex', 9)
    assert g.store.thread_id(9) == 'codex-original'
    assert g.store.engine_session(9, 'claude') == session
    configure(g, 'claude', 'replacement-key')
    await g.handle_command('/engine', '/engine claude', 9)
    await g.handle_command('/reasoning', '/reasoning low', 9)
    assert g.store.chat_settings(9).model == 'default'
    first = g.store.claim_next()
    assert (first['engine'], first['endpoint_ref']) == ('codex', codex)
    g.store.complete(first['id'], 'ok')
    second = g.store.claim_next()
    assert (second['engine'], second['model'], second['effort'], second['endpoint_ref']) == ('claude', 'claude-custom', 'xhigh', claude)
    assert g.credentials.get('claude', second['endpoint_ref'])['api_key'] == 'fixture-api-key'
    g.store.complete(second['id'], 'ok')
    reopened = Gateway(make_config(tmp_path))
    assert reopened.store.active_engine(9) == 'claude'
    assert reopened.store.engine_session(9, 'claude') == session
    assert reopened.store.thread_id(9) == 'codex-original'
    assert reopened.store.chat_settings(9).claude_effort == 'low'
    assert reopened.credentials.get('claude')['api_key'] == 'replacement-key'
    with reopened.store.connect() as conn:
        dump = '\n'.join(conn.iterdump())
    assert 'replacement-key' not in dump and 'fixture-api-key' not in dump
    assert g.credentials.path.stat().st_mode & 0o777 == 0o600
    await g.handle_command('/new', '/new', 9)
    assert g.store.engine_session(9, 'claude') != session
    assert g.store.thread_id(9) == 'codex-original'


@pytest.mark.asyncio
async def test_unconfigured_wizard_deletes_secret_before_testing(tmp_path):
    g = gateway(tmp_path)
    await g.handle_message(message('/engine claude'))
    assert g.pending_input['kind'] == 'endpoint_url'
    await g.handle_message(message('https://relay.invalid'))
    async def probe(engine, url, key):
        assert g.telegram.deleted == [(9, 77)]
        assert engine == 'claude' and key == 'secret-never-in-db'
        return True, 'success'
    g.test_endpoint = probe
    await g.handle_message(message('secret-never-in-db', 77))
    assert g.credentials.get('claude')['api_key'] == 'secret-never-in-db'
    assert g.pending_input is None
    await g.handle_command('/status', '/status', 9)
    assert 'secret-never-in-db' not in '\n'.join(g.telegram.sent)
    assert not g.store.queue_count()


@pytest.mark.asyncio
async def test_unauthorized_key_does_not_change_state(tmp_path):
    g = gateway(tmp_path)
    await g.handle_command('/tavilykey', '/tavilykey', 9)
    m = message('private-key')
    m['from']['id'] = 8
    await g.handle_message(m)
    assert not g.credentials.current_ref('tavily')
    assert not g.telegram.deleted
    assert g.pending_input['kind'] == 'tavily_key'


@pytest.mark.asyncio
async def test_failed_endpoint_keeps_new_config_and_redacts_candidate(tmp_path):
    g = gateway(tmp_path)
    old = configure(g, 'codex')
    async def probe(*args):
        raise RuntimeError('rejected candidate-secret')
    g.test_endpoint = probe
    await g.handle_message(message('/endpoint codex https://new.invalid candidate-secret', 88))
    assert g.telegram.deleted == [(9, 88)]
    assert g.credentials.current_ref('codex') != old
    assert g.credentials.get('codex')['api_key'] == 'candidate-secret'
    assert g.credentials.get('codex', old)['api_key'] == 'fixture-api-key'
    assert g.store.get_meta('endpoint_state:codex') == 'ERROR'
    assert '已保存；测试失败' in g.telegram.sent[-1]
    assert 'candidate-secret' not in '\n'.join(g.telegram.sent)


@pytest.mark.parametrize('value', ['https://user:password@example.com', 'https://example.com/?key=secret', 'https://example.com/#key', 'file:///root', 'https://a:bad'])
def test_endpoint_url_rejects_credential_bearing_urls(value):
    with pytest.raises(ValueError):
        validate_url(value)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 429, 503])
async def test_tavily_errors_always_request_native_fallback(status, monkeypatch):
    real_client = httpx.AsyncClient
    async def handler(request):
        assert request.headers['authorization'] == 'Bearer tavily-test'
        return httpx.Response(status, json={'error': 'secret server error'})
    monkeypatch.setattr('codex_tg.search.httpx.AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    client = TavilyClient('tavily-test')
    result = await client.search('public query')
    assert result['native_search_fallback'] is True
    assert 'secret server error' not in json.dumps(result)
    assert client.last_state == 'ERROR'


@pytest.mark.asyncio
async def test_tavily_timeout_and_no_key_fallback(monkeypatch):
    assert (await TavilyClient(None).search('query'))['native_search_fallback']
    real_client = httpx.AsyncClient
    async def handler(request):
        raise httpx.ReadTimeout('includes-secret-key', request=request)
    monkeypatch.setattr('codex_tg.search.httpx.AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    assert await TavilyClient('key').search('query') == {'native_search_fallback': True, 'reason': 'ReadTimeout'}


@pytest.mark.asyncio
async def test_tavily_key_commands_and_restart(tmp_path, monkeypatch):
    g = gateway(tmp_path)
    assert not g.store.chat_settings(9).tavily_enabled
    async def search(self, query):
        assert g.telegram.deleted == [(9, 55)]
        return {'native_search_fallback': False, 'results': []}
    monkeypatch.setattr(TavilyClient, 'search', search)
    await g.handle_message(message('/tavilykey my-tavily-secret', 55))
    await g.handle_command('/tavily', '/tavily off', 9)
    reopened = Gateway(make_config(tmp_path))
    assert not reopened.store.chat_settings(9).tavily_enabled
    assert reopened.tavily.api_key == 'my-tavily-secret'
    await g.handle_command('/tavily', '/tavily on', 9)
    assert g.store.chat_settings(9).tavily_enabled
    assert 'my-tavily-secret' not in '\n'.join(g.telegram.sent)


@pytest.mark.asyncio
async def test_command_entrypoints_open_inline_menus_and_claude_settings(tmp_path, monkeypatch):
    g = gateway(tmp_path)
    configure(g, 'codex')
    configure(g, 'claude')
    await g.handle_command('/engine', '/engine', 9)
    assert {button['callback_data'] for row in g.telegram.keyboards[-1][2] for button in row} == {
        'engine:codex', 'engine:claude'
    }
    await g.handle_command('/endpoint', '/endpoint', 9)
    assert 'menu:endpoint:codex' in {
        button['callback_data'] for row in g.telegram.keyboards[-1][2] for button in row
    }
    await g.handle_command('/tavily', '/tavily', 9)
    assert {'tavily:on', 'tavily:off', 'tavily:key'} <= {
        button['callback_data'] for row in g.telegram.keyboards[-1][2] for button in row
    }
    await g.handle_command('/update', '/update', 9)
    assert {'update:normal', 'update:force', 'update:codex', 'update:claude'} <= {
        button['callback_data'] for row in g.telegram.keyboards[-1][2] for button in row
    }
    await g.handle_command('/engine', '/engine claude', 9)
    from codex_tg.models import ProviderModel
    async def provider_options(*_args, **_kwargs):
        return [ProviderModel('relay-claude-model', 'Relay Claude')]
    monkeypatch.setattr(g.models, 'options', provider_options)
    await g.handle_command('/model', '/model', 9)
    assert any(':select:0' in button['callback_data']
               for row in g.telegram.keyboards[-1][2] for button in row)
    await g.handle_command('/reasoning', '/reasoning', 9)
    assert 'setting:reasoning:xhigh' in {
        button['callback_data'] for row in g.telegram.keyboards[-1][2] for button in row
    }


@pytest.mark.asyncio
async def test_tavily_key_enables_search_when_default_is_off(tmp_path, monkeypatch):
    g = gateway(tmp_path)
    assert not g.store.chat_settings(9).tavily_enabled

    async def search(self, query):
        return {'native_search_fallback': False, 'results': []}

    monkeypatch.setattr(TavilyClient, 'search', search)
    await g.handle_message(message('/tavilykey secret-tavily-key', 77))
    assert g.store.chat_settings(9).tavily_enabled
    assert g.tavily.api_key == 'secret-tavily-key'


@pytest.mark.asyncio
async def test_tavily_key_wizard_survives_restart_and_replayed_key_is_not_a_task(tmp_path, monkeypatch):
    g = gateway(tmp_path)
    await g.handle_message(message('/tavilykey', 40))
    restarted = gateway(tmp_path)
    assert restarted.pending_input == {'kind': 'tavily_key'}

    async def search(self, query):
        return {'native_search_fallback': False, 'results': []}

    monkeypatch.setattr(TavilyClient, 'search', search)
    key_message = message('opaque-tavily-key-without-prefix', 41)
    await restarted.handle_message(key_message)
    assert restarted.telegram.deleted == [(9, 41)]
    assert restarted.store.chat_settings(9).tavily_enabled
    assert restarted.pending_input is None
    assert 'Tavily 已保存' in restarted.telegram.sent[-1]

    replayed = gateway(tmp_path)
    await replayed.handle_message(key_message)
    assert replayed.store.queue_count() == 0
    assert not replayed.telegram.sent


@pytest.mark.asyncio
async def test_probable_secret_is_never_enqueued_as_a_task(tmp_path):
    g = gateway(tmp_path)
    await g.handle_message(message('tvly-1234567890abcdef', 88))
    assert not g.store.queue_count()
    assert g.telegram.deleted == [(9, 88)]
    assert '已阻止将其发送给 Agent' in g.telegram.sent[-1]


@pytest.mark.asyncio
async def test_claude_unattended_command_enables_full_access_and_mcp(tmp_path):
    marker = tmp_path / 'claude-args.json'
    script = tmp_path / 'fake-claude'
    script.write_text(f'''#!/usr/bin/env python3
import json, pathlib, sys
if '--version' in sys.argv:
    print('2.1.283 (Claude Code)')
    raise SystemExit(0)
pathlib.Path({str(marker)!r}).write_text(json.dumps(sys.argv[1:]))
payload = json.loads(sys.stdin.readline())
print(json.dumps({{'type':'assistant','message':{{'content':[{{'type':'tool_use','name':'Bash'}}]}}}}))
print(json.dumps({{'type':'result','result':'unattended tools completed','session_id':payload['session_id']}}))
''', encoding='utf-8')
    script.chmod(0o755)
    runtime = ClaudeRuntime(tmp_path / 'state', str(script))
    await runtime.start()
    async def progress(_text):
        pass
    search = {'command': sys.executable, 'args': ['-m', 'codex_tg.search'], 'env': {'PYTHONPATH': '/tmp'}}
    session = str(uuid.uuid4())
    try:
        result = await runtime.run(session, 'run shell and file tools', [], progress, 'sonnet', 'high',
                                   {'base_url': 'https://relay.invalid', 'api_key': 'fixture-key'}, search)
        assert result.response == 'unattended tools completed'
        args = json.loads(marker.read_text())
        assert '--permission-mode' in args
        assert args[args.index('--permission-mode') + 1] == 'bypassPermissions'
        assert '--dangerously-skip-permissions' in args
        assert args[args.index('--permission-prompts') + 1] == 'none'
        assert '--strict-mcp-config' in args
        assert args[args.index('--allowedTools') + 1] == '*'
        assert '--mcp-config' in args
        assert 'dontAsk' not in args
        assert '--disallowedTools' not in args
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_claude_interrupt_terminates_process_and_descendant(tmp_path):
    script = tmp_path / 'fake-claude'
    marker = tmp_path / 'child.pid'
    script.write_text(f'''#!/usr/bin/env python3
import subprocess,sys,time,pathlib,json
if '--version' in sys.argv:
 print('2.1.283 (Claude Code)'); sys.exit(0)
p=subprocess.Popen(['sleep','60'])
pathlib.Path({str(marker)!r}).write_text(str(p.pid))
for line in sys.stdin: pass
time.sleep(60)
''')
    script.chmod(0o755)
    runtime = ClaudeRuntime(tmp_path / 'state', str(script))
    await runtime.start()
    async def progress(text): pass
    task = asyncio.create_task(runtime.run(str(uuid.uuid4()), 'task', [], progress, 'sonnet', 'high', {'base_url':'https://invalid','api_key':'test'}))
    for _ in range(100):
        if marker.exists(): break
        await asyncio.sleep(.02)
    assert marker.exists()
    child = int(marker.read_text())
    await runtime.interrupt()
    result = await asyncio.wait_for(task, 5)
    assert result.interrupted
    proc = Path(f'/proc/{child}/stat')
    try:
        assert proc.read_text().split()[2] == 'Z'
    except (FileNotFoundError, ProcessLookupError):
        pass
    await runtime.close()


@pytest.mark.asyncio
async def test_claude_worker_dispatch_and_stop_uses_running_engine(tmp_path):
    g = gateway(tmp_path)
    configure(g, 'claude')
    g.store.set_active_engine(9, 'claude')
    await g.handle_message(message('Claude task'))
    seen = []
    class Runtime:
        ready = True
        async def run(self, session, text, attachments, progress, model, effort, endpoint, search):
            seen.append((session, model, effort, endpoint['api_key']))
            g.stop_event.set()
            return ClaudeRunResult('done', False, session)
        async def interrupt(self):
            seen.append('interrupted'); return True
    g.claude_runtime = Runtime()
    await g.worker()
    assert seen[0][3] == 'fixture-api-key'
    assert g.store.last_task()['status'] == 'DONE'
    assert g.store.engine_session(9, 'claude') == seen[0][0]
    g.store.enqueue(7, 9, 'session', 'next', [], engine='claude')
    g.store.claim_next()
    g.store.set_active_engine(9, 'codex')
    await g.handle_command('/stop', '/stop', 9)
    assert seen[-1] == 'interrupted'


@pytest.mark.asyncio
async def test_search_mcp_real_stdio_handles_missing_key(tmp_path):
    vault = Credentials(tmp_path / 'credentials.json')
    ref = vault.save('tavily', '', '')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'gateway'))
    p = await asyncio.create_subprocess_exec(sys.executable, '-m', 'codex_tg.search', str(vault.path), ref,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env)
    requests = [
        {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05'}},
        {'jsonrpc':'2.0','id':2,'method':'tools/list'},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'tavily_search','arguments':{'query':'test'}}},
    ]
    output,_ = await p.communicate(('\n'.join(json.dumps(r) for r in requests)+'\n').encode())
    replies = [json.loads(line) for line in output.splitlines()]
    assert replies[0]['result']['capabilities']['tools'] == {}
    assert replies[1]['result']['tools'][0]['name'] == 'tavily_search'
    assert json.loads(replies[2]['result']['content'][0]['text'])['native_search_fallback']
