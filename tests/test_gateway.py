from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from openai_codex import LocalImageInput, TextInput

from codex_tg.app import Gateway, ProgressMessage
from codex_tg.config import Config
from codex_tg.runtime import (
    CodexRuntime,
    HistoryPage,
    HistoryThread,
    ModelOption,
    RunResult,
    ValidationResult,
)
from codex_tg.store import ChatSettings, Store
from codex_tg.telegram import Telegram, clean_filename, split_text


def make_config(tmp_path: Path) -> Config:
    return Config(
        telegram_token="123456:" + "T" * 30,
        allowed_user_id=7,
        allowed_chat_id=9,
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        plan_reasoning_effort="xhigh",
        telegram_network="DIRECT",
        telegram_proxy_kind="none",
        telegram_proxy_url=None,
        release_base_url="https://release.example",
        release_public_key_path=tmp_path / "release.pub",
        database_path=tmp_path / "gateway.db",
        upload_dir=tmp_path / "uploads",
        log_dir=tmp_path / "logs",
        health_port=17654,
        version="1.2.3",
        max_upload_bytes=1024 * 1024,
        upload_retention_days=7,
    )


def test_release_file_is_authoritative_for_gateway_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release_root = tmp_path / "current"
    release_root.mkdir()
    (release_root / "VERSION").write_text("9.9.9\n", "ascii")
    monkeypatch.setenv("CODEX_TG_VERSION_FILE", str(release_root / "VERSION"))
    monkeypatch.setenv("CODEX_TG_VERSION", "1.0.1")
    from codex_tg.config import version_from_release_file

    assert version_from_release_file("1.0.1") == "9.9.9"


@pytest.mark.asyncio
async def test_codex_validation_uses_natural_language_and_relay_error_is_actionable(tmp_path: Path) -> None:
    runtime = CodexRuntime(make_config(tmp_path))
    prompts: list[str] = []

    class Status:
        value = "completed"

    class Result:
        status = Status()

    class Thread:
        id = "validation-thread"

        async def run(self, prompt: str, **_kwargs):
            prompts.append(prompt)
            return Result()

    async def new_thread(*_args, **_kwargs):
        return Thread()

    async def no_catalog(_model):
        return None

    runtime.new_thread = new_thread  # type: ignore[method-assign]
    runtime.supported_efforts = no_catalog  # type: ignore[method-assign]
    assert (await runtime.validate_model("gpt-test", "high")).verified
    assert (await runtime.validate_reasoning("gpt-test", "high")).verified
    assert len(prompts) == 2
    assert all("natural-language sentence" in prompt and len(prompt) > 90 for prompt in prompts)
    assert not any("MODEL_OK" in prompt or "REASONING_OK" in prompt for prompt in prompts)

    notice = Gateway.clarify_codex_error("Upstream rejected illegal short-input distillation or heartbeat probing.")
    assert "/endpoint codex" in notice
    assert "重试" in notice


