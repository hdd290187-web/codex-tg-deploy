from __future__ import annotations

import asyncio
import json
import time

QUIET_SECONDS = 3.0
MAX_MEMBERS = 40


class Batches:
    def __init__(self, gateway):
        self.g = gateway
        self.lock = asyncio.Lock()
        self.event = asyncio.Event()
        with self.g.store.connect() as conn:
            conn.execute("UPDATE upload_batches SET state='SUBMIT' WHERE state='SUBMITTING'")

    def open_batches(self):
        with self.g.store.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM upload_batches WHERE chat_id=? AND state IN ('OPEN','ERROR','SUBMIT','SUBMITTING') ORDER BY id", (self.g.config.allowed_chat_id,))]

    async def create(self, mode, group=None):
        with self.g.store.connect() as conn:
            cursor = conn.execute('INSERT INTO upload_batches(task_id,chat_id,mode,group_id,updated_at) VALUES(?,?,?,?,?)',
                (None, self.g.config.allowed_chat_id, mode, group, time.time()))
            batch = dict(conn.execute('SELECT * FROM upload_batches WHERE id=?', (cursor.lastrowid,)).fetchone())
        return batch

    async def handle(self, message) -> bool:
        text = str(message.get('text') or message.get('caption') or '')
        command = text.strip().split(maxsplit=1)[0].split('@')[0] if text.strip().startswith('/') else ''
        has_file = bool(message.get('document') or message.get('photo') or message.get('video') or message.get('audio'))
        async with self.lock:
            opened = self.open_batches()
            if command == '/batch':
                if opened:
                    await self.g.telegram.send(self.g.config.allowed_chat_id, '已有待提交批次；/run 提交或 /cancel 取消。')
                else:
                    batch = await self.create('manual')
                    if batch:
                        await self.g.telegram.send(self.g.config.allowed_chat_id, '手动批量模式已开启：后续文件和普通文字都进入本批次，尚不执行。/run 提交；/cancel 取消。')
                return True
            if command in {'/run', '/cancel'} and (opened or command == '/run'):
                if not opened:
                    await self.g.telegram.send(self.g.config.allowed_chat_id, '没有待提交批次；/batch 开启。')
                    return True
                batch = opened[-1]
                with self.g.store.connect() as conn:
                    if command == '/cancel':
                        conn.execute("UPDATE upload_batches SET state='CANCELLED' WHERE id=?", (batch['id'],))
                        if batch['task_id']:
                            conn.execute("UPDATE tasks SET status='CANCELLED' WHERE id=? AND status IN ('RECEIVING','QUEUED')", (batch['task_id'],))
                    else:
                        conn.execute("UPDATE upload_batches SET state='SUBMIT' WHERE id=?", (batch['id'],))
                        conn.execute("UPDATE upload_members SET state='PENDING',error=NULL WHERE batch_id=? AND state='FAILED'", (batch['id'],))
                self.event.set()
                self.g.worker_event.set()
                await self.g.telegram.send(self.g.config.allowed_chat_id, '批次已取消，未执行。' if command == '/cancel' else '正在完成所有附件下载并提交；失败附件会明确报告，不会静默遗漏。')
                return True
            if command:
                return False
            mid = int(message.get('message_id') or 0)
            with self.g.store.connect() as conn:
                if conn.execute('SELECT 1 FROM upload_members WHERE chat_id=? AND message_id=?', (self.g.config.allowed_chat_id, mid)).fetchone():
                    return True
            group = str(message.get('media_group_id') or '') or None
            batch = next((b for b in opened if b['mode'] == 'manual'), None)
            if batch is None and group:
                batch = next((b for b in opened if b['group_id'] == group), None)
                if batch is None:
                    with self.g.store.connect() as conn:
                        closed = conn.execute('SELECT 1 FROM upload_batches WHERE chat_id=? AND group_id=?', (self.g.config.allowed_chat_id, group)).fetchone()
                    if closed:
                        # Telegram has no end-of-album marker. Never silently split
                        # a late member into another Agent task.
                        batch = await self.create('manual', group)
                        await self.g.telegram.send(self.g.config.allowed_chat_id, '收到已提交相册的迟到附件，暂存为手动批次。请 /run 单独提交或 /cancel；未自动重复执行。')
            if batch is None and not group:
                batch = next((b for b in reversed(opened) if b['state'] == 'OPEN' and time.time() - b['updated_at'] < QUIET_SECONDS), None)
            if not batch and not has_file:
                return False
            if not batch:
                batch = await self.create('auto', group)
            if not batch:
                return True
            with self.g.store.connect() as conn:
                count = conn.execute('SELECT COUNT(*) FROM upload_members WHERE batch_id=?', (batch['id'],)).fetchone()[0]
                if count >= MAX_MEMBERS:
                    conn.execute("UPDATE upload_batches SET state='ERROR' WHERE id=?", (batch['id'],))
                    await self.g.telegram.send(self.g.config.allowed_chat_id, '批次超过 40 条，未接收本条；请拆分批次，当前批次需 /run 确认提交。')
                    return True
                conn.execute('INSERT INTO upload_members(chat_id,message_id,batch_id,message_json) VALUES(?,?,?,?)',
                    (self.g.config.allowed_chat_id, mid, batch['id'], json.dumps(message, ensure_ascii=False)))
                conn.execute('UPDATE upload_batches SET updated_at=? WHERE id=?', (time.time(), batch['id']))
            self.event.set()
            return True

    async def process(self):
        for batch in self.open_batches():
            with self.g.store.connect() as conn:
                members = [dict(r) for r in conn.execute('SELECT * FROM upload_members WHERE batch_id=? ORDER BY message_id', (batch['id'],))]
            if batch['state'] == 'SUBMITTING':
                continue
            for member in members:
                if member['state'] != 'PENDING':
                    continue
                message = json.loads(member['message_json'])
                try:
                    attachments = await self.g.save_attachment(message)
                    state, error = 'READY', None
                except Exception as exc:
                    attachments, state, error = [], 'FAILED', self.g.sanitize(str(exc))[:250]
                with self.g.store.connect() as conn:
                    conn.execute('UPDATE upload_members SET state=?,error=?,attachments_json=? WHERE chat_id=? AND message_id=?',
                                 (state, error, json.dumps(attachments), member['chat_id'], member['message_id']))
                await self.progress(batch)
            async with self.lock:
                with self.g.store.connect() as conn:
                    current = conn.execute('SELECT * FROM upload_batches WHERE id=?', (batch['id'],)).fetchone()
                    members = [dict(r) for r in conn.execute('SELECT * FROM upload_members WHERE batch_id=? ORDER BY message_id', (batch['id'],))]
                    if current['state'] == 'CANCELLED' or not members or any(m['state'] == 'PENDING' for m in members):
                        continue
                    if any(m['state'] == 'FAILED' for m in members):
                        conn.execute("UPDATE upload_batches SET state='ERROR' WHERE id=?", (batch['id'],))
                        continue
                    due = current['state'] == 'SUBMIT' or (current['mode'] == 'auto' and current['state'] == 'OPEN' and time.time() - current['updated_at'] >= QUIET_SECONDS)
                    if not due:
                        continue
                    texts, attachments = [], []
                    for m in members:
                        msg = json.loads(m['message_json'])
                        text = msg.get('text') or msg.get('caption') or ''
                        if text and text not in texts:
                            texts.append(text)
                        attachments += json.loads(m['attachments_json'] or '[]')
                with self.g.store.connect() as conn:
                    conn.execute("UPDATE upload_batches SET state='SUBMITTING' WHERE id=?", (batch['id'],))
            task_id = await self.g.submit_batch_task('\n\n'.join(texts) or '请检查我上传的文件，并按内容完成需要的操作。', attachments, batch_id=batch['id'])
            if task_id is None:
                with self.g.store.connect() as conn:
                    conn.execute("UPDATE upload_batches SET state='ERROR' WHERE id=?", (batch['id'],))
                await self.g.telegram.send(self.g.config.allowed_chat_id, f"批次 {batch['id']} 暂未提交：当前引擎未配置或工作目录无效。附件已保留，请修复配置后 /run 重试。")
                continue
            self.g.worker_event.set()
            await self.g.telegram.send(self.g.config.allowed_chat_id, f"批次 {batch['id']} 已提交为一个任务（ID {task_id}），共 {len(attachments)} 个附件、{len(texts)} 段说明。")

    async def progress(self, batch):
        with self.g.store.connect() as conn:
            rows = conn.execute('SELECT state,error FROM upload_members WHERE batch_id=?', (batch['id'],)).fetchall()
            current = conn.execute('SELECT state,progress_id FROM upload_batches WHERE id=?', (batch['id'],)).fetchone()
        if current['state'] == 'CANCELLED':
            return
        errors = [r['error'] for r in rows if r['state'] == 'FAILED']
        text = f"批次 {batch['id']} · {'手动 /run 提交' if batch['mode'] == 'manual' else '自动合并（静默 3 秒后提交）'}\n已接收 {len(rows)} 条；下载完成 {sum(r['state'] == 'READY' for r in rows)}；失败 {len(errors)}。"
        if errors:
            text += '\n' + '\n'.join(errors[:4]) + '\n尚未提交任何任务。/run 重试失败下载，/cancel 取消。'
        if current['progress_id']:
            await self.g.telegram.edit(self.g.config.allowed_chat_id, current['progress_id'], text)
        else:
            ids = await self.g.telegram.send(self.g.config.allowed_chat_id, text)
            with self.g.store.connect() as conn:
                conn.execute('UPDATE upload_batches SET progress_id=? WHERE id=?', (ids[-1], batch['id']))

    async def run(self):
        while not self.g.stop_event.is_set():
            try:
                await self.process()
            except Exception as exc:
                self.g.store.add_event('BATCH_ERROR', self.g.sanitize(str(exc))[:250])
                with self.g.store.connect() as conn:
                    conn.execute("UPDATE upload_batches SET state='SUBMIT' WHERE state='SUBMITTING'")
            self.event.clear()
            try:
                await asyncio.wait_for(self.event.wait(), .5)
            except asyncio.TimeoutError:
                pass
