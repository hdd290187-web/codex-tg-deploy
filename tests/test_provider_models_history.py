from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest

from codex_tg.claude_runtime import ClaudeRuntime
from codex_tg.models import ModelCatalog, ModelDiscoveryError
from test_dual_engine import configure, gateway, message
from test_gateway import SettingsRuntime


def callback(data, user=7):
    return {'id': 'callback-test', 'from': {'id': user}, 'data': data,
            'message': {'message_id': 81, 'chat': {'id': 9, 'type': 'private'}}}


def buttons(g):
    return [button for row in g.telegram.keyboards[-1][2] for button in row]


def transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs.get('trust_env') is False
        assert kwargs.get('follow_redirects') is False
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr('codex_tg.models.httpx.AsyncClient', client)


@pytest.mark.asyncio
@pytest.mark.parametrize('engine,auth', [('codex', 'api_key'), ('claude', 'api_key'), ('claude', 'bearer')])
async def test_provider_catalog_uses_endpoint_auth_and_paginated_actual_ids(engine, auth, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.url.path == '/relay/v1/models'
        if engine == 'claude' and auth == 'api_key':
            assert request.headers['x-api-key'] == 'private-key'
        else:
            assert request.headers['authorization'] == 'Bearer private-key'
        if engine == 'claude':
            assert request.headers['anthropic-version'] == '2023-06-01'
        if not request.url.params:
            return httpx.Response(200, json={'data': [{'id': 'custom/first'}], 'has_more': True, 'last_id': 'custom/first'})
        assert request.url.params['after_id'] == 'custom/first'
        return httpx.Response(200, json={'data': [
            {'id': 'custom/first'}, {'id': 'new-model-' + 'x' * 90, 'display_name': 'New Model'},
            {'id': 'disabled', 'available': False},
        ], 'has_more': False})

    transport(monkeypatch, handler)
    catalog = ModelCatalog()
    endpoint = {'base_url': 'https://example.invalid/relay/v1', 'api_key': 'private-key', 'auth': auth}
    models = await catalog.options(engine, endpoint)
    assert [x.model for x in models] == ['custom/first', 'new-model-' + 'x' * 90]
    assert len(requests) == 2
    assert await catalog.options(engine, endpoint) == models
    assert len(requests) == 2
    await catalog.options(engine, endpoint, refresh=True)
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_compatible_path_and_claude_bearer_fallback(monkeypatch):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == '/relay/v1/models':
            return httpx.Response(404)
        if 'authorization' not in request.headers:
            return httpx.Response(401)
        return httpx.Response(200, json={'models': ['provider-alias']})

    transport(monkeypatch, handler)
    models = await ModelCatalog().options('claude', {'base_url': 'https://relay.invalid/relay', 'api_key': 'private-key'})
    assert [x.model for x in models] == ['provider-alias']
    assert paths == ['/relay/v1/models', '/relay/models', '/relay/models']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [401, 403, 404, 429, 503, 'html', 'malformed', 'timeout', 'redirect'])
