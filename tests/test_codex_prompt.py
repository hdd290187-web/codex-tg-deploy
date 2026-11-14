from __future__ import annotations

import asyncio
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from codex_tg.app import Gateway
from codex_tg.pre_instructions import MAX_BYTES, PromptStore, PromptValidationError
from codex_tg.responses_adapter import ResponsesAdapter, prepend_instructions
from test_dual_engine import configure, gateway, message
from test_provider_models_history import callback


@pytest.mark.asyncio
async def test_adapter_injects_exact_order_preserves_fields_and_streams(tmp_path: Path):
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in (b"data: response.output_text.delta\n\n", b"data: response.completed\n\n"):
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    store = PromptStore(tmp_path / "codex-pre-instructions.txt")
    adapter = ResponsesAdapter(store, port=0)
    # Port zero is useful in tests; publish the actual selected socket port.
    await adapter.start()
    adapter.port = adapter.server.sockets[0].getsockname()[1]
    await adapter.configure({"base_url": f"http://127.0.0.1:{upstream.server_port}/v1", "api_key": "secret-key"})
    adapter.set_snapshot("原样第一行\n第二行; 不改写")
    auth = {'Authorization': 'Bearer ' + adapter.token}
    original = {
        "model": "relay-model", "input": [{"role": "user", "content": "run"}],
        "instructions": "Codex 原始 instructions", "tools": [{"type": "function", "name": "x"}],
        "reasoning": {"effort": "high"}, "stream": True, "previous_response_id": "resp_1",
        "tool_choice": "auto", "metadata": {"x": "y"},
    }
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        async with client.stream("POST", f"{adapter.base_url}/responses", json=original, headers=auth) as response:
            body = b"".join([chunk async for chunk in response.aiter_bytes()])
    assert response.status_code == 200
    assert b"response.completed" in body
    assert received[0]["instructions"] == "原样第一行\n第二行; 不改写\n\nCodex 原始 instructions"
    for field in ("model", "input", "tools", "reasoning", "stream", "previous_response_id", "tool_choice", "metadata"):
        assert received[0][field] == original[field]

    # A retry carries the original request again and is injected once, never
    # with the already modified body from the previous attempt.
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        await client.post(f"{adapter.base_url}/responses", content=json.dumps(original), headers=auth)
    assert received[1]["instructions"].count("原样第一行") == 1
    adapter.clear_snapshot()
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        await client.post(f"{adapter.base_url}/responses", content=json.dumps(original), headers=auth)
    assert received[2] == original
    without_original = dict(original)
    without_original.pop("instructions")
    adapter.set_snapshot("仅自定义内容")
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        await client.post(f"{adapter.base_url}/responses", json=without_original, headers=auth)
    assert received[3]["instructions"] == "仅自定义内容"
    await adapter.close()
    upstream.shutdown()
    upstream.server_close()


def test_prompt_store_utf8_bom_size_permissions_and_revision(tmp_path: Path):
    store = PromptStore(tmp_path / "codex-pre-instructions.txt")
    assert store.save("hello\n原文") == store.digest("hello\n原文")
    assert store.read() == "hello\n原文"
    assert store.load_revision(store.digest("hello\n原文")) == "hello\n原文"
    assert store.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(PromptValidationError):
        store.save("x" * (MAX_BYTES + 1))
    store.path.write_bytes(b"\xff\xfe")
    with pytest.raises(PromptValidationError):
        store.read()


