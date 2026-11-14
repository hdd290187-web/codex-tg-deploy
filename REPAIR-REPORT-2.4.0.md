# Codex TG 2.4.0 修复与发布报告

执行日期：2026-11-14
基线：2.3.4 审计报告
最终版本：2.4.0
最终 Commit：`5a6c97d8aa4a31c34521cc25ed3b85b59448ef89`
最终 Tag：`v2.4.0`

## 一、交付结果

本次完成了审计报告中的 P1 修复和主要 P2 修复，没有覆盖生产中的目标 VPS 运行数据，也没有上传生产密钥、数据库、日志、会话、用户文件或 Release 二进制。

2.4.0 已完成：

- Gateway Release 打包、签名和 SHA-256 发布。
- 公网 `latest.json` 更新到 2.4.0。
- Deployment Center 重启后保持 `active + enabled`。
- 本地和公网健康接口返回 `release=2.4.0`。
- 公网 Agent 配置清单包含全局版本 `2`，Gateway `ConfigSync.fetch()` 和临时目录 `sync()` 实测成功。
- Release 清单 Ed25519 签名验证通过。
- Agent 配置清单 Ed25519 签名验证通过。

Release SHA-256：

```text
cf5500256cbfb1c2065daa8fb584881ef140ddbb78ce564f83f28019059d61c9
```

## 二、P1 修复

### 1. ConfigSync 清单协议

Deployment Center 配置清单现在包含：

- 顶层 `version` 全局 generation。
- `codex.version`。
- `claude.version`。
- 每个引擎的 `content` 和 SHA-256。

Gateway 兼容带全局版本和旧的仅引擎版本格式，并验证签名、格式、每个引擎版本和内容哈希。当前公网清单实际验证结果：

```text
global version: 2
codex version: 1
claude version: 1
ConfigSync.fetch(): success
ConfigSync.sync(): success
```

### 2. Telegram 首次绑定 challenge

Deployment Center Bot 验证成功后生成一次性随机 challenge，网页显示：

```text
/start <challenge>
```

服务端只接受精确匹配 challenge 的私聊 `/start`，普通 `/start`、错误 challenge、其他用户消息都不会绑定。绑定 challenge 保存在加密 binding payload 中，过期后随 binding 清理。

### 3. Telegram durable inbox 和幂等任务

新增加密 `inbound_updates` 表和 0600 `inbox.key`：

- Telegram update 先加密落库，再推进 offset。
- 已授权消息进入 `RECEIVED` 状态。
- 任务/回调处理完成后标记 `DONE` 并清理 payload。
- 命令或外部副作用结果不确定时标记 `UNCERTAIN`，不会自动重放。
- 普通任务使用 `telegram:<chat_id>:<update_id>` 作为唯一 source reference。
- 相同 update 或重启恢复不会创建重复任务。
- `/inbox` 可查看待核对记录；只有可安全重放的普通消息允许显式重试。
- 启动时 `PROCESSING` 记录会按 replayable 状态恢复。

原来的“先写 offset、再处理 update”路径已删除。

### 4. 多文件批次事务

批次提交现在经过：

```text
OPEN/SUBMIT → SUBMITTING → SQLite 同事务写入 tasks + upload_batches.task_id → SUBMITTED
```

批次 task 使用 `source_ref=batch:<batch_id>` 唯一键。Gateway 在 `SUBMITTING` 状态退出后，启动时会恢复为 `SUBMIT`；再次提交只返回已有 task，不会重复执行。批次锁不再覆盖整个外部 Thread 创建和 Telegram 通知等待区间。

### 5. `/configexport` 脱敏

新增保守递归脱敏：

- Authorization、Cookie、Bearer、Basic、Token、Secret、Credential、Key。
- `env`、`headers`、`args`、`command` 等容器默认整体保护。
- URL 中的 userinfo、query、fragment 被移除。
- 常见 OpenAI、Anthropic、Tavily、GitHub、Bot Token、私钥格式被处理。
- 导出文件使用 UUID 文件名和 0600 原子写入。
- Telegram 发送结束后删除临时导出文件。

临时回归测试确认 `http_headers.Authorization` 和自定义环境值不会出现在导出结果中。

### 6. 配置同步实际生效

`/syncconfig` 现在：

- 要求无运行任务、队列和更新活动。
- 获取 activity exclusive lock。
- 解析、合并、校验后原子写入。
- Codex 配置写入后安全重建 Runtime，保留长期 Thread 映射。
- Claude 每个新 Turn 读取经过清理的 settings snapshot。
- 当前任务、队列任务和文件交付期间拒绝同步。
- 失败恢复原文件和原状态。

Claude Gateway 的 per-Turn snapshot 保留用户的普通官方设置，但强制 Gateway 的 bypass 权限和显式 MCP contract，移除 hooks、apiKeyHelper、外部认证和代理变量，避免配置模板破坏无人值守 root 运行。

### 7. Installer/Updater 代理路径

Installer 在 Release manifest、归档、wheelhouse 和 Claude 文件下载前完成代理 bootstrap；所有大文件下载使用临时 curl 配置，不写系统级代理环境变量。

Updater 的 `download()` 统一使用显式 curl、临时 proxy 配置、大小限制、超时和重试；订阅模式使用 Mihomo HTTP mixed port，其他流量不被重定向。

### 8. CLI/SDK 更新供应链

`/updatecodex` 和 `/updateclaude` 现在使用 Deployment Center 当前签名 Release manifest 的审核版本和资源：

