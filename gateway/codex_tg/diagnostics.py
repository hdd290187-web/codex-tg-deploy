from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization

from .components import codex_components
from .config_audit import audit_claude, audit_codex


async def systemd_state(unit: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec('systemctl', 'is-active', unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return '检查超时'
        return output.decode().strip() or '无法检查'
    except OSError:
        return 'systemctl 不可用'


async def diagnose(gateway) -> tuple[str, bool]:
    g = gateway
    lines = ['Gateway 一键诊断（未调用模型）']
    repairable = False
    def item(name, state, detail):
        lines.append(f'{name}：{state} · {g.sanitize(str(detail))[:380]}')
    state = await systemd_state('codex-tg.service')
    item('Gateway/systemd', '正常' if state == 'active' else '异常', f'v{g.config.version} / {state} / UID {os.geteuid()}')
    components = None
    try:
        components = await asyncio.to_thread(codex_components)
        await asyncio.wait_for(g.runtime.reasoning_catalog(refresh=True), 8)
        item('Codex SDK/Runtime', '正常' if components['paired'] and g.runtime.ready else '异常',
             f"SDK {components['sdk']} / {components['runtime_version']}\n{components['runtime_path']}")
        item('全局 Codex CLI', '正常' if components['cli_version'].startswith('codex-cli') else '异常',
             f"{components['cli_version']} / {components['cli_installation']}\n{components['cli_path']}")
    except Exception as exc:
        item('Codex SDK/Runtime', '异常', type(exc).__name__)
    codex_home = Path(os.getenv('CODEX_HOME', '/root/.codex'))
    codex_audit = await asyncio.to_thread(
        audit_codex, codex_home / 'config.toml', (components or {}).get('runtime_path')
    )
    item('Codex config.toml', codex_audit.state, codex_audit.detail)
    await g.claude_runtime.start()
    item('Claude CLI', '正常' if g.claude_runtime.ready else '异常', f'{g.claude_runtime.version} / {g.claude_runtime.command}')
    claude_audit = await asyncio.to_thread(
        audit_claude, g.claude_runtime.data_dir / 'settings.json', g.claude_runtime.command
    )
    item('Claude settings.json', claude_audit.state, claude_audit.detail)
    settings = g.store.chat_settings(g.config.allowed_chat_id)
    for engine in ('codex', 'claude'):
        status, url, key = g.engine_status(engine)
        model, effort, _ = settings.for_engine(engine)
        item(engine.title() + ' 配置', '已配置（本次未验证模型）' if url and key else '未配置',
             f'{model} / {effort} / {url or "无 Endpoint"} / 最近状态 {status}')
        cwd = g.store.workdir(g.config.allowed_chat_id, engine)
        item(engine.title() + ' 工作目录', '正常' if Path(cwd).is_dir() else '异常', cwd)
    try:
        await asyncio.wait_for(g.telegram.get_me(), 15)
        network = await g.telegram.network_status()
        item('Telegram Bot API', '正常', network['network'] + ' / getMe 成功；文件上传需实际发送验证')
    except Exception as exc:
        item('Telegram Bot API', '异常', g.sanitize(str(exc)) or type(exc).__name__)
    if g.config.telegram_proxy_kind == 'subscription':
        proxy = await systemd_state('codex-tg-proxy.service')
        refresh = await systemd_state('codex-tg-proxy-refresh.timer')
        network = await g.telegram.network_status()
        item('Mihomo/订阅', '正常' if proxy == 'active' and refresh == 'active' and network['state'] == '正常' else '异常',
             f"服务 {proxy} / 刷新 {refresh} / {network['state']}")
        repairable |= proxy != 'active' or refresh != 'active'
    else:
        item('Mihomo', '未配置' if g.config.telegram_proxy_kind == 'none' else '使用自定义代理', g.config.telegram_network)
    item('Tavily', '未配置' if not g.tavily.api_key else '已配置（本次未请求搜索）',
         f"{'开启' if settings.tavily_enabled else '关闭'} / 最近状态 {g.tavily_state(settings.tavily_enabled)}；不可用时原生搜索回退")
    item('SQLite', '正常' if g.store.healthy() else '异常', 'quick_check；任务和文件记录已持久化')
    for engine in ('codex', 'claude'):
        session = g.store.engine_session(g.config.allowed_chat_id, engine)
        if not session:
            item(engine.title() + ' 会话', '未配置', '尚未创建')
            continue
        try:
            if engine == 'claude':
                available = bool(g.claude_runtime.session_path(session))
            else:
                response = await asyncio.wait_for(g.runtime.client.thread_read(session, include_turns=False), 8)
                available = response.thread.id == session
            item(engine.title() + ' 会话', '正常' if available else '异常', session if available else '映射存在，但会话文件未找到（空会话可能尚未持久化）')
        except Exception:
            item(engine.title() + ' 会话', '未验证', '映射存在；运行时无法读取，未发送模型请求')
    for name, folder in [('上传目录', g.config.upload_dir), ('成品目录', g.artifacts.vault)]:
        try:
            with tempfile.TemporaryFile(dir=folder) as output:
                output.write(b'diagnostic filesystem check')
                output.flush()
                os.fsync(output.fileno())
            item(name, '正常', '可读写')
        except OSError as exc:
            item(name, '异常', type(exc).__name__)
            repairable = True
    files = g.store.artifacts(g.config.allowed_chat_id, 1)
    item('文件交付', '尚未验证' if not files else ('正常' if files[0]['status'] == 'DELIVERED' else '待处理/异常'),
         '尚无发送记录' if not files else f"最近文件 {files[0]['name']} / {files[0]['status']} / {files[0]['error'] or ''}")
    free = shutil.disk_usage(g.config.database_path.parent).free
    item('磁盘空间', '正常' if free >= 512 * 1024 * 1024 else '异常', f'剩余 {free / 1024**3:.2f} GiB')
    missing = [name for name in ('curl', 'openssl', 'systemctl') if not shutil.which(name)]
    item('必要依赖', '异常' if missing else '正常', ', '.join(missing) if missing else 'curl / openssl / systemctl')
    updater = Path('/usr/local/bin/codex-tg-updater')
    item('Updater', '正常' if os.access(updater, os.X_OK) else '异常',
         str((g.update_status() or {}).get('detail') or ('可执行' if updater.exists() else '未安装')))
    try:
        async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
            manifest, signature = await asyncio.gather(client.get(g.config.release_base_url + '/latest.json'),
                                                       client.get(g.config.release_base_url + '/latest.json.sig'))
            manifest.raise_for_status()
            signature.raise_for_status()
        public = serialization.load_pem_public_key(g.config.release_public_key_path.read_bytes())
        public.verify(signature.content, manifest.content)
        item('Release', '正常', f"最新 {json.loads(manifest.content)['version']} / Ed25519 验签通过")
    except Exception as exc:
        item('Release', '异常', f'下载或验签失败：{type(exc).__name__}')
    prompt = g.codex_prompt_state()
    item('Codex Instructions', '正常' if prompt.configured else '未配置', f"{'ON' if prompt.enabled else 'OFF'} / {prompt.length} 字符")
    backups = g.backups.list()
    try:
        if backups:
            g.backups.read(backups[0].name)
        item('加密配置备份', '正常' if backups else '未配置', backups[0].name if backups else '可用 /backup 创建')
    except Exception as exc:
        item('加密配置备份', '异常', f'解密校验失败：{type(exc).__name__}')
    report = '\n'.join(lines)
    # Only the sanitized report is persisted, never endpoint keys or proxy URLs.
    g.store.set_meta('last_diagnosis_sha256', hashlib.sha256(report.encode()).hexdigest())
    return report, repairable
