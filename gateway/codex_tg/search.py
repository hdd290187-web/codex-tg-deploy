from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

from .credentials import Credentials, atomic_json

class TavilyClient:
    def __init__(self, api_key: str | None):
        self.api_key = api_key or ''
        self.last_state = 'UNCONFIGURED' if not self.api_key else 'READY'

    async def search(self, query: str) -> dict:
        if not self.api_key:
            return {'native_search_fallback': True, 'reason': 'UNCONFIGURED'}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5), trust_env=False) as client:
                response = await client.post('https://api.tavily.com/search',
                    headers={'Authorization': f'Bearer {self.api_key}'},
                    json={'query': query[:2000], 'max_results': 5, 'search_depth': 'basic'})
                response.raise_for_status()
                data = response.json()
                if not isinstance(data.get('results'), list):
                    raise ValueError('invalid search response')
            self.last_state = 'READY'
            return {'native_search_fallback': False, 'results': [
                {'title': str(r.get('title', ''))[:300], 'url': str(r.get('url', ''))[:2000],
                 'content': str(r.get('content', ''))[:3500]} for r in data['results'][:5]]}
        except Exception as exc:
            self.last_state = 'ERROR'
            code = f'HTTP_{exc.response.status_code}' if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
            return {'native_search_fallback': True, 'reason': code}


async def serve(path: Path, revision: str) -> None:
    # MCP stdio: newline delimited JSON-RPC; never print diagnostics to stdout.
    key = Credentials(path).get('tavily', revision).get('api_key', '')
    client = TavilyClient(key)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if 'id' not in request:
                continue
            method = request.get('method')
            if method == 'initialize':
                result = {'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'),
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'codex-tg-search', 'version': '1.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': [{'name': 'tavily_search', 'description': 'First choice for web search. On native_search_fallback=true use native search immediately.',
                    'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}]}
            elif method == 'tools/call':
                params = request.get('params', {})
                if params.get('name') != 'tavily_search':
                    raise ValueError('unknown tool')
                data = await client.search(str(params.get('arguments', {}).get('query', '')))
                atomic_json(path.parent / 'search-status.json', {'revision': revision, 'state': client.last_state})
                result = {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}], 'isError': False}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
        except (ValueError, KeyError, TypeError):
            continue


if __name__ == '__main__':
    asyncio.run(serve(Path(sys.argv[1]), sys.argv[2]))
