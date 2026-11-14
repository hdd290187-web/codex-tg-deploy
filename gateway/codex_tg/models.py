"""Read model availability from the configured provider, never a CLI catalog."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from .credentials import validate_url

MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class ModelDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderModel:
    model: str
    display_name: str


class ModelCatalog:
    def __init__(self) -> None:
        self.cache: dict[str, tuple[float, list[ProviderModel]]] = {}

    @staticmethod
    def identity(engine: str, endpoint: dict) -> str:
        values = [engine, endpoint.get('base_url'), endpoint.get('api_key'), endpoint.get('auth')]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    def invalidate(self) -> None:
        self.cache.clear()

    async def options(self, engine: str, endpoint: dict, *, refresh: bool = False) -> list[ProviderModel]:
        if engine not in {'codex', 'claude'}:
            raise ModelDiscoveryError('未知引擎')
        if not endpoint.get('base_url') or not endpoint.get('api_key'):
            raise ModelDiscoveryError('尚未配置中转站，请先使用 /endpoint')
        identity = self.identity(engine, endpoint)
        cached = self.cache.get(identity)
        if cached and not refresh and time.monotonic() - cached[0] < 60:
            return list(cached[1])
        # A failed refresh must never leave a previous provider response usable.
        self.cache.pop(identity, None)
        try:
            models = await asyncio.wait_for(self._fetch(engine, endpoint), timeout=20)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise ModelDiscoveryError('模型目录请求超时') from None
        except httpx.HTTPError:
            raise ModelDiscoveryError('无法连接模型目录接口') from None
        if len(self.cache) >= 8:
            self.cache.clear()
        self.cache[identity] = (time.monotonic(), models)
        return list(models)

    @staticmethod
    def urls(base_url: str) -> list[str]:
        parsed = urlsplit(validate_url(base_url))
        path = parsed.path.rstrip('/')
        # Keep the configured routing prefix. Never guess another host/root.
        if path.endswith('/v1'):
            paths = [path + '/models', path[:-3] + '/models']
        else:
            paths = [path + '/v1/models', path + '/models']
        return [urlunsplit((parsed.scheme, parsed.netloc, p, '', '')) for p in paths]

    async def _fetch(self, engine: str, endpoint: dict) -> list[ProviderModel]:
        key = endpoint['api_key']
        headers = [{'Authorization': f'Bearer {key}'}]
        if engine == 'claude':
            headers = [{'x-api-key': key, 'anthropic-version': '2023-06-01'},
                       {'Authorization': f'Bearer {key}', 'anthropic-version': '2023-06-01'}]
            if endpoint.get('auth') == 'bearer':
                headers.reverse()
        failures = []
        async with httpx.AsyncClient(timeout=6, trust_env=False, follow_redirects=False) as client:
            for url in self.urls(endpoint['base_url']):
                for auth_index, auth in enumerate(headers):
                    try:
                        return await self._pages(client, url, auth, engine)
                    except ModelDiscoveryError as exc:
                        failures.append(str(exc))
                        # Same-origin auth compatibility only; do not generate
                        # candidate model names or make paid completion probes.
                        if str(exc) in {'HTTP 401', 'HTTP 403'} and auth_index + 1 < len(headers):
                            continue
                        if str(exc).startswith('HTTP ') and str(exc) not in {'HTTP 404', 'HTTP 405'}:
                            raise
                        break
        raise ModelDiscoveryError('目录接口不可用（' + '；'.join(dict.fromkeys(failures)) + '）')

    @staticmethod
    async def _pages(client, url: str, headers: dict, engine: str) -> list[ProviderModel]:
        models: dict[str, ProviderModel] = {}
        params: dict[str, str] = {}
        seen: set[str] = set()
        for _ in range(50):
            async with client.stream('GET', url, headers=headers, params=params) as response:
                if response.status_code != 200:
                    raise ModelDiscoveryError(f'HTTP {response.status_code}')
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise ModelDiscoveryError('模型目录响应过大')
            try:
                data = json.loads(body)
            except (ValueError, UnicodeError):
                raise ModelDiscoveryError('目录接口未返回 JSON') from None
            rows = data.get('data', data.get('models')) if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ModelDiscoveryError('无法识别模型目录格式')
            valid_rows = 0
            for row in rows:
                item = {'id': row} if isinstance(row, str) else row
                if not isinstance(item, dict):
                    continue
                model = item.get('id', item.get('model'))
                if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
                    continue
                valid_rows += 1
                if item.get('available') is False or item.get('enabled') is False:
                    continue
                protocols = item.get('supported_endpoint_types')
                if isinstance(protocols, list) and protocols:
                    expected = {'responses', 'openai', 'openai-responses'} if engine == 'codex' else {'messages', 'anthropic'}
                    if not expected.intersection(protocols):
                        continue
                label = item.get('display_name') or model
                label = ' '.join(str(label).split())[:100]
                models[model] = ProviderModel(model, label)
            if rows and not valid_rows:
                raise ModelDiscoveryError('模型目录没有有效的模型 ID')
            if not isinstance(data, dict) or not data.get('has_more'):
                return list(models.values())
            cursor = data.get('last_id')
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ModelDiscoveryError('模型目录分页无效')
            seen.add(cursor)
            params = {'after_id': cursor}
        raise ModelDiscoveryError('模型目录分页超出上限')
