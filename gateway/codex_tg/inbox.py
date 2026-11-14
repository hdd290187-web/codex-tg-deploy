"""Durable encrypted Telegram inbox; admission is replayable, execution isn't."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .config_files import atomic_write

LOG = logging.getLogger('codex_tg.inbox')


class Inbox:
    def __init__(self, gateway):
        self.g = gateway
        path = gateway.config.database_path.parent / 'inbox.key'
        if not path.exists():
            atomic_write(path, os.urandom(32))
        self.cipher = AESGCM(path.read_bytes())
        with self.g.store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS inbound_updates (
                update_id INTEGER PRIMARY KEY, payload BLOB, state TEXT NOT NULL,
                replayable INTEGER NOT NULL, created_at INTEGER NOT NULL, error TEXT)''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_inbound_state ON inbound_updates(state,update_id)')
            c.execute("UPDATE inbound_updates SET state=CASE WHEN replayable=1 THEN 'RECEIVED' ELSE 'UNCERTAIN' END WHERE state='PROCESSING'")

    def receive(self, updates):
        with self.g.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for update in updates:
                uid = int(update['update_id'])
                msg = update.get('message') or {}
                query = update.get('callback_query')
                allowed = self.g.authorized_callback(query) if query else self.g.authorized(msg)
                raw = json.dumps(update).encode()
                nonce = os.urandom(12)
                payload = nonce + self.cipher.encrypt(nonce, raw, str(uid).encode()) if allowed else None
                c.execute('INSERT OR IGNORE INTO inbound_updates VALUES(?,?,?,?,?,NULL)',
                          (uid, payload, 'RECEIVED' if allowed else 'DONE', 0, int(time.time())))
            if updates:
                offset = max(int(x['update_id']) for x in updates) + 1
                c.execute("INSERT INTO meta(key,value) VALUES('telegram_offset',?) ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(meta.value AS INTEGER), CAST(excluded.value AS INTEGER))", (str(offset),))
            c.commit()

    async def dispatch(self):
        while not self.g.stop_event.is_set():
            with self.g.store.connect() as c:
                row = c.execute("SELECT * FROM inbound_updates WHERE state='RECEIVED' ORDER BY update_id LIMIT 1").fetchone()
            if row is None:
                await asyncio.sleep(.25)
                continue
            uid = row['update_id']
            try:
                payload = row['payload']
                update = json.loads(self.cipher.decrypt(payload[:12], payload[12:], str(uid).encode()))
                msg = update.get('message') or {}
                text = str(msg.get('text') or msg.get('caption') or '')
                replayable = bool(msg and not text.lstrip().startswith('/') and not self.g.pending_input)
                with self.g.store.connect() as c:
                    c.execute("UPDATE inbound_updates SET state='PROCESSING',replayable=? WHERE update_id=?", (int(replayable), uid))
                if update.get('callback_query'):
                    await self.g.handle_callback(update['callback_query'])
                elif msg:
                    msg['_inbox_update_id'] = uid
                    await self.g.handle_message(msg)
                with self.g.store.connect() as c:
                    c.execute("UPDATE inbound_updates SET state='DONE',payload=NULL WHERE update_id=?", (uid,))
            except Exception as exc:
                # Never auto-repeat commands with uncertain external effects.
                with self.g.store.connect() as c:
                    c.execute("UPDATE inbound_updates SET state='UNCERTAIN',error=? WHERE update_id=?", (type(exc).__name__, uid))
                LOG.error('inbox update %s retained for recovery: %s', uid, type(exc).__name__)

    def summary(self):
        with self.g.store.connect() as c:
            return [dict(r) for r in c.execute("SELECT update_id,state,error,replayable FROM inbound_updates WHERE state != 'DONE' ORDER BY update_id LIMIT 20")]

    def retry(self, uid):
        with self.g.store.connect() as c:
            # Only ordinary task admission is replayed; task enqueue is keyed
            # by Telegram message ID. Interrupted Agent tasks stay interrupted.
            return c.execute("UPDATE inbound_updates SET state='RECEIVED',error=NULL WHERE update_id=? AND state='UNCERTAIN' AND replayable=1", (uid,)).rowcount

    async def poll(self):
        while not self.g.stop_event.is_set():
            try:
                offset = int(self.g.store.get_meta('telegram_offset', '0'))
                self.receive(await self.g.telegram.get_updates(offset))
            except Exception as exc:
                LOG.error('Telegram receive failed: %s', self.g.sanitize(str(exc)))
                await asyncio.sleep(3)