@pytest.mark.asyncio
async def test_codexprompt_menu_text_file_clear_off_on_and_persistence(tmp_path: Path):
    g = gateway(tmp_path)
    telegram = g.telegram
    await g.handle_command("/codexprompt", "/codexprompt", 9)
    assert "codexprompt:edit" in {b["callback_data"] for row in telegram.keyboards[-1][2] for b in row}
    await g.handle_command("/codexprompt", "/codexprompt edit", 9)
    await g.handle_message(message("原样 prompt\n不要改写", 41))
    state = g.codex_prompt_state()
    assert state.enabled and state.configured and g.prompt_store.read() == "原样 prompt\n不要改写"
    await g.handle_command("/codexprompt", "/codexprompt off", 9)
    assert g.codex_prompt_state().configured and not g.codex_prompt_state().enabled
    await g.handle_command("/codexprompt", "/codexprompt on", 9)
    assert g.codex_prompt_state().enabled
    await g.handle_command("/codexprompt", "/codexprompt clear", 9)
    confirmation = telegram.keyboards[-1][2][0][0]['callback_data']
    await g.handle_callback(callback(confirmation))
    assert not g.codex_prompt_state().configured
    restarted = Gateway(g.config)
    assert not restarted.codex_prompt_state().configured


@pytest.mark.asyncio
async def test_codex_task_captures_prompt_revision_and_claude_does_not(tmp_path: Path):
    g = gateway(tmp_path)
    configure(g, "codex")
    configure(g, "claude")
    g.store.set_thread(9, "codex-thread")
    g.store.register_thread(9, "codex-thread")
    g.save_codex_prompt("Prompt A", enabled=True)
    await g.handle_message(message("Codex task"))
    first = g.store.queued_tasks()[0]
    task = g.store.claim_next()
    assert task["codex_prompt_ref"] == g.prompt_store.digest("Prompt A")
    g.store.complete(task["id"], "done")
    g.save_codex_prompt("Prompt B", enabled=True)
    await g.handle_command("/engine", "/engine claude", 9)
    await g.handle_message(message("Claude task"))
    second = g.store.claim_next()
    assert first["text"] == "Codex task"
    assert second["engine"] == "claude" and second["codex_prompt_ref"] in (None, "")


def test_raw_json_rewrite_preserves_every_other_byte_and_utf8(tmp_path):
    raw = b'{ "model":"m", "metadata":{"value":1.234567890123456789}, "instructions" : "original", "input":[],"extra":1e90 }'
    prompt = '\ufeff  Exact\r\n\t中文 "quoted" \\path\n '
    encoded = json.dumps(prompt + '\n\noriginal', ensure_ascii=False).encode()
    assert prepend_instructions(raw, prompt) == raw.replace(b'"original"', encoded)
    assert prepend_instructions(raw, '') is raw
    store = PromptStore(tmp_path / 'instructions.txt')
    ref = store.save(prompt)
    assert store.path.read_bytes() == prompt.encode()
    assert store.read() == store.load_revision(ref) == prompt


@pytest.mark.parametrize('original', [None, ''])
def test_empty_original_has_no_wrapper_or_separator(original):
    payload = {'instructions': original, 'tools': [], 'model': 'm'}
    assert json.loads(prepend_instructions(json.dumps(payload).encode(), '  text\n'))['instructions'] == '  text\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [b'', b'\xef\xbb\xbf', b'\xffbinary', b'\x00binary', b'\x01bad', b'x' * (MAX_BYTES + 1)])
async def test_txt_invalid_content_and_download_failure_never_enters_queue(tmp_path, raw):
    g = gateway(tmp_path)
    g.save_codex_prompt('keep old', enabled=False)
    await g.request_codex_prompt(9)

    async def download(_id, path, limit):
        assert limit == MAX_BYTES
        path.write_bytes(raw)
    g.telegram.download = download
    m = message('', 71)
    m['document'] = {'file_id': 'f', 'file_name': '../../instructions.txt', 'file_size': 0}
    await g.handle_message(m)
    assert g.prompt_store.read() == 'keep old'
    assert not g.codex_prompt_state().enabled
    assert '失败' in g.telegram.sent[-1]
    assert not g.store.queue_count()
    assert not list(tmp_path.glob('.codexprompt-*'))


