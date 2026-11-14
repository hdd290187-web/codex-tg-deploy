from __future__ import annotations

import asyncio
import hashlib
import json
import os
import zipfile
from pathlib import Path

import httpx
import pytest

from codex_tg.activity import acquire_activity
from codex_tg.artifacts import ArtifactError
from codex_tg.backups import Backups
from codex_tg.diagnostics import diagnose
from codex_tg.delivery import call_tool
from codex_tg.runtime import RunResult
from codex_tg.claude_runtime import ClaudeRunResult
from codex_tg.telegram import Telegram, TelegramDeliveryUncertain, TelegramError
from test_dual_engine import gateway, configure, message
from test_provider_models_history import callback


@pytest.mark.asyncio
@pytest.mark.parametrize('engine', ['codex', 'claude'])
async def test_task_artifacts_automatic_delivery_multiple_files_upload_and_cwd(tmp_path, engine):
    g = gateway(tmp_path / 'data')
    configure(g, engine)
    g.store.set_active_engine(9, engine)
    cwd = tmp_path / 'project'
    cwd.mkdir()
    g.store.set_workdir(9, engine, str(cwd))
    g.store.set_thread(9, 'codex-thread')
    g.store.register_thread(9, 'codex-thread')
    uploaded = tmp_path / 'upload.csv'
    uploaded.write_text('name,salary\nTest,100\n')
    settings = g.store.chat_settings(9)
    g.store.enqueue(7, 9, 'session', 'Generate reports and return them.', [{'kind': 'document', 'path': str(uploaded)}],
                    settings, engine=engine, session_id='session', endpoint_ref=g.credentials.current_ref(engine))
    received = []

    async def send_document(chat, path, caption=''):
        assert chat == 9
        received.append((path.name, path.read_bytes()))
        return {'message_id': len(received) + 1, 'document': {'file_id': 'tg-file-' + str(len(received))}}
    g.telegram.send_document = send_document

    class Runtime:
        ready = True
        async def get_thread(self, *_args, **_kwargs):
            return type('Thread', (), {'id': 'session'})()
        async def run(self, *args):
            text, attachments = args[1], args[2]
            assert self.workdir == str(cwd)
            assert Path(attachments[0]['path']).read_text().startswith('name,salary')
            assert text == 'Generate reports and return them.'
            folder = cwd
            (folder / '员工工资表.txt').write_text('Test 100')
            with zipfile.ZipFile(folder / '工资表.xlsx', 'w') as output:
                output.writestr('[Content_Types].xml', '<Types/>')
                output.writestr('xl/workbook.xml', '<workbook/>')
            (folder / 'report.pdf').write_bytes(b'%PDF-1.4\nfixture report\n%%EOF')
            response = call_tool(g.config.database_path.parent, g.config.database_path, g.delivery_token, engine, 9, 7,
                                'telegram_send_file', {'paths': [str(folder / n) for n in ('员工工资表.txt', '工资表.xlsx', 'report.pdf')]})
            assert all(item['status'] == 'READY' for item in response['files'])
            return ClaudeRunResult('created', False, 'session') if engine == 'claude' else RunResult('created', False)
    runtime = Runtime()
    setattr(g, 'claude_runtime' if engine == 'claude' else 'runtime', runtime)
    worker = asyncio.create_task(g.worker())
    delivery = asyncio.create_task(g.delivery_worker())
    try:
        for _ in range(300):
            files = g.store.artifacts(9)
            if len(files) == 3 and all(x['status'] == 'DELIVERED' for x in files):
                break
            await asyncio.sleep(.02)
        assert len(received) == 3
        assert {x[0] for x in received} == {'员工工资表.txt', '工资表.xlsx', 'report.pdf'}
        assert g.store.last_task()['status'] == 'DONE'
        assert all(x['status'] == 'DELIVERED' and x['task_id'] for x in g.store.artifacts(9))
        await g.handle_message(message('/files', 60))
        assert '交付成功' in g.telegram.keyboards[-1][1]
        newest = g.store.artifacts(9)[0]
        await g.handle_callback(callback('ops:file:' + str(newest['id'])))
        for _ in range(100):
            if len(received) == 4:
                break
            await asyncio.sleep(.02)
        assert len(received) == 4
    finally:
        g.stop_event.set()
        g.worker_event.set()
        g.delivery_event.set()
        await asyncio.gather(worker, delivery)


