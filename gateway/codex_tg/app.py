from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import re
import signal
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .claude_runtime import ClaudeRuntime
from .credentials import Credentials, atomic_json, validate_key, validate_url
from .models import ModelCatalog, ModelDiscoveryError
from .search import TavilyClient
from .runtime import CodexRuntime
from .pre_instructions import MAX_BYTES, PromptState, PromptStore, PromptValidationError, validate_prompt
from .responses_adapter import ResponsesAdapter
from .store import ChatSettings, Store
from .telegram import Telegram, TelegramError, clean_filename, safe_error
from .activity import acquire_activity
from .artifacts import validate_directory
from .operations import Operations
from .batches import Batches
from .inbox import Inbox
from .configuration_flow import ConfigurationFlow


LOG = logging.getLogger("codex_tg")
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
EFFORT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
PROBABLE_SECRET_RE = re.compile(
    r"^(?:tvly-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9._-]{12,}|sk-ant-[A-Za-z0-9._-]{12,}|"
    r"xai-[A-Za-z0-9._-]{12,}|gsk_[A-Za-z0-9._-]{12,})$"
)
HELP = """Codex TG 命令

主菜单：/settings
/status  当前状态
/compact  使用当前引擎原生能力压缩上下文
/inbox  接收异常与幂等恢复
/syncconfig  拉取并三方合并 Agent 配置模板
/configstatus  查看配置模板、本地修改和冲突
/configexport  导出脱敏后的当前 Agent 配置
/engine  切换 Codex / Claude
/model  模型设置
/reasoning  Reasoning / Effort 设置
/endpoint  中转站设置
/tavily  搜索设置
/codexprompt  Codex 前置 Instructions

会话与任务：
/new  新建当前引擎会话
/sessions  选择当前引擎的历史会话
/stop  停止当前任务
/queue  查看等待任务
/clearqueue  清空等待队列
/log  最近执行记录
/tasks  任务结果与手动重试
/files  最近成品与重新发送
/sendfile  发送指定 VPS 文件
/batch  开启手动多文件批次
/run  提交当前批次
/cancel  取消当前输入或批次
/cd  切换当前引擎工作目录
/pwd  查看工作目录

维护：
/update  Gateway 更新
/version  版本信息
/diagnose  一键诊断
/backup  创建加密配置备份
/restore  恢复配置备份
/help  显示此菜单

兼容命令：/planreasoning、/tavilykey、/updatecodex、/updateclaude、/resume"""


