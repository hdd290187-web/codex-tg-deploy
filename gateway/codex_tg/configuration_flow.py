from __future__ import annotations


class ConfigurationFlow:
    async def config_status_message(self, chat_id: int) -> None:
        lines = ['Agent 配置状态（模板不会覆盖环境变量、Key 或命令行覆盖）']
        for engine in ('codex', 'claude'):
            item = self.config_sync.status(engine)
            lines.append(f"{engine.title()}：模板 v{item.get('template_version') or '未同步'}；本地修改：{'是' if item['local_modified'] else '否'}；冲突：{len(item['conflicts'])}")
            lines.append(f"路径：{item['path']}")
        await self.telegram.send(chat_id, '\n'.join(lines))

    async def sync_config(self, chat_id: int, strategy: str | None = None) -> None:
        try:
            if self.store.active() or self.store.queue_count() or self.updating():
                raise ValueError('请等待任务和更新结束后再同步配置')
            async def activate():
                await self.runtime.close()
                self.runtime.threads.clear()
                self.runtime.thread_plan_efforts.clear()
                await self.runtime.configure(self.credentials.get('codex'), self.search_config(self.credentials.current_ref('tavily')) if self.store.chat_settings(chat_id).tavily_enabled else None)
                # Claude reads an immutable --settings snapshot at the next turn.
                from .config_files import claude_snapshot
                claude_snapshot(self.claude_runtime.data_dir / 'settings.json')
            result = await self.config_sync.sync(strategy, activate=activate)
            if result['conflicts']:
                lines = ['配置同步发现冲突，冲突字段暂未覆盖：']
                lines += [f"{x['engine']}.{x['path']}" for x in result['conflicts'][:20]]
                await self.telegram.send_keyboard(chat_id, '\n'.join(lines), [[
                    {'text': '保留本地值', 'callback_data': 'config:local'},
                    {'text': '采用模板值', 'callback_data': 'config:template'},
                ]])
            else:
                changed = sum(len(v.get('changes', [])) for v in result['engines'].values())
                await self.telegram.send(chat_id, f"配置模板 v{result['version']} 已同步；合并变更 {changed} 项。Codex Runtime 已重载，原 Thread 保留；Claude 下一条任务读取新配置。")
        except Exception as exc:
            await self.telegram.send(chat_id, '配置同步失败，原配置保留：' + self.sanitize(str(exc))[:500])

    async def export_config(self, chat_id: int) -> None:
        paths = []
        try:
            for engine in ('codex', 'claude'):
                paths.append(self.config_sync.export(engine))
            for path in paths:
                await self.telegram.send_document(chat_id, path, f'{path.stem}（已脱敏）')
        finally:
            for path in paths:
                path.unlink(missing_ok=True)