@pytest.mark.asyncio
async def test_txt_download_failure_keeps_prompt_and_never_queues_input(tmp_path):
    from codex_tg.telegram import TelegramError
    g = gateway(tmp_path)
    g.save_codex_prompt('saved text', enabled=False)
    await g.request_codex_prompt(9)

    async def download(*_args):
        raise TelegramError('upstream failure including private-token')
    g.telegram.download = download
    m = message('', 76)
    m['document'] = {'file_id': 'f', 'file_name': 'prompt.txt', 'file_size': 15}
    await g.handle_message(m)
    assert g.prompt_store.read() == 'saved text' and not g.codex_prompt_state().enabled
    assert not g.store.queue_count() and g.pending_input['kind'] == 'codex_prompt'
    assert 'private-token' not in g.telegram.sent[-1]
    assert not list(tmp_path.glob('.codexprompt-*'))


@pytest.mark.asyncio
async def test_clear_retains_only_inflight_and_queued_snapshots(tmp_path):
    g = gateway(tmp_path)
    configure(g, 'codex')
    g.store.set_thread(9, 'existing-thread')
    g.store.register_thread(9, 'existing-thread')
    g.save_codex_prompt('Prompt A', enabled=True)
    await g.handle_message(message('first task', 80))
    first = g.store.claim_next()
    g.save_codex_prompt('Prompt B', enabled=True)
    await g.handle_message(message('second task', 81))
    g.clear_codex_prompt()
    assert g.prompt_store.read() == '' and not g.codex_prompt_state().configured
    assert g.prompt_store.load_revision(first['codex_prompt_ref']) == 'Prompt A'
    second_ref = g.prompt_store.digest('Prompt B')
    assert second_ref in g.store.codex_prompt_references() and g.store.queue_count() == 1
    assert g.prompt_store.load_revision(second_ref) == 'Prompt B'
    g.store.complete(first['id'], 'finished')
    g.store.clear_queue()
    g.prompt_store.prune(g.store.codex_prompt_references())
    assert not list(g.prompt_store.versions.glob('*.txt'))


@pytest.mark.asyncio
async def test_txt_exact_content_restart_long_show_authorization_and_replay(tmp_path):
    g = gateway(tmp_path)
    await g.request_codex_prompt(9)
    restarted = gateway(tmp_path)
    assert restarted.pending_input['kind'] == 'codex_prompt'
    value = '\ufeff  BOM\r\n中文\r\n\t' + 'long text ' * 500

    async def download(_id, path, limit):
        path.write_bytes(value.encode('utf-8'))
    restarted.telegram.download = download
    m = message('', 79)
    m['document'] = {'file_id': 'f', 'file_name': '../prompt.txt', 'file_size': len(value.encode())}
    await restarted.handle_message(m)
    assert restarted.prompt_store.read() == value
    assert restarted.codex_prompt_state().enabled
    await restarted.handle_message(m)
    assert not restarted.store.queue_count()
    await restarted.handle_command('/codexprompt', '/codexprompt show', 9)
    assert restarted.telegram.documents[-1] == restarted.prompt_store.path
    await restarted.handle_callback(callback('codexprompt:off', user=8))
    assert restarted.codex_prompt_state().enabled
    reopen = gateway(tmp_path)
    assert reopen.codex_prompt_state().enabled and reopen.prompt_store.read() == value
    with reopen.store.connect() as conn:
        assert value not in '\n'.join(conn.iterdump())


@pytest.mark.asyncio
async def test_clear_confirmation_cannot_clear_a_newer_prompt(tmp_path):
    g = gateway(tmp_path)
    g.save_codex_prompt('old', enabled=True)
    await g.confirm_clear_codex_prompt(9)
    confirm = g.telegram.keyboards[-1][2][0][0]['callback_data']
    g.save_codex_prompt('new', enabled=True)
    await g.handle_callback(callback(confirm))
    assert g.prompt_store.read() == 'new'
    await g.confirm_clear_codex_prompt(9)
    await g.handle_callback(callback(g.telegram.keyboards[-1][2][0][0]['callback_data']))
    assert g.prompt_store.path.read_bytes() == b''
    assert not list(g.prompt_store.versions.glob('*.txt'))


