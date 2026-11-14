from __future__ import annotations

import asyncio

import pytest

from test_dual_engine import configure, gateway, message


def file_message(message_id: int, name: str, group: str | None = None, caption: str = ''):
    value = message(caption, message_id)
    value['document'] = {'file_id': f'file-{message_id}', 'file_name': name, 'file_size': 10}
    if group:
        value['media_group_id'] = group
    return value


@pytest.mark.asyncio
async def test_media_group_three_files_becomes_one_task_with_all_attachments(tmp_path):
    g = gateway(tmp_path)
    configure(g, 'codex')
    g.store.set_thread(9, 'thread')
    g.store.register_thread(9, 'thread')
    for mid, name in enumerate(('one.txt', 'two.pdf', 'three.zip'), 10):
        assert await g.batches.handle(file_message(mid, name, 'album-1', '说明文字' if mid == 10 else ''))
    assert g.store.queue_count() == 0
    await asyncio.sleep(3.1)
    await g.batches.process()
    await g.batches.process()
    assert g.store.queue_count() == 1
    task = g.store.claim_next()
    assert task['text'] == '说明文字'
    assert len(task['attachments']) == 3
    assert {item['name'] for item in task['attachments']} == {'one.txt', 'two.pdf', 'three.zip'}
    assert g.store.task_history(9)[0]['kind'] == 'batch'


@pytest.mark.asyncio
async def test_manual_batch_run_and_cancel_do_not_create_individual_tasks(tmp_path):
    g = gateway(tmp_path)
    configure(g, 'codex')
    g.store.set_thread(9, 'thread')
    g.store.register_thread(9, 'thread')
    await g.handle_message(message('/batch', 1))
    await g.handle_message(file_message(2, 'a.txt', caption='first note'))
    await g.handle_message(file_message(3, 'b.txt', caption='second note'))
    assert g.store.queue_count() == 0
    await g.handle_message(message('/run', 4))
    await g.batches.process()
    assert g.store.queue_count() == 1
    task = g.store.claim_next()
    assert task['kind'] == 'batch' and len(task['attachments']) == 2
    assert 'first note' in task['text'] and 'second note' in task['text']
    await g.handle_message(message('/batch', 5))
    await g.handle_message(file_message(6, 'cancel.txt'))
    await g.handle_message(message('/cancel', 7))
    await g.batches.process()
    assert g.store.queue_count() == 0
    assert any('批次已取消' in text for text in g.telegram.sent)


@pytest.mark.asyncio
async def test_batch_duplicate_update_and_failed_member_are_visible(tmp_path):
    g = gateway(tmp_path)
    configure(g, 'codex')
    g.store.set_thread(9, 'thread')
    g.store.register_thread(9, 'thread')
    bad = file_message(11, 'bad.txt', 'album-2')
    async def download(_file_id, target, _limit):
        if target.name.startswith('bad'):
            raise RuntimeError('download unavailable')
        target.write_text('ok')
    g.telegram.download = download
    await g.batches.handle(bad)
    await g.batches.handle(bad)
    await g.batches.process()
    assert g.store.queue_count() == 0
    assert any('download unavailable' in text or '失败' in text for text in g.telegram.sent)
    with g.store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM upload_members').fetchone()[0] == 1
