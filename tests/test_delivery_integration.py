"""Actual official engine processes + local upstream/Bot API protocol fixtures."""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import sys
import threading
import uuid
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from codex_tg.claude_runtime import ClaudeRuntime
from codex_tg.telegram import Telegram
from test_dual_engine import gateway, message

ROOT = Path(__file__).resolve().parents[1]
_CLAUDE_ASSETS = sorted(ROOT.glob('releases/claude-*-linux-x64'))
CLAUDE = _CLAUDE_ASSETS[-1] if _CLAUDE_ASSETS else ROOT / 'releases/claude-missing'


@pytest.mark.asyncio
@pytest.mark.parametrize('engine', ['codex', 'claude'])
async def test_official_engine_files_and_real_cwd_are_delivered_by_bot_api(tmp_path, monkeypatch, engine):
    if engine == 'claude' and not CLAUDE.exists():
        pytest.skip('official Claude artifact missing')
    home = tmp_path / 'codex-home'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    project = tmp_path / 'project'
    project.mkdir()
    next_project = tmp_path / 'next-project'
    next_project.mkdir()
    g = gateway(tmp_path / 'data')
    files, upstream_requests = [], []
    helper = tmp_path / 'make_reports.py'
    helper.write_text('''import os,pathlib,sys,zipfile
root=pathlib.Path(sys.argv[1])
root.mkdir(exist_ok=True)
(root/'result.txt').write_text('DELIVERY_MARKER ' + os.getcwd())
(root/'result.pdf').write_bytes(b'%PDF-1.4\\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\\n2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\\ntrailer<</Root 1 0 R>>\\n%%EOF')
with zipfile.ZipFile(root/'result.xlsx','w') as z:
 z.writestr('[Content_Types].xml','<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>')
 z.writestr('_rels/.rels','<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
 z.writestr('xl/workbook.xml','<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Payroll" sheetId="1" r:id="rId1"/></sheets></workbook>')
 z.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
 z.writestr('xl/worksheets/sheet1.xml','<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Employee</t></is></c><c r="B1"><v>100</v></c></row></sheetData></worksheet>')
print('DELIVERY_MARKER',os.getcwd())
''')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *_args):
            pass

        def response(self, payload):
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def stream(self, events):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            for event in events:
                raw = ('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode()
                self.wfile.write(f'{len(raw):x}\r\n'.encode() + raw + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('content-length', 0)))
            if self.path.startswith('/botfixture/'):
                if self.path.endswith('/sendDocument'):
                    document = BytesParser(policy=policy.default).parsebytes(
                        ('Content-Type: ' + self.headers['Content-Type'] + '\r\n\r\n').encode() + raw)
                    fields = {p.get_param('name', header='content-disposition'): p for p in document.iter_parts()}
                    assert fields['chat_id'].get_content().strip() == '9'
                    file = fields['document']
                    files.append((file.get_filename(), file.get_payload(decode=True)))
                    self.response({'ok': True, 'result': {'message_id': len(files) + 100, 'document': {'file_id': str(uuid.uuid4())}}})
                else:
                    self.response({'ok': True, 'result': {'message_id': 50}})
                return
            data = json.loads(raw)
            if self.path.endswith('/count_tokens'):
                self.response({'input_tokens': 200})
                return
            upstream_requests.append(data)
            active = g.store.active()
            assert active['output_dir'] is None
            folder = str(Path(active['cwd']) / 'reports')
            call_id = 'deliver_' + str(active['id'])
            sent_id = 'send_' + str(active['id'])
            paths = [str(Path(folder) / name) for name in ('result.txt', 'result.xlsx', 'result.pdf')]
            command = f'{shlex.quote(sys.executable)} {shlex.quote(str(helper))} {shlex.quote(folder)}'
            if engine == 'codex':
                has_output = any(i.get('type') == 'function_call_output' and i.get('call_id') == call_id for i in data['input'])
                sent = next((i for i in data['input'] if i.get('type') == 'function_call_output' and i.get('call_id') == sent_id), None)
                assert 'Telegram 文件交付' not in json.dumps(data['input'], ensure_ascii=False)
                assert any(i.get('role') == 'user' and any(c.get('text') == 'Generate TXT, Excel and PDF and send the files to me.'
                           for c in i.get('content', []) if isinstance(c, dict)) for i in data['input'])
                if sent:
                    assert 'REJECTED' not in str(sent) and 'receipt' in str(sent)
                    item = {'type': 'message', 'id': 'msg_done', 'role': 'assistant', 'status': 'completed',
                            'content': [{'type': 'output_text', 'text': 'Reports created DELIVERY_MARKER', 'annotations': []}]}
                elif has_output:
                    namespace = next(t['name'] for t in data['tools'] if any(x.get('name') == 'telegram_send_file' for x in t.get('tools', [])))
                    item = {'type': 'function_call', 'id': 'fc_' + sent_id, 'name': 'telegram_send_file', 'namespace': namespace,
                            'arguments': json.dumps({'paths': paths}), 'call_id': sent_id, 'status': 'completed'}
                else:
                    names = {i.get('name') for i in data['tools']}
                    name = 'exec_command' if 'exec_command' in names else 'shell'
                    args = {'cmd': command} if name == 'exec_command' else {'command': ['bash', '-c', command]}
                    item = {'type': 'function_call', 'id': 'fc_' + call_id, 'name': name, 'arguments': json.dumps(args), 'call_id': call_id, 'status': 'completed'}
                response = {'id': 'resp_' + str(len(upstream_requests)), 'object': 'response', 'status': 'completed',
                            'output': [item], 'model': data['model'], 'usage': {'input_tokens': 40, 'output_tokens': 20, 'total_tokens': 60}}
                self.stream([{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                    {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                    {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                    {'type': 'response.completed', 'response': response}])
            else:
                has_output = any(i.get('type') == 'tool_result' and i.get('tool_use_id') == call_id
                    for m in data['messages'] for i in (m['content'] if isinstance(m.get('content'), list) else []))
                sent = next((i for m in data['messages'] for i in (m['content'] if isinstance(m.get('content'), list) else [])
                             if i.get('type') == 'tool_result' and i.get('tool_use_id') == sent_id), None)
                mcp_tool = next((t.get('name') for t in data.get('tools', [])
                                 if 'telegram_send_file' in str(t.get('name'))), None)
                if sent:
                    assert not sent.get('is_error') and 'receipt' in str(sent)
                    block = {'type': 'text', 'text': 'Reports created DELIVERY_MARKER'}
                    delta = {'type': 'text_delta', 'text': block['text']}
                elif has_output:
                    name = mcp_tool or 'mcp__telegram__telegram_send_file'
                    block = {'type': 'tool_use', 'id': sent_id, 'name': name, 'input': {}}
                    delta = {'type': 'input_json_delta', 'partial_json': json.dumps({'paths': paths})}
                elif not mcp_tool and any(t.get('name') == 'ToolSearch' for t in data.get('tools', [])):
                    # Claude Code 2.1.311+ defers MCP schemas until the
                    # official ToolSearch built-in is called.  Older
                    # fixtures sent telegram_send_file in the first body.
                    block = {'type': 'tool_use', 'id': 'search_' + str(active['id']),
                             'name': 'ToolSearch', 'input': {}}
                    delta = {'type': 'input_json_delta',
                             'partial_json': json.dumps({'query': 'telegram_send_file', 'max_results': 5})}
                else:
                    block = {'type': 'tool_use', 'id': call_id, 'name': 'Bash', 'input': {}}
                    delta = {'type': 'input_json_delta', 'partial_json': json.dumps({'command': command, 'description': 'Create requested reports'})}
                self.stream([
                    {'type': 'message_start', 'message': {'id': 'msg_' + call_id, 'type': 'message', 'role': 'assistant', 'model': data['model'],
                        'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 200, 'output_tokens': 0}}},
                    {'type': 'content_block_start', 'index': 0, 'content_block': {**block, **({'text': ''} if sent else {})}},
                    {'type': 'content_block_delta', 'index': 0, 'delta': delta}, {'type': 'content_block_stop', 'index': 0},
                    {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn' if sent else 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': 40}},
                    {'type': 'message_stop'}])

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = {'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key': 'fixture-only-key'}
    g.credentials.save(engine, endpoint['base_url'], endpoint['api_key'])
    g.engine_endpoints[engine] = endpoint
    g.store.set_active_engine(9, engine)
    g.store.update_chat_settings(9, model='relay-codex-model', reasoning_effort='high', claude_model='claude-sonnet-4-6')
    telegram = Telegram('fixture')
    telegram.base = f'http://127.0.0.1:{server.server_port}/botfixture'
    g.telegram = telegram
    if engine == 'codex':
        await g.runtime.configure(endpoint)
    else:
        g.claude_runtime = ClaudeRuntime(tmp_path / 'claude', str(CLAUDE))
        g.claude_runtime.delivery = g.delivery_config('claude')
        await g.claude_runtime.start()
    worker = asyncio.create_task(g.worker())
    delivery = asyncio.create_task(g.delivery_worker())
    try:
        for index, folder in enumerate((project, next_project)):
            await g.handle_message(message('/cd ' + str(folder), 10 + index * 2))
            await g.handle_message(message('Generate TXT, Excel and PDF and send the files to me.', 11 + index * 2))
            for _ in range(1200):
                items = g.store.artifacts(9)
                if len(items) == (index + 1) * 3 and all(i['status'] == 'DELIVERED' for i in items):
                    break
                if g.store.last_task() and g.store.last_task()['status'] == 'FAILED':
                    pytest.fail(g.store.last_task()['error'])
                await asyncio.sleep(.05)
            assert len(files) == (index + 1) * 3
            texts = [b for n, b in files if n == 'result.txt']
            assert texts[-1] == ('DELIVERY_MARKER ' + str(folder)).encode()
        assert all(row['status'] == 'DONE' for row in g.store.task_history(9))
        assert all(row['status'] == 'DELIVERED' for row in g.store.artifacts(9))
        assert all(re.fullmatch(r'[a-f0-9]{64}', row['sha256']) for row in g.store.artifacts(9))
        if engine == 'claude':
            assert 'Reports created DELIVERY_MARKER' in json.dumps(upstream_requests[-1]['messages'])
    finally:
        g.stop_event.set()
        g.worker_event.set()
        g.delivery_event.set()
        worker.cancel()
        delivery.cancel()
        await asyncio.gather(worker, delivery, return_exceptions=True)
        await g.runtime.close()
        await g.claude_runtime.close()
        await telegram.close()
        server.shutdown()
        server.server_close()
