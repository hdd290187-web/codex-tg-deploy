"""Actual official CLI against a local Anthropic protocol fixture (no paid API)."""
from __future__ import annotations
import asyncio
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from codex_tg.claude_runtime import ClaudeRuntime
from codex_tg.credentials import Credentials
from test_dual_engine import gateway
from test_provider_models_history import buttons, callback

ROOT = Path(__file__).resolve().parents[1]
_CLAUDE_ASSETS = sorted(ROOT.glob('releases/claude-*-linux-x64'))
CLI = _CLAUDE_ASSETS[-1] if _CLAUDE_ASSETS else ROOT / 'releases/claude-missing'


@pytest.mark.asyncio
async def test_official_claude_cli_root_tools_effort_resume_and_images(tmp_path):
    if not CLI.exists():
        pytest.skip('official CLI asset required')
    bodies = []
    model_requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            model_requests.append(self.path)
            assert self.path == '/v1/models'
            assert self.headers['x-api-key'] == 'fixture-only-key'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'data': [{'id': 'claude-sonnet-4-6', 'display_name': 'Fixture Claude'}]}).encode())
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get('content-length', 0))))
            if self.path.endswith('/count_tokens'):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"input_tokens":100}')
                return
            if '/messages' not in self.path:
                self.send_response(404)
                self.end_headers()
                return
            bodies.append(data)
            messages = data.get('messages', [])
            results = [b for m in messages for b in (m.get('content') if isinstance(m.get('content'), list) else []) if b.get('type') == 'tool_result']
            if not results:
                block = {'type':'tool_use','id':'toolu_root', 'name':'Bash', 'input':{'command':'id -u', 'description':'Verify root identity'}}
                reason = 'tool_use'
            else:
                # CLI must actually execute the shell and send its result.
                result = json.dumps(results)
                assert '0' in result, result
                block = {'type':'text', 'text':'ROOT_TOOL_OK session-marker-456'}
                reason = 'end_turn'
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.end_headers()
            def event(kind, data):
                self.wfile.write(('event: '+kind+'\ndata: '+json.dumps({'type':kind,**data})+'\n\n').encode())
                self.wfile.flush()
            event('message_start', {'message':{'id':'msg_fixture','type':'message','role':'assistant','model':data.get('model'), 'content':[], 'stop_reason':None, 'stop_sequence':None,'usage':{'input_tokens':100,'output_tokens':0}}})
            if block['type'] == 'tool_use':
                event('content_block_start',{'index':0,'content_block':{**block,'input':{}}})
                event('content_block_delta',{'index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps(block['input'])}})
            else:
                event('content_block_start',{'index':0,'content_block':{'type':'text','text':''}})
                event('content_block_delta',{'index':0,'delta':{'type':'text_delta','text':block['text']}})
            event('content_block_stop', {'index':0})
            event('message_delta', {'delta':{'stop_reason':reason,'stop_sequence':None},'usage':{'output_tokens':20}})
            event('message_stop', {})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    t=threading.Thread(target=server.serve_forever,daemon=True)
    t.start()
    runtime=ClaudeRuntime(tmp_path/'state',str(CLI))
    await runtime.start()
    session=str(uuid.uuid4())
    endpoint={'base_url':f'http://127.0.0.1:{server.server_port}','api_key':'fixture-only-key'}
    progress=[]
    async def report(text): progress.append(text)
    try:
        first=await asyncio.wait_for(runtime.run(session,'Execute id -u. Remember session-marker-456.',[],report,'claude-sonnet-4-6','high',endpoint),60)
        assert 'ROOT_TOOL_OK' in first.response
        assert any('Bash' in x for x in progress)
        other_session = str(uuid.uuid4())
        await asyncio.wait_for(runtime.run(other_session, 'Separate session B only.', [], report,
                                          'claude-sonnet-4-6', 'high', endpoint), 60)
        g = gateway(tmp_path / 'gateway')
        g.save_codex_prompt('CODEX_ONLY_PROMPT_NEVER_SEND_TO_CLAUDE', enabled=True)
        g.claude_runtime = runtime
        g.store.set_active_engine(9, 'claude')
        g.store.set_thread(9, 'codex-must-remain')
        g.store.set_engine_session(9, 'claude', other_session)
        g.engine_endpoints['claude'] = endpoint
        await g.handle_command('/model', '/model', 9)
        assert model_requests == ['/v1/models']
        await g.handle_callback(callback(next(b['callback_data'] for b in buttons(g) if ':select:' in b['callback_data'])))
        assert g.store.chat_settings(9).claude_model == 'claude-sonnet-4-6'
        await g.handle_command('/sessions', '/sessions', 9)
        assert {'claude:select:' + session, 'claude:select:' + other_session} <= {b['callback_data'] for b in buttons(g)}
        await g.handle_callback(callback('claude:select:' + session))
        assert g.store.claude_conversation_key(9) == session
        assert g.store.thread_id(9) == 'codex-must-remain'
        await runtime.close()
        runtime=ClaudeRuntime(tmp_path/'state',str(CLI))
        await runtime.start()
        selected = g.store.engine_session(9, 'claude')
        second=await asyncio.wait_for(runtime.run(selected,'Return the previous marker.',[],report,'claude-sonnet-4-6','low',endpoint),60)
        assert second.session_id == first.session_id == session
        assert 'session-marker-456' in second.response
        assert any('session-marker-456' in json.dumps(m) and m.get('role') == 'assistant' for m in bodies[-1]['messages'])
        assert 'Separate session B only.' not in json.dumps(bodies[-1]['messages'])
        assert bodies[0].get('output_config',{}).get('effort') == 'high'
        assert bodies[-1].get('output_config',{}).get('effort') == 'low'
        assert 'CODEX_ONLY_PROMPT_NEVER_SEND_TO_CLAUDE' not in json.dumps(bodies)
        assert g.responses_adapter.server is None
        image=ROOT/'tests/artifacts/center-mobile.png'
        if image.exists():
            await asyncio.wait_for(runtime.run(session,'Inspect image',[{'kind':'image','path':str(image),'name':'test.png'}],report,'claude-sonnet-4-6','high',endpoint),60)
            assert any(b.get('type')=='image' for m in bodies[-1]['messages'] for b in m.get('content',[]) if isinstance(b,dict))
    finally:
        await runtime.close()
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_name', ['Bash', 'Read', 'Write', 'Edit', 'mcp__tavily__tavily_search', 'WebSearch'])
async def test_official_claude_cli_unattended_tool_permissions(tmp_path, tool_name):
    if not CLI.exists():
        pytest.skip('official CLI asset required')
    target = tmp_path / 'tool-target.txt'
    target.write_text('before', encoding='utf-8')
    tool_inputs = {
        'Bash': {'command': 'id -u', 'description': 'Check effective uid'},
        'Read': {'file_path': str(target)},
        'Write': {'file_path': str(target), 'content': 'written'},
        'Edit': {'file_path': str(target), 'old_string': 'before', 'new_string': 'edited'},
        'mcp__tavily__tavily_search': {'query': 'Claude Code documentation'},
        'WebSearch': {'query': 'Claude Code documentation'},
    }
    results = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get('content-length', 0))))
            if self.path.endswith('/count_tokens'):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"input_tokens":100}')
                return
            if '/messages' not in self.path:
                self.send_response(404)
                self.end_headers()
                return
            tool_results = [block for item in data.get('messages', [])
                            for block in (item.get('content') if isinstance(item.get('content'), list) else [])
                            if isinstance(block, dict) and block.get('type') == 'tool_result']
            if tool_results:
                results.extend(tool_results)
                block = {'type': 'text', 'text': 'tool verification completed'}
                reason = 'end_turn'
            else:
                block = {'type': 'tool_use', 'id': 'toolu_permission', 'name': tool_name, 'input': tool_inputs[tool_name]}
                reason = 'tool_use'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()

            def event(kind, data):
                self.wfile.write(('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **data}) + '\n\n').encode())
                self.wfile.flush()

            event('message_start', {'message': {'id': 'msg_tool_fixture', 'type': 'message', 'role': 'assistant',
                'model': data.get('model'), 'content': [], 'stop_reason': None, 'stop_sequence': None,
                'usage': {'input_tokens': 100, 'output_tokens': 0}}})
            if block['type'] == 'tool_use':
                event('content_block_start', {'index': 0, 'content_block': {**block, 'input': {}}})
                event('content_block_delta', {'index': 0, 'delta': {'type': 'input_json_delta',
                    'partial_json': json.dumps(block['input'])}})
            else:
                event('content_block_start', {'index': 0, 'content_block': {'type': 'text', 'text': ''}})
                event('content_block_delta', {'index': 0, 'delta': {'type': 'text_delta', 'text': block['text']}})
            event('content_block_stop', {'index': 0})
            event('message_delta', {'delta': {'stop_reason': reason, 'stop_sequence': None},
                'usage': {'output_tokens': 20}})
            event('message_stop', {})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = ClaudeRuntime(tmp_path / 'state', str(CLI))
    await runtime.start()
    endpoint = {'base_url': f'http://127.0.0.1:{server.server_port}', 'api_key': 'fixture-only-key'}
    search = None
    if tool_name.startswith('mcp__'):
        vault = Credentials(tmp_path / 'credentials.json')
        search = {'command': sys.executable,
                  'args': ['-m', 'codex_tg.search', str(vault.path), 'missing-ref'],
                  'env': {'PYTHONPATH': str(ROOT / 'gateway')}}

    async def report(_text):
        pass

    try:
        output = await asyncio.wait_for(runtime.run(str(uuid.uuid4()),
            f'Use the {tool_name} tool as requested.', [], report, 'claude-sonnet-4-6', 'high', endpoint, search), 60)
        assert 'tool verification completed' in output.response
        assert results, f'{tool_name} produced no tool result'
        result_text = json.dumps(results, ensure_ascii=False).lower()
        assert 'permission' not in result_text or 'denied' not in result_text, result_text
        assert not results[-1].get('is_error'), result_text
        if tool_name == 'Bash':
            assert '0' in result_text
        elif tool_name == 'Write':
            assert target.read_text() == 'written'
        elif tool_name == 'Edit':
            assert target.read_text() == 'edited'
        elif tool_name.startswith('mcp__'):
            assert 'native_search_fallback' in result_text
    finally:
        await runtime.close()
        server.shutdown()
        server.server_close()