@pytest.mark.asyncio
async def test_sendfile_snapshot_auth_failures_and_unknown_ack(tmp_path):
    g = gateway(tmp_path / 'data')
    report = tmp_path / 'report.txt'
    report.write_text('snapshot')
    unauthorized = message('/sendfile ' + str(report), 21)
    unauthorized['from']['id'] = 8
    await g.handle_message(unauthorized)
    assert not g.store.artifacts(9)
    await g.handle_message(message('/sendfile ' + str(report), 22))
    report.write_text('changed later')
    item = g.store.claim_artifact()
    assert Path(item['stored_path']).read_text() == 'snapshot'
    async def no_ack(*_args):
        return None
    g.telegram.send_document = no_ack
    with pytest.raises(TelegramDeliveryUncertain):
        await g.deliver_artifact(item)
    assert g.store.artifact(item['id'], 9)['status'] == 'SENDING'
    g.store.recover_deliveries()
    assert g.store.artifact(item['id'], 9)['status'] == 'UNKNOWN'
    await g.handle_callback(callback('ops:file:' + str(item['id'])))
    assert g.store.artifact(item['id'], 9)['status'] == 'UNKNOWN'
    confirmation = g.telegram.keyboards[-1][2][0][0]['callback_data']
    await g.handle_callback(callback(confirmation))
    assert g.store.artifact(item['id'], 9)['status'] == 'READY'


def test_artifact_security_does_not_scan_vps_and_blocks_links_keys_and_archives(tmp_path):
    g = gateway(tmp_path / 'data')
    target = tmp_path / 'private.txt'
    target.write_text(g.config.telegram_token)
    with pytest.raises(ArtifactError):
        g.artifacts.register(target, 9)
    target.write_text('safe text')
    link = tmp_path / 'link.txt'
    link.symlink_to(target)
    with pytest.raises(ArtifactError):
        g.artifacts.register(link, 9)
    hard = tmp_path / 'hard.txt'
    os.link(target, hard)
    with pytest.raises(ArtifactError):
        g.artifacts.register(hard, 9)
    hard.unlink()
    archive = tmp_path / 'unsafe.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('keys/.env', 'KEY=value')
    with pytest.raises(ArtifactError):
        g.artifacts.register(archive, 9)
    with pytest.raises(ArtifactError):
        g.artifacts.register(Path('/etc/passwd'), 9)
    task_id, *_ = g.store.enqueue(7, 9, 'session', 'task', [])
    folder = g.artifacts.begin(task_id)
    (folder / 'safe.txt').write_text('deliver only this')
    (folder / 'steal.txt').symlink_to(target)
    records, errors = g.artifacts.collect(folder, 9, task_id)
    assert len(records) == 1 and len(errors) == 1
    assert g.store.artifacts(9)[0]['source_path'] == str(folder / 'safe.txt')


@pytest.mark.asyncio
async def test_large_file_parts_resume_and_merge(tmp_path, monkeypatch):
    monkeypatch.setattr('codex_tg.artifacts.CHUNK_SIZE', 20)
    g = gateway(tmp_path / 'data')
    original = tmp_path / 'large.txt'
    raw = b'large-file-content\n' * 8
    original.write_bytes(raw)
    record = g.artifacts.register(original, 9)
    item = g.store.claim_artifact()
    delivered = []
    fail = True
    async def send(chat, path, caption=''):
        nonlocal fail
        if path.name.endswith('part002') and fail:
            fail = False
            raise TelegramError('temporary failure')
        delivered.append(path.name)
        return {'message_id': len(delivered), 'document': {'file_id': path.name}}
    g.telegram.send_document = send
    with pytest.raises(TelegramError):
        await g.deliver_artifact(item)
    g.store.artifact_state(record, 'FAILED', 'temporary failure')
    assert g.store.resend_artifact(record, 9)
    await g.deliver_artifact(g.store.claim_artifact())
    assert delivered.count('large.txt.part001') == 1
    assert g.store.artifact(record, 9)['status'] == 'DELIVERED'
    parts = g.store.artifact_parts(record)
    merged = b''.join(Path(p['path']).read_bytes() for p in parts if '.part' in Path(p['path']).name)
    assert merged == raw and hashlib.sha256(merged).hexdigest() == item['sha256']


