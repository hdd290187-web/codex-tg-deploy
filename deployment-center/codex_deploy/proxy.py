from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import yaml


MAX_SUBSCRIPTION_BYTES = 5 * 1024 * 1024
SUPPORTED_URI_SCHEMES = {
    "anytls",
    "hysteria",
    "hysteria2",
    "hy2",
    "mieru",
    "naive+https",
    "socks",
    "socks5",
    "ss",
    "ssh",
    "ssr",
    "trojan",
    "tuic",
    "vless",
    "vmess",
    "wireguard",
}
URI_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://\S+$")


class SubscriptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SubscriptionInfo:
    format: str
    node_count: int


def _uri_count(text: str) -> tuple[int, int]:
    total = 0
    recognized = 0
    for line in text.replace("\r", "").split("\n"):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        match = URI_RE.fullmatch(value)
        if not match:
            continue
        total += 1
        if match.group(1).lower() in SUPPORTED_URI_SCHEMES:
            recognized += 1
    return total, recognized


def _decode_base64_subscription(text: str) -> str | None:
    compact = "".join(text.split())
    if len(compact) < 16 or not re.fullmatch(r"[A-Za-z0-9_+/=-]+", compact):
        return None
    padded = compact + "=" * (-len(compact) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded)
            decoded = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if "://" in decoded:
            return decoded
    return None


def classify_subscription(content: bytes) -> SubscriptionInfo:
    if not content or len(content) > MAX_SUBSCRIPTION_BYTES:
        raise SubscriptionError("订阅为空或超过 5 MiB 限制")
    try:
        text = content.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise SubscriptionError("订阅不是 UTF-8 文本") from exc

    parsed = None
    if re.search(r"(?m)^\s*proxies\s*:", text):
        if len(re.findall(r"(?m)(?:^|\s)[&*][A-Za-z0-9_-]+", text)) > 100:
            raise SubscriptionError("YAML alias 数量超过安全限制")
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("proxies"), list):
        proxies = [item for item in parsed["proxies"] if isinstance(item, dict)]
        recognized = sum(
            1
            for item in proxies
            if str(item.get("type", "")).strip().lower() in SUPPORTED_URI_SCHEMES | {"direct", "http"}
        )
        if not proxies or recognized == 0:
            raise SubscriptionError("YAML 中没有 Mihomo 可识别的代理节点")
        return SubscriptionInfo("Mihomo / Clash YAML", recognized)

    total, recognized = _uri_count(text)
    if total:
        if recognized == 0:
            raise SubscriptionError("URI 订阅中的协议暂不受 Mihomo 支持")
        return SubscriptionInfo("URI list", recognized)

    decoded = _decode_base64_subscription(text)
    if decoded is not None:
        total, recognized = _uri_count(decoded)
        if total and recognized:
            return SubscriptionInfo("Base64 URI list", recognized)
    raise SubscriptionError("不支持的订阅格式；需要 Mihomo/Clash YAML、URI 列表或 Base64 URI 列表")


def validate_subscription_url(value: str) -> str:
    value = value.strip()
    if any(character in value for character in "\r\n\x00"):
        raise ValueError("代理订阅 URL 包含无效控制字符")
    if len(value) > 4096:
        raise ValueError("代理订阅 URL 过长")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("代理订阅 URL 必须是完整的 HTTP(S) 地址")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("代理订阅 URL 不能包含 URL 用户凭据或片段")
    return value


async def _assert_public_host(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        raise SubscriptionError("订阅地址无效")
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise SubscriptionError("无法解析订阅服务器") from exc
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if not address.is_global:
            raise SubscriptionError("测试订阅不允许访问内网或本机地址")

    if not records:
        raise SubscriptionError('订阅服务器未返回地址')
    return records[0][4][0]


async def inspect_subscription(client: httpx.AsyncClient, url: str) -> SubscriptionInfo:
    current = validate_subscription_url(url)
    for _ in range(4):
        address = await _assert_public_host(current)
        parsed = urlsplit(current)
        ip_host = f'[{address}]' if ':' in address else address
        pinned = urlunsplit((parsed.scheme, ip_host + (f':{parsed.port}' if parsed.port else ''), parsed.path, parsed.query, ''))
        try:
            async with client.stream(
                "GET",
                pinned,
                headers={"User-Agent": "clash.meta", "Accept": "*/*", 'Host': parsed.netloc},
                extensions={'sni_hostname': parsed.hostname},
                follow_redirects=False,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise SubscriptionError("订阅重定向缺少目标地址")
                    current = validate_subscription_url(urljoin(current, location))
                    continue
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > MAX_SUBSCRIPTION_BYTES:
                        raise SubscriptionError("订阅超过 5 MiB 限制")
                return classify_subscription(bytes(buffer))
        except SubscriptionError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise SubscriptionError("无法读取代理订阅") from exc
    raise SubscriptionError("代理订阅重定向次数过多")
