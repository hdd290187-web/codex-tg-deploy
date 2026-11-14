from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import mimetypes
import os
import shutil
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .credentials import clean_environment
from .runtime import HistoryPage, HistoryThread

ProgressCallback = Callable[[str], Awaitable[None]]
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


@dataclass(slots=True)
class ClaudeRunResult:
    response: str
    interrupted: bool
    session_id: str | None


@dataclass(frozen=True)
class ClaudeSession:
    id: str
    title: str
    updated_at: int
    turns: int


class ClaudeRuntime:
    def __init__(self, data_dir: Path | None = None, command: str | None = None):
        self.command = command or os.getenv('CODEX_TG_CLAUDE_BIN') or str(Path('/opt/codex-tg/claude/current'))
        if not Path(self.command).exists() and not command:
            self.command = shutil.which('claude') or self.command
        self.data_dir = data_dir or Path('/opt/codex-tg/data/claude')
        self.process: asyncio.subprocess.Process | None = None
        self.interrupt_requested = False
        self.ready = False
        self.version = '未安装'
        self.workdir = '/'
        self.context_usage: dict[str, int | None] = {'used': None, 'window': None, 'total': None}
        self._history_cache: dict[str, tuple[int, int, ClaudeSession | None]] = {}

    def session_path(self, session_id: str) -> Path | None:
        try:
            if str(uuid.UUID(session_id)) != session_id:
                return None
        except (ValueError, AttributeError):
            return None
        root = self.data_dir / 'projects'
        # Only top-level conversations; subagent/sidechain logs are not sessions.
        candidates = []
        for path in root.glob(f'*/{session_id}.jsonl'):
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()):
                candidates.append(path)
        return max(candidates, key=lambda p: p.stat().st_mtime_ns) if candidates else None

    def _read_session(self, path: Path) -> ClaudeSession | None:
        stat = path.stat()
        cached = self._history_cache.get(str(path))
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
        first_prompt = ''
        title = ''
        turns: set[str] = set()
        with path.open('r', encoding='utf-8', errors='replace') as source:
            for line_number, line in enumerate(source):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # A still-running CLI can leave a partial final line.
                if not isinstance(event, dict) or event.get('sessionId') != path.stem or event.get('isSidechain'):
                    continue
                if event.get('type') == 'custom-title' and isinstance(event.get('customTitle'), str):
                    title = event['customTitle']
                if (event.get('type') != 'user' or event.get('isMeta')
                        or event.get('isCompactSummary') or event.get('turnCompanion')):
                    continue
                content = (event.get('message') or {}).get('content', [])
                if isinstance(content, str):
                    prompt = content
                elif isinstance(content, list):
                    # Tool results and image-only companions are not user turns.
                    prompt = ' '.join(str(b.get('text', '')) for b in content
                                      if isinstance(b, dict) and b.get('type') == 'text')
                else:
                    continue
                if prompt.strip():
                    turns.add(str(event.get('uuid') or line_number))
                    first_prompt = first_prompt or prompt
        session = ClaudeSession(path.stem, ' '.join((title or first_prompt).split())[:200],
                                int(stat.st_mtime), len(turns)) if turns else None
        self._history_cache[str(path)] = (stat.st_mtime_ns, stat.st_size, session)
        return session

    async def get_session(self, session_id: str) -> ClaudeSession:
        path = self.session_path(session_id)
        if path:
            session = await asyncio.to_thread(self._read_session, path)
            if session:
                return session
        raise ValueError('Claude 历史 Session 不存在或没有可恢复的对话，请刷新 /sessions。')

    async def list_sessions(self, page: int = 0, page_size: int = 6) -> HistoryPage:
        def scan():
            sessions = []
            for path in (self.data_dir / 'projects').glob('*/*.jsonl'):
                if self.session_path(path.stem) != path:
                    continue
                try:
                    session = self._read_session(path)
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
                if session:
                    sessions.append(session)
            return sorted(sessions, key=lambda item: (item.updated_at, item.id), reverse=True)
        sessions = await asyncio.to_thread(scan)
        page = max(0, min(page, max(0, (len(sessions) - 1) // page_size)))
        start = page * page_size
        return HistoryPage([HistoryThread(s.id, s.title, s.updated_at) for s in sessions[start:start + page_size]],
                           page, page > 0, start + page_size < len(sessions))

    async def start(self):
        self.ready = os.access(self.command, os.X_OK)
        if self.ready:
            p = await asyncio.create_subprocess_exec(self.command, '--version', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                output, _ = await asyncio.wait_for(p.communicate(), 10)
                self.version = output.decode().strip()
                self.ready = p.returncode == 0
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()
                self.ready = False

    async def close(self):
        await self._terminate()
        self.ready = False

    async def _terminate(self):
        p = self.process
        if p is None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(p.wait(), 3)
        except asyncio.TimeoutError:
            pass
        # Kill descendants still holding pipes even if the CLI already exited.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)
        await p.wait()

    async def interrupt(self) -> bool:
        self.interrupt_requested = True
        await self._terminate()
        return True

    async def compact(self, session_id: str) -> dict:
        # Claude Code exposes /compact in its interactive TUI. The official
        # headless stream-json interface used by Gateway does not expose a
        # native compact RPC/flag, so we must report unsupported rather than
        # send a normal user prompt that merely asks for a summary.
        return {'supported': False, 'confirmed': False, 'reason': 'Claude Code headless CLI 未提供可确认的原生 compact 接口'}

    def environment(self, endpoint: dict) -> dict[str, str]:
        env = clean_environment()
        env.update(CLAUDE_CONFIG_DIR=str(self.data_dir), DISABLE_AUTOUPDATER='1',
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                   # Claude Code refuses bypass mode for uid 0 unless the
                   # process explicitly declares the unattended environment.
                   # The Gateway itself still runs as root and owns the full
                   # VPS; this flag only satisfies the CLI's root guard.
                   IS_SANDBOX='1', ANTHROPIC_BASE_URL=endpoint['base_url'])
        name = 'ANTHROPIC_AUTH_TOKEN' if endpoint.get('auth') == 'bearer' else 'ANTHROPIC_API_KEY'
        env[name] = endpoint['api_key']
        return env

    async def run(self, session_id: str, text: str, attachments: list[dict[str, str]],
                  progress: ProgressCallback, model: str, effort: str, endpoint: dict,
                  search: dict | None = None, *, probe: bool = False) -> ClaudeRunResult:
        if not self.ready:
            raise RuntimeError('Claude Code CLI 未安装或不可执行')
        if not effort or any(c.isspace() for c in effort) or effort.startswith('-'):
            raise ValueError('Claude effort format invalid')
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        content: list[dict] = [{'type': 'text', 'text': text}]
        for item in attachments:
            path = Path(item['path'])
            if item.get('kind') == 'image':
                media = mimetypes.guess_type(item.get('name') or str(path))[0] or 'image/jpeg'
                if media not in {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}:
                    raise ValueError('Claude 图片仅支持 JPEG/PNG/GIF/WebP')
                content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': base64.b64encode(path.read_bytes()).decode()}})
            else:
                content.append({'type': 'text', 'text': f'Telegram 上传的文件：{path}'})
        # The Gateway is an explicitly unattended root service.  Keep Claude's
        # settings, hooks, and plugin policy from changing that contract, and
        # use the official non-interactive full-access mode.  `dontAsk` denies
        # tools that ask for permission, including Bash, writes, WebSearch, and
        # MCP tools whenever Claude asks for authorization.
        command = [self.command, '-p', '--output-format', 'stream-json', '--verbose',
                   '--input-format', 'stream-json', '--permission-mode', 'bypassPermissions',
                   '--dangerously-skip-permissions', '--permission-prompts', 'none',
                   '--setting-sources', '', '--strict-mcp-config', '--allowedTools', '*',
                   '--model', model, '--effort', effort]
        if self.session_path(session_id):
            command += ['--resume', str(self.session_path(session_id))]
        else:
            command += ['--session-id', session_id]
        if probe:
            command += ['--tools', '', '--no-session-persistence']
        servers = {'tavily': search} if search else {}
        if not probe and getattr(self, 'delivery', None):
            servers['telegram'] = self.delivery
        if servers:
            command += ['--mcp-config', json.dumps({'mcpServers': servers})]
        env = self.environment(endpoint)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
        p = await asyncio.create_subprocess_exec(*command, cwd=self.workdir, env=env, start_new_session=True,
             stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
             limit=16 * 1024 * 1024)
        self.process = p
        error_tail = bytearray()
        async def drain_stderr():
            while chunk := await p.stderr.read(4096):
                error_tail.extend(chunk)
                del error_tail[:-8000]
        drain = asyncio.create_task(drain_stderr())
        response = ''
        failure = ''
        got_result = False
        try:
            if self.interrupt_requested:
                await self._terminate()
            else:
                payload = {'type': 'user', 'message': {'role': 'user', 'content': content}, 'session_id': session_id}
                p.stdin.write((json.dumps(payload) + '\n').encode())
                await p.stdin.drain()
                p.stdin.close()
                async for line in p.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get('type') == 'assistant':
                        for block in event.get('message', {}).get('content', []):
                            if block.get('type') == 'tool_use':
                                await progress('Claude 工具：' + str(block.get('name', 'tool'))[:80])
                    if event.get('type') == 'result':
                        got_result = True
                        response = str(event.get('result') or '')
                        if event.get('is_error'):
                            failure = '; '.join(str(x) for x in event.get('errors', [])) or response or str(event.get('subtype'))
                        returned = event.get('session_id')
                        if returned and returned != session_id:
                            raise RuntimeError('Claude returned an unexpected session id')
            await p.wait()
            await drain
            if self.interrupt_requested:
                return ClaudeRunResult('', True, session_id)
            if p.returncode or failure or not got_result:
                raise RuntimeError(f'Claude Code failed: {failure or error_tail.decode("utf-8", "replace")[-1200:] or "missing result"}')
            return ClaudeRunResult(response or 'Claude Code 已完成。', False, session_id)
        finally:
            await self._terminate()
            self.process = None
            self.interrupt_requested = False
            if not drain.done():
                drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
