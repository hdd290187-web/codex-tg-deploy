from __future__ import annotations

import asyncio
import base64
import json
import hashlib
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives import serialization
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, field_validator, model_validator
import tomlkit

from .config import MODEL_EFFORTS, PLAN_EFFORTS, PREFIX, Settings
from .db import Database
from .proxy import (
    SubscriptionError,
    inspect_subscription,
    validate_subscription_url,
)
from .security import SecretBox, random_token, redact, token_hash, verify_password
from .telegram import TelegramAPI, TelegramError


LOG = logging.getLogger("codex_deploy")
PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Environment(
    loader=FileSystemLoader(PACKAGE_DIR / "templates"),
    autoescape=select_autoescape(("html", "xml")),
)
COOKIE_NAME = "codex_deploy_session"
BOT_TOKEN_RE = re.compile(r"^\d{6,15}:[A-Za-z0-9_-]{20,}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
CODEX_PROMPT_MAX_BYTES = 128 * 1024
CONFIG_TEMPLATE_MAX_BYTES = 1024 * 1024


def default_config_template(engine: str) -> str:
    if engine == 'codex':
        return '''model_provider = "codex_tg"
model = "gpt-5.6-sol"
model_reasoning_effort = "xhigh"
plan_mode_reasoning_effort = "xhigh"
model_reasoning_summary = "detailed"
model_verbosity = "high"
model_context_window = 1050000
model_auto_compact_token_limit = 850000
web_search = "live"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.codex_tg]
name = "Codex TG Relay"
base_url = "https://api.openai.com/v1"
env_key = "CODEX_TG_API_KEY"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
'''
    return '''{
  "model": "sonnet",
  "permissions": {
    "defaultMode": "bypassPermissions"
  },
  "effortLevel": "high",
  "modelSettings": {},
  "mcpServers": {}
}
'''


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class TelegramBody(BaseModel):
    bot_token: str = Field(min_length=20, max_length=256)

    @field_validator("bot_token")
    @classmethod
    def valid_token(cls, value: str) -> str:
        value = value.strip()
        if not BOT_TOKEN_RE.fullmatch(value):
            raise ValueError("Telegram Bot Token 格式无效")
        return value


class SubscriptionBody(BaseModel):
    subscription_url: str = Field(min_length=8, max_length=4096)

    @field_validator("subscription_url")
    @classmethod
    def valid_subscription_url(cls, value: str) -> str:
        return validate_subscription_url(value)


class ConfigTemplateBody(BaseModel):
    engine: str
    content: str = Field(max_length=CONFIG_TEMPLATE_MAX_BYTES)

    @field_validator('engine')
    @classmethod
    def valid_engine(cls, value: str) -> str:
        if value not in {'codex', 'claude'}:
            raise ValueError('engine must be codex or claude')
        return value

    @model_validator(mode='after')
    def valid_content(self):
        try:
            if self.engine == 'codex':
                value = tomlkit.loads(self.content)
            else:
                value = json.loads(self.content)
            value = value.unwrap() if hasattr(value, 'unwrap') else value
            if not isinstance(value, dict):
                raise ValueError('配置根必须是对象')
            if self.engine == 'claude':
                effort = value.get('effortLevel')
                if effort is not None and effort not in {'low', 'medium', 'high', 'xhigh'}:
                    raise ValueError('Claude 顶层 effortLevel 只接受 low/medium/high/xhigh')
                model_settings = value.get('modelSettings', {})
                if not isinstance(model_settings, dict) or 'effortLevel' in model_settings:
                    raise ValueError('Claude modelSettings 必须按模型名映射，不能直接放 effortLevel')
                for model_name, model_value in model_settings.items():
                    if not isinstance(model_value, dict):
                        raise ValueError(f'Claude modelSettings.{model_name} 必须是对象')
                    saved_effort = model_value.get('effortLevel')
                    if saved_effort is not None and saved_effort not in {'low', 'medium', 'high', 'xhigh'}:
                        raise ValueError(f'Claude modelSettings.{model_name}.effortLevel 无效')
            def inspect(node):
                if isinstance(node, dict):
                    for key, child in node.items():
                        lowered = str(key).lower()
                        if any(marker in lowered for marker in ('api_key', 'bot_token', 'password', 'secret', 'private_key', 'proxy_subscription')):
                            raise ValueError('模板不能包含 API Key、Token、密码或私钥字段')
                        inspect(child)
                elif isinstance(node, list):
                    for child in node:
                        inspect(child)
                elif isinstance(node, str) and any(marker in node.lower() for marker in ('sk-', 'tvly-', '-----begin ')):
                    raise ValueError('模板疑似包含凭据')
            inspect(value)
        except Exception as exc:
            raise ValueError(f'配置格式无效：{type(exc).__name__}') from None
        return self


class DeploymentBody(BaseModel):
    base_url: str = Field(default="", max_length=512)
    api_key: str = Field(default="", max_length=4096)
    model: str = Field(default="gpt-5.6-sol", max_length=128)
    reasoning_effort: str = "xhigh"
    plan_reasoning_effort: str | None = None
    codex_pre_instructions_enabled: bool = False
    codex_pre_instructions: str = Field(default="", max_length=CODEX_PROMPT_MAX_BYTES)
    claude_base_url: str = Field(default='', max_length=512)
    claude_api_key: str = Field(default='', max_length=4096)
    claude_model: str = Field(default='sonnet', max_length=128)
    claude_effort: str = 'high'
    tavily_api_key: str = Field(default='', max_length=4096)
    tavily_enabled: bool | None = None
    telegram_network_mode: str = "auto"
    proxy_subscription_url: str | None = Field(default=None, max_length=4096)
    custom_proxy_url: str | None = Field(default=None, max_length=2048)

    @field_validator("base_url", "claude_base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        if not value.strip():
            return ""
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Base URL 必须是完整的 HTTP(S) 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Base URL 不能包含凭据、查询参数或片段")
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    @field_validator("api_key", "claude_api_key", "tavily_api_key")
    @classmethod
    def clean_api_key(cls, value: str) -> str:
        value = value.strip()
        if any(char in value for char in "\r\n\x00"):
            raise ValueError("API Key 格式无效")
        return value

    @field_validator("codex_pre_instructions")
    @classmethod
    def valid_codex_pre_instructions(cls, value: str) -> str:
        if any(ord(c) < 32 and c not in '\t\r\n' for c in value) or '\x7f' in value:
            raise ValueError("Codex 前置 Instructions 包含非文本控制字符")
        try:
            encoded = value.encode('utf-8')
        except UnicodeError:
            raise ValueError('Codex 前置 Instructions 必须是有效 UTF-8 文本') from None
        if len(encoded) > CODEX_PROMPT_MAX_BYTES:
            raise ValueError("Codex 前置 Instructions 不能超过 128 KiB")
        return value

    @field_validator("model", "claude_model")
    @classmethod
    def valid_model(cls, value: str) -> str:
        value = value.strip()
        if not MODEL_RE.fullmatch(value):
            raise ValueError("模型名称格式无效")
        return value

    @field_validator("reasoning_effort")
    @classmethod
    def valid_effort(cls, value: str) -> str:
        if value not in MODEL_EFFORTS:
            raise ValueError("不支持的推理强度")
        return value

    @field_validator("plan_reasoning_effort")
    @classmethod
    def valid_plan_effort(cls, value: str | None) -> str | None:
        if value is not None and value not in PLAN_EFFORTS:
            raise ValueError("不支持的 Plan Mode 推理强度")
        return value

    @field_validator('claude_effort')
    @classmethod
    def valid_claude_effort(cls, value: str) -> str:
        if value not in {'low', 'medium', 'high', 'xhigh', 'max'}:
            raise ValueError('Claude effort 可选 low/medium/high/xhigh/max')
        return value

    @field_validator("telegram_network_mode")
    @classmethod
    def valid_network_mode(cls, value: str) -> str:
        if value not in {"auto", "direct", "subscription", "custom"}:
            raise ValueError("不支持的 Telegram 网络模式")
        return value

    @field_validator("proxy_subscription_url")
    @classmethod
    def valid_proxy_subscription_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return validate_subscription_url(value)

    @field_validator("custom_proxy_url")
    @classmethod
    def valid_custom_proxy_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if any(character in value for character in "\r\n\x00"):
            raise ValueError("自定义代理 URL 包含无效控制字符")
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError("自定义代理仅支持 SOCKS5 或 HTTP(S)")
        if not parsed.hostname or parsed.fragment or parsed.query or parsed.path not in {"", "/"}:
            raise ValueError("自定义代理 URL 格式无效")
        try:
            if not 1 <= (parsed.port or 0) <= 65535:
                raise ValueError("自定义代理必须包含有效端口")
        except ValueError as exc:
            raise ValueError("自定义代理端口无效") from exc
        return value

    @model_validator(mode="after")
    def network_fields_match_mode(self) -> "DeploymentBody":
        if self.telegram_network_mode == "subscription" and not self.proxy_subscription_url:
            raise ValueError("订阅代理模式必须填写代理订阅 URL")
        if self.telegram_network_mode == "custom" and not self.custom_proxy_url:
            raise ValueError("自定义代理模式必须填写代理 URL")
        if self.telegram_network_mode != "custom":
            self.custom_proxy_url = None
        if self.telegram_network_mode not in {"auto", "subscription"}:
            self.proxy_subscription_url = None
        return self


class ProgressBody(BaseModel):
    stage: str = Field(min_length=1, max_length=48)
    detail: str = Field(default="", max_length=240)


class FinishBody(BaseModel):
    success: bool
    error: str | None = Field(default=None, max_length=1000)


class ClaimBody(BaseModel):
    hostname: str = Field(default="unknown", max_length=255)
    os_id: str = Field(default="unknown", max_length=64)
    os_version: str = Field(default="unknown", max_length=64)
    architecture: str = Field(default="unknown", max_length=64)


@dataclass(slots=True)
class AuthSession:
    id_hash: str
    csrf_hash: str


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def render(name: str, **context: Any) -> HTMLResponse:
    return HTMLResponse(TEMPLATES.get_template(name).render(prefix=PREFIX, **context))


def public_key_pem(settings: Settings) -> str:
    key = serialization.load_pem_public_key(settings.signing_public_key_path.read_bytes())
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


async def cleanup_loop(app: FastAPI) -> None:
    while True:
        try:
            app.state.db.cleanup()
        except Exception:
            LOG.exception("periodic cleanup failed")
        await asyncio.sleep(60)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        settings.release_dir.mkdir(parents=True, exist_ok=True)
        app.state.settings = settings
        app.state.db = Database(settings.database_path)
        app.state.box = SecretBox(settings.master_key)
        app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8))
        cleanup_task = asyncio.create_task(cleanup_loop(app))
        try:
            yield
        finally:
            cleanup_task.cancel()
            await app.state.http.aclose()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Codex TG Deployment Center", docs_url=None, redoc_url=None, lifespan=lifespan)
    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={'detail': [{'loc': list(e['loc']), 'msg': str(e['msg']), 'type': e['type']} for e in exc.errors()]})

    app.mount(f"{PREFIX}/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.mount(f"{PREFIX}/releases", StaticFiles(directory=settings.release_dir, check_dir=False), name="releases")

    def require_session(request: Request) -> AuthSession:
        raw = request.cookies.get(COOKIE_NAME, "")
        if not raw:
            raise HTTPException(status_code=401, detail="未登录")
        hashed = token_hash(raw, settings.token_pepper)
        row = request.app.state.db.session(hashed)
        if not row:
            raise HTTPException(status_code=401, detail="会话已过期")
        return AuthSession(id_hash=hashed, csrf_hash=row["csrf_hash"])

    def require_csrf(
        request: Request,
        session: AuthSession = Depends(require_session),
        x_csrf_token: str | None = Header(default=None),
    ) -> AuthSession:
        if not x_csrf_token:
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        supplied = token_hash(x_csrf_token, settings.token_pepper)
        if not __import__("hmac").compare_digest(supplied, session.csrf_hash):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        return session

    def require_install_session(request: Request, deployment_id: str) -> dict[str, Any]:
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="installation session required")
        raw = authorization[7:]
        hashed = token_hash(raw, settings.token_pepper)
        row = request.app.state.db.install_session(deployment_id, hashed)
        if not row:
            raise HTTPException(status_code=401, detail="installation session expired")
        return dict(row)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if request.url.path.startswith(f"{PREFIX}/releases/"):
            response.headers["Cache-Control"] = "public, max-age=60"
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.get(f"{PREFIX}/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        raw = request.cookies.get(COOKIE_NAME, "")
        row = request.app.state.db.session(token_hash(raw, settings.token_pepper)) if raw else None
        if not row:
            return render("login.html", insecure=not settings.secure_cookie)
        csrf = random_token()
        csrf_hashed = token_hash(csrf, settings.token_pepper)
        with request.app.state.db.connect() as conn:
            conn.execute("UPDATE sessions SET csrf_hash=? WHERE id_hash=?", (csrf_hashed, row["id_hash"]))
        latest_version = "未发布"
        latest_path = settings.release_dir / "latest.json"
        if latest_path.exists():
            try:
                latest_version = json.loads(latest_path.read_text("utf-8"))["version"]
            except Exception:
                latest_version = "清单错误"
        return render(
            "index.html",
            csrf=csrf,
            insecure=not settings.secure_cookie,
            latest_version=latest_version,
            efforts=MODEL_EFFORTS,
            plan_efforts=PLAN_EFFORTS,
            recent=request.app.state.db.recent(),
        )

    @app.get(f"{PREFIX}")
    async def home_redirect() -> RedirectResponse:
        return RedirectResponse(f"{PREFIX}/", status_code=308)

    @app.get(f"{PREFIX}/config", response_class=HTMLResponse)
    async def config_page(request: Request) -> Response:
        raw = request.cookies.get(COOKIE_NAME, '')
        row = request.app.state.db.session(token_hash(raw, settings.token_pepper)) if raw else None
        if not row:
            return render('login.html', insecure=not settings.secure_cookie)
        csrf = random_token()
        with request.app.state.db.connect() as conn:
            conn.execute('UPDATE sessions SET csrf_hash=? WHERE id_hash=?', (token_hash(csrf, settings.token_pepper), row['id_hash']))
        return render('config.html', csrf=csrf, insecure=not settings.secure_cookie)

    def config_payload() -> tuple[bytes, bytes]:
        templates = {}
        for engine in ('codex', 'claude'):
            item = app.state.db.config_template(engine)
            if item is None:
                default = default_config_template(engine)
                item = {'version': 0, 'content': default, 'sha256': hashlib.sha256(default.encode()).hexdigest()}
            templates[engine] = {'version': item['version'], 'content': item['content'], 'sha256': item['sha256']}
        body = json.dumps({'format': 1, **templates}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        private = serialization.load_pem_private_key(settings.config_signing_private_key_path.read_bytes(), password=None)
        return body, private.sign(body)

    @app.get(f"{PREFIX}/config-templates/latest.json")
    async def config_template_manifest() -> Response:
        body, _ = config_payload()
        return Response(body, media_type='application/json', headers={'Cache-Control': 'no-store'})

    @app.get(f"{PREFIX}/config-templates/latest.json.sig")
    async def config_template_signature() -> Response:
        _, signature = config_payload()
        return Response(signature, media_type='application/octet-stream', headers={'Cache-Control': 'no-store'})

    @app.get(f"{PREFIX}/api/config/templates")
    async def config_templates(_session: AuthSession = Depends(require_session)) -> dict[str, Any]:
        result = {}
        for engine in ('codex', 'claude'):
            rows = app.state.db.config_templates(engine)
            if not rows:
                content = default_config_template(engine)
                rows = [{'engine': engine, 'version': 0, 'content': content,
                         'sha256': hashlib.sha256(content.encode()).hexdigest(), 'created_at': 0}]
            result[engine] = rows
        return result

    @app.get(f"{PREFIX}/api/config/templates/{{engine}}/{{version}}")
    async def config_template_detail(engine: str, version: int, request: Request, _session: AuthSession = Depends(require_session)) -> dict[str, Any]:
        if engine not in {'codex', 'claude'}:
            raise HTTPException(status_code=404, detail='引擎不存在')
        item = app.state.db.config_template(engine, version)
        if item is None and version == 0:
            content = default_config_template(engine)
            item = {'engine': engine, 'version': 0, 'content': content,
                    'sha256': hashlib.sha256(content.encode()).hexdigest(), 'created_at': 0}
        if not item:
            raise HTTPException(status_code=404, detail='模板版本不存在')
        return item

    @app.post(f"{PREFIX}/api/config/templates")
    async def save_config_template(request: Request, body: ConfigTemplateBody, _session: AuthSession = Depends(require_csrf)) -> dict[str, Any]:
        sha = hashlib.sha256(body.content.encode()).hexdigest()
        version = app.state.db.put_config_template(body.engine, body.content, sha)
        return {'engine': body.engine, 'version': version, 'sha256': sha}

    @app.post(f"{PREFIX}/api/config/templates/{{engine}}/{{version}}/rollback")
    async def rollback_config_template(request: Request, engine: str, version: int, _session: AuthSession = Depends(require_csrf)) -> dict[str, Any]:
        item = app.state.db.config_template(engine, version)
        if not item:
            raise HTTPException(status_code=404, detail='模板版本不存在')
        sha = hashlib.sha256(item['content'].encode()).hexdigest()
        new_version = app.state.db.put_config_template(engine, item['content'], sha)
        return {'engine': engine, 'version': new_version, 'rolled_back_from': version}

    @app.post(f"{PREFIX}/api/login")
    async def login(request: Request, body: LoginBody) -> dict[str, Any]:
        origin = request.headers.get("Origin")
        expected_origin = f"{urlsplit(settings.public_base_url).scheme}://{urlsplit(settings.public_base_url).netloc}"
        if origin and origin != expected_origin:
            raise HTTPException(status_code=403, detail="来源校验失败")
        ip = client_ip(request)
        failures = request.app.state.db.failed_login_count(ip, int(time.time()) - 900)
        if failures >= 8:
            raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")
        if not verify_password(settings.password_hash, body.password):
            request.app.state.db.record_failed_login(ip)
            await asyncio.sleep(min(0.25 * (failures + 1), 2.0))
            raise HTTPException(status_code=401, detail="密码错误")
        request.app.state.db.clear_failed_logins(ip)
        sid = random_token()
        csrf = random_token()
        request.app.state.db.create_session(
            token_hash(sid, settings.token_pepper),
            token_hash(csrf, settings.token_pepper),
            ip,
            int(time.time()) + settings.session_seconds,
        )
        response = Response(content=json.dumps({"ok": True}), media_type="application/json")
        response.set_cookie(
            COOKIE_NAME,
            sid,
            max_age=settings.session_seconds,
            httponly=True,
            secure=settings.secure_cookie,
            samesite="strict",
            path=f"{PREFIX}/",
        )
        return response

    @app.post(f"{PREFIX}/api/logout")
    async def logout(
        request: Request, session: AuthSession = Depends(require_csrf)
    ) -> Response:
        request.app.state.db.delete_session(session.id_hash)
        response = Response(content=json.dumps({"ok": True}), media_type="application/json")
        response.delete_cookie(COOKIE_NAME, path=f"{PREFIX}/")
        return response

    @app.post(f"{PREFIX}/api/telegram/verify")
    async def verify_telegram(
        request: Request,
        body: TelegramBody,
        session: AuthSession = Depends(require_csrf),
    ) -> dict[str, Any]:
        api = TelegramAPI(body.bot_token, request.app.state.http)
        try:
            bot, offset, removed = await api.prepare_binding()
        except TelegramError as exc:
            raise HTTPException(status_code=400, detail=redact(str(exc), body.bot_token)) from exc
        username = str(bot.get("username", ""))
        if not username:
            raise HTTPException(status_code=400, detail="该 Bot 没有 username")
        request.app.state.db.put_binding(
            {
                "session_hash": session.id_hash,
                "token_encrypted": request.app.state.box.seal(
                    {"bot_token": body.bot_token}, purpose=f"telegram:{session.id_hash}"
                ),
                "bot_id": int(bot["id"]),
                "username": username,
                "display_name": str(bot.get("first_name", username)),
                "update_offset": offset,
                "expires_at": int(time.time()) + settings.deployment_seconds,
            }
        )
        return {
            "ok": True,
            "bot": {"id": int(bot["id"]), "username": username, "name": bot.get("first_name", username)},
            "webhook_removed": removed,
        }

    @app.get(f"{PREFIX}/api/telegram/bind")
    async def bind_telegram(
        request: Request, session: AuthSession = Depends(require_session)
    ) -> dict[str, Any]:
        row = request.app.state.db.binding(session.id_hash)
        if not row:
            return {"verified": False, "bound": False}
        if row["user_id"] is not None:
            return {
                "verified": True,
                "bound": True,
                "bot": {"id": row["bot_id"], "username": row["username"], "name": row["display_name"]},
                "user_id": row["user_id"],
                "chat_id": row["chat_id"],
            }
        secret = request.app.state.box.open(
            row["token_encrypted"], purpose=f"telegram:{session.id_hash}"
        )
        api = TelegramAPI(secret["bot_token"], request.app.state.http)
        try:
            offset, user_id, chat_id = await api.poll_start(int(row["update_offset"]))
        except TelegramError as exc:
            raise HTTPException(status_code=409, detail=redact(str(exc), secret["bot_token"])) from exc
        request.app.state.db.update_binding_poll(session.id_hash, offset, user_id, chat_id)
        return {
            "verified": True,
            "bound": user_id is not None,
            "bot": {"id": row["bot_id"], "username": row["username"], "name": row["display_name"]},
            "user_id": user_id,
            "chat_id": chat_id,
        }

    @app.post(f"{PREFIX}/api/proxy/subscription/test")
    async def test_proxy_subscription(
        request: Request,
        body: SubscriptionBody,
        _session: AuthSession = Depends(require_csrf),
    ) -> dict[str, Any]:
        try:
            info = await inspect_subscription(request.app.state.http, body.subscription_url)
        except SubscriptionError as exc:
            raise HTTPException(
                status_code=400,
                detail=redact(str(exc), body.subscription_url),
            ) from exc
        return {
            "ok": True,
            "format": info.format,
            "node_count": info.node_count,
        }

    @app.post(f"{PREFIX}/api/deployments")
    async def create_deployment(
        request: Request,
        body: DeploymentBody,
        session: AuthSession = Depends(require_csrf),
    ) -> dict[str, Any]:
        binding = request.app.state.db.binding(session.id_hash)
        if not binding or binding["user_id"] is None or binding["chat_id"] is None:
            raise HTTPException(status_code=409, detail="请先完成 Telegram Bot 验证和 /start 绑定")
        binding_secret = request.app.state.box.open(
            binding["token_encrypted"], purpose=f"telegram:{session.id_hash}"
        )
        manifest_path = settings.release_dir / "latest.json"
        if not manifest_path.exists():
            raise HTTPException(status_code=503, detail="Gateway Release 尚未发布")
        manifest = json.loads(manifest_path.read_text("utf-8"))
        deployment_id = uuid.uuid4().hex[:12]
        raw_token = random_token(32)
        now = int(time.time())
        payload = {
            "base_url": body.base_url,
            "api_key": body.api_key,
            "model": body.model,
            "reasoning_effort": body.reasoning_effort,
            "plan_reasoning_effort": body.plan_reasoning_effort or body.reasoning_effort,
            "codex_pre_instructions_enabled": bool(body.codex_pre_instructions_enabled and body.codex_pre_instructions),
            "codex_pre_instructions": body.codex_pre_instructions,
            "claude_base_url": body.claude_base_url,
            "claude_api_key": body.claude_api_key,
            "claude_model": body.claude_model,
            "claude_effort": body.claude_effort,
            "tavily_api_key": body.tavily_api_key,
            "tavily_enabled": bool(body.tavily_api_key)
            and (body.tavily_enabled if body.tavily_enabled is not None else True),
            "telegram_bot_token": binding_secret["bot_token"],
            "allowed_user_id": int(binding["user_id"]),
            "allowed_chat_id": int(binding["chat_id"]),
            "telegram_network_mode": body.telegram_network_mode,
            "proxy_subscription_url": body.proxy_subscription_url or "",
            "custom_proxy_url": body.custom_proxy_url or "",
            "gateway_version": manifest["version"],
        }
        templates = json.loads(config_payload()[0])
        payload["codex_config_template"] = templates["codex"]["content"]
        payload["codex_config_template_version"] = templates["codex"]["version"]
        payload["claude_config_template"] = templates["claude"]["content"]
        payload["claude_config_template_version"] = templates["claude"]["version"]
        request.app.state.db.create_deployment(
            {
                "id": deployment_id,
                "token_hash": token_hash(raw_token, settings.token_pepper),
                "secret_encrypted": request.app.state.box.seal(
                    payload, purpose=f"deployment:{deployment_id}"
                ),
                "gateway_version": manifest["version"],
                "created_at": now,
                "expires_at": now + settings.deployment_seconds,
            }
        )
        command = f"curl -fsSL {settings.public_base_url}/i/{raw_token} | bash"
        return {
            "id": deployment_id,
            "command": command,
            "expires_at": now + settings.deployment_seconds,
            "version": manifest["version"],
        }

    @app.get(f"{PREFIX}/api/deployments/{{deployment_id}}")
    async def deployment_status(
        request: Request,
        deployment_id: str,
        _session: AuthSession = Depends(require_session),
    ) -> dict[str, Any]:
        row = request.app.state.db.deployment(deployment_id)
        if not row:
            raise HTTPException(status_code=404, detail="部署任务不存在")
        return {
            "id": row["id"],
            "status": row["status"],
            "version": row["gateway_version"],
            "expires_at": row["expires_at"],
            "progress": json.loads(row["progress_json"]),
            "error": row["error_summary"],
        }

    @app.get(f"{PREFIX}/i/{{raw_token}}", response_class=PlainTextResponse)
    async def installer(request: Request, raw_token: str) -> PlainTextResponse:
        if len(raw_token) < 40:
            raise HTTPException(status_code=404)
        row = request.app.state.db.deployment_by_token(token_hash(raw_token, settings.token_pepper))
        if not row or row["status"] != "WAITING" or row["expires_at"] < int(time.time()):
            raise HTTPException(status_code=410, detail="deployment token expired or used")
        template = settings.installer_template.read_text("utf-8")
        public_key_encoded = base64.b64encode(public_key_pem(settings).encode("ascii")).decode("ascii")
        script = (
            template.replace("__PUBLIC_BASE_URL__", settings.public_base_url)
            .replace("__CLAIM_TOKEN__", raw_token)
            .replace("__RELEASE_PUBLIC_KEY_B64__", public_key_encoded)
        )
        return PlainTextResponse(script, media_type="text/x-shellscript")

    @app.post(f"{PREFIX}/api/install/claim/{{raw_token}}")
    async def claim(request: Request, raw_token: str, body: ClaimBody) -> dict[str, Any]:
        install_token = random_token(32)
        install_hash = token_hash(install_token, settings.token_pepper)
        row = request.app.state.db.claim(
            token_hash(raw_token, settings.token_pepper),
            install_hash,
            int(time.time()) + settings.install_session_seconds,
        )
        if not row:
            raise HTTPException(status_code=410, detail="deployment token expired or already used")
        try:
            payload = request.app.state.box.open(
                row["secret_encrypted"], purpose=f"deployment:{row['id']}"
            )
        finally:
            request.app.state.db.destroy_claimed_secret(row["id"])
        LOG.info(
            "deployment claimed id=%s host=%s os=%s/%s arch=%s",
            row["id"],
            body.hostname,
            body.os_id,
            body.os_version,
            body.architecture,
        )
        return {
            "deployment_id": row["id"],
            "installation_token": install_token,
            "release_base_url": f"{settings.public_base_url}/releases",
            "release_manifest_url": (
                f"{settings.public_base_url}/releases/codex-tg-{payload['gateway_version']}.json"
            ),
            **payload,
            "mihomo_base_url": f"{settings.public_base_url}/releases",
        }

    @app.post(f"{PREFIX}/api/install/{{deployment_id}}/progress")
    async def install_progress(request: Request, deployment_id: str, body: ProgressBody) -> dict[str, bool]:
        require_install_session(request, deployment_id)
        request.app.state.db.add_progress(deployment_id, body.stage, body.detail)
        return {"ok": True}

    @app.post(f"{PREFIX}/api/install/{{deployment_id}}/finish")
    async def install_finish(request: Request, deployment_id: str, body: FinishBody) -> dict[str, bool]:
        require_install_session(request, deployment_id)
        if body.error:
            safe_error = redact(body.error)[:500]
        else:
            safe_error = None
        request.app.state.db.add_progress(
            deployment_id, "SUCCESS" if body.success else "FAILED", safe_error or ""
        )
        request.app.state.db.finish(deployment_id, body.success, safe_error)
        LOG.info("deployment finished id=%s success=%s", deployment_id, body.success)
        return {"ok": True}

    @app.get(f"{PREFIX}/health")
    async def health(request: Request) -> dict[str, Any]:
        latest = settings.release_dir / "latest.json"
        return {
            "status": "ok",
            "database": request.app.state.db.path.exists(),
            "release": json.loads(latest.read_text("utf-8"))["version"] if latest.exists() else None,
        }

    return app
