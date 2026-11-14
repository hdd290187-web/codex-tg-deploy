"""Local configuration/schema checks used by the Gateway diagnostics.

These checks deliberately do not call a model or a provider.  They validate
the files that are actually present on the VPS and ask the bundled runtimes to
parse them, then separately describe the command-line settings that the
Gateway applies for unattended turns.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomli as tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AuditResult:
    state: str
    detail: str


_CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh"}
_CLAUDE_MODES = {"default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions", "manual"}


def validate_claude_document(document: object) -> str | None:
    """Return a concise structural error for known Claude settings shapes."""
    if not isinstance(document, dict):
        return "JSON 根节点必须是对象"
    effort = document.get("effortLevel")
    if effort is not None and effort not in _CLAUDE_EFFORTS:
        return "settings.json 顶层 effortLevel 只接受 low/medium/high/xhigh"
    permissions = document.get("permissions", {})
    if not isinstance(permissions, dict):
        return "permissions 必须是对象"
    mode = permissions.get("defaultMode")
    if mode is not None and mode not in _CLAUDE_MODES:
        return f"permissions.defaultMode={mode!r} 无效"
    if "deny" in permissions and not isinstance(permissions["deny"], list):
        return "permissions.deny 必须是数组"
    model_settings = document.get("modelSettings", {})
    if not isinstance(model_settings, dict):
        return "modelSettings 必须是按模型名映射的对象"
    if "effortLevel" in model_settings:
        return "modelSettings.effortLevel 无效；应使用顶层 effortLevel 或 modelSettings.<model>.effortLevel"
    for model, value in model_settings.items():
        if not isinstance(value, dict):
            return f"modelSettings.{model} 必须是对象"
        if "effortLevel" in value and value["effortLevel"] not in _CLAUDE_EFFORTS:
            return f"modelSettings.{model}.effortLevel 无效"
    if "mcpServers" in document and not isinstance(document["mcpServers"], dict):
        return "mcpServers 必须是对象"
    return None


def _read(path: Path) -> tuple[str | None, str | None]:
    if not path.exists():
        return None, None
    # Existing installations may deliberately keep CODEX_HOME/config.toml as
    # a symlink to an administrator-managed file; the Installer already writes
    # through that link atomically.  Follow it for the same effective file the
    # runtime reads, while rejecting directories and other special paths.
    if not path.is_file():
        return None, "配置路径不是普通文件"
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return None, "配置文件超过 8 MiB 限制"
        return path.read_text("utf-8"), None
    except (OSError, UnicodeError) as exc:
        return None, f"读取失败：{type(exc).__name__}"


def audit_codex(path: Path, runtime: str | None = None) -> AuditResult:
    text, error = _read(path)
    if error:
        return AuditResult("异常", error)
    if text is None:
        return AuditResult("未配置", "没有 config.toml；Gateway 仍通过 SDK 启动参数提供运行设置")
    try:
        document = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return AuditResult("异常", f"TOML 无效：{type(exc).__name__}")
    if not isinstance(document, dict):
        return AuditResult("异常", "TOML 根节点必须是对象")
    provider_id = document.get("model_provider")
    providers = document.get("model_providers")
    if not isinstance(provider_id, str) or not provider_id:
        return AuditResult("异常", "缺少 model_provider")
    if not isinstance(providers, dict) or not isinstance(providers.get(provider_id), dict):
        return AuditResult("异常", f"model_provider={provider_id} 没有对应 model_providers 表")
    provider = providers[provider_id]
    if not isinstance(provider.get("name"), str) or not provider.get("name", "").strip():
        return AuditResult("异常", f"provider {provider_id} 缺少非空 name")
    if provider.get("wire_api", "responses") != "responses":
        return AuditResult("异常", f"provider {provider_id} 未使用 Responses API")
    base_url = provider.get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        return AuditResult("异常", f"provider {provider_id} 的 base_url 无效")
    env_key = provider.get("env_key")
    if env_key is not None and (not isinstance(env_key, str) or not env_key):
        return AuditResult("异常", "provider env_key 必须是非空字符串")
    effort = document.get("model_reasoning_effort")
    if effort is not None and (not isinstance(effort, str) or not effort):
        return AuditResult("异常", "model_reasoning_effort 类型无效")
    context_window = document.get("model_context_window")
    compact_limit = document.get("model_auto_compact_token_limit")
    if context_window is not None and (not isinstance(context_window, int) or context_window <= 0):
        return AuditResult("异常", "model_context_window 必须是正整数")
    if compact_limit is not None and (not isinstance(compact_limit, int) or compact_limit <= 0):
        return AuditResult("异常", "model_auto_compact_token_limit 必须是正整数")
    if isinstance(context_window, int) and isinstance(compact_limit, int) and compact_limit >= context_window:
        return AuditResult("异常", "自动压缩阈值必须小于上下文窗口")
    # This is a parser check against the exact bundled app-server binary used
    # by the Python SDK.  It catches unknown/deprecated keys without making a
    # network request.  The subprocess receives a copy in a private temp home
    # and never receives Gateway credentials.
    if runtime:
        try:
            with tempfile.TemporaryDirectory(prefix="codex-config-audit-") as directory:
                home = Path(directory)
                (home / "config.toml").write_text(text, encoding="utf-8")
                env = {"CODEX_HOME": str(home), "PATH": os.environ.get("PATH", "")}
                proc = subprocess.run(
                    [runtime, "app-server"],
                    input="",
                    text=True,
                    capture_output=True,
                    timeout=8,
                    env=env,
                )
                if proc.returncode:
                    detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:]
                    return AuditResult("异常", "bundled runtime 无法加载配置：" + (detail[0] if detail else "未知错误"))
                # Without --strict-config the official runtime deliberately
                # starts while ignoring keys introduced by a newer CLI.  That
                # is usable for Gateway turns, but must be visible instead of
                # being reported as fully valid.
                ignored = [line.strip() for line in (proc.stderr or '').splitlines()
                           if ' is ignored.' in line or ' is ignored' in line]
                if ignored:
                    return AuditResult("警告", "bundled runtime 可启动，但忽略配置项：" + "；".join(ignored)[:700])
        except (OSError, subprocess.TimeoutExpired) as exc:
            return AuditResult("未验证", f"无法运行 bundled runtime：{type(exc).__name__}")
    limits = "未在文件中固定上下文窗口（由模型/Runtime决定）"
    if isinstance(context_window, int):
        limits = f"context={context_window}, auto_compact={compact_limit or 'Runtime 默认'}（中转站实际上限仍需模型目录或请求验证）"
    effective = "Gateway Turn 覆盖 approval=never、sandbox=full-access、provider=codex_tg"
    return AuditResult("正常", f"{provider_id} / Responses / model={document.get('model', '未设置')}；{limits}；{effective}")


def audit_claude(path: Path, command: str | None = None) -> AuditResult:
    text, error = _read(path)
    if error:
        return AuditResult("异常", error)
    if text is None:
        return AuditResult("未配置", "没有 settings.json；Gateway 使用明确的 CLI 启动覆盖")
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return AuditResult("异常", f"JSON 无效：{type(exc).__name__}")
    if not isinstance(document, dict):
        return AuditResult("异常", "JSON 根节点必须是对象")
    structure_error = validate_claude_document(document)
    if structure_error:
        return AuditResult("异常", structure_error)
    if command:
        try:
            probe = subprocess.run([command, "--help"], capture_output=True, text=True, timeout=8)
            required = ("--permission-mode", "--dangerously-skip-permissions", "--permission-prompts",
                        "--setting-sources", "--strict-mcp-config", "--allowedTools", "--effort")
            if probe.returncode and not probe.stdout:
                return AuditResult("未验证", "Claude CLI --help 执行失败")
            missing = [flag for flag in required if flag not in probe.stdout]
            if missing:
                return AuditResult("异常", "Claude CLI 缺少 Gateway 所需参数：" + ", ".join(missing))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return AuditResult("未验证", f"无法读取 Claude CLI 参数：{type(exc).__name__}")
    effective = "Gateway 使用 bypassPermissions + skip-permissions + allowedTools=*；setting-sources 为空，文件 deny/hooks 不会覆盖无人值守策略"
    return AuditResult("正常", f"model={document.get('model', '由 Gateway --model 提供')}；{effective}")