- Release manifest 由项目 Release 公钥验证。
- SDK/Runtime 版本必须由签名清单固定。
- Claude 版本和二进制资源来自签名清单。
- wheelhouse 和组件 hash 使用中心发布值。
- 目标 VPS 不再根据 PyPI “latest”自行决定候选版本。
- 候选环境验证、pip check、bundled Runtime、自检和 health 通过后才切换。
- 失败保留旧环境并恢复旧服务。

## 三、P2 修复

- Claude `max` Effort 只保留在 Gateway CLI/任务快照中，不再写入无效的 settings 文件顶层 `effortLevel`。
- TOML ConfigSync 改为在 AST 上应用变更，减少无关内容和注释丢失。
- ConfigSync 备份使用纳秒和随机后缀，避免同一秒覆盖。
- ConfigSync 支持受控 symlink 目标，管理员托管的 Codex 配置不会被简单判为异常。
- Backups 覆盖 `inbox.key`、Codex config、Claude settings、ConfigSync state 和模板基线。
- Gateway 启动清理无有效 RUNNING 任务的旧 `delivery-context.json`。
- File MCP context 带过期时间，并动态读取已存在的环境密钥用于凭据扫描。
- 文件上传服务端错误/超时更保守地进入 UNKNOWN，不自动重复发送可能已被 Telegram 接收的文件。
- Mihomo subscription 改为本地 file provider，刷新前校验候选内容和配置，失败恢复文件并尝试 reload 旧配置。
- Deployment Center 关闭环境代理继承，订阅 URL 校验将 DNS 解析地址固定到请求连接，减少 DNS rebinding 风险。
- Agent Endpoint 对公网 HTTP 做限制，本地 loopback/private 开发地址保留兼容性。
- SQLite 增加任务、Artifact、批次和 inbound update 索引；maintenance 定期清理过期 events、已完成 inbound update 和长期任务。
- 新增 `release-tools/prune_releases.py --keep N`，默认 dry-run；不会自动删除仍可能用于回滚的旧 Release。
- 新增 CI quality workflow、贡献说明和安全扫描脚本。

## 四、架构优化

- Gateway 配置流程抽到 `gateway/codex_tg/configuration_flow.py`。
- Endpoint/任务 source reference 通过 SQLite 唯一键统一幂等处理。
- ConfigSync 使用活动锁保护配置写入、Runtime 重载和备份。
- Inbox、batch、delivery、updater 共享现有 activity lock，避免更新与运行任务并发切换。
- 清理了原始 polling 的重复 offset/processing 状态路径。
- 保留现有双引擎 API、Thread/Session、文件 MCP、Tavily、Instructions、Installer 和 updater 对外行为。

## 五、测试和验证

最终完整测试：

```text
234 passed, 6 warnings
```

额外通过：

- Ruff：`gateway deployment-center release-tools tests`。
- Python compileall。
- Bash 语法检查。
- Node 前端语法检查。
- pip check。
- staged Secret Scan：37 个变更文件，0 findings。
- Installer 容器矩阵：全部通过。
- ConfigSync 实际公网 `fetch()` 和临时数据目录 `sync()`：通过。
- Deployment Center 本地/公网健康：2.4.0。
- Release archive SHA-256 和 Ed25519：通过。
- Config template SHA-256 和 Ed25519：通过。
- Claude 2.1.283 本地协议 fixture：权限、MCP、文件工具、Session resume：通过。
- Codex 0.162.1 bundled Runtime fixture：Thread、streaming、interrupt、model catalog：通过。

没有在真实目标 VPS 上执行：

- 生产 Telegram Bot 消息和真实文件上传。
- 用户真实 Codex/Claude 中转站模型请求。
- 真实目标 VPS 的 reboot、kill -9、Mihomo 线上订阅和长期 Session。

这些项目只能通过隔离 fixture、容器和静态路径验证，不能标记为生产端到端通过。

## 六、GitHub 版本管理

已创建私有仓库：

<https://github.com/hdd290187-web/codex-tg-deploy>

当前认证账号：`hdd290187-web`。仓库为 private。

Git 历史：

```text
785ecd7 baseline-v2.3.4
4b2e674 fix: harden gateway delivery sync and updates
5a6c97d release: publish 2.4.0
```

远端 Tag：

- `baseline-v2.3.4`
- `v2.4.0`

远端 `main` 与本地 Commit `5a6c97d8aa4a31c34521cc25ed3b85b59448ef89` 一致。

GitHub 上传内容经过 `.gitignore` 和 staged Secret Scan：

- 未上传 Release 归档、wheelhouse、Mihomo 二进制。
- 未上传 `keys/`、私钥、生产数据库、`.env`、credentials、sessions、uploads、logs、backups。
- GitHub API 对私钥、生产数据库和 Release latest 文件的路径查询均不存在。
- Git 工作区与 `origin/main` clean，无强制推送。

## 七、剩余事项

本次已完成所有高优先级修复和主要 P2 修复，但仍建议后续处理：

1. 真实目标 VPS 上执行一次 `/syncconfig`、`/diagnose`、`/update` 和 `/updatecodex` 观察。
2. 对真实 Telegram API 做一次绑定 challenge、普通任务、文件发送和 restart recovery 验证。
3. 增加 coverage、类型检查和依赖 CVE 扫描作为 CI 阻断。
4. 根据真实 VPS 使用情况决定是否执行 `prune_releases.py --apply`；本次没有删除历史 Release。
5. 继续拆分 Gateway 命令路由和运行编排，避免未来功能继续扩大单体类。

## 八、最终结论

2.4.0 已完成修复、测试、签名发布和 GitHub 私有仓库同步。当前 Deployment Center 运行正常，Release 可从公网验证，GitHub 远端源码和 Tag 已核对，确认没有上传生产敏感数据。目标 VPS 的真实 Telegram、中转站和长期运行行为仍需要在用户实际环境中做一次受控验收。