async def test_failed_catalog_never_exposes_response_secrets_or_invents_models(failure, monkeypatch):
    def handler(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('private-key', request=request)
        if failure == 'redirect':
            return httpx.Response(302, headers={'location': 'https://another.invalid/models'})
        if failure == 'html':
            return httpx.Response(200, text='<html>private-key</html>')
        if failure == 'malformed':
            return httpx.Response(200, json={'error': 'private-key'})
        return httpx.Response(failure, text='private-key')

    transport(monkeypatch, handler)
    with pytest.raises(ModelDiscoveryError) as raised:
        await ModelCatalog().options('claude', {'base_url': 'https://relay.invalid', 'api_key': 'private-key'})
    assert 'private-key' not in str(raised.value)


@pytest.mark.asyncio
async def test_cache_identity_includes_key_engine_and_refresh_failure(monkeypatch):
    requests = []
    fail = False

    def handler(request):
        requests.append(request)
        if fail:
            return httpx.Response(503)
        return httpx.Response(200, json={'data': [{'id': 'live-alias'}]})

    transport(monkeypatch, handler)
    catalog = ModelCatalog()
    first = {'base_url': 'https://relay.invalid/v1', 'api_key': 'first'}
    second = {**first, 'api_key': 'second'}
    await catalog.options('codex', first)
    await catalog.options('codex', second)
    await catalog.options('claude', second)
    assert len(requests) == 3
    fail = True
    with pytest.raises(ModelDiscoveryError):
        await catalog.options('codex', first, refresh=True)
    with pytest.raises(ModelDiscoveryError):
        await catalog.options('codex', first)


@pytest.mark.asyncio
@pytest.mark.parametrize('engine', ['codex', 'claude'])
async def test_gateway_model_menu_selection_refresh_rotation_and_old_buttons(tmp_path, monkeypatch, engine):
    g = gateway(tmp_path)
    configure(g, engine)
    g.store.set_active_engine(9, engine)
    g.runtime = SettingsRuntime()
    calls = []
    model_ids = [f'relay-model-{n}' for n in range(11)] + ['custom/' + 'a' * 85]

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'data': [{'id': x} for x in model_ids]})

    transport(monkeypatch, handler)
    await g.handle_message(message('/model'))
    old_button = next(x['callback_data'] for x in buttons(g) if ':select:0' in x['callback_data'])
    await g.handle_callback(callback(old_button, user=8))
    assert 'Unauthorized' in g.telegram.callback_answers[-1][1]
    await g.handle_callback(callback(old_button))
    settings = g.store.chat_settings(9)
    assert (settings.model if engine == 'codex' else settings.claude_model) == 'relay-model-0'
    await g.handle_callback(callback(next(x['callback_data'] for x in buttons(g) if x['text'] == '下一页')))
    long_button = next(x['callback_data'] for x in buttons(g) if ':select:11' in x['callback_data'])
    assert len(long_button.encode()) <= 64
    await g.handle_callback(callback(long_button))
    settings = g.store.chat_settings(9)
    assert (settings.model if engine == 'codex' else settings.claude_model) == model_ids[-1]
    model_ids[:] = ['detected-after-refresh']
    await g.handle_callback(callback(next(x['callback_data'] for x in buttons(g) if ':refresh:' in x['callback_data'])))
    assert len(calls) == 2
    assert any(x['text'] == 'detected-after-refresh' for x in buttons(g))
    await g.handle_callback(callback(old_button))
    assert '过期' in g.telegram.callback_answers[-1][1]

    async def endpoint_test(*_args):
        return True, 'verified'
    g.test_endpoint = endpoint_test
    await g.save_endpoint(engine, 'https://new-relay.invalid/v1', 'new-key')
    assert not g.models.cache and not g.model_menus
    await g.handle_message(message('/model', 16))
    assert calls[-1].url.host == 'new-relay.invalid'
    fresh = next(x['callback_data'] for x in buttons(g) if ':select:' in x['callback_data'])
    g.store.set_active_engine(9, 'claude' if engine == 'codex' else 'codex')
    await g.handle_callback(callback(fresh))
    assert '引擎/中转站已改变' in g.telegram.callback_answers[-1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize('engine', ['codex', 'claude'])
async def test_no_catalog_shows_manual_input_not_fixed_models(tmp_path, monkeypatch, engine):
    g = gateway(tmp_path)
    configure(g, engine)
    g.store.set_active_engine(9, engine)
    g.runtime = SettingsRuntime()
    transport(monkeypatch, lambda request: httpx.Response(404))
    await g.handle_message(message('/model'))
    assert '无法自动获取模型列表' in g.telegram.keyboards[-1][1]
    assert not any(':select:' in b['callback_data'] for b in buttons(g))
    await g.handle_message(message('/model custom-manual-model', 13))
    settings = g.store.chat_settings(9)
    assert (settings.model if engine == 'codex' else settings.claude_model) == 'custom-manual-model'


@pytest.mark.asyncio
async def test_empty_refresh_retires_previous_models_and_never_adds_current_model(tmp_path, monkeypatch):
    g = gateway(tmp_path)
    configure(g, 'claude')
    g.store.set_active_engine(9, 'claude')
    ids = ['live-model']
    transport(monkeypatch, lambda request: httpx.Response(200, json={'data': [{'id': x} for x in ids]}))
    await g.handle_message(message('/model'))
    old = next(b['callback_data'] for b in buttons(g) if ':select:' in b['callback_data'])
    ids.clear()
    await g.handle_message(message('/model', 13))
    assert '列表为空' in g.telegram.keyboards[-1][1]
    assert not any(':select:' in b['callback_data'] for b in buttons(g))
    await g.handle_callback(callback(old))
    assert '已过期' in g.telegram.callback_answers[-1][1]


def session_file(runtime, title='An actual Claude task', turns=1, subagent=False):
    session_id = str(uuid.uuid4())
    path = runtime.data_dir / 'projects' / '-'
    if subagent:
        path = path / 'parent-session' / 'subagents'
    path.mkdir(parents=True, exist_ok=True)
    path = path / f'{session_id}.jsonl'
    rows = [{'type': 'user', 'sessionId': session_id, 'uuid': str(uuid.uuid4()),
             'message': {'content': [{'type': 'text', 'text': title}]}} for _ in range(turns)]
    rows.append({'type': 'user', 'sessionId': session_id, 'message': {'content': [{'type': 'tool_result', 'content': 'tool output'}]}})
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n{"unfinished":')
    return session_id, path


@pytest.mark.asyncio
async def test_claude_history_pagination_selection_numbering_restart_and_engine_separation(tmp_path):
    g = gateway(tmp_path)
    configure(g, 'claude')
    g.store.set_thread(9, 'codex-history-preserved')
    g.store.set_active_engine(9, 'claude')
    chosen, _ = session_file(g.claude_runtime, turns=3)
    for i in range(7):
        _, path = session_file(g.claude_runtime, title=f'History {i}')
        os.utime(path, (100 + i, 100 + i))
    session_file(g.claude_runtime, title='subagent must not appear', subagent=True)
    _, empty = session_file(g.claude_runtime, turns=0)
    await g.handle_command('/sessions', '/sessions', 9)
    assert 'Claude' in g.telegram.keyboards[-1][1]
    assert len([b for b in buttons(g) if b['callback_data'].startswith('claude:select:')]) == 6
    await g.handle_callback(callback('claude:page:1'))
    assert len([b for b in buttons(g) if b['callback_data'].startswith('claude:select:')]) == 2
    await g.handle_callback(callback('claude:select:' + empty.stem))
    assert '不存在' in g.telegram.callback_answers[-1][1]
    await g.handle_callback(callback('claude:select:' + chosen, user=8))
    assert g.store.engine_session(9, 'claude') is None
    await g.handle_callback(callback('claude:select:' + chosen))
    assert g.store.claude_conversation_key(9) == chosen
    assert g.store.engine_session(9, 'claude') == chosen
    assert g.store.thread_id(9) == 'codex-history-preserved'
    await g.handle_message(message('Continue the selected Claude history'))
    await g.handle_callback(callback('claude:select:' + chosen))
    assert '排队' in g.telegram.callback_answers[-1][1]
    task = g.store.claim_next()
    assert task['engine'] == 'claude' and task['session_id'] == chosen and task['thread_task_number'] == 4
    g.store.complete(task['id'], 'done')
    restarted = gateway(tmp_path)
    assert restarted.store.engine_session(9, 'claude') == chosen
    await restarted.handle_command('/new', '/new', 9)
    assert restarted.store.engine_session(9, 'claude') != chosen
    await restarted.handle_callback(callback('claude:select:' + chosen))
    assert restarted.store.thread_task_number(chosen) == 4
    restarted.store.set_active_engine(9, 'codex')
    await restarted.handle_callback(callback('claude:select:' + chosen))
    assert '切回 Claude' in restarted.telegram.callback_answers[-1][1]
    assert restarted.store.thread_id(9) == 'codex-history-preserved'


@pytest.mark.asyncio
async def test_claude_history_ignores_invalid_paths_and_updates_titles(tmp_path):
    runtime = ClaudeRuntime(tmp_path)
    session, path = session_file(runtime)
    assert (await runtime.get_session(session)).turns == 1
    with path.open('a') as out:
        out.write('\n' + json.dumps({'type': 'custom-title', 'sessionId': session, 'customTitle': 'Renamed session'}))
    assert (await runtime.get_session(session)).title == 'Renamed session'
    with pytest.raises(ValueError):
        await runtime.get_session('../../credentials')
    outside = tmp_path.parent / f'{uuid.uuid4()}.jsonl'
    outside.write_bytes(path.read_bytes())
    (path.parent / outside.name).symlink_to(outside)
    assert len((await runtime.list_sessions()).threads) == 1
    path.unlink()
    with pytest.raises(ValueError):
        await runtime.get_session(session)