@pytest.mark.asyncio
async def test_upload_retry_rewinds_file_and_ambiguous_timeout_is_not_retried(tmp_path, monkeypatch):
    attempts = []
    path = tmp_path / 'binary.xlsx'
    path.write_bytes(b'file-content-marker' * 30)
    async def handler(request):
        attempts.append(await request.aread())
        if len(attempts) == 1:
            return httpx.Response(429, json={'ok': False, 'parameters': {'retry_after': 0}})
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1, 'document': {'file_id': 'file'}}})
    async def sleep(_seconds):
        pass
    monkeypatch.setattr('codex_tg.telegram.asyncio.sleep', sleep)
    telegram = Telegram('fixture')
    await telegram.client.aclose()
    telegram.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert (await telegram.send_document(9, path))['message_id'] == 1
        assert len(attempts) == 2 and all(path.read_bytes() in raw for raw in attempts)
        async def timeout(request):
            attempts.append(await request.aread())
            raise httpx.ReadTimeout('ambiguous', request=request)
        await telegram.client.aclose()
        telegram.client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        with pytest.raises(TelegramDeliveryUncertain):
            await telegram.send_document(9, path)
        assert len(attempts) == 3
    finally:
        await telegram.close()


@pytest.mark.asyncio
async def test_cwd_engine_independence_queue_snapshot_restart_and_activity_lock(tmp_path):
    g = gateway(tmp_path / 'data')
    configure(g, 'codex')
    g.store.set_thread(9, 'thread')
    g.store.register_thread(9, 'thread')
    folder = tmp_path / 'work'
    folder.mkdir()
    await g.handle_message(message('/cd ' + str(folder), 30))
    await g.handle_message(message('first', 31))
    await g.handle_message(message('/cd /', 32))
    task = g.store.claim_next()
    assert task['cwd'] == str(folder)
    assert g.store.workdir(9, 'claude') == '/'
    g.store.complete(task['id'], 'done')
    recreated = gateway(tmp_path / 'data')
    assert recreated.store.workdir(9, 'codex') == '/'
    assert recreated.store.task(task['id'], 9)['cwd'] == str(folder)
    lock = acquire_activity(g.config.database_path.parent, exclusive=True)
    assert lock is not None
    try:
        assert acquire_activity(g.config.database_path.parent) is None
    finally:
        lock.close()


def test_encrypted_backup_roundtrip_preserves_tasks_files_and_no_plaintext_keys(tmp_path):
    g = gateway(tmp_path / 'data')
    configure(g, 'codex', 'backup-secret-value')
    g.save_codex_prompt('exact prompt\r\n', enabled=True)
    g.store.set_workdir(9, 'codex', str(tmp_path))
    environment = tmp_path / 'gateway.env'
    environment.write_text('API_KEY=backup-secret-value\n')
    proxy = tmp_path / 'proxy'
    proxy.mkdir()
    (proxy / 'config.yaml').write_text('subscription: private-subscription-value')
    backups = Backups(g.config.database_path.parent, environment, proxy)
    g.store.enqueue(7, 9, 'history', 'before backup', [])
    backup = backups.create()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert backups.key_file.stat().st_mode & 0o777 == 0o600
    assert b'backup-secret-value' not in backup.read_bytes()
    assert b'exact prompt' not in backup.read_bytes()
    g.store.enqueue(7, 9, 'history', 'must remain', [])
    g.save_codex_prompt('changed', enabled=False)
    g.store.set_workdir(9, 'codex', '/')
    configure(g, 'codex', 'new-key')
    backups.restore(backup.name, 9, 7)
    assert g.prompt_store.read() == 'exact prompt\r\n'
    assert g.store.workdir(9, 'codex') == str(tmp_path)
    assert g.store.queue_count() == 2
    _, _, number = g.store.enqueue(7, 9, 'history', 'after restore', [])
    assert number == 3
    assert 'backup-secret-value' in (g.config.database_path.parent / 'credentials.json').read_text()
    raw = bytearray(backup.read_bytes())
    raw[-1] ^= 1
    backup.write_bytes(raw)
    with pytest.raises(Exception):
        backups.read(backup.name)


