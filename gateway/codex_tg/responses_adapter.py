from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
import zlib
from urllib.parse import urlsplit, urlunsplit

import h11
import httpx

from .credentials import validate_url
from .pre_instructions import PromptStore

LOG = logging.getLogger('codex_tg.responses_adapter')
MAX_BODY = 128 * 1024 * 1024  # Includes base64 image inputs and long conversations.
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}


def prepend_instructions(body: bytes, prompt: str) -> bytes:
    """Replace one top-level JSON value; every other byte stays unchanged."""
    if not prompt:
        return body
    text = body.decode('utf-8')
    decoder = json.JSONDecoder()
    parsed = decoder.decode(text)
    if not isinstance(parsed, dict):
        raise ValueError('Responses body must be a JSON object')
    index = text.index('{') + 1
    spans = []
    while True:
        while text[index].isspace():
            index += 1
        if text[index] == '}':
            end = index
            break
        key, index = decoder.raw_decode(text, index)
        while text[index].isspace():
            index += 1
        index += 1  # colon; the whole document was already validated above.
        while text[index].isspace():
            index += 1
        start = index
        value, index = decoder.raw_decode(text, index)
        if key == 'instructions':
            spans.append((start, index, value))
        while text[index].isspace():
            index += 1
        if text[index] == ',':
            index += 1
    if len(spans) > 1:
        raise ValueError('Duplicate instructions fields')
    if spans:
        start, end, original = spans[0]
        if original is not None and not isinstance(original, str):
            raise ValueError('instructions must be a string or null')
        combined = prompt + '\n\n' + original if original else prompt
        return (text[:start] + json.dumps(combined, ensure_ascii=False) + text[end:]).encode('utf-8')
    field = (',' if parsed else '') + '"instructions":' + json.dumps(prompt, ensure_ascii=False)
    return (text[:end] + field + text[end:]).encode('utf-8')


def filtered_headers(headers):
    connection = set()
    for name, value in headers:
        if name.lower() == b'connection':
            connection.update(x.strip().lower() for x in value.decode('latin-1').split(','))
    removed = HOP_HEADERS | connection
    return [(k, v) for k, v in headers if k.decode('ascii').lower() not in removed]


