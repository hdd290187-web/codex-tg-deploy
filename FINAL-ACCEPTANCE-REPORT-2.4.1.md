# Codex TG 2.4.1 最终验收与组件更新报告

验收日期：2026-11-14
最终版本：2.4.1
最终 Release SHA-256：`accdea1629db961f6818a583cb4b41916ba1cfef52b4b6b29d28bd9f01b16088`

## 版本与运行状态

- Gateway Release：`2.4.1`
- Gateway SDK：`openai-codex 0.169.0`
- Gateway bundled Runtime：`openai-codex-cli-bin 0.169.0`，实际 `codex-cli 0.169.0`
- 全局 Codex CLI：`codex-cli 0.169.0`（npm 安装，Gateway 保持独立 bundled Runtime）
- Claude Code：官方 stable 渠道 `2.1.311`；x86_64 与 arm64 二进制均按官方 SHA-256 校验
- Deployment Center：`codex-deploy.service` active + enabled
- 公网健康接口：`status=ok`、`database=true`、`release=2.4.1`
- 公网 `latest.json`、签名、归档 SHA-256 和配置资源已重新校验

Claude 2.1.311 的 headless 运行参数仍包含 `bypassPermissions`、`--dangerously-skip-permissions`、Session resume、Effort、严格 MCP 和文件工具所需选项。新版本将 MCP 工具按官方 `ToolSearch` 延迟加载协议处理，Gateway 的文件交付回归测试覆盖了这个流程。

## 审计问题验收

- P1：上一轮报告中的 8 项全部已修复并由现有硬化测试、ConfigSync、Installer、Updater、回滚和安全用例覆盖。
- P2：上一轮报告中的 9 项全部已处理；TOML 注释保留和旧 Release 清理分别由 AST 合并和本次 `--keep 1 --apply` 清理验证。
- ConfigSync：公网模板顶层版本、签名、三方合并、冲突、原子写入、symlink 安全边界和 Runtime reload 已验证。
- Telegram：durable inbox、幂等任务源、批次事务、重启恢复、身份绑定 challenge 和文件交付授权已验证。
- 安全：导出脱敏、凭据分离、root/bypass 权限边界、下载代理、归档路径校验、签名和 SHA-256 校验已验证。

## 测试结果

- Python 回归：`234 passed, 6 warnings`
- Ruff：`gateway deployment-center release-tools tests` 全部通过
- compileall：Gateway、Deployment Center、Release Tools 全部通过
- Shell 语法：Installer、Updater、Mihomo setup/refresh 和 Installer 容器脚本通过
- `pip check`：无依赖冲突
- Installer 容器矩阵：15 个场景全部通过，包括 Ubuntu 22.04/Python 3.10、Ubuntu 24.04、Debian 12、重复安装、部分配置、代理、签名失败、Codex 失败、健康失败回滚和 Prompt 状态保护
- Codex 0.169.0：组件配对、app-server fixture、Thread、streaming、interrupt 和模型目录测试通过
- Claude 2.1.311：CLI 参数、权限、Session、Effort、MCP、ToolSearch 延迟工具和 Codex/Claude 文件交付 fixture 通过
- Deployment Center：本地服务和公网 health/latest manifest/signature 实测通过

没有真实 Telegram Bot Token、真实中转站 Key 或用户目标 VPS 凭据，因此本机没有冒充执行生产 Telegram 消息、真实模型请求或远程 VPS `/updatecodex`/`/updateclaude`。这些命令已通过隔离候选、签名资源、版本配对、健康回滚和本地协议 fixture 验证；目标 VPS 的真实升级仍需在目标机上受控执行。

## GitHub 公开仓库

- 仓库：`https://github.com/hdd290187-web/codex-tg-deploy`
- 仓库保持 Public；未上传 Release 私钥、配置私钥、生产数据库、凭据、Session、上传文件、日志或 Release 二进制。
- 当前工作树 Secret Scan：0 findings。
- 对全部 5 个可达 Git commit 的历史扫描未发现真实凭据；命中的内容仅是 Installer 变量名、测试 fixture 和明确标记的假凭据。
- GitHub Secret Scanning、Push Protection 和 Dependabot security updates 已启用；GitHub 账户权限不允许启用额外的 non-provider pattern 规则时，仓库仍由本地 secret scanner 和 Push Protection 双重检查。
- Actions 使用标准 `ubuntu-latest` runner、`contents: read`、pip cache、20 分钟超时、过时运行取消和固定 Action commit SHA；外部 PR 不获得任何 Secrets 或写权限。
- GitHub Actions 修复后的新运行将在本次 push 后执行，必须以 `quality` 成功结论为最终远端验收条件。

## Release 清理

按照本次授权，Deployment Center 的旧 Gateway 归档、旧 wheelhouse、旧 SDK wheel 和旧版本 manifest 已删除；保留 2.4.1 当前归档、8 个 ABI/架构 wheelhouse、Claude 2.1.311、Mihomo、PySocks、latest manifest 和签名。Git 提交历史和版本 Tag 不重写，便于审计追踪。

## 未完成事项

没有已知的代码或发布阻塞问题。未验证项只有用户真实目标 VPS 的生产 Telegram、代理订阅、真实中转站模型调用和长期运行压力；这些需要目标机实际凭据和网络环境，当前服务器无法安全代测。
