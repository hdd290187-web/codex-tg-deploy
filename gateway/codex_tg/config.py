from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def version_from_release_file(fallback: str) -> str:
    path = Path(os.getenv("CODEX_TG_VERSION_FILE", "/opt/codex-tg/current/VERSION"))
    try:
        value = path.read_text("utf-8").strip()
    except OSError:
        value = ""
    return value or fallback


@dataclass(frozen=True, slots=True)
class Config:
    telegram_token: str
    allowed_user_id: int
    allowed_chat_id: int
    model: str
    reasoning_effort: str
    plan_reasoning_effort: str
    telegram_network: str
    telegram_proxy_kind: str
    telegram_proxy_url: str | None
    release_base_url: str
    release_public_key_path: Path
    database_path: Path
    upload_dir: Path
    log_dir: Path
    health_port: int
    version: str
    max_upload_bytes: int
    upload_retention_days: int
    # Optional second-engine and search settings.  These fields have defaults so
    # older callers/tests that construct Config directly remain compatible.
    claude_model: str = "sonnet"
    claude_effort: str = "high"
    claude_base_url: str | None = None
    claude_api_key: str | None = None
    tavily_enabled: bool = False
    tavily_api_key: str | None = None
    environment_file: Path | None = None
    codex_prompt_enabled: bool = False
    adapter_port: int = 0

    @classmethod
    def from_env(cls) -> "Config":
        tavily_api_key = os.getenv("TAVILY_API_KEY", "").strip() or None
        configured_tavily = os.getenv("TAVILY_ENABLED", "").strip().lower()
        tavily_enabled = (
            bool(tavily_api_key)
            if not configured_tavily
            else configured_tavily not in {"0", "false", "off", "no"}
        )
        return cls(
            telegram_token=required("TELEGRAM_BOT_TOKEN"),
            allowed_user_id=int(required("ALLOWED_USER_ID")),
            allowed_chat_id=int(required("ALLOWED_CHAT_ID")),
            model=os.getenv("CODEX_TG_MODEL", "gpt-5.6-sol") or "gpt-5.6-sol",
            reasoning_effort=os.getenv("CODEX_TG_REASONING_EFFORT", "xhigh") or "xhigh",
            plan_reasoning_effort=os.getenv(
                "CODEX_TG_PLAN_REASONING_EFFORT", os.getenv("CODEX_TG_REASONING_EFFORT", "xhigh") or "xhigh"
            ).strip(),
            telegram_network=os.getenv("CODEX_TG_TELEGRAM_NETWORK", "DIRECT").strip().upper(),
            telegram_proxy_kind=os.getenv("CODEX_TG_TELEGRAM_PROXY_KIND", "none").strip().lower(),
            telegram_proxy_url=os.getenv("CODEX_TG_TELEGRAM_PROXY_URL", "").strip() or None,
            release_base_url=required("CODEX_TG_RELEASE_BASE_URL").rstrip("/"),
            release_public_key_path=Path(
                os.getenv("CODEX_TG_RELEASE_PUBLIC_KEY", "/etc/codex-tg-release.pub")
            ),
            database_path=Path(os.getenv("CODEX_TG_DATABASE", "/opt/codex-tg/data/gateway.db")),
            upload_dir=Path(os.getenv("CODEX_TG_UPLOAD_DIR", "/opt/codex-tg/data/uploads")),
            log_dir=Path(os.getenv("CODEX_TG_LOG_DIR", "/opt/codex-tg/logs")),
            health_port=int(os.getenv("CODEX_TG_HEALTH_PORT", "17654")),
            version=version_from_release_file(os.getenv("CODEX_TG_VERSION", "1.0.0")),
            max_upload_bytes=int(os.getenv("CODEX_TG_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))),
            upload_retention_days=max(1, int(os.getenv("CODEX_TG_UPLOAD_RETENTION_DAYS", "7"))),
            claude_model=os.getenv("CLAUDE_CODE_MODEL", "sonnet").strip()
            or "sonnet",
            claude_effort=os.getenv("CLAUDE_CODE_EFFORT", "high").strip() or "high",
            claude_base_url=os.getenv("ANTHROPIC_BASE_URL", "").strip() or None,
            claude_api_key=(
                os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
                or os.getenv("ANTHROPIC_API_KEY", "").strip()
                or None
            ),
            tavily_enabled=tavily_enabled,
            tavily_api_key=tavily_api_key,
            environment_file=Path(os.getenv("CODEX_TG_ENV_FILE", "/etc/codex-tg.env")),
            codex_prompt_enabled=os.getenv("CODEX_TG_CODEX_PROMPT_ENABLED", "0").strip().lower() not in {"0", "false", "off", "no"},
            adapter_port=int(os.getenv("CODEX_TG_ADAPTER_PORT", "0")),
        )