class ResponsesAdapter:
    """One authenticated loopback relay per Codex runtime. No shared upstream."""

    def __init__(self, prompt_store: PromptStore, port: int = 0):
        self.prompt_store = prompt_store
        self.port = port
        self.token = secrets.token_urlsafe(32)
        self.server: asyncio.AbstractServer | None = None
        self.client: httpx.AsyncClient | None = None
        self.upstream: dict[str, str] | None = None
        self.snapshot = ''
        self.injected_requests = 0
        self._tasks: set[asyncio.Task] = set()

    @property
    def base_url(self) -> str:
        return f'http://127.0.0.1:{self.port}/v1'

    @property
    def active(self) -> bool:
        return self.server is not None and self.upstream is not None

    async def start(self) -> None:
        if self.server is not None:
            return
        self.server = await asyncio.start_server(self._handle, '127.0.0.1', self.port)
        self.port = int(self.server.sockets[0].getsockname()[1])
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10), trust_env=False,
                                       follow_redirects=False)

    async def cancel_requests(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        await self.cancel_requests()
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        self.upstream = None
        self.snapshot = ''

    async def configure(self, endpoint: dict) -> None:
        base_url = validate_url(str(endpoint.get('base_url') or ''))
        parsed = urlsplit(base_url)
        if parsed.hostname in {'127.0.0.1', 'localhost', '::1'} and parsed.port == self.port and self.server:
            raise ValueError('Adapter upstream cannot point to itself')
        self.upstream = {'base_url': base_url, 'api_key': str(endpoint.get('api_key') or '')}
        await self.start()

    def set_snapshot(self, prompt: str) -> None:
        self.snapshot = prompt

    def clear_snapshot(self) -> None:
        self.snapshot = ''

    @staticmethod
    async def _event(connection, reader):
        while True:
            event = connection.next_event()
            if event is not h11.NEED_DATA:
                return event
            data = await asyncio.wait_for(reader.read(65536), 30)
            connection.receive_data(data)

    async def _handle(self, reader, writer) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=65536)
        forward = monitor = None
        try:
            request = await self._event(connection, reader)
            if not isinstance(request, h11.Request):
                return
            headers = dict(request.headers)
            if not hmac.compare_digest(headers.get(b'authorization', b''), ('Bearer ' + self.token).encode()):
                await self._error(connection, writer, 401, 'Adapter authentication required')
                return
            target = urlsplit(request.target.decode('ascii'))
            if target.scheme or target.netloc or not (
                target.path == '/v1/models' or target.path == '/v1/responses'
                or target.path.startswith('/v1/responses/')
            ):
                await self._error(connection, writer, 404, 'Unknown Codex endpoint')
                return
            endpoint = dict(self.upstream or {})
            prompt = self.snapshot  # Never consult mutable configuration mid-request.
            body = bytearray()
            while True:
                event = await self._event(connection, reader)
                if isinstance(event, h11.EndOfMessage):
                    break
                if not isinstance(event, h11.Data):
                    raise ValueError('Incomplete request')
                body.extend(event.data)
                if len(body) > MAX_BODY:
                    await self._error(connection, writer, 413, 'Responses request too large')
                    return
            outgoing = filtered_headers(request.headers)
            outgoing = [(k, v) for k, v in outgoing if k.lower() != b'authorization']
            if endpoint.get('api_key'):
                outgoing.append((b'authorization', ('Bearer ' + endpoint['api_key']).encode()))
            raw = bytes(body)
            if request.method == b'POST' and target.path in {'/v1/responses', '/v1/responses/compact'} and prompt:
                encoding = headers.get(b'content-encoding', b'identity').lower()
                if encoding in {b'gzip', b'deflate'}:
                    decoder = zlib.decompressobj(31 if encoding == b'gzip' else 15)
                    raw = decoder.decompress(raw, MAX_BODY + 1)
                    if len(raw) > MAX_BODY or not decoder.eof:
                        raise ValueError('Invalid compressed request')
                elif encoding != b'identity':
                    await self._error(connection, writer, 415, 'Unsupported Responses content encoding')
                    return
                raw = prepend_instructions(raw, prompt)
                outgoing = [(k, v) for k, v in outgoing if k.lower() != b'content-encoding']
                self.injected_requests += 1
                LOG.info('Codex instructions injected length=%d sha256=%s', len(prompt), PromptStore.digest(prompt))
            base = urlsplit(endpoint['base_url'])
            path = base.path.rstrip('/') + target.path[len('/v1'):]
            url = urlunsplit((base.scheme, base.netloc, path, target.query, ''))
            forward = asyncio.create_task(self._proxy(connection, writer, request.method.decode(), url, outgoing, raw))
            monitor = asyncio.create_task(reader.read(1))
            done, _ = await asyncio.wait([forward, monitor], return_when=asyncio.FIRST_COMPLETED)
            if forward in done:
                await forward
            # A closed SDK stream (/stop) cancels the upstream immediately.
        except (ValueError, UnicodeError, h11.ProtocolError):
            await self._error(connection, writer, 400, 'Invalid Responses request')
        except Exception as exc:
            LOG.warning('Codex adapter failed: %s', type(exc).__name__)
            await self._error(connection, writer, 502, 'Codex upstream unavailable')
        finally:
            for pending in (forward, monitor):
                if pending and not pending.done():
                    pending.cancel()
            await asyncio.gather(*(p for p in (forward, monitor) if p), return_exceptions=True)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self._tasks.discard(task)

    async def _proxy(self, connection, writer, method, url, headers, body):
        async with self.client.stream(method, url, headers=headers, content=body) as response:
            # Preserve upstream status, rate-limit/request-id and encoding headers;
            # raw chunks preserve SSE and compressed response bytes unchanged.
            response_headers = filtered_headers(response.headers.raw) + [(b'connection', b'close')]
            writer.write(connection.send(h11.Response(status_code=response.status_code, headers=response_headers)))
            await writer.drain()
            async for chunk in response.aiter_raw():
                writer.write(connection.send(h11.Data(data=chunk)))
                await writer.drain()
            writer.write(connection.send(h11.EndOfMessage()))
            await writer.drain()

    @staticmethod
    async def _error(connection, writer, status, message):
        # Once SSE headers have gone out, end the stream instead of appending a
        # second HTTP status line (which would corrupt the protocol).
        if connection.our_state is not h11.SEND_RESPONSE:
            return
        with contextlib.suppress(ConnectionError, h11.ProtocolError):
            data = json.dumps({'error': {'message': message}}).encode()
            writer.write(connection.send(h11.Response(status_code=status, headers=[
                (b'content-type', b'application/json'), (b'connection', b'close')])))
            writer.write(connection.send(h11.Data(data=data)))
            writer.write(connection.send(h11.EndOfMessage()))
            await writer.drain()