class ProgressMessage:
    def __init__(self, telegram: Telegram, store: Store, chat_id: int, task_id: int, message_id: int, engine: str = "Codex") -> None:
        self.telegram = telegram
        self.engine = engine
        self.store = store
        self.chat_id = chat_id
        self.task_id = task_id
        self.message_id = message_id
        self.lines: list[str] = []
        self.last_edit = 0.0
        self.pending: asyncio.Task[None] | None = None

    async def update(self, summary: str) -> None:
        if not self.lines or self.lines[-1] != summary:
            self.lines.append(summary[:180])
            self.lines = self.lines[-8:]
            self.store.add_event("PROGRESS", summary, self.task_id)
        delay = max(0.0, 1.5 - (time.monotonic() - self.last_edit))
        if delay:
            if self.pending is None or self.pending.done():
                self.pending = asyncio.create_task(self._delayed(delay))
            return
        await self.flush()

    async def _delayed(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.flush()

    async def flush(self) -> None:
        text = f"{self.engine} 正在执行\n\n" + "\n".join(
            ("✓ " if index < len(self.lines) - 1 else "→ ") + line
            for index, line in enumerate(self.lines)
        )
        try:
            await self.telegram.edit(self.chat_id, self.message_id, text)
            self.last_edit = time.monotonic()
        except TelegramError as exc:
            LOG.warning("progress edit failed: %s", exc)

    async def close(self) -> None:
        if self.pending and not self.pending.done():
            self.pending.cancel()
            try:
                await self.pending
            except asyncio.CancelledError:
                pass
        await self.flush()

    async def finish(self, label: str) -> None:
        if self.pending and not self.pending.done():
            self.pending.cancel()
            try:
                await self.pending
            except asyncio.CancelledError:
                pass
        history = "\n".join(f"✓ {line}" for line in self.lines[-7:])
        text = label if not history else f"{label}\n\n{history}"
        await self.telegram.edit(self.chat_id, self.message_id, text)


class Gateway(Operations, ConfigurationFlow):
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = Store(config.database_path)
        self.prompt_store = PromptStore(config.database_path.parent / "codex-pre-instructions.txt")
        self.responses_adapter = ResponsesAdapter(self.prompt_store, config.adapter_port)
        self.codex_prompt_state()
        self.telegram = Telegram(
            config.telegram_token,
            config.telegram_proxy_url,
            config.telegram_proxy_kind,
        )
        self.runtime = CodexRuntime(config, self.responses_adapter)
        self.claude_runtime = ClaudeRuntime(config.database_path.parent / "claude")
        self.tavily = TavilyClient(config.tavily_api_key)
        self.models = ModelCatalog()
        self.model_menus: dict[str, dict[str, Any]] = {}
        self.engine_endpoints: dict[str, dict[str, str | None]] = {
            "codex": {
                "base_url": os.getenv("CODEX_TG_BASE_URL", "").strip() or None,
                "api_key": os.getenv("CODEX_TG_API_KEY", "").strip() or None,
            },
            "claude": {"base_url": config.claude_base_url, "api_key": config.claude_api_key},
        }
        self.credentials = Credentials(config.database_path.parent / "credentials.json")
        if not self.engine_endpoints['codex']['base_url']:
            # Upgrade from <=1.3.8: recover only the named Gateway provider.
            import tomli
            try:
                home = Path(os.getenv('CODEX_HOME', '/root/.codex'))
                old = tomli.loads((home / 'config.toml').read_text())
                self.engine_endpoints['codex']['base_url'] = old.get('model_providers', {}).get('codex_tg', {}).get('base_url')
            except (OSError, ValueError):
                pass
        for engine, values in self.engine_endpoints.items():
            if not self.credentials.current_ref(engine) and values.get('api_key'):
                self.credentials.save(engine, values.get('base_url') or '', values['api_key'],
                    'bearer' if engine == 'claude' and os.getenv('ANTHROPIC_AUTH_TOKEN') else 'api_key')
            if self.credentials.current_ref(engine):
                self.engine_endpoints[engine] = self.credentials.get(engine)
        if not self.credentials.current_ref('tavily') and config.tavily_api_key:
            self.credentials.save('tavily', '', config.tavily_api_key)
        self.tavily.api_key = self.credentials.get('tavily').get('api_key', '')
        try:
            restored_pending = json.loads(self.store.get_meta("pending_input", "null"))
        except ValueError:
            restored_pending = None
        self._pending_input: dict[str, str] | None = restored_pending if isinstance(restored_pending, dict) else None
        self.pending_since = 0.0
        self.worker_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.health_server: asyncio.Server | None = None
        self.started_at = int(time.time())
        self.bot: dict[str, Any] = {}
        self.initialize_operations()
        self.batches = Batches(self)
        self.inbox = Inbox(self)

    def authorized(self, message: dict[str, Any]) -> bool:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        return (
            int(sender.get("id", 0)) == self.config.allowed_user_id
            and int(chat.get("id", 0)) == self.config.allowed_chat_id
            and chat.get("type") == "private"
        )

    def authorized_callback(self, query: dict[str, Any]) -> bool:
        sender = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        return (
            int(sender.get("id", 0)) == self.config.allowed_user_id
            and int(chat.get("id", 0)) == self.config.allowed_chat_id
            and chat.get("type") == "private"
        )

    @staticmethod
    def task_number(task: dict[str, Any]) -> int:
        return int(task.get("thread_task_number") or task["id"])

    @property
    def pending_input(self) -> dict[str, str] | None:
        return self._pending_input

    @pending_input.setter
    def pending_input(self, value: dict[str, str] | None) -> None:
        self._pending_input = value
        self.store.set_meta("pending_input", json.dumps(value, separators=(",", ":")))

    def mark_secret_message(self, message_id: int) -> None:
        if message_id:
            self.store.set_meta("last_secret_message_id", str(message_id))

    async def start(self) -> None:
        self.config.upload_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_uploads()
        self.delivery_context.unlink(missing_ok=True)
        self.store.recover_deliveries()
        for task in self.store.uncollected_outputs():
            folder = Path(task['output_dir'])
            if folder.is_dir() and folder.resolve().is_relative_to(self.artifacts.outboxes.resolve()):
                await asyncio.to_thread(self.artifacts.collect, folder, task['chat_id'], task['id'])
            self.store.mark_files_collected(task['id'])
        prompt_state = self.store.codex_prompt_state()
        try:
            prompt = self.prompt_store.read()
        except PromptValidationError:
            LOG.error("Codex pre-instructions file is not valid UTF-8; disabling injection")
            prompt = ""
        if prompt_state["updated_at"] is None:
            if prompt:
                self.prompt_store.save(prompt)
            self.store.set_codex_prompt_state(
                enabled=bool(self.config.codex_prompt_enabled and prompt),
                updated_at=self.prompt_store.now() if prompt else 0,
                length=len(prompt),
                sha256=self.prompt_store.digest(prompt) if prompt else "",
            )
        self.bot = await self.telegram.get_me()
        await self.telegram.delete_webhook()
        self.store.ensure_chat(
            self.config.allowed_user_id,
            self.config.allowed_chat_id,
            ChatSettings(
                self.config.model,
                self.config.reasoning_effort,
                self.config.plan_reasoning_effort,
                True,
                "codex",
                self.config.claude_model,
                self.config.claude_effort,
                bool(self.config.claude_api_key),
                self.config.tavily_enabled,
            ),
        )
        if self.config_sync.public_key.exists() and not any(self.config_sync.status(engine).get('template_version') for engine in ('codex', 'claude')):
            try:
                await self.config_sync.sync()
                self.store.add_event('CONFIG_SYNC', 'Initial signed Agent config template synchronized')
            except Exception as exc:
                LOG.info('Initial Agent config template sync skipped: %s', type(exc).__name__)
        current_settings = self.store.chat_settings(self.config.allowed_chat_id)
        if not self.backups.list():
            try:
                await asyncio.to_thread(self.backups.create, 'initial-configuration')
            except Exception as exc:
                self.store.add_event('BACKUP_ERROR', self.sanitize(str(exc))[:300])
        if not self.tavily.api_key and current_settings.tavily_enabled:
            self.store.update_chat_settings(self.config.allowed_chat_id, tavily_enabled=False)
        interrupted = self.store.recover_interrupted()
        endpoint = self.credentials.get('codex')
        if endpoint and hasattr(self.runtime, 'configure'):
            await self.runtime.configure(endpoint)
        else:
            await self.runtime.start()
        await self.claude_runtime.start()
        for engine in ('codex', 'claude'):
            if not self.store.get_meta('endpoint_state:' + engine):
                self.store.set_meta('endpoint_state:' + engine, self.engine_status(engine)[0])
        self.store.set_meta('tavily_state', self.tavily_state(self.store.chat_settings(self.config.allowed_chat_id).tavily_enabled))
        self.health_server = await asyncio.start_server(
            self.health_request, "127.0.0.1", self.config.health_port
        )
        if interrupted:
            await self.telegram.send(
                self.config.allowed_chat_id,
                "上一次任务因 Gateway 或服务器重启而中断，没有自动重试。",
            )
        pending_update = self.store.get_meta('telegram_processing_update', '')
        if pending_update:
            self.store.set_meta('telegram_processing_update', '')
            await self.telegram.send(self.config.allowed_chat_id,
                '上次有一条 Telegram 消息处理期间 Gateway 重启。为避免重复执行，未自动重放；请用 /tasks、/files 核对，必要时重新发送。')
        self.store.add_event("START", f"Gateway v{self.config.version} started")

    def sanitize(self, text: str) -> str:
        result = safe_error(text, self.config.telegram_token)
        if self.config.telegram_proxy_url:
            result = result.replace(self.config.telegram_proxy_url, "[REDACTED]")
        api_key = os.getenv("CODEX_TG_API_KEY", "")
        if api_key:
            result = result.replace(api_key, "[REDACTED]")
        for secret in self.credentials.secrets():
            result = result.replace(secret, "[REDACTED]")
        result = result.replace(self.responses_adapter.token, '[REDACTED]')
        if getattr(self, 'delivery_token', None):
            result = result.replace(self.delivery_token, '[REDACTED]')
        return result

    @staticmethod
    def clarify_codex_error(message: str) -> str:
        lowered = message.lower()
        if "illegal short-input distillation" in lowered or "heartbeat probing" in lowered:
            return (
                "当前 Codex 中转站拒绝了短输入/探测请求。Gateway 没有定时 heartbeat 或 ping；"
                "请重试一次，如仍失败请使用 /endpoint codex 更换中转站。"
            )
        return message

    @staticmethod
    def mask_secret(secret: str | None) -> str:
        if not secret:
            return "未配置"
        return "*" * max(4, min(12, len(secret) - 4)) + secret[-4:]

    def redact_codex_prompt(self, text: str) -> str:
        # Relay errors can echo a request body; keep prompt contents out of logs.
        values = {self.responses_adapter.snapshot}
        try:
            values.add(self.prompt_store.read())
        except PromptValidationError:
            pass
        for value in values - {''}:
            for representation in (value, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1]):
                text = text.replace(representation, '[INSTRUCTIONS_REDACTED]')
        return text

    def codex_prompt_state(self) -> PromptState:
        stored = self.store.codex_prompt_state()
        try:
            value = self.prompt_store.read()
        except PromptValidationError:
            value = ""
        digest = self.prompt_store.digest(value) if value else ""
        if stored['updated_at'] is None or stored["sha256"] != digest or stored["length"] != len(value):
            if value:
                self.prompt_store.save(value)
            self.store.set_codex_prompt_state(
                enabled=bool((self.config.codex_prompt_enabled if stored['updated_at'] is None else stored["enabled"]) and value),
                updated_at=int(self.prompt_store.now()) if value else int(stored['updated_at'] or 0),
                length=len(value),
                sha256=digest,
            )
            stored = self.store.codex_prompt_state()
        return PromptState(
            enabled=bool(stored["enabled"] and value),
            configured=bool(value),
            length=len(value),
            sha256=digest,
            updated_at=int(stored["updated_at"]) if stored["updated_at"] else None,
            revision=digest,
        )

    def codex_prompt_revision(self) -> str:
        state = self.codex_prompt_state()
        return state.revision if state.enabled else ""

    def save_codex_prompt(self, value: str, *, enabled: bool | None = None) -> PromptState:
        value = validate_prompt(value)
        revision = self.prompt_store.save(value)
        next_enabled = bool(value) if enabled is None else bool(enabled and value)
        self.store.set_codex_prompt_state(
            enabled=next_enabled,
            updated_at=self.prompt_store.now(),
            length=len(value),
            sha256=revision,
        )
        self.prompt_store.prune(self.store.codex_prompt_references() | {revision})
        return self.codex_prompt_state()

    def set_codex_prompt_enabled(self, enabled: bool) -> PromptState:
        state = self.codex_prompt_state()
        self.store.set_codex_prompt_state(
            enabled=bool(enabled and state.configured),
            updated_at=self.prompt_store.now(),
            length=state.length,
            sha256=state.sha256,
        )
        return self.codex_prompt_state()

    def clear_codex_prompt(self) -> PromptState:
        return self.save_codex_prompt("", enabled=False)

    @staticmethod
    def format_prompt_time(updated_at: int | None) -> str:
        if not updated_at:
            return "从未"
        return datetime.fromtimestamp(updated_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    async def show_codex_prompt(self, chat_id: int, message_id: int | None = None) -> None:
        state = self.codex_prompt_state()
        text = (
            f"Codex 前置 Instructions：{'ON' if state.enabled else 'OFF'}\n"
            f"配置：{'已配置' if state.configured else '未配置'}\n"
            f"字符数：{state.length}\n"
            f"更新时间：{self.format_prompt_time(state.updated_at)}\n"
            "只作用于 Codex；内容会原样添加到 Codex 原始 instructions 前面。"
        )
        keyboard = [
            [{"text": "查看", "callback_data": "codexprompt:show"},
             {"text": "修改", "callback_data": "codexprompt:edit"}],
            [{"text": "开启", "callback_data": "codexprompt:on"},
             {"text": "关闭", "callback_data": "codexprompt:off"}],
            [{"text": "清空", "callback_data": "codexprompt:clear"}],
        ]
        if message_id is None:
            await self.telegram.send_keyboard(chat_id, text, keyboard)
        else:
            await self.telegram.edit_keyboard(chat_id, message_id, text, keyboard)

    async def show_codex_prompt_content(self, chat_id: int) -> None:
        state = self.codex_prompt_state()
        if not state.configured:
            await self.telegram.send(chat_id, "Codex 前置 Instructions 尚未配置。")
            return
        if state.length > 3200:
            await self.telegram.send_document(chat_id, self.prompt_store.path, "Codex 前置 Instructions")
            return
        await self.telegram.send(chat_id, "Codex 前置 Instructions 原文：\n" + self.prompt_store.read())

    async def request_codex_prompt(self, chat_id: int) -> None:
        self.pending_input = {"kind": "codex_prompt"}
        await self.telegram.send(chat_id, "请直接发送新的前置 Instructions，或发送 UTF-8 `.txt` 文件。\n发送 /cancel 可取消。")

    async def confirm_clear_codex_prompt(self, chat_id: int) -> None:
        token = uuid.uuid4().hex[:16]
        self.store.set_meta('codex_prompt_clear', json.dumps({'token': token, 'sha256': self.codex_prompt_state().sha256}))
        await self.telegram.send_keyboard(
            chat_id,
            "确定清空 Codex 前置 Instructions？清空后需重新输入才能恢复。",
            [[{"text": "确认清空", "callback_data": f"codexprompt:confirm:{token}"},
              {"text": "取消", "callback_data": "codexprompt:cancel"}]],
        )

    async def test_endpoint(self, engine: str, base_url: str, api_key: str) -> tuple[bool, str]:
        base_url, api_key = validate_url(base_url), validate_key(api_key)
        settings = self.store.chat_settings(self.config.allowed_chat_id)
        if engine == 'codex':
            adapter = ResponsesAdapter(self.prompt_store)
            candidate = CodexRuntime(self.config, adapter)
            candidate.endpoint_base_url, candidate.endpoint_api_key = base_url, api_key
            try:
                revision = self.codex_prompt_revision()
                adapter.set_snapshot(self.prompt_store.load_revision(revision) if revision else "")
                await candidate.start()
                result = await asyncio.wait_for(candidate.validate_model(settings.model, settings.reasoning_effort), 60)
                if result.accepted and result.verified:
                    return True, '模型调用成功'
                detail = self.clarify_codex_error(self.sanitize(result.message))
                return False, detail or '模型调用失败；请检查 Responses API、Key 和模型'
            finally:
                await candidate.close()
        import tempfile
        from .claude_runtime import ClaudeRuntime
        for auth in ('api_key', 'bearer'):
            with tempfile.TemporaryDirectory(prefix='claude-endpoint-') as directory:
                candidate = ClaudeRuntime(Path(directory), self.claude_runtime.command)
                try:
                    await candidate.start()
                    async def noop(_text):
                        pass
                    await asyncio.wait_for(candidate.run(str(uuid.uuid4()), 'Please reply with one concise natural-language sentence confirming that this API endpoint is reachable. Do not use tools.', [], noop,
                        settings.claude_model, settings.claude_effort,
                        {'base_url': base_url, 'api_key': api_key, 'auth': auth}, probe=True), 60)
                    self._tested_auth = auth
                    return True, 'Claude CLI 模型调用成功'
                except Exception as exc:
                    safe = str(exc).replace(api_key, '[REDACTED]')
                    if auth == 'api_key' and any(x in safe.lower() for x in ('401', '403', 'authentication', 'api key', 'unauthorized')):
                        continue
                    return False, 'Claude CLI 测试失败：' + self.sanitize(safe)[:400]
                finally:
                    await candidate.close()
        return False, 'Claude 鉴权失败'

    async def save_endpoint(self, engine: str, base_url: str, api_key: str) -> tuple[bool, str]:
        candidate_key = api_key
        try:
            base_url, api_key = validate_url(base_url), validate_key(api_key)
        except Exception as exc:
            return False, '配置格式无效，未保存：' + self.sanitize(str(exc).replace(candidate_key, '[REDACTED]'))[:400]
        auth = self.credentials.get(engine).get('auth', 'api_key')
        ref = self.credentials.save(engine, base_url, api_key, auth)
        self.engine_endpoints[engine] = self.credentials.get(engine, ref)
        self.models.invalidate()
        self.model_menus.clear()
        self.store.set_meta('endpoint_state:' + engine, 'UNVERIFIED')
        self.store.update_chat_settings(self.config.allowed_chat_id,
            claude_verified=False if engine == 'claude' else None,
            model_verified=False if engine == 'codex' else None)
        self._tested_auth = auth
        try:
            ok, detail = await self.test_endpoint(engine, base_url, api_key)
        except Exception as exc:
            ok, detail = False, self.sanitize(str(exc))[:400] or type(exc).__name__
        detail = self.sanitize(str(detail))
        # An explicit change is persisted regardless of model availability.
        # Never overwrite a newer configuration with a late test result.
        if self.credentials.current_ref(engine) == ref:
            if ok and engine == 'claude' and self._tested_auth != auth:
                ref = self.credentials.save(engine, base_url, api_key, self._tested_auth)
                self.engine_endpoints[engine] = self.credentials.get(engine, ref)
            self.store.set_meta('endpoint_state:' + engine, 'READY' if ok else 'ERROR')
            self.store.update_chat_settings(self.config.allowed_chat_id,
                claude_verified=ok if engine == 'claude' else None,
                model_verified=ok if engine == 'codex' else None)
        if ok:
            return True, 'Endpoint / Key 已保存；测试成功：' + detail
        return True, 'Endpoint / Key 已保存；测试失败：' + detail + '\n后续新任务使用新配置，没有恢复旧 Key。可调整 /model 后再测试。'

    def engine_status(self, engine: str) -> tuple[str, str | None, str | None]:
        endpoint = self.engine_endpoints[engine]
        configured = bool(endpoint.get('base_url') and endpoint.get('api_key'))
        state = self.store.get_meta('endpoint_state:' + engine, 'READY') if configured else 'UNCONFIGURED'
        if configured and not (self.runtime if engine == 'codex' else self.claude_runtime).ready:
            state = 'ERROR'
        return state, endpoint.get('base_url'), endpoint.get('api_key')

    def search_config(self, ref: str) -> dict | None:
        if not ref:
            return None
        import sys
        return {'command': sys.executable, 'args': ['-m', 'codex_tg.search', str(self.credentials.path), ref],
                'env': {'PYTHONPATH': str(Path(__file__).resolve().parents[1])}}

    def tavily_state(self, enabled: bool) -> str:
        if not enabled:
            return 'DISABLED'
        if not self.tavily.api_key:
            return 'UNCONFIGURED'
        try:
            state = json.loads((self.credentials.path.parent / 'search-status.json').read_text())
            if state['revision'] == self.credentials.current_ref('tavily'):
                return state['state']
        except (OSError, ValueError, KeyError):
            pass
        return 'READY'

    def cleanup_uploads(self, now: float | None = None) -> int:
        cutoff = (now or time.time()) - self.config.upload_retention_days * 86400
        removed = 0
        if not self.config.upload_dir.exists():
            return removed
        with self.store.connect() as conn:
            protected = {str(Path(item['path']).resolve()) for row in conn.execute(
                "SELECT attachments_json FROM tasks WHERE status IN ('QUEUED','RUNNING','STOPPING')")
                for item in json.loads(row[0]) if item.get('path')}
        paths = sorted(self.config.upload_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True)
        for path in paths:
            try:
                if path.is_symlink() or path.is_file():
                    if str(path.resolve()) in protected:
                        continue
                    if path.stat(follow_symlinks=False).st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                elif path.is_dir():
                    path.rmdir()
            except (FileNotFoundError, OSError):
                continue
        if removed:
            self.store.add_event("CLEANUP", f"Removed {removed} expired upload(s)")
        return removed

    async def provision_claude(self) -> None:
        if self.claude_runtime.ready:
            return
        release = Path(__file__).resolve().parents[1]
        if not (release / 'CLAUDE.json').exists():
            return
        try:
            from .native import install_bundled
            self.store.add_event('CLAUDE_INSTALL', 'Installing verified Claude CLI for Gateway migration')
            await asyncio.to_thread(install_bundled, release, self.config.release_base_url,
                                    self.config.database_path.parent.parent / 'claude')
            await self.claude_runtime.start()
            await self.telegram.send(self.config.allowed_chat_id, 'Claude Code CLI 已安装；/endpoint claude 可配置。')
        except Exception as exc:
            self.store.add_event('CLAUDE_INSTALL_ERROR', self.sanitize(str(exc))[:300])
            await self.telegram.send(self.config.allowed_chat_id, 'Claude CLI 安装失败；可使用 /updateclaude 重试。')

    async def maintenance(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=6 * 60 * 60)
            except asyncio.TimeoutError:
                self.cleanup_uploads()
                result = self.store.prune_history()
                if result.get('events') or result.get('tasks') or result.get('inbound'):
                    self.store.add_event('CLEANUP', f"Pruned events={result.get('events',0)} tasks={result.get('tasks',0)} inbound={result.get('inbound',0)}")

    def update_status(self) -> dict[str, Any] | None:
        path = self.config.database_path.parent / "update-status.json"
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    async def update_status_monitor(self) -> None:
        while not self.stop_event.is_set():
            status = self.update_status()
            if status and status.get("state") in {"SUCCESS", "FAILED"}:
                update_id = str(status.get("id") or "")
                announced = self.store.get_meta("announced_update_id", "")
                if update_id and update_id != announced:
                    if not status.get("notified"):
                        await self.telegram.send(
                            self.config.allowed_chat_id,
                            self.sanitize(str(status.get("detail") or "更新任务已结束"))[:1500],
                        )
                    self.store.set_meta("announced_update_id", update_id)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        if self.health_server:
            self.health_server.close()
            await self.health_server.wait_closed()
        await self.runtime.close()
        await self.responses_adapter.close()
        await self.claude_runtime.close()
        await self.telegram.close()

    async def health_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            telegram_network = await self.telegram.network_status()
            payload = json.dumps(
                {
                    "gateway": "ok" if self.runtime.ready and self.store.healthy() else "error",
                    "db": self.store.healthy(),
                    "codex_runtime": self.runtime.ready,
                    "codex_adapter": self.responses_adapter.active,
                    "queue": self.store.queue_count(),
                    "version": self.config.version,
                    "telegram_network": telegram_network["network"],
                    "telegram_proxy": telegram_network["proxy"],
                },
                separators=(",", ":"),
            ).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nContent-Length: "
                + str(len(payload)).encode("ascii")
                + b"\r\nConnection: close\r\n\r\n"
                + payload
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def save_attachment(self, message: dict[str, Any]) -> list[dict[str, str]]:
        source: dict[str, Any] | None = None
        kind = "document"
        filename = "document.bin"
        if message.get("photo"):
            source = message["photo"][-1]
            kind = "image"
            filename = "photo.jpg"
        elif message.get("document"):
            source = message["document"]
            filename = clean_filename(str(source.get("file_name") or filename))
            mime = str(source.get("mime_type") or "")
            if mime.startswith("image/"):
                kind = "image"
        elif message.get('video'):
            source = message['video']
            kind = 'document'
            filename = clean_filename(str(source.get('file_name') or 'video.mp4'))
        elif message.get('audio'):
            source = message['audio']
            kind = 'document'
            filename = clean_filename(str(source.get('file_name') or 'audio.mp3'))
        if source is None:
            return []
        declared = int(source.get("file_size") or 0)
        if declared and declared > self.config.max_upload_bytes:
            raise TelegramError(
                f"文件超过限制（最大 {self.config.max_upload_bytes // 1024 // 1024} MiB）"
            )
        suffix = Path(filename).suffix[:16]
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        directory = self.config.upload_dir / str(self.config.allowed_chat_id) / date
        target = directory / f"{uuid.uuid4().hex}{suffix}"
        await self.telegram.download(source["file_id"], target, self.config.max_upload_bytes)
        self.store.add_event("UPLOAD", f"Saved {kind} upload: {target}")
        return [{"path": str(target), "kind": kind, "name": filename}]

    async def submit_batch_task(self, text: str, attachments: list[dict[str, str]], batch_id: int | None = None) -> int | None:
        chat_id = self.config.allowed_chat_id
        settings = self.store.chat_settings(chat_id)
        engine = settings.active_engine
        endpoint = self.engine_endpoints[engine]
        if not endpoint.get('base_url') or not endpoint.get('api_key'):
            return None
        try:
            cwd = validate_directory(self.store.workdir(chat_id, engine))
            thread_id = self.ensure_claude_conversation(chat_id) if engine == 'claude' else await self.ensure_current_thread(chat_id)
        except (ValueError, OSError, RuntimeError):
            return None
        task_id, _, _ = self.store.enqueue(self.config.allowed_user_id, chat_id, thread_id, text, attachments, settings,
            engine=engine, session_id=thread_id if engine == 'claude' else self.store.engine_session(chat_id, engine),
            endpoint_ref=self.credentials.current_ref(engine),
            search_ref=self.credentials.current_ref('tavily') if settings.tavily_enabled else '',
            codex_prompt_ref=self.codex_prompt_revision() if engine == 'codex' else '', cwd=cwd, kind='batch', batch_id=batch_id, source_ref=f'batch:{batch_id}' if batch_id else None)
        self.store.add_event('BATCH_QUEUED', 'Multi-file batch queued', task_id)
        return task_id

    async def ensure_current_thread(self, chat_id: int) -> str:
        settings = self.store.chat_settings(chat_id)
        if not self.store.active():
            self.runtime.workdir = validate_directory(self.store.workdir(chat_id, 'codex'))
        thread_id = self.store.thread_id(chat_id)
        if thread_id:
            if self.store.has_thread_counter(thread_id):
                return thread_id
            thread = await self.runtime.get_thread(
                thread_id,
                settings.model,
                settings.plan_reasoning_effort,
                apply_settings=True,
            )
            baseline = (
                0 if thread.id != thread_id else await self.runtime.thread_turn_count(thread)
            )
            self.store.register_thread(chat_id, thread.id, baseline)
            if thread.id != thread_id:
                self.store.set_thread(chat_id, thread.id)
                self.store.add_event(
                    "THREAD",
                    f"Replaced unavailable empty thread {thread_id} with {thread.id}",
                )
            return thread.id
        thread = await self.runtime.new_thread(
            settings.model, settings.plan_reasoning_effort
        )
        self.store.register_thread(chat_id, thread.id, 0)
        self.store.set_thread(chat_id, thread.id)
        self.store.add_event("THREAD", f"New thread {thread.id}")
        return thread.id

    def ensure_claude_conversation(self, chat_id: int) -> str:
        key = self.store.claude_conversation_key(chat_id)
        if not key:
            key = str(uuid.uuid4())
            self.store.set_claude_conversation_key(chat_id, key)
            self.store.set_engine_session(chat_id, "claude", key)
        return key

    async def handle_pending_input(self, message: dict[str, Any], text: str, chat_id: int) -> bool:
        pending = self.pending_input
        if not pending:
            return False
        message_id = int(message.get("message_id") or 0)
        if pending["kind"] == "codex_prompt":
            if message_id:
                self.mark_secret_message(message_id)
            if text == "/cancel":
                self.pending_input = None
                await self.telegram.send(chat_id, "已取消 Codex 前置 Instructions 修改。")
                return True
            if message.get("document"):
                document = message["document"]
                name = clean_filename(str(document.get("file_name") or ""))
                if not name.lower().endswith(".txt"):
                    await self.telegram.send(chat_id, "只接受 UTF-8 .txt 文件。")
                    return True
                declared = int(document.get("file_size") or 0)
                if declared > MAX_BYTES:
                    await self.telegram.send(chat_id, f"TXT 文件不能超过 {MAX_BYTES // 1024} KiB。")
                    return True
                try:
                    with tempfile.TemporaryDirectory(prefix='.codexprompt-', dir=self.config.database_path.parent) as folder:
                        path = Path(folder) / 'instructions.txt'
                        await self.telegram.download(document['file_id'], path, MAX_BYTES)
                        raw = path.read_bytes()
                        if len(raw) > MAX_BYTES:
                            raise PromptValidationError('TXT 文件不能超过 128 KiB')
                        value = raw.decode('utf-8')
                        if value in {'', '\ufeff'}:
                            raise PromptValidationError('TXT 文件为空，已有内容未修改；清空请使用 /codexprompt clear')
                        self.save_codex_prompt(value, enabled=True)
                except (OSError, UnicodeError, PromptValidationError, TelegramError, IndexError) as exc:
                    detail = str(exc) if isinstance(exc, PromptValidationError) else '下载失败或不是有效 UTF-8 文本，请重新上传。'
                    await self.telegram.send(chat_id, f"TXT 读取失败：{detail}")
                    return True
                self.pending_input = None
                if message_id:
                    self.mark_secret_message(message_id)
                    await self.telegram.delete_message(chat_id, message_id)
                await self.telegram.send(chat_id, "✓ Codex 前置 Instructions 已从 TXT 保存并开启。")
                return True
            if text == "":
                await self.telegram.send(chat_id, "内容为空；如需清空请使用 /codexprompt clear。")
                return True
            try:
                self.save_codex_prompt(text, enabled=True)
            except PromptValidationError as exc:
                await self.telegram.send(chat_id, self.sanitize(str(exc)))
                return True
            self.pending_input = None
            if message_id:
                self.mark_secret_message(message_id)
            await self.telegram.send(chat_id, "✓ Codex 前置 Instructions 已保存并开启。")
            return True
        if not text or text.startswith("/"):
            return False
        if pending["kind"] == "endpoint_url":
            try:
                text = validate_url(text)
            except ValueError:
                await self.telegram.send(chat_id, "URL 格式无效，请重新发送不含凭据的 Base URL。")
                return True
            self.pending_input = {"kind": "endpoint_key", "engine": pending["engine"], "url": text.rstrip("/")}
            await self.telegram.send(chat_id, "请发送 API Key（消息将立即尝试删除）。")
            return True
        if pending["kind"] == "endpoint_key":
            engine = pending["engine"]
            url = pending["url"]
            if message_id:
                self.mark_secret_message(message_id)
                await self.telegram.delete_message(chat_id, message_id)
            self.pending_input = None
            ok, detail = await self.save_endpoint(engine, url, text.strip())
            await self.telegram.send(chat_id, ("✓ " if ok else "✗ ") + detail)
            return True
        if pending["kind"] == "tavily_key":
            if message_id:
                self.mark_secret_message(message_id)
                await self.telegram.delete_message(chat_id, message_id)
            self.pending_input = None
            candidate = text.strip()
            if not candidate:
                await self.telegram.send(chat_id, "Tavily Key 为空，未修改。")
                return True
            try:
                candidate = validate_key(candidate)
            except ValueError:
                await self.telegram.send(chat_id, 'Tavily Key 格式无效。')
                return True
            tester = TavilyClient(candidate)
            result = await tester.search("Tavily API connectivity test")
            if result.get('native_search_fallback'):
                await self.telegram.send(chat_id, "✗ Tavily 测试失败，Key 未修改。")
                return True
            self.tavily.api_key = candidate
            self.credentials.save('tavily', '', candidate)
            self.store.update_chat_settings(chat_id, tavily_enabled=True)
            await self.telegram.send(chat_id, f"✓ Tavily 已保存，Key：{self.mask_secret(candidate)}")
            return True
        return False

    async def handle_message(self, message: dict[str, Any]) -> None:
        if not self.authorized(message):
            sender = (message.get("from") or {}).get("id", "unknown")
            chat = (message.get("chat") or {}).get("id", "unknown")
            LOG.warning("unauthorized Telegram message user=%s chat=%s", sender, chat)
            return
        chat_id = self.config.allowed_chat_id
        raw_text = str(message.get("text") or message.get("caption") or "")
        text = raw_text.strip()
        message_id = int(message.get("message_id") or 0)
        if message_id and message_id == int(self.store.get_meta("last_secret_message_id", "0")):
            return
        if await self.handle_pending_input(message, raw_text if self.pending_input and self.pending_input.get("kind") == "codex_prompt" else text, chat_id):
            return
        if PROBABLE_SECRET_RE.fullmatch(text):
            self.mark_secret_message(message_id)
            await self.telegram.delete_message(chat_id, message_id)
            await self.telegram.send(chat_id, '已阻止将其发送给 Agent；请通过 /endpoint 或 /tavilykey 配置。')
            return
        if await self.batches.handle(message):
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        if command:
            try:
                await self.handle_command(command, text, chat_id, int(message.get("message_id") or 0))
            except Exception as exc:
                await self.telegram.send(
                    chat_id, f"设置或命令执行失败：{self.sanitize(str(exc))}"
                )
            return
        try:
            attachments = await self.save_attachment(message)
        except Exception as exc:
            await self.telegram.send(chat_id, f"文件接收失败：{self.sanitize(str(exc))}")
            return
        if not text and attachments:
            text = "请检查我上传的文件，并按内容完成需要的操作。"
        if not text:
            await self.telegram.send(chat_id, "请发送任务文字、图片或文件。")
            return
        # A secret sent outside an explicit configuration flow must never be
        # interpreted as a user task.  This also protects against a pending
        # wizard being lost during a restart or an unrelated callback race.
        if PROBABLE_SECRET_RE.fullmatch(text):
            message_id = int(message.get("message_id") or 0)
            if message_id:
                self.mark_secret_message(message_id)
                await self.telegram.delete_message(chat_id, message_id)
            await self.telegram.send(
                chat_id,
                "检测到疑似 API Key，已阻止将其发送给 Agent。请使用 /tavilykey 或 /endpoint 配置；消息已尝试删除。",
            )
            return
        settings = self.store.chat_settings(chat_id)
        engine = settings.active_engine
        try:
            cwd = validate_directory(self.store.workdir(chat_id, engine))
        except (ValueError, OSError):
            await self.telegram.send(chat_id, '当前引擎工作目录已不存在或不可访问，请先 /cd /有效目录。')
            return
        endpoint = self.engine_endpoints[engine]
        if not endpoint.get("api_key") or not endpoint.get("base_url"):
            self.pending_input = {"kind": "endpoint_url", "engine": engine}
            await self.telegram.send(chat_id, f"{engine.title()} 尚未配置。请先发送 Base URL，再发送 API Key。完成后重新发送任务。")
            return
        try:
            thread_id = (
                self.ensure_claude_conversation(chat_id)
                if engine == "claude"
                else await self.ensure_current_thread(chat_id)
            )
        except Exception as exc:
            await self.telegram.send(chat_id, f"无法打开 {engine.title()} 会话：{self.sanitize(str(exc))}")
            return
        active_before = self.store.active() is not None
        task_id, position, thread_number = self.store.enqueue(
            self.config.allowed_user_id,
            chat_id,
            thread_id,
            raw_text if raw_text.strip() else text,
            attachments,
            settings,
            engine=engine,
            session_id=thread_id if engine == 'claude' else self.store.engine_session(chat_id, engine),
            endpoint_ref=self.credentials.current_ref(engine),
            search_ref=self.credentials.current_ref('tavily') if settings.tavily_enabled else '',
            codex_prompt_ref=self.codex_prompt_revision() if engine == 'codex' else '',
            cwd=cwd,
            source_ref=f"telegram:{chat_id}:{message.get('_inbox_update_id')}" if message.get('_inbox_update_id') else None,
        )
        self.store.add_event("QUEUED", "Task queued", task_id)
        if active_before or position > 1:
            await self.telegram.send(
                chat_id,
                f"当前 Agent 正在执行任务；{engine.title()} 任务 #{thread_number} 已加入队列，当前排队第 {position} 位。",
            )
        else:
            await self.telegram.send(chat_id, f"任务 #{thread_number} 已接收。")
        self.worker_event.set()

    async def show_thread_selector(
        self, chat_id: int, page_number: int = 0, message_id: int | None = None
    ) -> None:
        engine = self.store.active_engine(chat_id)
        if engine == 'claude':
            page = await self.claude_runtime.list_sessions(page_number)
            current = self.store.engine_session(chat_id, 'claude')
        else:
            page = await self.runtime.list_threads(page_number)
            current = self.store.thread_id(chat_id)
        prefix = 'claude' if engine == 'claude' else 'thread'
        if not page.threads:
            text = f"没有可恢复的 {engine.title()} 历史会话。"
            keyboard: list[list[dict[str, str]]] = []
        else:
            text = f"选择历史 {engine.title()} 会话（第 {page.page + 1} 页）"
            keyboard = []
            for item in page.threads:
                marker = "✓ " if item.id == current else ""
                title = " ".join(self.sanitize(item.title).split())[:42] or "未命名会话"
                date = datetime.fromtimestamp(item.updated_at, timezone.utc).strftime("%m-%d")
                keyboard.append(
                    [
                        {
                            "text": f"{marker}{date} · {title}"[:64],
                            "callback_data": f"{prefix}:select:{item.id}",
                        }
                    ]
                )
            navigation: list[dict[str, str]] = []
            if page.has_previous:
                navigation.append(
                    {"text": "上一页", "callback_data": f"{prefix}:page:{page.page - 1}"}
                )
            if page.has_next:
                navigation.append(
                    {"text": "下一页", "callback_data": f"{prefix}:page:{page.page + 1}"}
                )
            if navigation:
                keyboard.append(navigation)
        if message_id is None:
            await self.telegram.send_keyboard(chat_id, text, keyboard)
        else:
            await self.telegram.edit_keyboard(chat_id, message_id, text, keyboard)

    @staticmethod
    def _button_rows(buttons: list[dict[str, str]], columns: int = 2) -> list[list[dict[str, str]]]:
        return [buttons[index : index + columns] for index in range(0, len(buttons), columns)]

    async def show_engine_selector(
        self, chat_id: int, message_id: int | None = None
    ) -> None:
        settings = self.store.chat_settings(chat_id)
        keyboard = [[
            {"text": f"{'✓ ' if settings.active_engine == 'codex' else ''}Codex", "callback_data": "engine:codex"},
            {"text": f"{'✓ ' if settings.active_engine == 'claude' else ''}Claude Code", "callback_data": "engine:claude"},
        ]]
        text = f"当前引擎：{settings.active_engine.title()}\n选择后会继续该引擎的独立会话。"
        if message_id is None:
            await self.telegram.send_keyboard(chat_id, text, keyboard)
        else:
            await self.telegram.edit_keyboard(chat_id, message_id, text, keyboard)

    @staticmethod
    def settings_keyboard() -> list[list[dict[str, str]]]:
        return [
            [{"text": "文件", "callback_data": "ops:files"}, {"text": "任务", "callback_data": "ops:tasks"}],
            [{"text": "工作目录", "callback_data": "ops:pwd"}, {"text": "诊断", "callback_data": "ops:diagnose"}],
            [{"text": "切换引擎", "callback_data": "menu:engine"},
             {"text": "模型", "callback_data": "menu:model"}],
            [{"text": "Reasoning / Effort", "callback_data": "menu:reasoning"},
             {"text": "中转站", "callback_data": "menu:endpoint"}],
            [{"text": "Tavily 搜索", "callback_data": "menu:tavily"},
             {"text": "Codex Instructions", "callback_data": "menu:codexprompt"}],
            [{"text": "历史会话", "callback_data": "menu:sessions"},
             {"text": "新建当前会话", "callback_data": "menu:new"}],
            [{"text": "更新组件", "callback_data": "menu:update"}],
        ]

    async def show_endpoint_selector(self, chat_id: int) -> None:
        await self.telegram.send_keyboard(
            chat_id,
            "中转站设置\n选择要配置或测试的引擎。",
            [[
                {"text": "Codex", "callback_data": "menu:endpoint:codex"},
                {"text": "Claude Code", "callback_data": "menu:endpoint:claude"},
            ]],
        )

    async def show_endpoint_actions(self, chat_id: int, engine: str) -> None:
        current = self.engine_endpoints[engine]
        state, _, _ = self.engine_status(engine)
        await self.telegram.send_keyboard(
            chat_id,
            f"{engine.title()} Endpoint：{current.get('base_url') or '未配置'}\n状态：{state}\nKey：{self.mask_secret(current.get('api_key'))}",
            [[
                {"text": "配置 URL + Key", "callback_data": f"endpoint:configure:{engine}"},
                {"text": "修改 Key", "callback_data": f"endpoint:key:{engine}"},
            ], [{"text": "测试连接", "callback_data": f"endpoint:test:{engine}"}]],
        )

    async def show_tavily_menu(self, chat_id: int) -> None:
        settings = self.store.chat_settings(chat_id)
        state = self.tavily_state(settings.tavily_enabled)
        await self.telegram.send_keyboard(
            chat_id,
            f"Tavily：{'ON' if settings.tavily_enabled else 'OFF'} / {state}\n"
            f"Key：{self.mask_secret(self.tavily.api_key)}\n"
            "策略：Tavily First → Native Search Fallback",
            [[
                {"text": "开启", "callback_data": "tavily:on"},
                {"text": "关闭", "callback_data": "tavily:off"},
            ], [{"text": "设置 Key", "callback_data": "tavily:key"}]],
        )

    async def show_update_menu(self, chat_id: int) -> None:
        await self.telegram.send_keyboard(
            chat_id,
            "更新中心\n更新会交给独立 updater，并保留失败回滚。",
            [[
                {"text": "Gateway", "callback_data": "update:normal"},
                {"text": "Gateway 强制重装", "callback_data": "update:force"},
            ], [{"text": "Codex CLI / SDK", "callback_data": "update:codex"},
                {"text": "Claude Code CLI", "callback_data": "update:claude"}]],
        )

    async def show_model_selector(self, chat_id: int, message_id: int | None = None) -> None:
        settings = self.store.chat_settings(chat_id)
        engine = settings.active_engine
        endpoint = dict(self.engine_endpoints[engine])
        current = settings.claude_model if engine == 'claude' else settings.model
        token = uuid.uuid4().hex[:16]
        try:
            options = await self.models.options(engine, endpoint, refresh=True)
            detail = f"中转站返回 {len(options)} 个模型。" if options else "中转站返回的模型列表为空。"
        except (ModelDiscoveryError, ValueError) as exc:
            options = []
            detail = f"无法自动获取模型列表：{self.sanitize(str(exc))}"
        options = sorted(options, key=lambda item: item.model != current)
        # Opening a fresh catalog retires older snapshots for this engine,
        # including after an empty/failed response from the same provider.
        self.model_menus = {key: menu for key, menu in self.model_menus.items() if menu['engine'] != engine}
        self.model_menus[token] = {
            'engine': engine, 'identity': self.models.identity(engine, endpoint),
            'models': options, 'detail': detail, 'created': time.monotonic(),
        }
        await self.render_model_menu(chat_id, token, 0, message_id)

    async def render_model_menu(self, chat_id: int, token: str, page: int,
                                message_id: int | None = None) -> None:
        menu = self.model_menus[token]
        engine = menu['engine']
        settings = self.store.chat_settings(chat_id)
        current = settings.claude_model if engine == 'claude' else settings.model
        options = menu['models']
        page = max(0, min(page, max(0, (len(options) - 1) // 8)))
        start = page * 8
        buttons = [
            {'text': f"{'✓ ' if item.model == current else ''}{self.sanitize(item.display_name)}"[:64],
             'callback_data': f'models:{token}:select:{index}'}
            for index, item in enumerate(options[start:start + 8], start)
        ]
        keyboard = self._button_rows(buttons)
        nav = []
        if page:
            nav.append({'text': '上一页', 'callback_data': f'models:{token}:page:{page - 1}'})
        if start + 8 < len(options):
            nav.append({'text': '下一页', 'callback_data': f'models:{token}:page:{page + 1}'})
        if nav:
            keyboard.append(nav)
        keyboard.append([{'text': '重新检测', 'callback_data': f'models:{token}:refresh:0'},
                         {'text': '手动输入模型名', 'callback_data': f'models:{token}:custom:0'}])
        text = (f"{engine.title()} 当前模型：{current}\n{menu['detail']}\n"
                f"第 {page + 1} 页 · 选择模型，或发送 /model <model_name> 手动设置。")
        if message_id is None:
            await self.telegram.send_keyboard(chat_id, text, keyboard)
        else:
            await self.telegram.edit_keyboard(chat_id, message_id, text, keyboard)

    async def handle_model_callback(self, chat_id: int, message_id: int, callback_id: str, data: str) -> None:
        parts = data.split(':')
        if len(parts) != 4 or parts[2] not in {'select', 'page', 'refresh', 'custom'}:
            await self.telegram.answer_callback(callback_id, '模型按钮无效，请重新 /model。', True)
            return
        _, token, action, value = parts
        menu = self.model_menus.get(token)
        engine = self.store.active_engine(chat_id)
        if (not menu or menu['engine'] != engine or time.monotonic() - menu['created'] > 300
                or menu['identity'] != self.models.identity(engine, self.engine_endpoints[engine])):
            await self.telegram.answer_callback(callback_id, '模型列表已过期或引擎/中转站已改变，请重新 /model。', True)
            return
        if action == 'custom':
            await self.telegram.answer_callback(callback_id, '请发送 /model <model_name> 手动设置模型。', True)
            return
        if not value.isdigit() or (action == 'select' and int(value) >= len(menu['models'])):
            await self.telegram.answer_callback(callback_id, '模型按钮无效，请重新 /model。', True)
            return
        await self.telegram.answer_callback(callback_id)
        if action == 'refresh':
            self.model_menus.pop(token, None)
            await self.show_model_selector(chat_id, message_id)
        elif action == 'page':
            await self.render_model_menu(chat_id, token, int(value), message_id)
        else:
            await self.set_model(chat_id, menu['models'][int(value)].model)

    async def show_reasoning_selector(self, chat_id: int, *, plan: bool = False) -> None:
        settings = self.store.chat_settings(chat_id)
        if settings.active_engine == "claude":
            current = settings.claude_effort
            buttons = [
                {"text": f"{'✓ ' if effort == current else ''}{effort}",
                 "callback_data": f"setting:reasoning:{effort}"}
                for effort in ("low", "medium", "high", "xhigh", "max")
            ]
            await self.telegram.send_keyboard(
                chat_id,
                f"当前 Claude Effort：{current}\nClaude 的 /planreasoning 与 Effort 共用。",
                self._button_rows(buttons, 3),
            )
            return
        current = settings.plan_reasoning_effort if plan else settings.reasoning_effort
        try:
            efforts = await self.runtime.supported_efforts(settings.model)
        except Exception as exc:
            await self.telegram.send(
                chat_id,
                f"当前{' Plan' if plan else ''} Reasoning Effort：{current}\n"
                f"模型能力目录暂时不可用：{self.sanitize(str(exc))}\n"
                f"可直接 /{'planreasoning' if plan else 'reasoning'} <等级> 保存自定义值。",
            )
            return
        if not efforts:
            await self.telegram.send(
                chat_id,
                f"当前模型：{settings.model}\n当前{' Plan' if plan else ''} Reasoning Effort：{current}\n"
                "该自定义模型没有 reasoning 能力目录。\n"
                f"可直接 /{'planreasoning' if plan else 'reasoning'} <等级> 保存自定义值。",
            )
            return
        kind = "plan" if plan else "reasoning"
        buttons = [
            {
                "text": f"{'✓ ' if effort == current else ''}{effort}",
                "callback_data": f"setting:{kind}:{effort}",
            }
            for effort in efforts
        ]
        await self.telegram.send_keyboard(
            chat_id,
            f"当前模型：{settings.model}\n当前{' Plan' if plan else ''} Reasoning Effort：{current}\n"
            f"实际可选项：{', '.join(efforts)}",
            self._button_rows(buttons, 3),
        )

    def _effective_notice(self) -> str:
        if self.store.active():
            return "设置已保存，将从下一条任务开始生效。当前任务不受影响。"
        if self.store.queue_count():
            return "设置已保存，将从下一条新提交的任务开始生效；已排队任务保持原设置。"
        return "设置已保存，将从下一条任务开始生效。"

    async def set_model(self, chat_id: int, model: str) -> None:
        model = model.strip()
        if not MODEL_NAME_RE.fullmatch(model):
            await self.telegram.send(chat_id, "模型名称格式无效，设置未修改。")
            return
        current = self.store.chat_settings(chat_id)
        if current.active_engine == "claude":
            self.store.update_chat_settings(chat_id, claude_model=model, claude_verified=False)
            await self.telegram.send(chat_id, f"Claude 模型已设置为：{model}\n{self._effective_notice()}")
            return
        try:
            option = await self.runtime.model_option(model)
        except Exception:
            option = None
        reasoning = current.reasoning_effort
        plan_reasoning = current.plan_reasoning_effort
        adjustments: list[str] = []
        if option:
            if reasoning not in option.efforts:
                reasoning = option.default_effort
                adjustments.append(f"Reasoning 已调整为 {reasoning}")
            if plan_reasoning not in option.efforts:
                plan_reasoning = option.default_effort
                adjustments.append(f"Plan Reasoning 已调整为 {plan_reasoning}")
        if self.store.active():
            validation = None
        else:
            revision = self.codex_prompt_revision()
            prompt = self.prompt_store.load_revision(revision) if revision else ''
            validation = await self.runtime.validate_model(model, reasoning, endpoint=self.engine_endpoints['codex'],
                                                          pre_instructions=prompt)
            if not validation.accepted:
                try:
                    available = ", ".join(
                        item.model for item in await self.models.options('codex', self.engine_endpoints['codex'])
                    )
                except Exception:
                    available = "模型目录暂时不可用"
                await self.telegram.send(
                    chat_id,
                    f"模型不可用，设置未修改：{self.sanitize(validation.message)}\n可选模型：{available}",
                )
                return
        verified = bool(validation and validation.verified)
        self.store.update_chat_settings(
            chat_id,
            model=model,
            reasoning_effort=reasoning,
            plan_reasoning_effort=plan_reasoning,
            model_verified=verified,
        )
        lines = [f"模型已设置为：{model}"]
        if adjustments:
            lines.extend(adjustments)
        if verified:
            lines.append("模型已通过当前 provider 最小调用验证。")
        else:
            detail = self.sanitize(validation.message) if validation and validation.message else "当前任务运行中，未执行并行验证"
            lines.append(f"模型配置已保存，但尚未验证可用：{detail}")
        lines.append(self._effective_notice())
        await self.telegram.send(chat_id, "\n".join(lines))

    async def set_reasoning(self, chat_id: int, effort: str, *, plan: bool = False) -> None:
        effort = effort.strip().lower()
        if not EFFORT_RE.fullmatch(effort):
            await self.telegram.send(chat_id, "Reasoning Effort 格式无效，设置未修改。")
            return
        settings = self.store.chat_settings(chat_id)
        if settings.active_engine == "claude":
            self.store.update_chat_settings(chat_id, claude_effort=effort)
            await self.telegram.send(chat_id, f"Claude Effort 已设置为：{effort}\n{self._effective_notice()}")
            return
        if plan:
            self.store.update_chat_settings(chat_id, plan_reasoning_effort=effort)
            label = "Plan Reasoning Effort"
        else:
            self.store.update_chat_settings(chat_id, reasoning_effort=effort)
            label = "Reasoning Effort"
        await self.telegram.send(
            chat_id, f"{label} 已设置为：{effort}\n{self._effective_notice()}"
        )

    async def codex_versions(self) -> tuple[str, str]:
        from .components import codex_components
        try:
            info = await asyncio.to_thread(codex_components)
            return f"{info['cli_version']}\nGateway Runtime：{info['runtime_version']}\nRuntime 路径：{info['runtime_path']}", info['sdk']
        except Exception as exc:
            return '组件检查失败：' + type(exc).__name__, '未验证'

    def context_status(self, engine: str) -> str:
        if engine == 'codex':
            usage = getattr(self.runtime, 'context_usage', {'used': None, 'window': None, 'total': None})
            if usage.get('used') is None or usage.get('window') is None:
                return '未知（等待 SDK TokenUsage 事件）'
            return f"{usage['used']} / {usage['window']} tokens"
        return '未知（Claude headless CLI 未提供可靠 TokenUsage）'

    async def perform_compact(self, chat_id: int) -> None:
        settings = self.store.chat_settings(chat_id)
        engine = settings.active_engine
        try:
            if engine == 'codex':
                thread_id = self.store.thread_id(chat_id)
                if not thread_id:
                    await self.telegram.send(chat_id, '当前还没有 Codex Thread，无法压缩。')
                    return
                result = await self.runtime.compact_thread(thread_id, settings.model, settings.plan_reasoning_effort)
                if result['confirmed']:
                    await self.telegram.send(chat_id, 'Codex 原生上下文压缩已完成。\n'
                        f"压缩前：{self._usage_text(result['before'])}\n压缩后：{self._usage_text(result['after'])}")
                else:
                    await self.telegram.send(chat_id, 'Codex 已接受原生压缩请求，但没有收到可确认的 contextCompaction 事件；原会话保留，请用 /status 查看。')
            else:
                session = self.store.engine_session(chat_id, 'claude')
                if not session:
                    await self.telegram.send(chat_id, '当前还没有 Claude Session，无法压缩。')
                    return
                result = await self.claude_runtime.compact(session)
                await self.telegram.send(chat_id, result.get('reason', 'Claude 当前不支持可确认的原生上下文压缩。'))
        except Exception as exc:
            await self.telegram.send(chat_id, '上下文压缩失败；原会话未修改：' + self.sanitize(str(exc))[:500])

    @staticmethod
    def _usage_text(value: dict) -> str:
        if value.get('used') is None or value.get('window') is None:
            return '未知'
        return f"{value['used']} / {value['window']} tokens"

    async def request_compact(self, chat_id: int) -> None:
        if self.store.active() or self.store.queue_count() or self.store.delivery_busy():
            self.store.set_meta('compact_pending', '1')
            await self.telegram.send(chat_id, '压缩请求已排队；当前任务、队列和文件发送完成后调用当前引擎原生压缩。')
            return
        await self.perform_compact(chat_id)

    async def show_settings(self, chat_id: int) -> None:
        settings = self.store.chat_settings(chat_id)
        active = self.store.active()
        if active:
            status = active["status"]
            task = f"#{self.task_number(active)} {active['text'][:100]}"
            task_settings = (
                f"\n当前任务模型：{active.get('model') or self.config.model}"
                f"\n当前任务 Reasoning：{active.get('reasoning_effort') or self.config.reasoning_effort}"
                f"\n当前任务 Plan Reasoning："
                f"{active.get('plan_reasoning_effort') or self.config.plan_reasoning_effort}"
            )
        else:
            status = "IDLE"
            task = "无"
            task_settings = ""
        cli_version, sdk_version = await self.codex_versions()
        telegram_network = await self.telegram.network_status()
        verification = "已验证" if settings.model_verified else "尚未验证"
        codex_status, codex_url, codex_key = self.engine_status("codex")
        claude_status, claude_url, claude_key = self.engine_status("claude")
        tavily_status = self.tavily_state(settings.tavily_enabled)
        prompt_state = self.codex_prompt_state()
        await self.telegram.send_keyboard(
            chat_id,
            f"当前引擎：{settings.active_engine.title()}\n\n"
            f"工作目录：{self.store.workdir(chat_id, settings.active_engine)}\n\n"
            f"Codex：{codex_status}\n模型：{settings.model}（{verification}）\n"
            f"Reasoning Effort：{settings.reasoning_effort}\n"
            f"Plan Reasoning Effort：{settings.plan_reasoning_effort}\n"
            f"Endpoint：{codex_url or '未配置'}\nKey：{self.mask_secret(codex_key)}\n\n"
            f"Claude：{claude_status}\n模型：{settings.claude_model}\nEffort：{settings.claude_effort}\n"
            f"Endpoint：{claude_url or '未配置'}\nKey：{self.mask_secret(claude_key)}\n"
            f"Session：{self.short_thread(self.store.engine_session(chat_id, 'claude'))}\n\n"
            f"Tavily：{'ON' if settings.tavily_enabled else 'OFF'} / {tavily_status}\n"
            f"Codex Instructions：{'ON' if prompt_state.enabled else 'OFF'} / "
            f"{'已配置' if prompt_state.configured else '未配置'} / {prompt_state.length} 字符\n"
            "搜索策略：Tavily First → Native Search Fallback\n\n"
            f"Gateway：{self.config.version}\n"
            f"Codex CLI：{cli_version}\nCodex SDK：{sdk_version}\nClaude CLI：{self.claude_runtime.version}\n"
            f"Telegram 网络：{telegram_network['network']}\n"
            f"代理：{telegram_network['proxy']}\n"
            f"工作目录：/\n权限：{'root / full access' if os.geteuid() == 0 else f'uid={os.geteuid()} / full access'}\n"
            f"Thread：{self.short_thread(self.store.thread_id(chat_id))}\n"
            f"当前状态：{status}\n当前任务：{task}{task_settings}",
            self.settings_keyboard(),
        )

    async def handle_callback(self, query: dict[str, Any]) -> None:
        callback_id = str(query.get("id") or "")
        if not self.authorized_callback(query):
            if callback_id:
                await self.telegram.answer_callback(callback_id, "Unauthorized", True)
            return
        message = query.get("message") or {}
        chat_id = int((message.get("chat") or {})["id"])
        message_id = int(message.get("message_id", 0))
        data = str(query.get("data") or "")
        if data.startswith('config:'):
            await self.telegram.answer_callback(callback_id)
            await self.sync_config(chat_id, data.split(':', 1)[1])
            return
        if await self.operation_callback(data, chat_id, callback_id):
            return
        if data.startswith('models:'):
            await self.handle_model_callback(chat_id, message_id, callback_id, data)
            return
        if data.startswith('claude:'):
            if self.store.active_engine(chat_id) != 'claude':
                await self.telegram.answer_callback(callback_id, '这是 Claude 的历史按钮，请切回 Claude 后操作。', True)
                return
            if self.store.active() or self.store.queue_count():
                await self.telegram.answer_callback(callback_id, '当前有运行中或排队任务，暂时不能切换会话。', True)
                return
            try:
                if data.startswith('claude:page:'):
                    await self.show_thread_selector(chat_id, int(data.rsplit(':', 1)[1]), message_id)
                elif data.startswith('claude:select:'):
                    session = await self.claude_runtime.get_session(data.removeprefix('claude:select:'))
                    self.store.select_claude_session(chat_id, session.id, session.turns)
                    self.store.add_event('SESSION', f'Selected Claude session {session.id}')
                    await self.telegram.edit_keyboard(chat_id, message_id,
                        f"已切换到 Claude 历史会话\n{self.sanitize(session.title)}\n"
                        f"Session：{session.id}\n下一任务编号：#{self.store.thread_task_number(session.id) + 1}", [])
                else:
                    raise ValueError('无效的 Claude 会话按钮')
                await self.telegram.answer_callback(callback_id)
            except Exception as exc:
                await self.telegram.answer_callback(callback_id, self.sanitize(str(exc))[:160], True)
            return
        if data in {"engine:codex", "engine:claude"}:
            engine = data.split(":", 1)[1]
            await self.telegram.answer_callback(callback_id, "正在切换…")
            await self.select_engine(chat_id, engine)
            return
        if data == "menu:engine":
            await self.telegram.answer_callback(callback_id)
            await self.show_engine_selector(chat_id, message_id)
            return
        if data in {"menu:model", "menu:reasoning", "menu:endpoint", "menu:tavily", "menu:codexprompt", "menu:sessions", "menu:new", "menu:update"} or data.startswith("menu:endpoint:"):
            await self.telegram.answer_callback(callback_id)
            if data == "menu:model":
                await self.show_model_selector(chat_id)
            elif data == "menu:reasoning":
                await self.show_reasoning_selector(chat_id)
            elif data == "menu:endpoint":
                await self.show_endpoint_selector(chat_id)
            elif data.startswith("menu:endpoint:"):
                await self.show_endpoint_actions(chat_id, data.rsplit(":", 1)[1])
            elif data == "menu:tavily":
                await self.show_tavily_menu(chat_id)
            elif data == "menu:codexprompt":
                await self.show_codex_prompt(chat_id, message_id)
            elif data == "menu:sessions":
                await self.handle_command("/sessions", "/sessions", chat_id, message_id)
            elif data == "menu:new":
                await self.handle_command("/new", "/new", chat_id, message_id)
            elif data == "menu:update":
                await self.show_update_menu(chat_id)
            return
        if data.startswith("endpoint:"):
            _, action, engine = data.split(":", 2)
            await self.telegram.answer_callback(callback_id)
            if action == "configure":
                await self.handle_command("/endpoint", f"/endpoint {engine}", chat_id, message_id)
            elif action == "key":
                await self.handle_command("/endpoint", f"/endpoint {engine} key", chat_id, message_id)
            elif action == "test":
                await self.handle_command("/endpoint", f"/endpoint {engine} test", chat_id, message_id)
            return
        if data == "menu:queue":
            await self.telegram.answer_callback(callback_id)
            await self.handle_command("/queue", "/queue", chat_id, message_id)
            return
        if data == "queue:clear":
            await self.telegram.answer_callback(callback_id, "等待队列已清空")
            await self.handle_command("/clearqueue", "/clearqueue", chat_id, message_id)
            return
        if data.startswith("tavily:"):
            action = data.split(":", 1)[1]
            await self.telegram.answer_callback(callback_id)
            if action == "key":
                await self.handle_command("/tavilykey", "/tavilykey", chat_id, message_id)
            else:
                await self.handle_command("/tavily", f"/tavily {action}", chat_id, message_id)
            return
        if data.startswith("codexprompt:"):
            parts = data.split(":")
            action = parts[1] if len(parts) > 1 else ""
            await self.telegram.answer_callback(callback_id)
            if action == "show":
                await self.show_codex_prompt_content(chat_id)
            elif action == "edit":
                await self.request_codex_prompt(chat_id)
            elif action == "on":
                state = self.set_codex_prompt_enabled(True)
                await self.telegram.send(chat_id, "✓ Codex 前置 Instructions 已开启。" if state.enabled else "尚未配置内容，请先修改。")
            elif action == "off":
                self.set_codex_prompt_enabled(False)
                await self.telegram.send(chat_id, "Codex 前置 Instructions 已关闭；已有内容保留。")
            elif action == "clear" and len(parts) == 2:
                await self.confirm_clear_codex_prompt(chat_id)
            elif action == 'confirm' and len(parts) == 3:
                pending_clear = json.loads(self.store.get_meta('codex_prompt_clear', '{}'))
                self.store.set_meta('codex_prompt_clear', '{}')
                if (pending_clear.get('token') != parts[2]
                        or pending_clear.get('sha256') != self.codex_prompt_state().sha256):
                    await self.telegram.send(chat_id, '清空确认已过期或内容已改变，请重新 /codexprompt clear。')
                else:
                    self.clear_codex_prompt()
                    self.pending_input = None
                    await self.telegram.send(chat_id, "Codex 前置 Instructions 已清空并关闭。")
            elif action == 'cancel':
                self.store.set_meta('codex_prompt_clear', '{}')
                await self.show_codex_prompt(chat_id, message_id)
            return
        if data.startswith("update:"):
            mode = data.split(":", 1)[1]
            await self.telegram.answer_callback(callback_id, "正在启动 updater…")
            await self.start_update(chat_id, mode)
            return
        if data.startswith('thread:') and self.store.active_engine(chat_id) != 'codex':
            await self.telegram.answer_callback(callback_id, '这是 Codex 的旧按钮；请切回 Codex 后操作。', True)
            return
        if data == "setting:model:custom":
            await self.telegram.answer_callback(
                callback_id, "请发送 /model <model_name> 设置自定义模型。", True
            )
            return
        if data.startswith('setting:model:'):
            await self.telegram.answer_callback(callback_id, '这是旧模型按钮，请使用 /model 查询当前中转站。', True)
            return
        if data.startswith("setting:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                await self.telegram.answer_callback(callback_id, "无效的设置按钮", True)
                return
            kind, value = parts[1], parts[2]
            await self.telegram.answer_callback(callback_id, "正在验证并保存…")
            if kind == "model":
                await self.set_model(chat_id, value)
            elif kind == "reasoning":
                await self.set_reasoning(chat_id, value)
            elif kind == "plan":
                await self.set_reasoning(chat_id, value, plan=True)
            else:
                await self.telegram.send(chat_id, "无效的设置按钮。")
            return
        if data.startswith("thread:page:"):
            try:
                page = int(data.rsplit(":", 1)[1])
                await self.show_thread_selector(chat_id, page, message_id)
                await self.telegram.answer_callback(callback_id)
            except Exception as exc:
                await self.telegram.answer_callback(callback_id, self.sanitize(str(exc))[:160], True)
            return
        if not data.startswith("thread:select:"):
            await self.telegram.answer_callback(callback_id, "无效的会话选择", True)
            return
        if self.store.active() or self.store.queue_count():
            await self.telegram.answer_callback(
                callback_id, "当前有运行中或排队任务，暂时不能切换会话。", True
            )
            return
        thread_id = data.removeprefix("thread:select:")
        if not re.fullmatch(r"[A-Za-z0-9-]{10,64}", thread_id):
            await self.telegram.answer_callback(callback_id, "Thread ID 无效", True)
            return
        try:
            settings = self.store.chat_settings(chat_id)
            thread = await self.runtime.get_thread(
                thread_id,
                settings.model,
                settings.plan_reasoning_effort,
                apply_settings=True,
            )
            baseline = (
                0 if thread.id != thread_id else await self.runtime.thread_turn_count(thread)
            )
            self.store.register_thread(chat_id, thread.id, baseline)
            self.store.set_thread(chat_id, thread.id)
            self.store.add_event("THREAD", f"Selected thread {thread.id}")
            await self.telegram.answer_callback(callback_id, "已切换到历史会话")
            await self.telegram.edit_keyboard(
                chat_id,
                message_id,
                f"已切换到历史会话\nThread：{self.short_thread(thread.id)}\n下一任务编号：#{self.store.thread_task_number(thread.id) + 1}",
                [],
            )
        except Exception as exc:
            await self.telegram.answer_callback(callback_id, self.sanitize(str(exc))[:160], True)

    async def select_engine(self, chat_id: int, engine: str) -> None:
        if engine not in {"codex", "claude"}:
            await self.telegram.send(chat_id, "引擎可选：Codex / Claude Code")
            return
        self.store.set_active_engine(chat_id, engine)
        endpoint = self.engine_endpoints[engine]
        if not endpoint.get("base_url") or not endpoint.get("api_key"):
            self.pending_input = {"kind": "endpoint_url", "engine": engine}
            await self.telegram.send(chat_id, f"已切换到 {engine.title()}。该引擎还没有 Endpoint，请发送 Base URL，再发送 API Key。")
        else:
            await self.telegram.send(chat_id, f"已切换到 {engine.title()}，将继续该引擎的独立会话。")

    async def start_update(self, chat_id: int, mode: str) -> None:
        if self.store.active() or self.store.delivery_busy():
            await self.telegram.send(chat_id, "当前任务或文件发送正在执行，请等待完成；Agent 任务可先 /stop。")
            return
        if self.updating():
            await self.telegram.send(chat_id, '已有更新任务正在运行，/status 可查看进度。')
            return
        if mode not in {"normal", "force", "codex", "claude"}:
            await self.telegram.send(chat_id, "更新类型无效。")
            return
        activity = acquire_activity(self.config.database_path.parent, exclusive=True)
        if activity is None:
            await self.telegram.send(chat_id, '任务结果整理或后台交付尚未结束，请稍后更新。')
            return
        self.store.set_meta('update_requested_at', str(time.time()))
        activity.close()
        try:
            await asyncio.to_thread(self.backups.create, 'before-' + mode)
            process = await asyncio.create_subprocess_exec(
            "systemctl", "start", "--no-block", f"codex-tg-update@{mode}.service",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            )
            code = await process.wait()
        except Exception:
            self.store.set_meta('update_requested_at', '0')
            raise
        if code:
            self.store.set_meta('update_requested_at', '0')
        await self.telegram.send(
            chat_id,
            "更新任务已交给独立 updater；可用 /status 查看结果。"
            if code == 0 else "无法启动 updater 服务。",
        )

    async def handle_command(self, command: str, text: str, chat_id: int, message_id: int = 0) -> None:
        if await self.operation_command(command, text, chat_id):
            return
        active = self.store.active()
        thread_id = self.store.thread_id(chat_id)
        argument = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        if command == "/cancel":
            self.pending_input = None
            await self.telegram.send(chat_id, "已取消配置输入。")
        elif command in {"/start", "/help"}:
            await self.telegram.send(
                chat_id,
                "Codex TG Gateway 已连接。直接发送任务即可。\n\n" + HELP,
            )
        elif command == "/engine":
            settings = self.store.chat_settings(chat_id)
            if not argument:
                await self.show_engine_selector(chat_id)
                return
            await self.select_engine(chat_id, argument.lower())
        elif command == "/endpoint":
            parts = argument.split()
            if not parts:
                await self.show_endpoint_selector(chat_id)
                return
            engine = parts[0].lower() if parts and parts[0].lower() in {"codex", "claude"} else self.store.active_engine(chat_id)
            if parts and parts[0].lower() in {"codex", "claude"}:
                parts = parts[1:]
            current = self.engine_endpoints[engine]
            if parts and parts[0] == "test":
                if not current.get("base_url") or not current.get("api_key"):
                    self.pending_input = {"kind": "endpoint_url", "engine": engine}
                    await self.telegram.send(chat_id, "请发送 Base URL 开始配置。")
                else:
                    ok, detail = await self.test_endpoint(engine, str(current["base_url"]), str(current["api_key"]))
                    await self.telegram.send(chat_id, ("✓ " if ok else "✗ ") + detail)
            elif parts and parts[0] == "key":
                if not current.get("base_url"):
                    self.pending_input = {"kind": "endpoint_url", "engine": engine}
                    await self.telegram.send(chat_id, "请先发送 Base URL。")
                else:
                    self.pending_input = {"kind": "endpoint_key", "engine": engine, "url": str(current["base_url"])}
                    await self.telegram.send(chat_id, "请发送新的 API Key。")
            elif parts and parts[0].startswith(("http://", "https://")):
                if len(parts) > 1:
                    if message_id:
                        self.mark_secret_message(message_id)
                        await self.telegram.delete_message(chat_id, message_id)
                    ok, detail = await self.save_endpoint(engine, parts[0], parts[1])
                    await self.telegram.send(chat_id, ("✓ " if ok else "✗ ") + detail)
                elif current.get("api_key"):
                    ok, detail = await self.save_endpoint(engine, parts[0], str(current["api_key"]))
                    await self.telegram.send(chat_id, ("✓ " if ok else "✗ ") + detail)
                else:
                    self.pending_input = {"kind": "endpoint_key", "engine": engine, "url": parts[0]}
                    await self.telegram.send(chat_id, "请发送 API Key。")
            else:
                self.pending_input = {"kind": "endpoint_url", "engine": engine}
                await self.telegram.send(
                    chat_id,
                    f"{engine.title()} Endpoint：{current.get('base_url') or '未配置'}\nKey：{self.mask_secret(current.get('api_key'))}\n"
                    f"请发送新的 Base URL 开始配置；也可 /endpoint {engine} key 修改 Key，或 /endpoint {engine} test 测试。",
                )
        elif command == "/tavily":
            settings = self.store.chat_settings(chat_id)
            if not argument:
                await self.show_tavily_menu(chat_id)
                return
            if argument in {"on", "off"}:
                enabled = argument == "on"
                if enabled and not self.tavily.api_key:
                    self.pending_input = {"kind": "tavily_key"}
                    await self.telegram.send(chat_id, "Tavily 还没有 Key。请先发送 Key，验证成功后会自动开启。")
                    return
                settings = self.store.update_chat_settings(chat_id, tavily_enabled=enabled)

            state = self.tavily_state(settings.tavily_enabled)
            await self.telegram.send(chat_id, f"Tavily：{'ON' if settings.tavily_enabled else 'OFF'} / {state}\nKey：{self.mask_secret(self.tavily.api_key)}\n搜索策略：Tavily First → Native Search Fallback")
        elif command == "/codexprompt":
            if not argument:
                await self.show_codex_prompt(chat_id)
                return
            action = argument.lower()
            if action == "show":
                await self.show_codex_prompt_content(chat_id)
            elif action == "edit":
                await self.request_codex_prompt(chat_id)
            elif action == "on":
                state = self.set_codex_prompt_enabled(True)
                await self.telegram.send(chat_id, "✓ Codex 前置 Instructions 已开启。" if state.enabled else "尚未配置内容，请先使用 /codexprompt edit。")
            elif action == "off":
                self.set_codex_prompt_enabled(False)
                await self.telegram.send(chat_id, "Codex 前置 Instructions 已关闭；已有内容保留。")
            elif action == "clear":
                await self.confirm_clear_codex_prompt(chat_id)
            else:
                await self.telegram.send(chat_id, "用法：/codexprompt [show|edit|on|off|clear]")
        elif command == "/tavilykey":
            self.pending_input = {"kind": "tavily_key"}
            if argument:
                await self.handle_pending_input({'message_id': message_id}, argument, chat_id)
            else:
                await self.telegram.send(chat_id, "请发送 Tavily API Key（消息将立即尝试删除，测试成功后保存）。")
        elif command == "/status":
            settings = self.store.chat_settings(chat_id)
            prompt_state = self.codex_prompt_state()
            telegram_network = await self.telegram.network_status()
            update = self.update_status()
            if active:
                status = active["status"]
                current = f"任务 #{self.task_number(active)}：{active['text'][:120]}"
                active_settings = (
                    f"\n当前任务模型：{active.get('model') or self.config.model}"
                    f"\n当前任务 Reasoning："
                    f"{active.get('reasoning_effort') or self.config.reasoning_effort}"
                    f"\n当前任务 Plan Reasoning："
                    f"{active.get('plan_reasoning_effort') or self.config.plan_reasoning_effort}"
                )
            else:
                last_task = self.store.last_task()
                status = "ERROR" if last_task and last_task["status"] == "ERROR" else "IDLE"
                current = "无"
                active_settings = ""
            root_status = "root" if os.geteuid() == 0 else f"非 root (uid={os.geteuid()})"
            codex_status, codex_url, _ = self.engine_status("codex")
            claude_status, claude_url, _ = self.engine_status("claude")
            tavily_status = self.tavily_state(settings.tavily_enabled)
            await self.telegram.send(
                chat_id,
                f"Gateway：正常\n当前引擎：{settings.active_engine.title()}\nCodex：{codex_status}\n"
                f"版本：{self.config.version}\n模型：{settings.model}\n"
                f"Reasoning Effort：{settings.reasoning_effort}\n"
                f"Plan Reasoning Effort：{settings.plan_reasoning_effort}\n"
                f"Endpoint：{codex_url or '未配置'}\n"
                f"Claude：{claude_status}\n模型：{settings.claude_model}\nEffort：{settings.claude_effort}\n"
                f"Endpoint：{claude_url or '未配置'}\n"
                f"Session：{self.short_thread(self.store.engine_session(chat_id, 'claude'))}\n"
                f"Tavily：{'ON' if settings.tavily_enabled else 'OFF'} / {tavily_status}\n"
                f"Codex Instructions：{'ON' if prompt_state.enabled else 'OFF'} / {prompt_state.length} 字符\n"
                f"Telegram 网络：{telegram_network['network']}\n"
                f"代理：{telegram_network['proxy']}\n"
                f"权限：{root_status} / full access\n"
                f"工作目录：{self.store.workdir(chat_id, settings.active_engine)}\nThread：{self.short_thread(thread_id)}\n"
                f"上下文：{self.context_status(settings.active_engine)}\n状态：{status}\n"
                f"队列：{self.store.queue_count()}\n当前任务：{current}{active_settings}\n"
                f"最近更新："
                f"{(update or {}).get('state', '无')}"
                f"{(' · ' + self.sanitize(str((update or {}).get('detail', '')))[:300]) if update else ''}",
            )
        elif command == '/compact':
            await self.request_compact(chat_id)
        elif command == '/configstatus':
            await self.config_status_message(chat_id)
        elif command == '/syncconfig':
            await self.sync_config(chat_id, argument or None)
        elif command == '/configexport':
            await self.export_config(chat_id)
        elif command == "/new":
            if active or self.store.queue_count():
                await self.telegram.send(chat_id, "当前有运行中或排队任务；请先完成、/stop 或 /clearqueue。")
                return
            settings = self.store.chat_settings(chat_id)
            if settings.active_engine == "claude":
                session = str(uuid.uuid4())
                self.store.set_engine_session(chat_id, "claude", session)
                self.store.set_claude_conversation_key(chat_id, session)
                await self.telegram.send(chat_id, "已新建 Claude 会话；下一条任务从 #1 开始。Codex Thread 保留。")
                return
            thread = await self.runtime.new_thread(
                settings.model, settings.plan_reasoning_effort
            )
            self.store.register_thread(chat_id, thread.id, 0)
            self.store.set_thread(chat_id, thread.id)
            self.store.add_event("THREAD", f"New thread {thread.id}")
            await self.telegram.send(
                chat_id,
                f"已创建新的 Codex Thread：{self.short_thread(thread.id)}\n下一条消息将从任务 #1 开始。",
            )
        elif command in {"/sessions", "/resume"}:
            if active or self.store.queue_count():
                await self.telegram.send(chat_id, "当前有运行中或排队任务，暂时不能切换会话。")
                return
            await self.show_thread_selector(chat_id)
        elif command == "/model":
            if argument:
                await self.set_model(chat_id, argument)
            else:
                await self.show_model_selector(chat_id)
        elif command == "/reasoning":
            if argument:
                await self.set_reasoning(chat_id, argument)
            else:
                await self.show_reasoning_selector(chat_id)
        elif command == "/planreasoning":
            if argument:
                await self.set_reasoning(chat_id, argument, plan=True)
            else:
                await self.show_reasoning_selector(chat_id, plan=True)
        elif command == "/settings":
            await self.show_settings(chat_id)
        elif command == "/stop":
            if not active:
                await self.telegram.send(chat_id, "当前没有正在执行的任务。")
                return
            self.store.set_stopping(active["id"])
            stopped = await (self.claude_runtime if active.get("engine") == "claude" else self.runtime).interrupt()
            await self.telegram.send(
                chat_id,
                "已登记中断请求，正在停止当前 Agent 任务。"
                if stopped
                else "当前 Turn 已结束。",
            )
        elif command == "/queue":
            queued = self.store.queued_tasks(10)
            if not queued:
                await self.telegram.send_keyboard(chat_id, "等待任务：0 个", [[{"text": "刷新", "callback_data": "menu:queue"}]])
            else:
                lines = [
                    f"{index}. #{self.task_number(item)} {self.sanitize(item['text'])[:120]}"
                    for index, item in enumerate(queued, start=1)
                ]
                extra = self.store.queue_count() - len(queued)
                suffix = f"\n另有 {extra} 个任务" if extra > 0 else ""
                await self.telegram.send_keyboard(
                    chat_id, "等待任务\n" + "\n".join(lines) + suffix,
                    [[{"text": "清空等待队列", "callback_data": "queue:clear"}, {"text": "刷新", "callback_data": "menu:queue"}]],
                )
        elif command == "/clearqueue":
            count = self.store.clear_queue()
            self.prompt_store.prune(self.store.codex_prompt_references() | {self.codex_prompt_state().revision})
            await self.telegram.send(chat_id, f"已清除 {count} 个等待任务；当前任务不受影响。")
        elif command == "/log":
            events = self.store.recent_events(15)
            tasks = self.store.recent_tasks(10)
            lines = ["最近任务（UTC）"] + [
                f"#{self.task_number(item)} {item['status']} "
                f"{datetime.fromtimestamp(item['created_at'], timezone.utc).strftime('%m-%d %H:%M:%S')} "
                f"{self.sanitize(item['text'])[:180]}"
                + (f" | {self.sanitize(item['error'])[:240]}" if item.get("error") else "")
                for item in tasks
            ] + ["", "最近操作摘要（UTC）"] + [
                f"{datetime.fromtimestamp(item['created_at'], timezone.utc).strftime('%m-%d %H:%M:%S')} "
                f"{item['kind']} {self.sanitize(item['summary'])}"
                for item in events
            ]
            content = "\n".join(lines) if tasks or events else "暂无记录"
            if len(content) > 3500:
                self.config.log_dir.mkdir(parents=True, exist_ok=True)
                path = self.config.log_dir / f"telegram-log-{int(time.time())}.txt"
                path.write_text(content + "\n", encoding="utf-8")
                path.chmod(0o600)
                await self.telegram.send(chat_id, "日志较长，已作为 TXT 文件发送。")
                await self.telegram.send_document(chat_id, path, "Codex TG 最近日志")
            else:
                await self.telegram.send(chat_id, content)
        elif command == "/version":
            latest = "检查失败"
            try:
                async with __import__("httpx").AsyncClient(timeout=10) as client:
                    response = await client.get(f"{self.config.release_base_url}/latest.json")
                    response.raise_for_status()
                    latest = response.json().get("version", "未知")
            except Exception:
                pass
            import importlib.metadata as metadata
            sdk_version = metadata.version("openai-codex")
            await self.telegram.send(
                chat_id,
                f"Gateway：{self.config.version}\n最新 Gateway：{latest}\n"
                f"Codex SDK：{sdk_version}\nClaude CLI：{self.claude_runtime.version}\nPython：{platform.python_version()}",
            )
        elif command in {"/update", "/updatecodex", "/updateclaude"}:
            if command == "/update" and not argument:
                await self.show_update_menu(chat_id)
                return
            update_mode = "normal"
            if command == "/updatecodex":
                update_mode = "codex"
            elif command == "/updateclaude":
                update_mode = "claude"
            elif text.strip().lower() == "/update force":
                update_mode = "force"
            await self.start_update(chat_id, update_mode)
        else:
            await self.telegram.send(chat_id, HELP)

    @staticmethod
    def short_thread(thread_id: str | None) -> str:
        if not thread_id:
            return "未创建"
        return thread_id if len(thread_id) <= 18 else f"{thread_id[:10]}…{thread_id[-6:]}"

    async def worker(self) -> None:
        while not self.stop_event.is_set():
            activity = None if self.updating() else acquire_activity(self.config.database_path.parent)
            task = self.store.claim_next() if activity else None
            if task is None:
                if self.store.get_meta('compact_pending', '0') == '1' and not self.store.delivery_busy():
                    self.store.set_meta('compact_pending', '0')
                    await self.perform_compact(self.config.allowed_chat_id)
                if activity:
                    activity.close()
                self.worker_event.clear()
                try:
                    await asyncio.wait_for(self.worker_event.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                continue
            task_id = int(task["id"])
            chat_id = int(task["chat_id"])
            if chat_id != self.config.allowed_chat_id or int(task['user_id']) != self.config.allowed_user_id:
                self.store.fail(task_id, '授权身份已改变，未执行历史队列任务')
                activity.close()
                continue
            display_number = self.task_number(task)
            self.store.add_event("START", "Task started", task_id)
            engine = task.get("engine") or "codex"
            progress = None
            try:
                progress_ids = await self.telegram.send(
                    chat_id, f"任务 #{display_number} · {engine.title()} 正在执行\n\n→ 正在准备运行环境")
                progress = ProgressMessage(self.telegram, self.store, chat_id, task_id, progress_ids[-1], engine.title())
                engine = task.get('engine') or 'codex'
                cwd = validate_directory(task.get('cwd') or '/')
                task_text = task['text']
                atomic_json(self.delivery_context, {'task_id': task_id, 'engine': engine, 'token': self.delivery_token,
                    'expires_at': int(time.time()) + 86400})
                model = task.get('model') or self.config.model
                endpoint = self.credentials.get(engine, task.get('endpoint_ref'))
                search_ref = task.get('search_ref')
                search = self.search_config(search_ref) if search_ref else None
                if engine == 'claude':
                    self.claude_runtime.delivery = self.delivery_config('claude')
                    self.claude_runtime.workdir = cwd
                    result = await self.claude_runtime.run(
                        task['session_id'] or task['thread_id'], task_text, task['attachments'],
                        progress.update, model, task.get('effort') or 'high', endpoint, search)
                    if result.session_id:
                        # Switching engines never overwrites the other session.
                        self.store.set_engine_session(chat_id, 'claude', result.session_id)
                else:
                    self.runtime.delivery = self.delivery_config('codex')
                    self.runtime.workdir = cwd
                    prompt_snapshot = (
                        self.prompt_store.load_revision(task.get("codex_prompt_ref"))
                        if task.get("codex_prompt_ref") else ""
                    )
                    if endpoint and hasattr(self.runtime, 'configure'):
                        await self.runtime.configure(endpoint, search)
                    self.responses_adapter.set_snapshot(prompt_snapshot)
                    thread_id = task.get("thread_id") or self.store.thread_id(chat_id)
                    reasoning_effort = task.get("reasoning_effort") or self.config.reasoning_effort
                    plan_reasoning_effort = (
                        task.get("plan_reasoning_effort") or self.config.plan_reasoning_effort
                    )
                    thread = await self.runtime.get_thread(
                        thread_id,
                        model,
                        plan_reasoning_effort,
                        apply_settings=bool(thread_id),
                    )
                    if not thread_id or thread.id != thread_id:
                        self.store.register_thread(
                            chat_id,
                            thread.id,
                            0 if not thread_id else display_number,
                        )
                        self.store.set_thread(chat_id, thread.id)
                        if thread_id:
                            self.store.add_event(
                                "THREAD",
                                f"Replaced unavailable empty thread {thread_id} with {thread.id}",
                                task_id,
                            )
                    result = await self.runtime.run(
                        thread,
                        task_text,
                        task["attachments"],
                        progress.update,
                        model,
                        reasoning_effort,
                    )
                await progress.close()
                if result.interrupted:
                    self.store.fail(task_id, "Interrupted by user", interrupted=True)
                    self.store.add_event("INTERRUPTED", "Task interrupted", task_id)
                    await progress.finish(f"任务 #{display_number} 已中断")
                else:
                    self.store.complete(task_id, self.sanitize(result.response))
                    self.store.mark_files_collected(task_id)
                    self.delivery_event.set()
                    same_endpoint = self.credentials.current_ref(engine) == (task.get('endpoint_ref') or '')
                    if same_endpoint:
                        self.store.set_meta("endpoint_state:" + engine, "READY")
                    current_settings = self.store.chat_settings(chat_id)
                    if same_endpoint and engine == "codex" and current_settings.model == model and not current_settings.model_verified:
                        self.store.update_chat_settings(chat_id, model_verified=True)
                    self.store.add_event("DONE", "Task completed", task_id)
                    await progress.finish(f"任务 #{display_number} 完成")
                    await self.telegram.send(chat_id, self.sanitize(result.response))
            except Exception as exc:
                if progress:
                    with contextlib.suppress(Exception):
                        await progress.close()
                safe = self.clarify_codex_error(self.redact_codex_prompt(self.sanitize(str(exc)))) if engine == "codex" else self.sanitize(str(exc))
                if (self.store.task(task_id, chat_id) or {}).get('status') != 'DONE':
                    self.store.fail(task_id, safe)
                self.store.add_event("ERROR", safe[:300], task_id)
                LOG.error("task %s failed: %s", task_id, safe)
                if progress:
                    with contextlib.suppress(Exception):
                        await progress.finish(f"任务 #{display_number} 失败")
                if ((self.store.task(task_id, chat_id) or {}).get('status') != 'DONE'
                        and self.credentials.current_ref(engine) == (task.get('endpoint_ref') or '')):
                    self.store.set_meta("endpoint_state:" + engine, "ERROR")
                with contextlib.suppress(Exception):
                    await self.telegram.send(chat_id, f"{engine.title()} 任务或结果通知失败：{safe[:1500]}\n/tasks 可核对执行状态。")
            finally:
                self.delivery_context.unlink(missing_ok=True)
                activity.close()
                if engine == "codex":
                    await self.responses_adapter.cancel_requests()
                    self.responses_adapter.clear_snapshot()
                    self.prompt_store.prune(self.store.codex_prompt_references() | {self.codex_prompt_state().revision})
            self.worker_event.set()

    async def poll(self) -> None:
        await self.inbox.poll()

    async def run(self) -> None:
        await self.start()
        settings = self.store.chat_settings(self.config.allowed_chat_id)
        telegram_network = await self.telegram.network_status()
        self.worker_event.set()
        provision_task = asyncio.create_task(self.provision_claude(), name="claude-provision")
        poll_task = asyncio.create_task(self.poll(), name="telegram-poll")
        self.background(self.inbox.dispatch())
        worker_task = asyncio.create_task(self.worker(), name="task-worker")
        batch_task = asyncio.create_task(self.batches.run(), name='upload-batches')
        delivery_task = asyncio.create_task(self.delivery_worker(), name='file-delivery')
        maintenance_task = asyncio.create_task(self.maintenance(), name="upload-maintenance")
        update_status_task = asyncio.create_task(
            self.update_status_monitor(), name="update-status-monitor"
        )
        await self.telegram.send(
            self.config.allowed_chat_id,
            f"Codex VPS 已上线\n\n主机名：{platform.node()}\n"
            f"系统：{platform.system()} {platform.release()}\nGateway：{self.config.version}\n"
            f"当前引擎：{settings.active_engine.title()}\n"
            f"Codex：{self.engine_status('codex')[0]}\nClaude：{self.engine_status('claude')[0]}\n"
            f"Tavily：{'ON' if settings.tavily_enabled else 'OFF'} / {self.tavily_state(settings.tavily_enabled)}\n"
            f"模型：{settings.model}\nReasoning：{settings.reasoning_effort}\n"
            f"Plan Reasoning：{settings.plan_reasoning_effort}\n"
            f"Telegram 网络：{telegram_network['network']}\n"
            f"代理：{telegram_network['proxy']}\n"
            f"权限：root / full access\n状态：正常",
        )
        try:
            await self.stop_event.wait()
        finally:
            for task in (poll_task, worker_task, delivery_task, batch_task, maintenance_task, update_status_task, provision_task, *self.background_operations):
                task.cancel()
            await asyncio.gather(
                poll_task,
                worker_task,
                delivery_task,
                batch_task,
                maintenance_task,
                update_status_task,
                provision_task,
                *self.background_operations,
                return_exceptions=True,
            )
            await self.close()


async def async_main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    gateway = Gateway(Config.from_env())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, gateway.stop_event.set)
    await gateway.run()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