def test_relay_error_cannot_log_current_or_active_prompt(tmp_path):
    g = gateway(tmp_path)
    g.save_codex_prompt('Current prompt\nExact 中文', enabled=True)
    g.responses_adapter.set_snapshot('Old active prompt\r\nExact text')
    for prompt in (g.prompt_store.read(), g.responses_adapter.snapshot):
        assert prompt not in g.redact_codex_prompt('relay error: ' + prompt)
        encoded = json.dumps(prompt, ensure_ascii=False)[1:-1]
        assert encoded not in g.redact_codex_prompt('relay error: ' + encoded)


@pytest.mark.asyncio
async def test_adapter_auth_compression_upstream_headers_and_request_snapshot(tmp_path):
    seen = []
    response_body = gzip.compress(b'{"error":"rate limited"}')

    async def upstream(reader, writer):
        header = await reader.readuntil(b'\r\n\r\n')
        length = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                      if line.lower().startswith(b'content-length:'))
        seen.append((header, await reader.readexactly(length)))
        writer.write(b'HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\n'
                     b'Content-Encoding: gzip\r\nRetry-After: 23\r\nX-Request-Id: fixture-id\r\n'
                     + f'Content-Length: {len(response_body)}\r\nConnection: close\r\n\r\n'.encode()
                     + response_body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    adapter = ResponsesAdapter(PromptStore(tmp_path / 'prompt.txt'))
    await adapter.configure({'base_url': f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1', 'api_key': 'upstream-only'})
    adapter.set_snapshot('Prompt A')
    original = b'{ "instructions":"original", "metadata":{"n":1.234567890123456789}, "stream":true }'
    auth = {'Authorization': 'Bearer ' + adapter.token, 'Content-Encoding': 'gzip'}
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            rejected = await client.post(adapter.base_url + '/responses', content=original)
            assert rejected.status_code == 401 and not seen
            async with client.stream('POST', adapter.base_url + '/responses/compact',
                                     content=gzip.compress(original), headers=auth) as response:
                raw_response = b''.join([chunk async for chunk in response.aiter_raw()])
            assert response.status_code == 429 and raw_response == response_body
            assert response.headers['retry-after'] == '23'
            assert response.headers['x-request-id'] == 'fixture-id'
            assert seen[0][1] == prepend_instructions(original, 'Prompt A')
            assert b'Bearer upstream-only' in seen[0][0] and adapter.token.encode() not in seen[0][0]
            adapter.clear_snapshot()
            compressed = gzip.compress(original)
            await client.post(adapter.base_url + '/responses', content=compressed, headers=auth)
            assert seen[1][1] == compressed and b'content-encoding: gzip' in seen[1][0].lower()
    finally:
        await adapter.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_disconnect_really_cancels_upstream(tmp_path):
    disconnected = asyncio.Event()

    async def upstream(reader, writer):
        headers = await reader.readuntil(b'\r\n\r\n')
        length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                      if line.lower().startswith(b'content-length:'))
        await reader.readexactly(length)
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n5\r\ndata:\r\n')
        await writer.drain()
        await reader.read()
        disconnected.set()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    adapter = ResponsesAdapter(PromptStore(tmp_path / 'prompt.txt'))
    await adapter.configure({'base_url': f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1', 'api_key': 'key'})
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            async with client.stream('POST', adapter.base_url + '/responses', json={'stream': True},
                                     headers={'Authorization': 'Bearer ' + adapter.token}) as response:
                assert await response.aiter_raw().__anext__() == b'data:'
        await asyncio.wait_for(disconnected.wait(), 3)
        await adapter.cancel_requests()
        assert not adapter._tasks
    finally:
        await adapter.close()
        server.close()
        await server.wait_closed()
