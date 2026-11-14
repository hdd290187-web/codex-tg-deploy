from __future__ import annotations

import os
import asyncio
import json
import shlex
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
from codex_tg.runtime import CodexRuntime
from test_dual_engine import gateway
from test_provider_models_history import buttons, callback


@pytest.mark.asyncio
async def test_managed_config_starts_sdk_and_creates_ephemeral_thread(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '''model_provider = "codex_tg"
model = "gpt-5.6-sol"
model_reasoning_effort = "xhigh"
plan_mode_reasoning_effort = "xhigh"
model_reasoning_summary = "detailed"
model_verbosity = "high"
model_context_window = 1050000
model_auto_compact_token_limit = 850000
web_search = "live"
personality = "pragmatic"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.codex_tg]
name = "Codex TG Relay"
base_url = "http://127.0.0.1:9/v1"
env_key = "CODEX_TG_API_KEY"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 2
stream_max_retries = 2
stream_idle_timeout_ms = 600000
supports_websockets = false
''',
        "utf-8",
    )
    environment = dict(os.environ)
    environment.update({"CODEX_HOME": str(codex_home), "CODEX_TG_API_KEY": "test-only-key"})
    async with AsyncCodex(CodexConfig(cwd=str(tmp_path), env=environment)) as client:
        thread = await client.thread_start(
            approval_mode=ApprovalMode.deny_all,
            cwd=str(tmp_path),
            ephemeral=True,
            model="gpt-5.6-sol",
            model_provider="codex_tg",
            sandbox=Sandbox.full_access,
        )
        assert thread.id.startswith("019") or len(thread.id) > 10


