from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_tg.app import Gateway
from codex_tg.credentials import atomic_json
from codex_tg.delivery import call_tool
from test_dual_engine import gateway, configure, message


@pytest.mark.asyncio
@pytest.mark.parametrize('engine', ['codex', 'claude'])
async def test_manual_effort_ignores_catalog_and_keeps_queued_snapshot(tmp_path, engine):
    g = gateway(tmp_path)
    configure(g, engine)
    g.store.set_active_engine(9, engine)
    g.store.set_thread(9, 'thread')
    g.store.register_thread(9, 'thread')
    async def forbidden(*_args, **_kwargs):
        pytest.fail('manual effort must not fetch or validate capabilities')
    g.runtime.supported_efforts = forbidden
    g.runtime.validate_reasoning = forbidden
    g.runtime.validate_plan_reasoning = forbidden
    await g.handle_message(message('queued task', 30))
    active = g.store.claim_next()
    await g.handle_message(message('/reasoning ultra', 31))
    await g.handle_message(message('/planreasoning custom_level', 32))
    settings = g.store.chat_settings(9)
    if engine == 'codex':
        assert settings.reasoning_effort == 'ultra'
        assert settings.plan_reasoning_effort == 'custom_level'
    else:
        assert settings.claude_effort == 'custom_level'
    assert active['effort'] != 'ultra'
    assert g.store.task(active['id'], 9)['effort'] == active['effort']
    restarted = gateway(tmp_path)
    assert restarted.store.chat_settings(9) == settings
    await restarted.handle_message(message('/reasoning invalid;command', 33))
    assert restarted.store.chat_settings(9) == settings


@pytest.mark.asyncio
@pytest.mark.parametrize('engine', ['codex', 'claude'])
@pytest.mark.parametrize('failure', ['no-models', 'network-error'])
async def test_new_key_saved_before_test_failure_and_old_queue_kept(tmp_path, engine, failure):
    g = gateway(tmp_path)
    old = configure(g, engine)
    g.store.set_active_engine(9, engine)
    g.store.set_thread(9, 'thread')
    g.store.register_thread(9, 'thread')
    await g.handle_message(message('old task', 10))
    async def probe(_engine, _url, key):
        assert g.credentials.get(engine)['api_key'] == key == 'replacement-key'
        assert Gateway(g.config).credentials.get(engine)['api_key'] == key
        if failure == 'network-error':
            raise RuntimeError('connection failed replacement-key')
        return False, 'No models available replacement-key'
    g.test_endpoint = probe
    await g.handle_message(message('/endpoint ' + engine + ' key', 11))
    await g.handle_message(message('replacement-key', 12))
    assert (9, 12) in g.telegram.deleted
    assert g.credentials.current_ref(engine) != old
    assert g.credentials.get(engine)['api_key'] == 'replacement-key'
    assert g.engine_endpoints[engine]['api_key'] == 'replacement-key'
    assert g.store.get_meta('endpoint_state:' + engine) == 'ERROR'
    assert '已保存；测试失败' in g.telegram.sent[-1]
    assert 'replacement-key' not in '\n'.join(g.telegram.sent)
    task = g.store.claim_next()
    assert task['endpoint_ref'] == old
    assert g.credentials.get(engine, task['endpoint_ref'])['api_key'] == 'fixture-api-key'
    g.store.complete(task['id'], 'done')
    await g.handle_message(message('next task', 13))
    next_task = g.store.claim_next()
    assert next_task['endpoint_ref'] == g.credentials.current_ref(engine)
    assert gateway(tmp_path).credentials.get(engine)['api_key'] == 'replacement-key'


def test_file_tool_rejects_inactive_wrong_identity_and_ended_tasks(tmp_path):
    g = gateway(tmp_path / 'data')
    task_id, *_ = g.store.enqueue(7, 9, 'thread', 'send requested file', [])
    report = tmp_path / 'report.txt'
    report.write_text('test report')
    args = (g.config.database_path.parent, g.config.database_path, g.delivery_token, 'codex', 9, 7,
            'telegram_send_file', {'paths': [str(report)]})
    atomic_json(g.delivery_context, {'task_id': task_id, 'engine': 'codex', 'token': 'wrong'})
    with pytest.raises(ValueError):
        call_tool(*args)
    atomic_json(g.delivery_context, {'task_id': task_id, 'engine': 'codex', 'token': g.delivery_token})
    with pytest.raises(ValueError):
        call_tool(*args)  # queued, not RUNNING
    g.store.claim_next()
    for chat, user, engine in ((8, 7, 'codex'), (9, 8, 'codex'), (9, 7, 'claude')):
        with pytest.raises(ValueError):
            call_tool(args[0], args[1], args[2], engine, chat, user, args[6], args[7])
    result = call_tool(*args)
    assert result['files'][0]['status'] == 'READY'
    assert call_tool(*args)['files'][0]['id'] == result['files'][0]['id']
    status = call_tool(*args[:6], 'telegram_file_status', {})
    assert status['files'][0]['status'] == 'READY'
    g.store.complete(task_id, 'done')
    with pytest.raises(ValueError):
        call_tool(*args)
    assert len(g.store.artifacts(9)) == 1


def test_no_builtin_prompt_in_runtime_paths():
    root = Path(__file__).resolve().parents[1] / 'gateway/codex_tg'
    runtime = (root / 'runtime.py').read_text()
    claude = (root / 'claude_runtime.py').read_text()
    app = (root / 'app.py').read_text()
    assert 'SEARCH_POLICY' not in runtime + claude
    assert '--append-system-prompt' not in claude
    assert "task_text = task['text']" in app
    assert 'self.artifacts.begin(task_id)' not in app
    assert 'self.artifacts.instruction' not in app
    assert 'telegram_send_file' in (root / 'delivery.py').read_text()


@pytest.mark.asyncio
async def test_plain_task_is_queued_without_trimmed_content_or_extra_prompts(tmp_path):
    g = gateway(tmp_path)
    configure(g, 'codex')
    g.store.set_thread(9, 'thread')
    g.store.register_thread(9, 'thread')
    text = '  original text\n\n Keep spaces  \n'
    await g.handle_message(message(text, 40))
    task = g.store.claim_next()
    assert task['text'] == text
    assert not task['codex_prompt_ref'] and task['output_dir'] is None
    assert not g.codex_prompt_state().enabled and not g.codex_prompt_state().configured
    assert 'original text' in json.dumps(task)
