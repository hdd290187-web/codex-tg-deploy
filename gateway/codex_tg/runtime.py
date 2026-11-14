from __future__ import annotations

import asyncio
import os
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncThread,
    AsyncTurnHandle,
    CodexConfig,
    LocalImageInput,
    Sandbox,
    TextInput,
)
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    CommandExecutionThreadItem,
    FileChangeThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    McpToolCallThreadItem,
    MessagePhase,
    ReasoningEffort,
    SortDirection,
    ThreadSortKey,
    ThreadSourceKind,
    TurnCompletedNotification,
    TurnStatus,
    ThreadTokenUsageUpdatedNotification,
)

from .config import Config
from .credentials import clean_environment
from .responses_adapter import ResponsesAdapter


ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class RunResult:
    response: str
    interrupted: bool


@dataclass(slots=True)
class HistoryThread:
    id: str
    title: str
    updated_at: int


@dataclass(slots=True)
class HistoryPage:
    threads: list[HistoryThread]
    page: int
    has_previous: bool
    has_next: bool


@dataclass(frozen=True, slots=True)
class ModelOption:
    model: str
    display_name: str
    efforts: tuple[str, ...]
    default_effort: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    accepted: bool
    verified: bool
    message: str = ""


class CodexRuntime:
    def __init__(self, config: Config, adapter: ResponsesAdapter | None = None) -> None:
        self.config = config
        self.adapter = adapter
        self.client: AsyncCodex | None = None
        self.threads: dict[str, AsyncThread] = {}
        self.thread_plan_efforts: dict[str, str | None] = {}
        self.active_handle: AsyncTurnHandle | None = None
        self.active_lock = asyncio.Lock()
        self.interrupt_requested = False
        self._model_cache: tuple[float, list[ModelOption]] | None = None
        self.ready = False
        self.workdir = '/'
        self.context_usage: dict[str, int | None] = {'used': None, 'window': None, 'total': None}
        self.endpoint_base_url = os.getenv("CODEX_TG_BASE_URL", "") or None
        self.endpoint_api_key = os.getenv("CODEX_TG_API_KEY", "") or None

    async def start(self) -> None:
        if self.adapter and self.endpoint_base_url and self.endpoint_api_key:
            await self.adapter.configure({'base_url': self.endpoint_base_url, 'api_key': self.endpoint_api_key})
        environment = clean_environment()
        if self.endpoint_api_key:
            environment["CODEX_TG_API_KEY"] = self.adapter.token if self.adapter and self.adapter.active else self.endpoint_api_key
        provider_base_url = self.adapter.base_url if self.adapter and self.adapter.active else self.endpoint_base_url
        if provider_base_url:
            environment["CODEX_TG_BASE_URL"] = provider_base_url
        overrides = []
        if provider_base_url:
            provider = {'name': 'Codex TG Relay', 'base_url': provider_base_url,
                'env_key': 'CODEX_TG_API_KEY', 'wire_api': 'responses',
                'requires_openai_auth': False, 'supports_websockets': False}
            for key, value in provider.items():
                overrides.append(f'model_providers.codex_tg.{key}={json.dumps(value)}')
        if getattr(self, 'search', None):
            search = self.search
            overrides += [f'mcp_servers.tavily.command={json.dumps(search["command"])}',
                f'mcp_servers.tavily.args={json.dumps(search["args"])}',
                f'mcp_servers.tavily.env.PYTHONPATH={json.dumps(search["env"]["PYTHONPATH"])}',
                'mcp_servers.tavily.required=false', 'mcp_servers.tavily.tool_timeout_sec=20']
        if getattr(self, 'delivery', None):
            delivery = self.delivery
            overrides += [f'mcp_servers.telegram.command={json.dumps(delivery["command"])}',
                          f'mcp_servers.telegram.args={json.dumps(delivery["args"])}',
                          'mcp_servers.telegram.required=false', 'mcp_servers.telegram.tool_timeout_sec=120']
            overrides += [f'mcp_servers.telegram.env.{key}={json.dumps(value)}' for key, value in delivery['env'].items()]
        sdk_config = CodexConfig(cwd=self.workdir, env=environment, config_overrides=tuple(overrides))
        self.client = AsyncCodex(sdk_config)
        await self.client.__aenter__()
        self.ready = True

    async def configure(self, endpoint: dict, search: dict | None = None) -> None:
        fingerprint = (endpoint.get('base_url'), endpoint.get('api_key'), json.dumps(search, sort_keys=True))
        if getattr(self, '_fingerprint', None) == fingerprint and self.ready:
            return
        await self.close()
        self.threads.clear()
        self.thread_plan_efforts.clear()
        self.endpoint_base_url = endpoint.get('base_url')
        self.endpoint_api_key = endpoint.get('api_key')
        self._model_cache = None
        self.search = search
        self._fingerprint = fingerprint
        await self.start()

    async def close(self) -> None:
        self.ready = False
        try:
            if self.client is not None:
                await self.client.close()
                self.client = None
        finally:
            if self.adapter:
                await self.adapter.close()

    async def compact_thread(self, thread_id: str, model: str | None = None,
                             plan_reasoning_effort: str | None = None) -> dict:
        """Use the app-server's native thread/compact/start endpoint.

        The endpoint returns an empty acknowledgement and emits a persisted
        contextCompaction item. We wait for that item rather than treating an
        acknowledgement as a completed compaction.
        """
        thread = await self.get_thread(thread_id, model, plan_reasoning_effort, apply_settings=False)
        before = dict(self.context_usage)
        await thread.compact()
        confirmed = False
        # The Python SDK does not expose a dedicated compact subscription. Poll
        # persisted thread items instead of consuming the global notification
        # queue (which can contain unrelated turns and cannot be cancelled
        # safely while its worker thread is blocked).
        deadline = asyncio.get_running_loop().time() + 120
        while asyncio.get_running_loop().time() < deadline:
            try:
                stored = await asyncio.wait_for(thread.read(include_turns=True), 8)
            except asyncio.TimeoutError:
                continue
            turns = getattr(stored.thread, 'turns', [])
            if any('contextCompaction' in str(item) for turn in turns for item in getattr(turn, 'items', [])):
                confirmed = True
                break
            await asyncio.sleep(1)
        return {'supported': True, 'confirmed': confirmed, 'before': before, 'after': dict(self.context_usage)}

    async def new_thread(
        self,
        model: str | None = None,
        plan_reasoning_effort: str | None = None,
        *,
        ephemeral: bool | None = None,
        sandbox: Sandbox = Sandbox.full_access,
    ) -> AsyncThread:
        if not self.client:
            raise RuntimeError("Codex runtime is not initialized")
        thread = await self.client.thread_start(
            approval_mode=ApprovalMode.deny_all,
            cwd=self.workdir,
            config=(
                {"plan_mode_reasoning_effort": plan_reasoning_effort}
                if plan_reasoning_effort
                else None
            ),
            ephemeral=ephemeral,
            model=model or self.config.model,
            model_provider="codex_tg" if self.endpoint_base_url else None,
            sandbox=sandbox,
        )
        self.threads[thread.id] = thread
        self.thread_plan_efforts[thread.id] = plan_reasoning_effort
        return thread

    async def get_thread(
        self,
        thread_id: str | None,
        model: str | None = None,
        plan_reasoning_effort: str | None = None,
        *,
        apply_settings: bool = False,
    ) -> AsyncThread:
        if thread_id and thread_id in self.threads:
            if (
                not apply_settings
                or self.thread_plan_efforts.get(thread_id) == plan_reasoning_effort
            ):
                return self.threads[thread_id]
        if not self.client:
            raise RuntimeError("Codex runtime is not initialized")
        if thread_id:
            try:
                thread = await self.client.thread_resume(
                    thread_id,
                    approval_mode=ApprovalMode.deny_all,
                    config=(
                        {"plan_mode_reasoning_effort": plan_reasoning_effort}
                        if plan_reasoning_effort
                        else None
                    ),
                    cwd=self.workdir,
                    model=model or self.config.model,
                    model_provider="codex_tg" if self.endpoint_base_url else None,
                    sandbox=Sandbox.full_access,
                )
            except Exception as exc:
                if "no rollout found for thread id" not in str(exc).lower():
                    raise
                self.threads.pop(thread_id, None)
                self.thread_plan_efforts.pop(thread_id, None)
                return await self.new_thread(model, plan_reasoning_effort)
            self.threads[thread.id] = thread
            self.thread_plan_efforts[thread.id] = plan_reasoning_effort
            return thread
        return await self.new_thread(model, plan_reasoning_effort)

    async def reasoning_catalog(self, *, refresh: bool = False) -> list[ModelOption]:
        """SDK capability metadata only; never used for provider model menus."""
        if not self.client:
            raise RuntimeError("Codex runtime is not initialized")
        now = __import__("time").monotonic()
        if self._model_cache and not refresh and now - self._model_cache[0] < 300:
            return list(self._model_cache[1])
        response = await self.client.models(include_hidden=False)
        options = [
            ModelOption(
                model=item.model,
                display_name=item.display_name,
                efforts=tuple(option.reasoning_effort.value for option in item.supported_reasoning_efforts),
                default_effort=item.default_reasoning_effort.value,
            )
            for item in response.data
            if not item.hidden
        ]
        self._model_cache = (now, options)
        return list(options)

    async def model_option(self, model: str) -> ModelOption | None:
        options = await self.reasoning_catalog()
        return next((item for item in options if item.model == model), None)

    async def supported_efforts(self, model: str) -> tuple[str, ...] | None:
        option = await self.model_option(model)
        return option.efforts if option else None

    @staticmethod
    def _is_invalid_model_error(message: str) -> bool:
        lowered = message.lower()
        return any(
            marker in lowered
            for marker in (
                "model not found",
                "model_not_found",
                "model_not_supported",
                "unknown model",
                "invalid model",
                "unsupported model",
                "unsupported_model",
                "model is not supported",
                "not a valid model",
                "does not exist",
            )
        )

    @staticmethod
    def _is_invalid_effort_error(message: str) -> bool:
        lowered = message.lower()
        return "reasoning" in lowered and any(
            marker in lowered for marker in ("unsupported", "not support", "invalid", "unknown")
        )

    async def validate_model(
        self, model: str, reasoning_effort: str | None = None, *, endpoint: dict | None = None,
        pre_instructions: str | None = None
    ) -> ValidationResult:
        if endpoint is not None:
            adapter = ResponsesAdapter(self.adapter.prompt_store) if self.adapter else None
            candidate = CodexRuntime(self.config, adapter)
            candidate.endpoint_base_url = endpoint.get('base_url')
            candidate.endpoint_api_key = endpoint.get('api_key')
            try:
                await candidate.start()
                if adapter:
                    adapter.set_snapshot(pre_instructions if pre_instructions is not None else self.adapter.snapshot)
                return await candidate.validate_model(model, reasoning_effort)
            finally:
                await candidate.close()
        thread: AsyncThread | None = None
        try:
            thread = await self.new_thread(model, ephemeral=True, sandbox=Sandbox.read_only)
            result = await thread.run(
                "Please confirm that this model endpoint is reachable by replying with one concise natural-language sentence. Do not use tools.",
                approval_mode=ApprovalMode.deny_all,
                effort=ReasoningEffort(reasoning_effort) if reasoning_effort else None,
                model=model,
                sandbox=Sandbox.read_only,
            )
            if result.status.value == "completed":
                return ValidationResult(True, True)
            return ValidationResult(False, False, f"model validation ended with {result.status.value}")
        except Exception as exc:
            message = str(exc)
            if self._is_invalid_model_error(message):
                return ValidationResult(False, False, message)
            return ValidationResult(True, False, message)
        finally:
            if thread is not None:
                self.threads.pop(thread.id, None)
                self.thread_plan_efforts.pop(thread.id, None)

    async def validate_reasoning(self, model: str, effort: str) -> ValidationResult:
        efforts = await self.supported_efforts(model)
        if efforts is not None:
            if effort not in efforts:
                return ValidationResult(
                    False,
                    True,
                    f"该模型不支持 {effort}；可选项：{', '.join(efforts)}",
                )
            return ValidationResult(True, True)
        thread: AsyncThread | None = None
        try:
            thread = await self.new_thread(model, ephemeral=True, sandbox=Sandbox.read_only)
            result = await thread.run(
                "Please respond with one concise natural-language sentence so we can verify that this reasoning setting is accepted. Do not use tools.",
                approval_mode=ApprovalMode.deny_all,
                effort=ReasoningEffort(effort),
                model=model,
                sandbox=Sandbox.read_only,
            )
            return ValidationResult(result.status.value == "completed", True)
        except Exception as exc:
            message = str(exc)
            if self._is_invalid_model_error(message) or self._is_invalid_effort_error(message):
                return ValidationResult(False, False, message)
            return ValidationResult(False, False, f"无法验证该等级：{message}")
        finally:
            if thread is not None:
                self.threads.pop(thread.id, None)
                self.thread_plan_efforts.pop(thread.id, None)

    async def validate_plan_reasoning(self, model: str, effort: str) -> ValidationResult:
        efforts = await self.supported_efforts(model)
        if efforts is not None:
            if effort not in efforts:
                return ValidationResult(
                    False,
                    True,
                    f"该模型不支持 {effort}；可选项：{', '.join(efforts)}",
                )
            return ValidationResult(True, True)
        if efforts is None:
            return ValidationResult(False, False, "自定义模型未提供 Plan reasoning 能力目录，无法安全验证。")

    async def thread_turn_count(self, thread: AsyncThread) -> int:
        response = await thread.read(include_turns=True)
        return len(response.thread.turns)

    async def list_threads(self, page: int = 0, page_size: int = 6) -> HistoryPage:
        if not self.client:
            raise RuntimeError("Codex runtime is not initialized")
        page = max(0, min(page, 50))
        cursor: str | None = None
        response = None
        source_kinds = [
            ThreadSourceKind.cli,
            ThreadSourceKind.vscode,
            ThreadSourceKind.exec,
            ThreadSourceKind.app_server,
            ThreadSourceKind.unknown,
        ]
        for index in range(page + 1):
            response = await self.client.thread_list(
                cursor=cursor,
                limit=page_size,
                model_providers=["codex_tg"],
                sort_direction=SortDirection.desc,
                sort_key=ThreadSortKey.updated_at,
                source_kinds=source_kinds,
            )
            if index < page:
                if not response.next_cursor:
                    return HistoryPage([], page, page > 0, False)
                cursor = response.next_cursor
        assert response is not None
        threads = [
            HistoryThread(
                id=item.id,
                title=(item.name or item.preview or "未命名会话").strip(),
                updated_at=item.updated_at,
            )
            for item in response.data
            if not item.ephemeral and item.parent_thread_id is None
        ]
        return HistoryPage(
            threads=threads,
            page=page,
            has_previous=page > 0,
            has_next=response.next_cursor is not None,
        )

    async def interrupt(self) -> bool:
        async with self.active_lock:
            handle = self.active_handle
            if handle is None:
                self.interrupt_requested = True
                return True
        await handle.interrupt()
        return True

    async def run(
        self,
        thread: AsyncThread,
        text: str,
        attachments: list[dict[str, str]],
        progress: ProgressCallback,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> RunResult:
        inputs = [TextInput(text)]
        for attachment in attachments:
            path = attachment["path"]
            if attachment.get("kind") == "image":
                inputs.append(LocalImageInput(path))
            else:
                inputs.append(TextInput(f"Telegram 用户上传的文件已安全保存到：{path}"))
        handle = await thread.turn(
            inputs,
            approval_mode=ApprovalMode.deny_all,
            cwd=self.workdir,
            effort=ReasoningEffort(reasoning_effort) if reasoning_effort else None,
            model=model or self.config.model,
            sandbox=Sandbox.full_access,
        )
        async with self.active_lock:
            self.active_handle = handle
            interrupt_requested = self.interrupt_requested
            self.interrupt_requested = False
        if interrupt_requested:
            await handle.interrupt()
        final_response = ""
        interrupted = False
        try:
            async for event in handle.stream():
                payload = event.payload
                if isinstance(payload, ItemStartedNotification):
                    item = payload.item.root if hasattr(payload.item, "root") else payload.item
                    if isinstance(item, CommandExecutionThreadItem):
                        await progress("正在执行系统操作")
                    elif isinstance(item, FileChangeThreadItem):
                        await progress("正在修改文件")
                    elif isinstance(item, McpToolCallThreadItem):
                        await progress(f"正在调用工具：{item.tool}")
                elif isinstance(payload, ItemCompletedNotification):
                    item = payload.item.root if hasattr(payload.item, "root") else payload.item
                    if isinstance(item, AgentMessageThreadItem):
                        if item.phase == MessagePhase.final_answer or item.phase is None:
                            final_response = item.text
                    elif isinstance(item, CommandExecutionThreadItem):
                        await progress("系统操作已完成")
                    elif isinstance(item, FileChangeThreadItem):
                        await progress(f"文件修改已完成（{len(item.changes)} 项）")
                elif isinstance(payload, TurnCompletedNotification):
                    interrupted = payload.turn.status == TurnStatus.interrupted
                    if payload.turn.status == TurnStatus.failed:
                        message = payload.turn.error.message if payload.turn.error else "Codex turn failed"
                        raise RuntimeError(message)
                elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
                    usage = payload.token_usage
                    self.context_usage = {
                        'used': getattr(getattr(usage, 'last', None), 'total', None),
                        'window': usage.model_context_window,
                        'total': getattr(getattr(usage, 'total', None), 'total', None),
                    }
        finally:
            async with self.active_lock:
                if self.active_handle is handle:
                    self.active_handle = None
        return RunResult(final_response or "任务已结束，但 Codex 没有返回最终文本。", interrupted)