@pytest.mark.asyncio
async def test_real_codex_provider_catalog_and_history_resume(tmp_path, monkeypatch):
    requests = []
    catalog_requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass

        def do_GET(self):
            catalog_requests.append(self.path)
            assert self.path == '/v1/models'
            assert self.headers['Authorization'] == 'Bearer fixture-key'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            payload = json.dumps({'data': [{'id': 'relay-codex-model'}]}).encode()
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            data = json.loads(raw)
            requests.append(data)
            text = 'The provider completed this conversation turn.'
            item = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
            response = {'id': 'resp_fixture', 'object': 'response', 'status': 'completed',
                        'model': 'relay-codex-model', 'output': [item],
                        'usage': {'input_tokens': 30, 'output_tokens': 10, 'total_tokens': 40}}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            events = [
                {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                {'type': 'response.output_item.added', 'output_index': 0,
                 'item': {**item, 'status': 'in_progress', 'content': []}},
                {'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': 0,
                 'content_index': 0, 'delta': text},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                {'type': 'response.completed', 'response': response},
            ]
            for event in events:
                payload = ('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode()
                self.wfile.write(f'{len(payload):x}\r\n'.encode() + payload + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tmp_path / 'codex-home'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    g = gateway(tmp_path / 'gateway')
    g.responses_adapter.port = 0
    endpoint = {'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key': 'fixture-key'}
    g.engine_endpoints['codex'] = endpoint
    g.store.set_engine_session(9, 'claude', 'claude-session-must-remain')
    g.save_codex_prompt('Adapter prompt A', enabled=True)
    runtime = g.runtime

    async def progress(_text):
        pass

    try:
        await runtime.configure(endpoint)
        await g.handle_command('/model', '/model', 9)
        assert catalog_requests == ['/v1/models']
        await g.handle_callback(callback(next(b['callback_data'] for b in buttons(g) if ':select:' in b['callback_data'])))
        assert g.store.chat_settings(9).model == 'relay-codex-model'
        g.responses_adapter.set_snapshot('Adapter prompt A')
        first = await runtime.new_thread('relay-codex-model')
        first_result = await asyncio.wait_for(runtime.run(first, 'Remember unique conversation ALPHA.', [], progress,
                                                          'relay-codex-model', 'high'), 30)
        assert 'provider completed' in first_result.response
        assert any(item.get('instructions') == 'Adapter prompt A' or item.get('instructions', '').startswith('Adapter prompt A\n\n') for item in requests)
        other = await runtime.new_thread('relay-codex-model')
        await asyncio.wait_for(runtime.run(other, 'This is separate conversation BRAVO.', [], progress,
                                           'relay-codex-model', 'high'), 30)
        assert 'ALPHA' not in json.dumps(requests[-1]['input'])
        assert 'BRAVO' in json.dumps(requests[-1]['input'])
        await runtime.close()
        runtime = CodexRuntime(g.config, g.responses_adapter)
        g.runtime = runtime
        await runtime.configure(endpoint)
        g.responses_adapter.set_snapshot('Adapter prompt A')
        g.store.set_thread(9, other.id)
        await g.handle_command('/sessions', '/sessions', 9)
        assert 'thread:select:' + first.id in {b['callback_data'] for b in buttons(g)}
        await g.handle_callback(callback('thread:select:' + first.id))
        assert g.store.thread_id(9) == first.id
        assert g.store.engine_session(9, 'claude') == 'claude-session-must-remain'
        resumed = await runtime.get_thread(g.store.thread_id(9), 'relay-codex-model', 'high', apply_settings=True)
        await asyncio.wait_for(runtime.run(resumed, 'Continue the selected history.', [], progress,
                                           'relay-codex-model', 'high'), 30)
        assert 'ALPHA' in json.dumps(requests[-1])
        assert 'BRAVO' not in json.dumps(requests[-1])
    finally:
        await runtime.close()
        await g.responses_adapter.close()
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_real_codex_adapter_tools_tavily_turn_snapshot_new_model_and_endpoint(tmp_path, monkeypatch):
    requests = []
    target = tmp_path / 'actually-written.txt'
    prompt_a = '\ufeff  Original prompt A\r\nExact whitespace\t\n'
    prompt_b = 'Next turn prompt B'

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            outputs = [x for x in body['input'] if x.get('type') == 'function_call_output']
            tools = {x.get('name'): x for x in body.get('tools', []) if x.get('name')}
            if not outputs:
                name = next(n for n in ('exec_command', 'shell') if n in tools)
                command = f"printf 'written by Codex' > {shlex.quote(str(target))}; id -u"
                arguments = {'cmd': command} if name == 'exec_command' else {'command': ['bash', '-lc', command]}
                item = {'id': 'fc_shell', 'type': 'function_call', 'name': name, 'call_id': 'call_shell',
                        'arguments': json.dumps(arguments), 'status': 'completed'}
            elif not any(x.get('call_id') == 'call_search' for x in outputs):
                namespace = next(n for n, tool in tools.items() if any(
                    x.get('name') == 'tavily_search' for x in tool.get('tools', [])))
                item = {'id': 'fc_search', 'type': 'function_call', 'name': 'tavily_search',
                        'namespace': namespace, 'call_id': 'call_search',
                        'arguments': json.dumps({'query': 'Codex documentation'}), 'status': 'completed'}
            else:
                assert any('native_search_fallback' in str(x.get('output')) for x in outputs)
                item = {'id': 'msg_done', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': 'TOOLS_AND_STREAM_OK', 'annotations': []}]}
            response = {'id': f'resp_{len(requests)}', 'object': 'response', 'status': 'completed',
                        'model': body['model'], 'output': [item],
                        'usage': {'input_tokens': 40, 'output_tokens': 20, 'total_tokens': 60}}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                      {'type': 'response.output_item.added', 'output_index': 0, 'item': item}]
            if item['type'] == 'message':
                events.append({'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': 0,
                               'content_index': 0, 'delta': 'TOOLS_AND_STREAM_OK'})
            events += [{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                       {'type': 'response.completed', 'response': response}]
            for event in events:
                data = ('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode()
                self.wfile.write(f'{len(data):x}\r\n'.encode() + data + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    other = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    g = gateway(tmp_path / 'gateway')
    g.save_codex_prompt(prompt_a, enabled=True)
    endpoint = {'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key': 'fixture-key'}
    search = {'command': sys.executable, 'args': ['-m', 'codex_tg.search', str(g.credentials.path), 'unconfigured'],
              'env': {'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'gateway')}}
    await g.runtime.configure(endpoint, search)
    g.responses_adapter.set_snapshot(prompt_a)
    changed = False

    async def progress(_text):
        nonlocal changed
        if not changed:
            changed = True
            g.save_codex_prompt(prompt_b, enabled=True)

    try:
        thread = await g.runtime.new_thread('relay-codex-model')
        result = await asyncio.wait_for(g.runtime.run(thread, 'Write the test file, check uid and search.', [], progress,
                                                      'relay-codex-model', 'high'), 45)
        assert 'TOOLS_AND_STREAM_OK' in result.response
        assert target.read_text() == 'written by Codex' and changed
        assert len(requests) >= 3
        for request in requests:
            assert request['instructions'] == prompt_a or request['instructions'].startswith(prompt_a + '\n\n')
            assert request['instructions'].count(prompt_a) == 1
            assert prompt_b not in request['instructions']
        assert g.prompt_store.read() == prompt_b

        # /new and a different model retain the saved global prompt. Switching
        # upstream restarts the SDK without changing the durable instructions.
        g.engine_endpoints['codex'] = endpoint
        g.store.set_thread(9, thread.id)
        await g.handle_command('/new', '/new', 9)
        assert g.store.thread_id(9) != thread.id and g.prompt_store.read() == prompt_b
        await g.runtime.configure({'base_url': f'http://127.0.0.1:{other.server_port}/v1', 'api_key': 'another-fixture-key'}, search)
        g.responses_adapter.set_snapshot(g.prompt_store.load_revision(g.codex_prompt_revision()))
        new_thread = await g.runtime.new_thread('relay-codex-next-model')
        start = len(requests)
        image = tmp_path / 'image.png'
        def png_chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        image.write_bytes(b'\x89PNG\r\n\x1a\n'
                          + png_chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
                          + png_chunk(b'IDAT', zlib.compress(b'\x00\xff\xff\xff')) + png_chunk(b'IEND', b''))
        await asyncio.wait_for(g.runtime.run(new_thread, 'Run another full turn.',
                                            [{'kind': 'image', 'path': str(image)}, {'kind': 'document', 'path': str(target)}], progress,
                                            'relay-codex-next-model', 'medium'), 45)
        assert all(x['instructions'] == prompt_b or x['instructions'].startswith(prompt_b + '\n\n') for x in requests[start:])
        assert all(x['model'] == 'relay-codex-next-model' for x in requests[start:])
        assert g.responses_adapter.upstream['base_url'].endswith(f':{other.server_port}/v1')
        assert any(block.get('type') == 'input_image' for request in requests[start:] for item in request['input']
                   for block in item.get('content', []) if isinstance(block, dict))
        assert str(target) in json.dumps(requests[start]['input'])
    finally:
        await g.runtime.close()
        for fixture in (server, other):
            fixture.shutdown()
            fixture.server_close()


@pytest.mark.asyncio
async def test_real_sdk_interrupt_closes_adapter_and_upstream_stream(tmp_path, monkeypatch):
    started, disconnected = asyncio.Event(), asyncio.Event()

    async def upstream(reader, writer):
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            length = next(int(x.split(b':', 1)[1]) for x in header.split(b'\r\n')
                          if x.lower().startswith(b'content-length:'))
            await reader.readexactly(length)
            event = {'type': 'response.created', 'response': {'id': 'resp_interrupt', 'status': 'in_progress', 'output': []}}
            data = ('event: response.created\ndata: ' + json.dumps(event) + '\n\n').encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n'
                         + f'{len(data):x}\r\n'.encode() + data + b'\r\n')
            await writer.drain()
            started.set()
            await reader.read()
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    g = gateway(tmp_path / 'gateway')
    await g.runtime.configure({'base_url': f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1', 'api_key': 'fixture-key'})
    g.responses_adapter.set_snapshot('Interrupt test instructions')

    async def progress(_text):
        pass

    task = None
    try:
        thread = await g.runtime.new_thread('relay-codex-model')
        task = asyncio.create_task(g.runtime.run(thread, 'Verify interruption of a streaming turn.', [], progress,
                                                'relay-codex-model', 'high'))
        await asyncio.wait_for(started.wait(), 10)
        assert await g.runtime.interrupt()
        result = await asyncio.wait_for(task, 10)
        assert result.interrupted
        await asyncio.wait_for(disconnected.wait(), 3)
        await g.responses_adapter.cancel_requests()
        assert not g.responses_adapter._tasks
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await g.runtime.close()
        server.close()
        await server.wait_closed()