def test_config_from_env_reads_release_version_over_stale_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release_version = tmp_path / "VERSION"
    release_version.write_text("1.0.5\n", "ascii")
    monkeypatch.setenv("CODEX_TG_VERSION_FILE", str(release_version))
    monkeypatch.setenv("CODEX_TG_VERSION", "1.0.1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:" + "T" * 30)
    monkeypatch.setenv("ALLOWED_USER_ID", "1")
    monkeypatch.setenv("ALLOWED_CHAT_ID", "1")
    monkeypatch.setenv("CODEX_TG_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("CODEX_TG_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("CODEX_TG_RELEASE_BASE_URL", "https://release.example")
    from codex_tg.config import Config as GatewayConfig

    config = GatewayConfig.from_env()
    assert config.version == "1.0.5"
    assert config.plan_reasoning_effort == "xhigh"


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edited: list[tuple[int, int, str]] = []
        self.documents: list[Path] = []
        self.keyboards: list[tuple[int, str, list[list[dict[str, str]]]]] = []
        self.callback_answers: list[tuple[str, str, bool]] = []

    async def send(self, _chat_id: int, text: str) -> list[int]:
        self.sent.append(text)
        return [100 + len(self.sent)]

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        self.edited.append((chat_id, message_id, text))

    async def send_document(self, _chat_id: int, path: Path, _caption: str = "") -> None:
        self.documents.append(path)

    async def send_keyboard(self, chat_id: int, text: str, keyboard) -> int:
        self.keyboards.append((chat_id, text, keyboard))
        return 501

    async def edit_keyboard(self, chat_id: int, _message_id: int, text: str, keyboard) -> None:
        self.keyboards.append((chat_id, text, keyboard))

    async def answer_callback(self, callback_id: str, text: str = "", show_alert: bool = False) -> None:
        self.callback_answers.append((callback_id, text, show_alert))

    async def download(self, _file_id: str, target: Path, _max_bytes: int) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test payload")
        target.chmod(0o600)

    async def network_status(self):
        return {"network": "DIRECT", "proxy": "未使用", "state": "正常"}


class SettingsRuntime:
    ready = True

    def __init__(self) -> None:
        self.options = [
            ModelOption(
                "gpt-5.6-sol",
                "GPT-5.6 Sol",
                ("low", "medium", "high", "xhigh", "max", "ultra"),
                "low",
            ),
            ModelOption(
                "gpt-5.5",
                "GPT-5.5",
                ("low", "medium", "high", "xhigh"),
                "medium",
            ),
        ]

    async def model_options(self, **_kwargs):
        return self.options

    async def model_option(self, model):
        return next((item for item in self.options if item.model == model), None)

    async def supported_efforts(self, model):
        option = await self.model_option(model)
        return option.efforts if option else None

    async def validate_model(self, model, _reasoning, **_kwargs):
        if model == "missing-model":
            return ValidationResult(False, False, "model not found")
        if model == "transient-model":
            return ValidationResult(True, False, "provider timeout")
        return ValidationResult(True, True)

    async def validate_reasoning(self, model, effort):
        efforts = await self.supported_efforts(model)
        return ValidationResult(bool(efforts and effort in efforts), True, "unsupported reasoning")

    async def validate_plan_reasoning(self, model, effort):
        return await self.validate_reasoning(model, effort)


def test_queue_is_fifo_and_clear_does_not_touch_running(tmp_path: Path) -> None:
    store = Store(tmp_path / "gateway.db")
    store.ensure_chat(7, 9)
    first, first_position, first_number = store.enqueue(7, 9, "thread-a", "first", [])
    second, second_position, second_number = store.enqueue(7, 9, "thread-a", "second", [])
    third, third_position, third_number = store.enqueue(7, 9, "thread-a", "third", [])
    assert first_position == 1
    assert second_position == 2
    assert third_position == 3
    assert [first_number, second_number, third_number] == [1, 2, 3]
    running = store.claim_next()
    assert running["id"] == first
    assert running["text"] == "first"
    assert [item["id"] for item in store.queued_tasks()] == [second, third]
    assert store.clear_queue() == 2
    assert store.active()["id"] == first
    assert store.queue_count() == 0
    store.complete(first, "done")
    assert store.active() is None


def test_restart_marks_running_interrupted_without_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "gateway.db")
    store.ensure_chat(7, 9)
    task_id, _, _ = store.enqueue(7, 9, "thread-a", "dangerous deployment", [])
    store.claim_next()
    assert store.recover_interrupted() == 1
    assert store.active() is None
    with store.connect() as conn:
        row = conn.execute("SELECT status,error FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["status"] == "INTERRUPTED"
    assert "not retried" in row["error"]


def test_thread_mapping_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "gateway.db"
    store = Store(path)
    store.ensure_chat(1, 2)
    store.set_thread(2, "thr_persistent")
    assert Store(path).thread_id(2) == "thr_persistent"


def test_chat_settings_survive_gateway_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "gateway.db"
    defaults = ChatSettings("gpt-5.6-sol", "low", "medium", True)
    store = Store(path)
    store.ensure_chat(7, 9, defaults)
    store.update_chat_settings(
        9,
        model="gpt-5.5",
        reasoning_effort="high",
        plan_reasoning_effort="xhigh",
        model_verified=True,
    )
    assert Store(path).chat_settings(9) == ChatSettings("gpt-5.5", "high", "xhigh", True)


def test_queued_tasks_keep_submission_settings(tmp_path: Path) -> None:
    store = Store(tmp_path / "gateway.db")
    store.ensure_chat(7, 9, ChatSettings("gpt-5.6-sol", "low", "medium", True))
    first_id, _, _ = store.enqueue(7, 9, "thread-settings", "first", [])
    first = store.claim_next()
    store.update_chat_settings(
        9,
        model="gpt-5.5",
        reasoning_effort="high",
        plan_reasoning_effort="xhigh",
    )
    second_id, _, _ = store.enqueue(7, 9, "thread-settings", "second", [])
    assert first["id"] == first_id
    assert (first["model"], first["reasoning_effort"], first["plan_reasoning_effort"]) == (
        "gpt-5.6-sol",
        "low",
        "medium",
    )
    store.complete(first_id, "done")
    second = store.claim_next()
    assert second["id"] == second_id
    assert (second["model"], second["reasoning_effort"], second["plan_reasoning_effort"]) == (
        "gpt-5.5",
        "high",
        "xhigh",
    )


def test_task_numbers_are_independent_per_thread(tmp_path: Path) -> None:
    store = Store(tmp_path / "gateway.db")
    store.ensure_chat(7, 9)
    store.register_thread(9, "old-thread", 8)
    _, _, old_number = store.enqueue(7, 9, "old-thread", "continue old", [])
    store.register_thread(9, "new-thread", 0)
    _, _, new_first = store.enqueue(7, 9, "new-thread", "first new", [])
    _, _, new_second = store.enqueue(7, 9, "new-thread", "second new", [])
    _, _, old_next = store.enqueue(7, 9, "old-thread", "continue old again", [])
    assert (old_number, old_next) == (9, 10)
    assert (new_first, new_second) == (1, 2)


def test_existing_gateway_database_is_migrated_without_losing_tasks(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chats(chat_id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,thread_id TEXT,
            created_at INTEGER NOT NULL,last_active_at INTEGER NOT NULL);
        CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,text TEXT NOT NULL,attachments_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,created_at INTEGER NOT NULL,started_at INTEGER,completed_at INTEGER,
            error TEXT,final_response TEXT);
        CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER,kind TEXT NOT NULL,
            summary TEXT NOT NULL,created_at INTEGER NOT NULL);
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO chats(chat_id,user_id,created_at,last_active_at) VALUES(9,7,1,1);
        INSERT INTO tasks(chat_id,user_id,text,status,created_at) VALUES(9,7,'legacy task','DONE',1);
        """
    )
    conn.commit()
    conn.close()
    store = Store(path)
    store.ensure_chat(7, 9, ChatSettings("gpt-5.6-sol", "high", "xhigh", True))
    with store.connect() as migrated:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")}
        chat_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(chats)")}
        row = migrated.execute("SELECT text,thread_id,thread_task_number FROM tasks").fetchone()
    assert {
        "thread_id",
        "thread_task_number",
        "model",
        "reasoning_effort",
        "plan_reasoning_effort",
    } <= columns
    assert {"model", "reasoning_effort", "plan_reasoning_effort", "model_verified"} <= chat_columns
    assert row["text"] == "legacy task"
    assert row["thread_id"] is None
    assert store.chat_settings(9) == ChatSettings("gpt-5.6-sol", "high", "xhigh", True)


def test_telegram_chunking_is_lossless() -> None:
    text = ("line of output\n" * 1000).strip()
    parts = split_text(text, 300)
    assert all(len(part) <= 300 for part in parts)
    assert "\n".join(parts) == text


def test_filename_cleanup_blocks_traversal() -> None:
    assert clean_filename("../../etc/passwd") == "passwd"
    assert "/" not in clean_filename("..\\evil name.txt")


def test_telegram_client_uses_explicit_proxy_without_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def aclose(self):
            return None

    monkeypatch.setattr("codex_tg.telegram.httpx.AsyncClient", Client)
    Telegram(
        "123456:" + "A" * 30,
        "socks5://127.0.0.1:7890",
        "subscription",
    )
    assert captured["proxy"] == "socks5://127.0.0.1:7890"
    assert captured["trust_env"] is False


@pytest.mark.asyncio
async def test_telegram_network_status_distinguishes_direct_and_local_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def aclose(self):
            return None

    monkeypatch.setattr("codex_tg.telegram.httpx.AsyncClient", Client)
    direct = Telegram("123456:" + "A" * 30)
    direct.last_success = True
    assert await direct.network_status() == {
        "network": "DIRECT",
        "proxy": "未使用",
        "state": "正常",
    }

    async def unavailable(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("codex_tg.telegram.asyncio.open_connection", unavailable)
    proxy = Telegram(
        "123456:" + "A" * 30,
        "socks5://127.0.0.1:7890",
        "subscription",
    )
    status = await proxy.network_status()
    assert status["network"] == "PROXY"
    assert status["proxy"] == "本地代理未运行"


@pytest.mark.asyncio
async def test_telegram_network_status_reports_all_nodes_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"proxies": [{"name": "one", "alive": False}, {"name": "two", "alive": False}]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return Response()

        async def aclose(self):
            return None

    class Writer:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def available(*_args, **_kwargs):
        return object(), Writer()

    monkeypatch.setattr("codex_tg.telegram.httpx.AsyncClient", Client)
    monkeypatch.setattr("codex_tg.telegram.asyncio.open_connection", available)
    proxy = Telegram(
        "123456:" + "A" * 30,
        "socks5://127.0.0.1:7890",
        "subscription",
    )
    proxy.last_success = True
    proxy.last_error_code = "OK"
    status = await proxy.network_status()
    assert status["proxy"] == "所有代理节点不可用"


@pytest.mark.parametrize(
    "filename",
    [
        "task.txt",
        "server.log",
        "data.json",
        "config.toml",
        "config.yaml",
        "config.yml",
        "site.conf",
        "script.py",
        "client.js",
        "types.ts",
        "install.sh",
        "project.zip",
    ],
)
def test_required_document_extensions_are_preserved_safely(filename: str) -> None:
    assert clean_filename(filename) == filename


@pytest.mark.asyncio
async def test_attachment_pipeline_saves_document_and_image_with_safe_paths(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    document = await gateway.save_attachment(
        {
            "document": {
                "file_id": "doc-1",
                "file_name": "../../task.toml",
                "mime_type": "application/toml",
                "file_size": 20,
            }
        }
    )
    image = await gateway.save_attachment(
        {"photo": [{"file_id": "small"}, {"file_id": "large", "file_size": 20}]}
    )
    assert document[0]["kind"] == "document"
    assert Path(document[0]["path"]).suffix == ".toml"
    assert Path(document[0]["path"]).is_relative_to(gateway.config.upload_dir)
    assert image[0]["kind"] == "image"
    assert Path(image[0]["path"]).suffix == ".jpg"
    assert all(Path(item[0]["path"]).stat().st_mode & 0o777 == 0o600 for item in (document, image))


@pytest.mark.asyncio
async def test_runtime_passes_real_paths_and_local_image_input(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = CodexRuntime(config)
    captured: list[object] = []
    turn_options: dict[str, object] = {}

    class Handle:
        id = "turn-test"

        async def stream(self):
            if False:
                yield None

    class Thread:
        async def turn(self, inputs, **kwargs):
            captured.extend(inputs)
            turn_options.update(kwargs)
            return Handle()

    image_path = tmp_path / "screen.png"
    file_path = tmp_path / "config.yaml"
    image_path.write_bytes(b"png")
    file_path.write_text("enabled: true", "utf-8")
    await runtime.run(
        Thread(),  # type: ignore[arg-type]
        "检查附件",
        [
            {"path": str(image_path), "kind": "image"},
            {"path": str(file_path), "kind": "document"},
        ],
        lambda _summary: _done(),
        "gpt-5.5",
        "high",
    )
    assert any(isinstance(item, LocalImageInput) and item.path == str(image_path) for item in captured)
    assert any(isinstance(item, TextInput) and str(file_path) in item.text for item in captured)
    assert turn_options["model"] == "gpt-5.5"
    assert turn_options["effort"].value == "high"


async def _done() -> None:
    return None


@pytest.mark.asyncio
async def test_stop_calls_sdk_turn_interrupt(tmp_path: Path) -> None:
    runtime = CodexRuntime(make_config(tmp_path))

    class Handle:
        def __init__(self) -> None:
            self.called = 0

        async def interrupt(self) -> None:
            self.called += 1

    handle = Handle()
    runtime.active_handle = handle  # type: ignore[assignment]
    assert await runtime.interrupt() is True
    assert handle.called == 1


@pytest.mark.asyncio
async def test_stop_before_turn_handle_is_registered_is_honored(tmp_path: Path) -> None:
    runtime = CodexRuntime(make_config(tmp_path))
    assert await runtime.interrupt() is True

    class Handle:
        id = "turn-race"
        called = 0

        async def interrupt(self) -> None:
            self.called += 1

        async def stream(self):
            if False:
                yield None

    handle = Handle()

    class Thread:
        async def turn(self, _inputs, **_kwargs):
            return handle

    result = await runtime.run(Thread(), "race", [], _done)
    assert handle.called == 1
    assert result.interrupted is False


@pytest.mark.asyncio
async def test_progress_uses_single_editable_message_and_keeps_summary(tmp_path: Path) -> None:
    store = Store(tmp_path / "gateway.db")
    telegram = FakeTelegram()
    progress = ProgressMessage(telegram, store, 9, 1, 42)  # type: ignore[arg-type]
    await progress.update("正在检查系统")
    await progress.update("正在修改配置")
    await progress.close()
    await progress.finish("任务完成")
    assert telegram.sent == []
    assert all(item[1] == 42 for item in telegram.edited)
    assert "正在检查系统" in telegram.edited[-1][2]
    assert telegram.edited[-1][2].startswith("任务完成")


def test_upload_retention_removes_only_expired_files(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    old = gateway.config.upload_dir / "9/20260101/old.log"
    current = gateway.config.upload_dir / "9/20260907/current.zip"
    old.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    old.write_text("old", "utf-8")
    current.write_text("new", "utf-8")
    now = time.time()
    os.utime(old, (now - 10 * 86400, now - 10 * 86400))
    assert gateway.cleanup_uploads(now) == 1
    assert not old.exists()
    assert current.exists()


@pytest.mark.asyncio
async def test_status_queue_and_long_log_are_backed_by_store(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.runtime.ready = True
    gateway.store.ensure_chat(7, 9)
    failed, _, _ = gateway.store.enqueue(7, 9, "thread-a", "failed task", [])
    gateway.store.claim_next()
    gateway.store.fail(failed, "safe failure")
    for index in range(20):
        gateway.store.add_event("PROGRESS", f"event {index} " + "x" * 300, failed)
    await gateway.handle_command("/status", "/status", 9)
    assert "状态：ERROR" in telegram.sent[-1]
    await gateway.handle_command("/log", "/log", 9)
    assert telegram.documents
    assert telegram.documents[-1].stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_runtime_setting_commands_use_catalog_buttons_and_persist(tmp_path: Path, monkeypatch) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.runtime = SettingsRuntime()  # type: ignore[assignment]
    gateway.store.ensure_chat(
        7, 9, ChatSettings("gpt-5.6-sol", "low", "medium", True)
    )

    async def provider_options(*_args, **_kwargs):
        return gateway.runtime.options
    monkeypatch.setattr(gateway.models, 'options', provider_options)

    await gateway.handle_command("/model", "/model", 9)
    assert "当前模型：gpt-5.6-sol" in telegram.keyboards[-1][1]
    callbacks = [button["callback_data"] for row in telegram.keyboards[-1][2] for button in row]
    assert any(':select:1' in item for item in callbacks)
    assert any(':custom:' in item for item in callbacks)

    await gateway.handle_command("/reasoning", "/reasoning", 9)
    callbacks = [button["callback_data"] for row in telegram.keyboards[-1][2] for button in row]
    assert "setting:reasoning:max" in callbacks
    await gateway.handle_command("/planreasoning", "/planreasoning", 9)
    assert "实际可选项" in telegram.keyboards[-1][1]

    await gateway.handle_command("/model", "/model gpt-5.5", 9)
    await gateway.handle_command("/reasoning", "/reasoning high", 9)
    await gateway.handle_command("/reasoning", "/reasoning xhigh", 9)
    await gateway.handle_command("/planreasoning", "/planreasoning high", 9)
    assert Store(gateway.config.database_path).chat_settings(9) == ChatSettings(
        "gpt-5.5", "xhigh", "high", True
    )
    assert "从下一条任务开始生效" in telegram.sent[-1]


@pytest.mark.asyncio
async def test_settings_and_status_report_real_values_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.runtime = SettingsRuntime()  # type: ignore[assignment]
    gateway.store.ensure_chat(7, 9, ChatSettings("gpt-5.5", "high", "xhigh", True))
    gateway.store.set_thread(9, "thread-settings-12345")

    async def versions():
        return "codex-cli 0.147.0", "0.147.0"

    gateway.codex_versions = versions  # type: ignore[method-assign]
    monkeypatch.setenv("CODEX_TG_API_KEY", "secret-api-key")
    await gateway.handle_command("/settings", "/settings", 9)
    settings_text = telegram.keyboards[-1][1]
    for expected in (
        "模型：gpt-5.5",
        "Reasoning Effort：high",
        "Plan Reasoning Effort：xhigh",
        "Gateway：1.2.3",
        "Codex CLI：codex-cli 0.147.0",
        "Codex SDK：0.147.0",
        "工作目录：/",
        "当前状态：IDLE",
    ):
        assert expected in settings_text
    assert "secret-api-key" not in settings_text
    assert gateway.config.telegram_token not in settings_text

    # /settings shows the status once and the settings keyboard once.  A
    # previous regression invoked the keyboard renderer twice.
    assert len(telegram.keyboards) == 1
    assert not telegram.sent

    await gateway.handle_command("/status", "/status", 9)
    status_text = telegram.sent[-1]
    assert "模型：gpt-5.5" in status_text
    assert "Reasoning Effort：high" in status_text
    assert "Plan Reasoning Effort：xhigh" in status_text


@pytest.mark.asyncio
async def test_status_reports_proxy_health_without_proxy_secret(tmp_path: Path) -> None:
    gateway = Gateway(
        replace(
            make_config(tmp_path),
            telegram_network="PROXY",
            telegram_proxy_kind="subscription",
            telegram_proxy_url="socks5://secret-user:secret-pass@127.0.0.1:7890",
        )
    )
    telegram = FakeTelegram()

    async def proxy_status():
        return {
            "network": "PROXY",
            "proxy": "订阅刷新失败，正在使用最后有效缓存",
            "state": "订阅刷新失败，正在使用最后有效缓存",
        }

    telegram.network_status = proxy_status  # type: ignore[method-assign]
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.store.ensure_chat(7, 9, ChatSettings("gpt-5.6-sol", "high", "xhigh", True))
    await gateway.handle_command("/status", "/status", 9)
    text = telegram.sent[-1]
    assert "Telegram 网络：PROXY" in text
    assert "代理：订阅刷新失败，正在使用最后有效缓存" in text
    assert "secret-user" not in text
    assert "secret-pass" not in text


@pytest.mark.asyncio
async def test_gateway_replays_terminal_update_when_updater_notification_failed(
    tmp_path: Path,
) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    status_path = gateway.config.database_path.parent / "update-status.json"
    status_path.write_text(
        json.dumps(
            {
                "id": "update-123",
                "mode": "codex",
                "state": "FAILED",
                "detail": "Codex 更新失败：candidate validation failed",
                "notified": False,
            }
        ),
        "utf-8",
    )
    monitor = __import__("asyncio").create_task(gateway.update_status_monitor())
    for _ in range(50):
        if telegram.sent:
            break
        await __import__("asyncio").sleep(0.02)
    gateway.stop_event.set()
    await monitor
    assert telegram.sent == ["Codex 更新失败：candidate validation failed"]
    assert gateway.store.get_meta("announced_update_id") == "update-123"


@pytest.mark.asyncio
async def test_invalid_model_is_rejected_but_manual_reasoning_is_saved(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.runtime = SettingsRuntime()  # type: ignore[assignment]
    gateway.store.ensure_chat(7, 9, ChatSettings("gpt-5.5", "high", "high", True))

    await gateway.handle_command("/model", "/model bad model", 9)
    assert "格式无效" in telegram.sent[-1]
    await gateway.handle_command("/model", "/model missing-model", 9)
    assert "模型不可用" in telegram.sent[-1]
    await gateway.handle_command("/reasoning", "/reasoning max", 9)
    assert "已设置为：max" in telegram.sent[-1]
    assert gateway.store.chat_settings(9) == ChatSettings("gpt-5.5", "max", "high", True)

    await gateway.handle_command("/model", "/model transient-model", 9)
    settings = gateway.store.chat_settings(9)
    assert settings.model == "transient-model"
    assert settings.model_verified is False
    assert "已保存，但尚未验证可用" in telegram.sent[-1]


@pytest.mark.asyncio
async def test_setting_changes_during_task_only_affect_next_submitted_task(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.runtime = SettingsRuntime()  # type: ignore[assignment]
    gateway.store.ensure_chat(7, 9, ChatSettings("gpt-5.6-sol", "low", "medium", True))
    first_id, _, _ = gateway.store.enqueue(7, 9, "thread-live", "running", [])
    first = gateway.store.claim_next()

    await gateway.handle_command("/model", "/model gpt-5.5", 9)
    assert "当前任务不受影响" in telegram.sent[-1]
    await gateway.handle_command("/reasoning", "/reasoning high", 9)
    await gateway.handle_command("/planreasoning", "/planreasoning xhigh", 9)
    assert (first["model"], first["reasoning_effort"], first["plan_reasoning_effort"]) == (
        "gpt-5.6-sol",
        "low",
        "medium",
    )

    second_id, _, _ = gateway.store.enqueue(7, 9, "thread-live", "next", [])
    gateway.store.complete(first_id, "done")
    second = gateway.store.claim_next()
    assert second["id"] == second_id
    assert (second["model"], second["reasoning_effort"], second["plan_reasoning_effort"]) == (
        "gpt-5.5",
        "high",
        "xhigh",
    )


@pytest.mark.asyncio
async def test_setting_inline_callback_requires_user_and_chat_authorization(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.runtime = SettingsRuntime()  # type: ignore[assignment]
    gateway.store.ensure_chat(7, 9, ChatSettings("gpt-5.6-sol", "low", "medium", True))
    await gateway.handle_callback(
        {
            "id": "bad-setting",
            "from": {"id": 999},
            "data": "setting:reasoning:high",
            "message": {"message_id": 1, "chat": {"id": 9, "type": "private"}},
        }
    )
    assert telegram.callback_answers[-1] == ("bad-setting", "Unauthorized", True)
    assert gateway.store.chat_settings(9).reasoning_effort == "low"

    await gateway.handle_callback(
        {
            "id": "good-setting",
            "from": {"id": 7},
            "data": "setting:reasoning:high",
            "message": {"message_id": 1, "chat": {"id": 9, "type": "private"}},
        }
    )
    assert gateway.store.chat_settings(9).reasoning_effort == "high"


@pytest.mark.asyncio
async def test_gateway_start_restart_recovers_running_task_and_keeps_thread_mapping(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(config, health_port=0)
    store = Store(config.database_path)
    store.ensure_chat(7, 9, ChatSettings("gpt-5.5", "high", "xhigh", True))
    store.set_thread(9, "thread-before-restart")
    task_id, _, _ = store.enqueue(7, 9, "thread-before-restart", "dangerous task", [])
    store.claim_next()

    class StartupTelegram(FakeTelegram):
        async def get_me(self):
            return {"id": 1, "username": "fake_bot"}

        async def delete_webhook(self):
            return None

        async def close(self):
            return None

    class Runtime:
        ready = False

        async def start(self):
            self.ready = True

        async def close(self):
            self.ready = False

    gateway = Gateway(config)
    telegram = StartupTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.runtime = Runtime()  # type: ignore[assignment]
    await gateway.start()
    assert gateway.store.thread_id(9) == "thread-before-restart"
    assert gateway.store.chat_settings(9) == ChatSettings("gpt-5.5", "high", "xhigh", True)
    assert gateway.store.active() is None
    with gateway.store.connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["status"] == "INTERRUPTED"
    assert any("没有自动重试" in message for message in telegram.sent)
    socket = gateway.health_server.sockets[0]
    assert socket.getsockname()[0] == "127.0.0.1"
    await gateway.close()


@pytest.mark.asyncio
async def test_all_gateway_commands_dispatch_to_real_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.store.ensure_chat(7, 9)

    class Runtime:
        ready = True
        interrupted = 0

        async def new_thread(self, *_args, **_kwargs):
            return type("Thread", (), {"id": "new-thread-id"})()

        async def interrupt(self):
            self.interrupted += 1
            return True

    runtime = Runtime()
    gateway.runtime = runtime  # type: ignore[assignment]
    await gateway.handle_command("/start", "/start", 9)
    await gateway.handle_command("/help", "/help", 9)
    await gateway.handle_command("/new", "/new", 9)
    assert gateway.store.thread_id(9) == "new-thread-id"
    gateway.store.enqueue(7, 9, "new-thread-id", "running", [])
    gateway.store.claim_next()
    await gateway.handle_command("/stop", "/stop", 9)
    assert runtime.interrupted == 1
    gateway.store.enqueue(7, 9, "new-thread-id", "queued", [])
    await gateway.handle_command("/queue", "/queue", 9)
    assert "#" in telegram.keyboards[-1][1]
    await gateway.handle_command("/clearqueue", "/clearqueue", 9)
    assert gateway.store.queue_count() == 0
    await gateway.handle_command("/log", "/log", 9)
    await gateway.handle_command("/version", "/version", 9)

    starts: list[str] = []

    class Process:
        pid = 123

        async def wait(self):
            return 0

    async def fake_process(*args, **kwargs):
        starts.append(args[-1])
        return Process()

    monkeypatch.setattr(__import__("asyncio"), "create_subprocess_exec", fake_process)
    gateway.store.complete(gateway.store.active()["id"], "done")
    await gateway.handle_command("/update", "/update normal", 9)
    gateway.store.set_meta('update_requested_at', '0')
    await gateway.handle_command("/update", "/update force", 9)
    gateway.store.set_meta('update_requested_at', '0')
    await gateway.handle_command("/updatecodex", "/updatecodex", 9)
    assert starts == [
        "codex-tg-update@normal.service",
        "codex-tg-update@force.service",
        "codex-tg-update@codex.service",
    ]


@pytest.mark.asyncio
async def test_unauthorized_user_cannot_enqueue_or_read_status(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    await gateway.handle_message(
        {"from": {"id": 999}, "chat": {"id": 999, "type": "private"}, "text": "/status"}
    )
    await gateway.handle_message(
        {"from": {"id": 999}, "chat": {"id": 999, "type": "private"}, "text": "run root task"}
    )
    assert telegram.sent == []
    assert gateway.store.queue_count() == 0


@pytest.mark.asyncio
async def test_plain_text_enters_queue_without_command(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.store.ensure_chat(7, 9)
    gateway.engine_endpoints["codex"] = {"base_url": "https://relay.invalid/v1", "api_key": "fixture-key"}
    gateway.store.set_thread(9, "plain-thread")
    gateway.store.register_thread(9, "plain-thread", 0)
    await gateway.handle_message(
        {"from": {"id": 7}, "chat": {"id": 9, "type": "private"}, "text": "检查 CPU"}
    )
    assert gateway.store.queue_count() == 1
    assert "已接收" in telegram.sent[-1]
    assert gateway.store.queued_tasks()[0]["text"] == "检查 CPU"
    assert "任务 #1 已接收" in telegram.sent[-1]


@pytest.mark.asyncio
async def test_new_thread_resets_number_and_history_selection_resumes_old_number(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.store.ensure_chat(7, 9)

    class Thread:
        def __init__(self, thread_id: str) -> None:
            self.id = thread_id

    class Runtime:
        ready = True

        async def new_thread(self, *_args, **_kwargs):
            return Thread("new-thread-12345")

        async def get_thread(self, thread_id, *_args, **_kwargs):
            return Thread(thread_id)

        async def thread_turn_count(self, thread):
            return 8 if thread.id == "old-thread-12345" else 0

        async def list_threads(self, page):
            return HistoryPage(
                [
                    HistoryThread("old-thread-12345", "旧任务对话", 1788790000),
                    HistoryThread("new-thread-12345", "新对话", 1788800000),
                ],
                page,
                False,
                False,
            )

    gateway.runtime = Runtime()  # type: ignore[assignment]
    gateway.engine_endpoints["codex"] = {"base_url": "https://relay.invalid/v1", "api_key": "fixture-key"}
    await gateway.handle_command("/new", "/new", 9)
    await gateway.handle_message(
        {"from": {"id": 7}, "chat": {"id": 9, "type": "private"}, "text": "新会话第一条"}
    )
    assert "任务 #1 已接收" in telegram.sent[-1]
    queued = gateway.store.claim_next()
    gateway.store.complete(queued["id"], "done")
    await gateway.handle_command("/sessions", "/sessions", 9)
    assert telegram.keyboards[-1][2][0][0]["callback_data"] == "thread:select:old-thread-12345"
    await gateway.handle_callback(
        {
            "id": "callback-1",
            "from": {"id": 7},
            "data": "thread:select:old-thread-12345",
            "message": {"message_id": 88, "chat": {"id": 9, "type": "private"}},
        }
    )
    assert gateway.store.thread_id(9) == "old-thread-12345"
    await gateway.handle_message(
        {"from": {"id": 7}, "chat": {"id": 9, "type": "private"}, "text": "继续旧会话"}
    )
    assert "任务 #9 已接收" in telegram.sent[-1]


@pytest.mark.asyncio
async def test_history_callback_requires_both_authorized_ids(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    await gateway.handle_callback(
        {
            "id": "bad-callback",
            "from": {"id": 999},
            "data": "thread:select:old-thread-12345",
            "message": {"message_id": 1, "chat": {"id": 9, "type": "private"}},
        }
    )
    assert telegram.callback_answers == [("bad-callback", "Unauthorized", True)]
    assert gateway.store.thread_id(9) is None


@pytest.mark.asyncio
async def test_single_worker_executes_fifo_and_returns_final_responses(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.store.ensure_chat(7, 9)
    for text in ("A", "B", "C"):
        gateway.store.enqueue(7, 9, "thread-fifo", text, [])
    calls: list[str] = []

    class Thread:
        id = "thread-fifo"

    class Runtime:
        ready = True

        async def get_thread(self, _thread_id, *_args, **_kwargs):
            return Thread()

        async def run(self, _thread, text, _attachments, progress, *_args):
            calls.append(text)
            await progress(f"执行 {text}")
            return RunResult(f"完成 {text}", False)

    gateway.runtime = Runtime()  # type: ignore[assignment]
    worker = __import__("asyncio").create_task(gateway.worker())
    gateway.worker_event.set()
    for _ in range(100):
        if gateway.store.queue_count() == 0 and gateway.store.active() is None:
            break
        await __import__("asyncio").sleep(0.02)
    gateway.stop_event.set()
    gateway.worker_event.set()
    await worker
    assert calls == ["A", "B", "C"]
    assert [text for text in telegram.sent if text.startswith("完成 ")] == ["完成 A", "完成 B", "完成 C"]


@pytest.mark.asyncio
async def test_worker_applies_task_snapshot_to_same_thread(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.store.ensure_chat(7, 9, ChatSettings("gpt-5.6-sol", "low", "medium", True))
    gateway.store.enqueue(7, 9, "long-lived-thread", "snapshot task", [])
    gateway.store.update_chat_settings(
        9,
        model="gpt-5.5",
        reasoning_effort="high",
        plan_reasoning_effort="xhigh",
    )
    calls: list[tuple[object, ...]] = []

    class Thread:
        id = "long-lived-thread"

    class Runtime:
        ready = True

        async def get_thread(self, thread_id, model, plan_effort, **kwargs):
            calls.append(("resume", thread_id, model, plan_effort, kwargs["apply_settings"]))
            return Thread()

        async def run(self, thread, text, _attachments, _progress, model, effort):
            assert 'Telegram 文件交付' not in text
            calls.append(("turn", thread.id, text, model, effort))
            return RunResult("snapshot done", False)

    gateway.runtime = Runtime()  # type: ignore[assignment]
    worker = __import__("asyncio").create_task(gateway.worker())
    gateway.worker_event.set()
    for _ in range(100):
        if gateway.store.queue_count() == 0 and gateway.store.active() is None:
            break
        await __import__("asyncio").sleep(0.02)
    gateway.stop_event.set()
    gateway.worker_event.set()
    await worker
    assert calls == [
        ("resume", "long-lived-thread", "gpt-5.6-sol", "medium", True),
        ("turn", "long-lived-thread", "snapshot task", "gpt-5.6-sol", "low"),
    ]


@pytest.mark.asyncio
async def test_worker_persists_replacement_for_missing_empty_rollout(tmp_path: Path) -> None:
    gateway = Gateway(make_config(tmp_path))
    telegram = FakeTelegram()
    gateway.telegram = telegram  # type: ignore[assignment]
    gateway.store.ensure_chat(7, 9, ChatSettings("gpt-5.6-sol", "high", "high", True))
    gateway.store.set_thread(9, "stale-empty-thread")
    gateway.store.enqueue(7, 9, "stale-empty-thread", "first task", [])

    class Thread:
        id = "replacement-thread"

    class Runtime:
        ready = True

        async def get_thread(self, *_args, **_kwargs):
            return Thread()

        async def run(self, *_args, **_kwargs):
            return RunResult("replacement completed", False)

    gateway.runtime = Runtime()  # type: ignore[assignment]
    worker = __import__("asyncio").create_task(gateway.worker())
    gateway.worker_event.set()
    for _ in range(100):
        if gateway.store.queue_count() == 0 and gateway.store.active() is None:
            break
        await __import__("asyncio").sleep(0.02)
    gateway.stop_event.set()
    gateway.worker_event.set()
    await worker
    assert gateway.store.thread_id(9) == "replacement-thread"
    assert gateway.store.thread_task_number("replacement-thread") == 1
    _, _, next_number = gateway.store.enqueue(7, 9, "replacement-thread", "next", [])
    assert next_number == 2


@pytest.mark.asyncio
async def test_runtime_resumes_persisted_thread_instead_of_starting_new(tmp_path: Path) -> None:
    runtime = CodexRuntime(make_config(tmp_path))
    calls: list[str] = []

    class Thread:
        id = "persisted-thread"

    class Client:
        async def thread_resume(self, thread_id, **kwargs):
            calls.append(thread_id)
            assert kwargs["cwd"] == "/"
            assert kwargs["sandbox"].value == "full-access"
            assert kwargs["approval_mode"].value == "deny_all"
            return Thread()

        async def thread_start(self, **_kwargs):
            raise AssertionError("thread_start must not be used for a persisted mapping")

    runtime.client = Client()  # type: ignore[assignment]
    first = await runtime.get_thread("persisted-thread")
    second = await runtime.get_thread("persisted-thread")
    assert first is second
    assert calls == ["persisted-thread"]


@pytest.mark.asyncio
async def test_first_turn_uses_new_thread_without_resuming_empty_rollout(tmp_path: Path) -> None:
    runtime = CodexRuntime(make_config(tmp_path))
    resume_calls = 0

    class Thread:
        id = "brand-new-thread"

    class Client:
        async def thread_start(self, **_kwargs):
            return Thread()

        async def thread_resume(self, *_args, **_kwargs):
            nonlocal resume_calls
            resume_calls += 1
            raise AssertionError("an empty new thread must not be resumed before its first turn")

    runtime.client = Client()  # type: ignore[assignment]
    created = await runtime.new_thread("gpt-5.6-sol", "high")
    selected = await runtime.get_thread(
        created.id,
        "gpt-5.6-sol",
        "high",
        apply_settings=True,
    )
    assert selected is created
    assert resume_calls == 0


@pytest.mark.asyncio
async def test_missing_empty_rollout_is_replaced_with_new_thread(tmp_path: Path) -> None:
    runtime = CodexRuntime(make_config(tmp_path))

    class Thread:
        id = "replacement-thread"

    class Client:
        async def thread_resume(self, *_args, **_kwargs):
            raise RuntimeError(
                "JSON-RPC error -32600: no rollout found for thread id stale-thread"
            )

        async def thread_start(self, **_kwargs):
            return Thread()

    runtime.client = Client()  # type: ignore[assignment]
    thread = await runtime.get_thread(
        "stale-thread",
        "gpt-5.6-sol",
        "high",
        apply_settings=True,
    )
    assert thread.id == "replacement-thread"
    assert runtime.thread_plan_efforts[thread.id] == "high"


@pytest.mark.asyncio
async def test_runtime_reasoning_metadata_keeps_new_effort_levels(tmp_path: Path) -> None:
    runtime = CodexRuntime(make_config(tmp_path))

    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

    class EffortOption:
        def __init__(self, value: str) -> None:
            self.reasoning_effort = Value(value)

    class Client:
        async def models(self, **_kwargs):
            item = type(
                "Model",
                (),
                {
                    "model": "future-model",
                    "display_name": "Future Model",
                    "hidden": False,
                    "default_reasoning_effort": Value("medium"),
                    "supported_reasoning_efforts": [
                        EffortOption("none"),
                        EffortOption("max"),
                        EffortOption("ultra"),
                    ],
                },
            )()
            return type("Response", (), {"data": [item]})()

    runtime.client = Client()  # type: ignore[assignment]
    options = await runtime.reasoning_catalog()
    assert options == [ModelOption("future-model", "Future Model", ("none", "max", "ultra"), "medium")]
    assert await runtime.supported_efforts("future-model") == ("none", "max", "ultra")


@pytest.mark.asyncio
async def test_runtime_reapplies_settings_when_resuming_same_thread(tmp_path: Path) -> None:
    runtime = CodexRuntime(make_config(tmp_path))
    calls: list[tuple[str, dict[str, object]]] = []

    class Thread:
        id = "same-long-lived-thread"

    class Client:
        async def thread_resume(self, thread_id, **kwargs):
            calls.append((thread_id, kwargs))
            return Thread()

    runtime.client = Client()  # type: ignore[assignment]
    first = await runtime.get_thread(
        "same-long-lived-thread",
        "gpt-5.6-sol",
        "high",
        apply_settings=True,
    )
    second = await runtime.get_thread(
        "same-long-lived-thread",
        "gpt-5.5",
        "xhigh",
        apply_settings=True,
    )
    assert first.id == second.id == "same-long-lived-thread"
    assert [item[0] for item in calls] == ["same-long-lived-thread", "same-long-lived-thread"]
    assert calls[0][1]["model"] == "gpt-5.6-sol"
    assert calls[0][1]["config"] == {"plan_mode_reasoning_effort": "high"}
    assert calls[1][1]["model"] == "gpt-5.5"
    assert calls[1][1]["config"] == {"plan_mode_reasoning_effort": "xhigh"}


@pytest.mark.asyncio
async def test_codex_runtime_does_not_receive_telegram_proxy_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    class Client:
        def __init__(self, config):
            captured.update(config.env)

        async def __aenter__(self):
            return self

        async def close(self):
            return None

    monkeypatch.setenv(
        "CODEX_TG_TELEGRAM_PROXY_URL",
        "socks5://proxy-user:proxy-password@127.0.0.1:7890",
    )
    monkeypatch.setattr("codex_tg.runtime.AsyncCodex", Client)
    runtime = CodexRuntime(make_config(tmp_path))
    await runtime.start()
    assert "CODEX_TG_TELEGRAM_PROXY_URL" not in captured
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        assert captured.get(name) == os.environ.get(name)
    await runtime.close()


def test_gateway_does_not_fall_back_to_codex_exec() -> None:
    content = "\n".join(
        path.read_text("utf-8")
        for path in (Path(__file__).resolve().parents[1] / "gateway/codex_tg").glob("*.py")
    )
    assert "codex exec" not in content
    runtime_source = (Path(__file__).resolve().parents[1] / 'gateway/codex_tg/runtime.py').read_text()
    assert 'import subprocess' not in runtime_source