@pytest.mark.asyncio
async def test_diagnose_reports_unverified_model_and_real_failures_without_model_calls(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    g = gateway(tmp_path / 'data')
    configure(g, 'codex')
    g.config.upload_dir.mkdir()
    private = Ed25519PrivateKey.generate()
    g.config.release_public_key_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    manifest = json.dumps({'version': '2.2.0'}).encode()
    requests = []
    async def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith('.sig'):
            return httpx.Response(200, content=private.sign(manifest))
        return httpx.Response(200, content=manifest)
    real_client = httpx.AsyncClient
    monkeypatch.setattr('codex_tg.diagnostics.httpx.AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    async def unit(_name):
        return 'active'
    async def get_me():
        return {'id': 1}
    async def catalog(**_kwargs):
        return []
    async def claude_start():
        g.claude_runtime.ready = False
    g.telegram.get_me = get_me
    g.runtime.ready = True
    g.runtime.reasoning_catalog = catalog
    g.claude_runtime.start = claude_start
    monkeypatch.setattr('codex_tg.diagnostics.systemd_state', unit)
    monkeypatch.setattr('codex_tg.diagnostics.codex_components', lambda: {
        'paired': True, 'sdk': '0.162.1', 'runtime_version': 'codex-cli 0.162.1', 'runtime_path': '/bundled/codex',
        'cli_path': '/bin/codex', 'cli_version': 'codex-cli 0.162.1', 'cli_installation': 'Gateway 管理'})
    report, _repair = await diagnose(g)
    assert 'Gateway/systemd：正常' in report
    assert 'Codex 配置：已配置（本次未验证模型）' in report
    assert 'Claude CLI：异常' in report and 'Claude 配置：未配置' in report
    assert '文件交付：尚未验证' in report and 'Release：正常' in report
    assert 'fixture-api-key' not in report
    assert requests and all(method == 'GET' and '/responses' not in path for method, path in requests)


@pytest.mark.asyncio
async def test_delivery_failure_does_not_change_agent_done_and_no_automatic_task_retry(tmp_path):
    g = gateway(tmp_path / 'data')
    task_id, *_ = g.store.enqueue(7, 9, 'session', 'potentially destructive task', [])
    g.store.claim_next()
    folder = g.artifacts.begin(task_id)
    (folder / 'report.txt').write_text('generated')
    g.store.complete(task_id, 'execution complete')
    records, errors = g.artifacts.collect(folder, 9, task_id)
    assert records and not errors
    again, errors = g.artifacts.collect(folder, 9, task_id)
    assert again == records and len(g.store.artifacts(9)) == 1
    async def fail(*_args):
        raise TelegramError('network unavailable')
    g.telegram.send_document = fail
    worker = asyncio.create_task(g.delivery_worker())
    for _ in range(100):
        if g.store.artifacts(9)[0]['status'] == 'FAILED':
            break
        await asyncio.sleep(.02)
    g.stop_event.set()
    g.delivery_event.set()
    await worker
    assert g.store.artifacts(9)[0]['status'] == 'FAILED'
    assert g.store.task(task_id, 9)['status'] == 'DONE'
    assert g.store.queue_count() == 0


@pytest.mark.asyncio
async def test_poll_does_not_replay_task_when_reply_fails_after_enqueue(tmp_path, monkeypatch):
    g = gateway(tmp_path / 'data')
    offsets = []
    async def updates(offset):
        offsets.append(offset)
        if offset < 101:
            return [{'update_id': 100, 'message': message('one task')}]
        g.stop_event.set()
        return []
    async def message_handler(_message):
        g.store.enqueue(7, 9, 'thread', 'one task', [])
        raise TelegramError('reply network failure after task queued')
    async def delay(_seconds):
        return
    g.telegram.get_updates = updates
    g.handle_message = message_handler
    monkeypatch.setattr('codex_tg.app.asyncio.sleep', delay)
    await g.poll()
    assert offsets == [0, 101]
    assert g.store.queue_count() == 1
    assert g.store.get_meta('telegram_offset') == '101'


def test_file_delivery_never_claims_previous_chat_after_reconfiguration(tmp_path):
    g = gateway(tmp_path / 'data')
    path = tmp_path / 'old-report.txt'
    path.write_text('report for previous authorized chat')
    g.artifacts.register(path, 8)
    assert g.store.claim_artifact(9) is None
    assert g.store.artifact(g.store.artifacts(8)[0]['id'], 9) is None
