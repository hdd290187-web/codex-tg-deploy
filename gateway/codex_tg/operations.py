from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .activity import acquire_activity
from .artifacts import ArtifactError, Artifacts, validate_directory
from .backups import Backups
from .diagnostics import diagnose
from .telegram import TelegramDeliveryUncertain
from .config_sync import ConfigSync

FILE_STATES = {'READY': '已生成，等待发送', 'SENDING': '文件发送中', 'DELIVERED': '文件交付成功',
               'FAILED': '文件交付失败', 'UNKNOWN': '发送结果待确认'}
TASK_STATES = {'QUEUED': '排队中', 'RUNNING': '执行中', 'STOPPING': '正在中断', 'DONE': '执行完成',
               'ERROR': '执行失败', 'INTERRUPTED': '已中断', 'CANCELLED': '已取消'}


class Operations:
    def initialize_operations(self):
        self.artifacts = Artifacts(self.config.database_path.parent, self.store,
                                   lambda: self.credentials.secrets() + [self.config.telegram_token, self.config.telegram_proxy_url or ''])
        self.backups = Backups(self.config.database_path.parent, self.config.environment_file or Path('/etc/codex-tg.env'))
        self.delivery_event = asyncio.Event()
        self.background_operations: set[asyncio.Task] = set()
        self.delivery_token = secrets.token_urlsafe(32)
        self.delivery_context = self.config.database_path.parent / 'delivery-context.json'
        self.runtime.delivery = self.delivery_config('codex')
        self.claude_runtime.delivery = self.delivery_config('claude')
        self.config_sync = ConfigSync(self.config.database_path.parent, self.config.release_base_url,
                                      self.config.release_public_key_path.parent / 'codex-tg-config.pub')

    def delivery_config(self, engine: str) -> dict:
        return {'command': sys.executable,
                'args': ['-m', 'codex_tg.delivery', str(self.config.database_path.parent), str(self.config.database_path),
                         engine, str(self.config.allowed_chat_id), str(self.config.allowed_user_id)],
                'env': {'PYTHONPATH': str(Path(__file__).resolve().parents[1]), 'CODEX_TG_DELIVERY_TOKEN': self.delivery_token}}

    def background(self, coro):
        task = asyncio.create_task(coro)
        self.background_operations.add(task)
        task.add_done_callback(self.background_operations.discard)

    def updating(self) -> bool:
        # Kernel lock is authoritative; a stale JSON status cannot pause FIFO forever.
        import fcntl
        path = self.config.database_path.parent / 'updater.lock'
        if path.exists():
            with path.open('a') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
        pending = float(self.store.get_meta('update_requested_at', '0') or 0)
        return bool(pending and time.time() - pending < 15)

    async def confirm_operation(self, chat_id: int, action: str, value, text: str):
        token = uuid.uuid4().hex[:16]
        self.store.set_meta('operation_confirmation', json.dumps({'token': token, 'action': action, 'value': value, 'expires': time.time() + 300}))
        await self.telegram.send_keyboard(chat_id, text, [[{'text': '确认', 'callback_data': 'ops:confirm:' + token},
                                                        {'text': '取消', 'callback_data': 'ops:cancel'}]])

    async def show_files(self, chat_id: int):
        rows = self.store.artifacts(chat_id)
        lines = ['最近成品（点击重发只重传文件，不会重新执行 Agent）']
        buttons = []
        for item in rows:
            owner = f"任务 {item['task_id']}" if item['task_id'] else '/sendfile'
            lines.append(f"F{item['id']} · {item['name']} · {item['size'] / 1024:.1f} KiB\n{owner} · {FILE_STATES[item['status']]}")
            if item['error']:
                lines.append(self.sanitize(item['error'])[:150])
            buttons.append([{'text': f"重发 F{item['id']} {item['name']}"[:60], 'callback_data': f"ops:file:{item['id']}"}])
        await self.telegram.send_keyboard(chat_id, '\n'.join(lines) if rows else '暂无文件发送记录；可直接告诉 Agent“把那个文件发给我”，或使用 /sendfile /绝对路径。', buttons)

    async def show_tasks(self, chat_id: int, argument: str = ''):
        if argument:
            if not argument.isdigit():
                raise ValueError('用法：/tasks 或 /tasks 任务ID')
            task = self.store.task(int(argument), chat_id)
            if not task:
                raise ValueError('任务不存在')
            text = f"任务 {task['id']} · {task['engine']} · {TASK_STATES.get(task['status'], task['status'])}\n目录：{task['cwd']}\n{self.sanitize(task['text'])[:700]}\n\n"
            text += self.sanitize(task.get('final_response') or task.get('error') or '暂无结果')[:1800]
            buttons = [[{'text': '查看成品', 'callback_data': 'ops:files'}]]
            if task['status'] in {'ERROR', 'INTERRUPTED'}:
                buttons.append([{'text': '重试任务（需确认）', 'callback_data': f"ops:retry:{task['id']}"}])
            await self.telegram.send_keyboard(chat_id, text, buttons)
            return
        tasks = self.store.task_history(chat_id)
        lines, buttons = ['最近任务（任务 ID 全局唯一）'], []
        for task in tasks:
            lines.append(f"{task['id']} · {task['engine']} · {TASK_STATES.get(task['status'], task['status'])} · 成品 {task['files']} / 已交付 {task['delivered']}\n{self.sanitize(task['text'])[:100]}")
            buttons.append([{'text': f"任务 {task['id']} 详情", 'callback_data': f"ops:task:{task['id']}"}])
        await self.telegram.send_keyboard(chat_id, '\n'.join(lines) if tasks else '暂无任务', buttons)

    async def run_diagnosis(self, chat_id: int):
        try:
            report, repairable = await diagnose(self)
            if len(report) <= 3600:
                await self.telegram.send_keyboard(chat_id, report,
                    [[{'text': '尝试修复目录/代理服务', 'callback_data': 'ops:repair'}]] if repairable else [])
            else:
                with tempfile.TemporaryDirectory(prefix='diagnose-', dir=self.config.database_path.parent) as folder:
                    path = Path(folder) / 'gateway-diagnose.txt'
                    path.write_text(report)
                    path.chmod(0o600)
                    await self.telegram.send_document(chat_id, path, '完整诊断报告；未调用模型')
                await self.telegram.send_keyboard(chat_id, '诊断已完成，详见报告。',
                    [[{'text': '尝试修复目录/代理服务', 'callback_data': 'ops:repair'}]] if repairable else [])
        except Exception as exc:
            with contextlib.suppress(Exception):
                await self.telegram.send(chat_id, '诊断未完成：' + self.sanitize(str(exc))[:300])

    async def operation_command(self, command: str, text: str, chat_id: int) -> bool:
        argument = text.partition(' ')[2].strip()
        if command == '/inbox':
            if argument.isdigit():
                ok = self.inbox.retry(int(argument))
                await self.telegram.send(chat_id, '已安排幂等重试接收。' if ok else '该操作不允许自动重放；请核对 /tasks 后重新发送命令。')
            else:
                rows = self.inbox.summary()
                await self.telegram.send(chat_id, '待处理接收记录：\n' + '\n'.join(str(r) for r in rows) + '\n普通消息可用 /inbox 更新ID 重试；命令需核对后重新发送。')
        elif command == '/files':
            await self.show_files(chat_id)
        elif command == '/sendfile':
            if not argument:
                await self.telegram.send(chat_id, '用法：/sendfile /opt/report.pdf\n仅接受明确指定的普通成品文件；系统及凭据文件不会发送。')
                return True
            path = Path(argument).expanduser()
            record = await asyncio.to_thread(self.artifacts.register, path, chat_id)
            self.delivery_event.set()
            await self.telegram.send(chat_id, f'文件 F{record} 已加入发送队列；/files 可查看交付结果。')
        elif command in {'/pwd', '/cd'}:
            engine = self.store.active_engine(chat_id)
            current = self.store.workdir(chat_id, engine)
            if command == '/cd' and argument:
                current = validate_directory(argument, current)
                self.store.set_workdir(chat_id, engine, current)
                await self.telegram.send(chat_id, f'{engine.title()} 后续任务工作目录：{current}\n当前和已排队任务保持原目录，长期会话保留。')
            else:
                active = self.store.active()
                note = f"\n当前执行任务目录：{active['cwd']}" if active and active['engine'] == engine else ''
                valid = '存在' if Path(current).is_dir() else '目录已不存在，后续任务将报错'
                await self.telegram.send(chat_id, f'{engine.title()} 工作目录：{current}（{valid}）{note}\n切换目录：/cd /opt/myproject')
        elif command == '/tasks':
            await self.show_tasks(chat_id, argument)
        elif command == '/diagnose':
            if any(t.get_name() == 'diagnosis' and not t.done() for t in self.background_operations):
                await self.telegram.send(chat_id, '诊断正在执行，请稍候。')
            else:
                await self.telegram.send(chat_id, '正在进行本地与普通网络检查，不调用模型。')
                task = asyncio.create_task(self.run_diagnosis(chat_id), name='diagnosis')
                self.background_operations.add(task)
                task.add_done_callback(self.background_operations.discard)
        elif command in {'/backup', '/restore'}:
            if command == '/backup' and not argument:
                path = await asyncio.to_thread(self.backups.create)
                await self.telegram.send(chat_id, f'已创建 AES-GCM 加密配置备份：\n{path}\n密钥单独保存在 VPS 的 data/backup.key（0600）。\n/backup list 查看，/restore 恢复；未向 Telegram 发送任何 Key。')
            elif command == '/restore' and argument:
                self.backups.read(argument)
                await self.confirm_operation(chat_id, 'restore', argument, f'确认恢复 {argument}？\n将恢复配置并重启 Gateway；任务、文件记录和会话文件保留。恢复前会再创建备份。')
            else:
                paths = self.backups.list()
                await self.telegram.send_keyboard(chat_id, '最近加密配置备份：' if paths else '暂无备份，请 /backup',
                    [[{'text': '恢复 ' + path.name, 'callback_data': 'ops:restore:' + path.name}] for path in paths])
        else:
            return False
        return True

    async def operation_callback(self, data: str, chat_id: int, callback_id: str) -> bool:
        if not data.startswith('ops:'):
            return False
        await self.telegram.answer_callback(callback_id)
        try:
            parts = data.split(':', 2)
            action, value = parts[1], parts[2] if len(parts) > 2 else ''
            if action in {'files', 'tasks', 'diagnose', 'pwd', 'backup'}:
                await self.operation_command('/' + action, '/' + action, chat_id)
            elif action == 'file':
                item = self.store.artifact(int(value), chat_id)
                if not item:
                    raise ValueError('文件记录不存在')
                if item['status'] == 'UNKNOWN':
                    await self.confirm_operation(chat_id, 'resend', item['id'], '上次发送结果未确认。重发可能产生重复文件，确认继续？')
                else:
                    queued = self.store.resend_artifact(item['id'], chat_id)
                    self.delivery_event.set()
                    await self.telegram.send(chat_id, '已加入重发队列。' if queued else '文件已在发送队列中。')
            elif action == 'task':
                await self.show_tasks(chat_id, value)
            elif action == 'retry':
                item = self.store.task(int(value), chat_id)
                if not item or item['status'] not in {'ERROR', 'INTERRUPTED'}:
                    raise ValueError('仅失败或中断任务可手动重试')
                await self.confirm_operation(chat_id, 'retry', item['id'],
                    f"确认重新执行任务 {item['id']}？\n删除文件、数据库修改、部署等操作可能重复产生影响。"
                    '\n重试使用该引擎当前模型、Endpoint、目录与会话；不会自动重试。')
            elif action == 'restore':
                await self.operation_command('/restore', '/restore ' + value, chat_id)
            elif action == 'repair':
                await self.confirm_operation(chat_id, 'repair', '', '尝试修复上传/成品目录权限，并在订阅模式下重新启动 Mihomo 与刷新 timer？')
            elif action == 'cancel':
                self.store.set_meta('operation_confirmation', '{}')
                await self.telegram.send(chat_id, '已取消。')
            elif action == 'confirm':
                confirmation = json.loads(self.store.get_meta('operation_confirmation', '{}'))
                self.store.set_meta('operation_confirmation', '{}')
                if confirmation.get('token') != value or confirmation.get('expires', 0) < time.time():
                    raise ValueError('确认已过期，请重新操作')
                await self.perform_operation(confirmation['action'], confirmation['value'], chat_id)
        except Exception as exc:
            await self.telegram.send(chat_id, '操作失败：' + self.sanitize(str(exc))[:400])
        return True

    async def perform_operation(self, action: str, value, chat_id: int):
        if action == 'resend':
            self.store.resend_artifact(int(value), chat_id)
            self.delivery_event.set()
            await self.telegram.send(chat_id, '已加入重发队列。')
        elif action == 'retry':
            task = self.store.task(int(value), chat_id)
            if not task or task['status'] not in {'ERROR', 'INTERRUPTED'}:
                raise ValueError('任务状态不允许重试')
            engine = task['engine']
            settings = self.store.chat_settings(chat_id)
            endpoint = self.credentials.get(engine)
            if not endpoint.get('api_key') or not endpoint.get('base_url'):
                raise ValueError('先配置该引擎 /endpoint')
            attachments = json.loads(task['attachments_json'])
            if any(not Path(x['path']).is_file() for x in attachments):
                raise ValueError('原上传附件已不存在，请重新上传并提交任务')
            session = self.ensure_claude_conversation(chat_id) if engine == 'claude' else await self.ensure_current_thread(chat_id)
            new_id, _, _ = self.store.enqueue(self.config.allowed_user_id, chat_id, session, task['text'], attachments,
                settings, engine=engine, session_id=session, endpoint_ref=self.credentials.current_ref(engine),
                search_ref=self.credentials.current_ref('tavily') if settings.tavily_enabled else '',
                codex_prompt_ref=self.codex_prompt_revision() if engine == 'codex' else '',
                cwd=validate_directory(self.store.workdir(chat_id, engine)))
            self.worker_event.set()
            await self.telegram.send(chat_id, f'已手动提交重试任务 {new_id}，原任务记录保留。')
        elif action in {'restore', 'repair'}:
            if self.store.active() or self.store.queue_count() or self.store.delivery_busy() or self.updating():
                raise ValueError('请等待当前任务、队列、文件发送及更新结束后再操作')
            lock = acquire_activity(self.config.database_path.parent, exclusive=True)
            if lock is None:
                raise ValueError('后台任务正在使用 Gateway')
            with lock:
                if action == 'restore':
                    await asyncio.to_thread(self.backups.restore, value, chat_id, self.config.allowed_user_id)
                    await self.telegram.send(chat_id, '配置已恢复，正在重启 Gateway。')
                    command = ['systemctl', 'restart', '--no-block', 'codex-tg.service']
                else:
                    for folder in (self.config.upload_dir, self.artifacts.vault, self.artifacts.outboxes):
                        if folder.is_symlink():
                            raise ValueError('目录为符号链接，未自动修改权限')
                        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                        folder.chmod(0o700)
                    command = ['systemctl', 'restart', 'codex-tg-proxy.service', 'codex-tg-proxy-refresh.timer'] if self.config.telegram_proxy_kind == 'subscription' else None
                if command:
                    proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    if await proc.wait():
                        raise ValueError('systemd 操作失败；配置已保存，请检查服务日志')
                if action == 'repair':
                    await self.run_diagnosis(chat_id)

    async def deliver_artifact(self, item: dict):
        if item['chat_id'] != self.config.allowed_chat_id:
            raise ArtifactError('交付目标与授权 Chat 不一致')
        parts = await asyncio.to_thread(self.artifacts.parts, item)
        if len(parts) > 1:
            await self.telegram.send(item['chat_id'], f"{item['name']} 超过 Telegram 单文件限制，正在分片交付。"
                '\n将所有 .part、.merge.json 和 .merge.py 放在同一目录，运行 merge.py 合并并校验 SHA-256。')
        for part in parts:
            if part['message_id']:
                continue
            path = Path(part['path'])
            if path.is_symlink() or not path.resolve().is_relative_to(self.artifacts.vault.resolve()):
                raise ArtifactError('文件副本路径异常')
            def check():
                with path.open('rb') as source:
                    return Artifacts._digest(source)
            if await asyncio.to_thread(check) != part['sha256']:
                raise ArtifactError('文件分片校验失败')
            self.store.begin_part_attempt(item['id'], part['number'])
            result = await self.telegram.send_document(item['chat_id'], path,
                f"F{item['id']} · " + (f"任务 {item['task_id']}" if item['task_id'] else '手动发送') + f" · {path.name}")
            if not isinstance(result, dict) or not result.get('message_id') or not result.get('document', {}).get('file_id'):
                raise TelegramDeliveryUncertain('Telegram 未确认文件消息，不能标记为已交付')
            self.store.acknowledge_part(item['id'], part['number'], result['message_id'], result['document']['file_id'])
        self.store.artifact_state(item['id'], 'DELIVERED')

    async def delivery_worker(self):
        while not self.stop_event.is_set():
            lock = None if self.updating() else acquire_activity(self.config.database_path.parent)
            item = self.store.claim_artifact(self.config.allowed_chat_id) if lock else None
            if not item:
                if lock:
                    lock.close()
                self.delivery_event.clear()
                try:
                    await asyncio.wait_for(self.delivery_event.wait(), 2)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                with lock:
                    await self.deliver_artifact(item)
            except asyncio.CancelledError:
                self.store.artifact_state(item['id'], 'UNKNOWN', '发送期间停止；请核对 Telegram 后重发')
                raise
            except Exception as exc:
                state = 'UNKNOWN' if isinstance(exc, TelegramDeliveryUncertain) else 'FAILED'
                detail = self.sanitize(str(exc))[:400]
                self.store.artifact_state(item['id'], state, detail)
                if item['chat_id'] == self.config.allowed_chat_id:
                    with contextlib.suppress(Exception):
                        await self.telegram.send(item['chat_id'], f"文件 F{item['id']} {FILE_STATES[state]}：{detail}\n/files 可手动重发；Agent 任务不会重新执行。")
