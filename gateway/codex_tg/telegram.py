from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


class TelegramError(RuntimeError):
    pass


class TelegramDeliveryUncertain(TelegramError):
    """Request may have succeeded; never automatically resend an ambiguous upload."""


def safe_error(text: str, token: str, *secrets: str) -> str:
    result = text.replace(token, "[REDACTED]") if token else text
    for secret in secrets:
        if secret and len(secret) >= 6:
            result = result.replace(secret, "[REDACTED]")
    result = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~-]+", r"\1[REDACTED]", result)
    result = re.sub(r"\b\d{6,15}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_BOT_TOKEN]", result)
    result = re.sub(r"\bsk-[A-Za-z0-9._-]{12,}\b", "[REDACTED_API_KEY]", result)
    return result


def split_text(text: str, limit: int = 3900) -> list[str]:
    text = text or "（Codex 未返回文本）"
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


def clean_filename(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:120] or "document.bin"


class Telegram:
    def __init__(self, token: str, proxy_url: str | None = None, proxy_kind: str = "none") -> None:
        self.token = token
        self.proxy_url = proxy_url
        self.proxy_kind = proxy_kind
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(40, connect=10),
            proxy=proxy_url,
            trust_env=False,
        )
        self.last_success = False
        self.last_error_code = "NOT_TESTED"

    def _network_failure(self, exc: Exception) -> str:
        message = str(exc).lower()
        if not self.proxy_url:
            return "DIRECT_FAILED"
        if self.proxy_kind == "subscription" and any(
            marker in message for marker in ("connection refused", "connect call failed", "all connection attempts failed")
        ):
            return "PROXY_DOWN"
        return "PROXY_UNAVAILABLE"

    async def network_status(self) -> dict[str, str]:
        if not self.proxy_url:
            return {
                "network": "DIRECT",
                "proxy": "未使用",
                "state": "正常" if self.last_success else self.last_error_code,
            }
        state = self.last_error_code
        if self.proxy_kind == "subscription":
            parsed = urlsplit(self.proxy_url)
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(parsed.hostname or "127.0.0.1", parsed.port or 7890),
                    timeout=0.4,
                )
                del reader
                writer.close()
                await writer.wait_closed()
            except Exception:
                state = "PROXY_DOWN"
            if state != "PROXY_DOWN":
                try:
                    async with httpx.AsyncClient(timeout=0.5, trust_env=False) as local:
                        response = await local.get(
                            "http://127.0.0.1:9090/providers/proxies/subscription"
                        )
                        response.raise_for_status()
                        provider = response.json()
                    proxies = provider.get("proxies") or []
                    alive = [item.get("alive") for item in proxies if "alive" in item]
                    if not proxies:
                        state = "SUBSCRIPTION_EMPTY"
                    elif alive and not any(alive):
                        state = "ALL_NODES_DOWN"
                except Exception:
                    if not self.last_success:
                        state = "PROXY_UNAVAILABLE"
                try:
                    refresh = json.loads(
                        Path("/etc/codex-tg-proxy/refresh-status.json").read_text("utf-8")
                    )
                    if refresh.get("ok") is False and self.last_success:
                        state = "REFRESH_FAILED_CACHED"
                except (OSError, ValueError):
                    pass
        if self.last_success and state in {"OK", "NOT_TESTED"}:
            state = "正常"
        labels = {
            "NOT_TESTED": "尚未测试",
            "DIRECT_FAILED": "Telegram 直连失败",
            "PROXY_DOWN": "本地代理未运行",
            "PROXY_UNAVAILABLE": "代理节点不可用",
            "TOKEN_ERROR": "Bot Token 错误",
            "SUBSCRIPTION_EMPTY": "订阅没有可识别节点",
            "ALL_NODES_DOWN": "所有代理节点不可用",
            "REFRESH_FAILED_CACHED": "订阅刷新失败，正在使用最后有效缓存",
        }
        return {
            "network": "PROXY",
            "proxy": labels.get(state, state),
            "state": labels.get(state, state),
        }

    async def close(self) -> None:
        await self.client.aclose()

    async def call(
        self, method: str, data: dict[str, Any] | None = None, files: dict[str, Any] | None = None
    ) -> Any:
        for attempt in range(5):
            try:
                if files:
                    for item in files.values():
                        if isinstance(item, tuple) and hasattr(item[1], 'seek'):
                            item[1].seek(0)
                    response = await self.client.post(f"{self.base}/{method}", data=data or {}, files=files,
                                                      timeout=httpx.Timeout(300, connect=10))
                else:
                    response = await self.client.post(f"{self.base}/{method}", json=data or {})
                payload = response.json()
            except Exception as exc:
                self.last_success = False
                self.last_error_code = self._network_failure(exc)
                if files and not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
                    raise TelegramDeliveryUncertain('Telegram 未确认文件发送结果；请核对聊天后从 /files 手动重发') from None
                if attempt == 4:
                    raise TelegramError(
                        safe_error(
                            f"Telegram network error ({self.last_error_code}): {exc}",
                            self.token,
                            self.proxy_url or "",
                        )
                    ) from exc
                await asyncio.sleep(2**attempt)
                continue
            if payload.get("ok"):
                self.last_success = True
                self.last_error_code = "OK"
                return payload.get("result")
            if files and response.status_code >= 500:
                raise TelegramDeliveryUncertain('Telegram 服务端失败，发送结果待确认；请从 /files 核对后重发')
            if response.status_code >= 500 and attempt < 4:
                await asyncio.sleep(min(2**attempt, 10))
                continue
            if response.status_code == 429 and attempt < 4:
                wait = int((payload.get("parameters") or {}).get("retry_after", 2))
                await asyncio.sleep(min(wait + 1, 30))
                continue
            description = safe_error(str(payload.get("description", "Telegram API failed")), self.token)
            self.last_success = False
            self.last_error_code = "TOKEN_ERROR" if response.status_code in {401, 404} else "API_ERROR"
            raise TelegramError(description)
        raise TelegramError("Telegram retry limit exceeded")

    async def get_updates(self, offset: int) -> list[dict[str, Any]]:
        return await self.call(
            "getUpdates",
            {"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]},
        )

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook", {"drop_pending_updates": False})

    async def send(self, chat_id: int, text: str) -> list[int]:
        message_ids: list[int] = []
        for part in split_text(text):
            result = await self.call(
                "sendMessage", {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            )
            message_ids.append(int(result["message_id"]))
        return message_ids

    async def send_keyboard(
        self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]]
    ) -> int:
        result = await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:3900],
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )
        return int(result["message_id"])

    async def edit_keyboard(
        self, chat_id: int, message_id: int, text: str, keyboard: list[list[dict[str, str]]]
    ) -> None:
        await self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:3900],
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )

    async def answer_callback(self, callback_id: str, text: str = "", show_alert: bool = False) -> None:
        await self.call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:180], "show_alert": show_alert},
        )

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError:
            # Telegram may reject deletion after its retention window or when
            # the bot lacks permission.  Secret handling still succeeds.
            return

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self.call(
                "editMessageText",
                {"chat_id": chat_id, "message_id": message_id, "text": text[:3900]},
            )
        except TelegramError as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> dict:
        with path.open("rb") as handle:
            result = await self.call(
                "sendDocument",
                {"chat_id": str(chat_id), "caption": caption[:900]},
                {"document": (path.name, handle, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')},
            )
        if not isinstance(result, dict) or not result.get('message_id') or not result.get('document', {}).get('file_id'):
            raise TelegramDeliveryUncertain('Telegram 返回未包含文件确认；请核对聊天后重发')
        return result

    async def file_info(self, file_id: str) -> dict[str, Any]:
        return await self.call("getFile", {"file_id": file_id})

    async def download(self, file_id: str, target: Path, max_bytes: int) -> None:
        info = await self.file_info(file_id)
        size = int(info.get("file_size") or 0)
        if size and size > max_bytes:
            raise TelegramError(f"文件超过限制（最大 {max_bytes // 1024 // 1024} MiB）")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".part")
        downloaded = 0
        try:
            async with self.client.stream("GET", f"{self.file_base}/{info['file_path']}") as response:
                response.raise_for_status()
                with temp.open("xb") as output:
                    async for chunk in response.aiter_bytes():
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise TelegramError("文件下载过程中超过大小限制")
                        output.write(chunk)
            temp.replace(target)
            target.chmod(0o600)
            self.last_success = True
            self.last_error_code = "OK"
        except Exception as exc:
            temp.unlink(missing_ok=True)
            self.last_success = False
            self.last_error_code = self._network_failure(exc)
            raise TelegramError(
                safe_error(
                    f"Telegram file download failed ({self.last_error_code}): {exc}",
                    self.token,
                    self.proxy_url or "",
                )
            ) from exc
