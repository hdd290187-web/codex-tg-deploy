# Codex TG Deployment Center + Gateway 2.3.4 审计报告

审计日期：2026-11-14  
审计对象：`/opt/codex-deploy` 当前源码、当前 Deployment Center 服务、2.3.4 Release 清单及可访问的本机运行环境  
审计范围：代码、数据流、数据库、异步任务、权限、文件交付、代理、更新/回滚、配置同步、测试和供应链  
审计方式：只读代码追踪、实际命令和服务检查、完整测试、隔离协议测试、签名校验、临时验证脚本。没有修改源码、线上配置或发布新版本。

## 一、审计边界和实际环境

当前源码版本是 2.3.4。Deployment Center 在本机运行，systemd 状态为 `active + enabled`，本地和公网健康接口均返回 `status=ok`、`release=2.3.4`。2.3.4 Release 归档 SHA-256、Release Ed25519 签名和配置模板 Ed25519 签名均已重新验证。

当前这台服务器没有 `/opt/codex-tg`、`codex-tg.service`、Mihomo 或目标 Gateway 数据库。因此，本次不能把 Deployment Center 服务器当作一台已经部署 Gateway 的目标 VPS，也不能声称目标 VPS 的实时 systemd、Telegram、真实中转站和生产会话全部正常。当前本机有全局 Codex CLI 0.166.0，但 Gateway 发布包使用的 Python SDK 和 bundled Runtime 是 0.162.1；这两者是有意隔离的。

项目目录不是 Git 仓库，没有 branch、commit、tag 或可自动追踪的源码基线。当前 `releases/` 中有 177 个 Gateway 归档、约 776 MB wheelhouse，整个工作目录约 2.4 GB。

## 二、整体架构和数据流

```mermaid
flowchart LR
    Web[Deployment Center Web] -->|加密一次性 claim| Installer[Installer]
    Installer -->|签名清单/SHA256| Release[Release files]
    Installer --> VPS[目标 VPS]
    VPS --> Gateway[Telegram Gateway root service]
    Gateway --> Store[(SQLite WAL)]
    Gateway --> Creds[0600 credentials.json]
    Gateway --> Codex[Python SDK + bundled Codex app-server]
    Gateway --> Claude[Claude Code CLI]
    Gateway --> Tavily[Tavily MCP]
    Gateway --> Telegram[Telegram Bot API]
    Gateway --> Files[Artifacts vault + delivery worker]
    TGUpdate[/update] --> Updater[独立 updater]
    Updater -->|签名/SHA256/health| Release
    Config[/syncconfig] --> SignedConfig[签名配置模板]
    SignedConfig --> Merge[三方合并]
```

项目实际采用单 Chat、单 Gateway、单 Worker 的模型。Gateway 中仍有两个独立引擎状态：Codex 的长期 Thread 和 Claude 的长期 Session；任务入队时冻结 engine、model、effort、Endpoint revision、Tavily revision、工作目录和 Codex prompt revision。SQLite 负责状态和队列，`credentials.json` 负责版本化密钥，`PromptStore` 负责前置 Instructions，`Artifacts` 负责文件副本和交付状态。

Deployment Center 负责网页认证、Bot Token 验证与绑定、一次性部署 claim、Release 服务、配置模板编辑和签名。Installer 在目标 VPS 上以 root 原子写入配置、部署 systemd、安装离线 wheelhouse、安装 Claude 二进制并执行自检。Gateway 自身更新、Codex SDK/Runtime 更新和 Claude CLI 更新由独立 updater 处理，目标是旁路候选、验证后切换、健康失败回滚。

整体模块边界已经比早期单体 CodexService 清晰：Telegram、Store、Runtime、ClaudeRuntime、Artifacts、ConfigSync、Backups、Updater 和 Deployment Center 都有独立文件。`Gateway` 仍然是一个 2200 行级别的编排类，命令路由、启动恢复、队列、更新、交付和诊断仍集中在一个类中，这是未来维护成本的主要来源。

## 三、实际验证结果

已执行的检查如下：

- 完整测试套件：**231 passed**，约 130 秒，6 个弃用警告。
- `compileall`：通过。
- `ruff check gateway deployment-center release-tools`：通过。
- `bash -n`：Installer 和容器脚本通过。
- Node `--check`：Deployment Center 三个前端脚本通过。
- `pip check`：通过。
- Claude 2.1.283 本地 Anthropic SSE fixture：Bash、Read、Write、Edit、WebSearch、Tavily MCP、Session resume、Effort 请求参数均通过。
- Codex 0.162.1 bundled Runtime：配置加载、Thread 创建、模型目录 fixture、Responses streaming、interrupt、history resume 通过。
- Installer 容器矩阵：Ubuntu 22.04/Python 3.10、Ubuntu 24.04、Debian 12、重复安装、部分配置、DIRECT、订阅代理、自定义 SOCKS、签名失败、Codex 失败、健康失败回滚和 Prompt 场景通过。
- Release 2.3.4：公开 `latest.json`、归档 SHA-256、Release 签名、配置模板签名通过。
- Deployment Center 认证配置页：临时会话实际请求返回 200，编辑器内容存在。
- 受限静态扫描：没有发现 `shell=True`、`os.system` 或使用未经校验的 shell 拼接来执行 Agent 输入；Gateway 的外部命令使用 `create_subprocess_exec` 或参数数组。

额外检查发现：

- `ruff` 如果把 `tests/` 也纳入检查，会在 `tests/test_dual_engine.py:325` 和 `tests/test_dual_engine.py:354` 报 E701/E702。Release 门禁目前只 lint Gateway 和 Deployment Center，所以测试目录的 lint 并未纳入发布阻断。
- 直接对 Python 文件运行 ShellCheck 会得到 SC1071，这是工具误用；对 Installer 单独运行 ShellCheck 还会报告 `&& ... || true` 的 SC2015 信息，以及 `SUCCESS` 未使用的 SC2034 警告。Installer 的 Bash 语法本身通过。
- 环境没有 coverage 插件，完整测试没有覆盖率数字。`pip-audit`、Bandit、Semgrep 也没有安装，因此没有完成依赖 CVE 扫描或通用 SAST 扫描。
- `tests/live_*.py` 是显式 opt-in 的真实中转站测试，完整测试不会运行它们；没有生产 Telegram Bot Token、目标 VPS 或用户中转站凭据，本次没有执行生产模型请求。

## 四、已确认的问题（按优先级）

### P0：目前没有确认的 P0 代码漏洞

没有发现无需 Telegram 身份、Deployment Center 会话或一次性安装 Token 即可直接调用 Gateway Agent 的路径。没有发现将任意 Telegram 文本直接拼接为 shell 命令并在 Deployment Center 进程中执行的代码。

但是 Gateway 明确以 root、full access、无人确认模式运行。只要攻击者取得有效的授权 Telegram 身份、Bot 控制权或目标 VPS 上的 Gateway 配置密钥，影响就是整台 VPS 的完全控制。这是产品明确选择的能力边界，不是可以忽略的安全风险；长期部署应当把 Bot Token、授权 User ID/Chat ID、VPS 文件权限和 Telegram 账号安全视为同一条 root 信任链。

### P1-01：公网配置模板清单与 Gateway ConfigSync 协议不兼容（已实际复现）

位置：

- [deployment-center/codex_deploy/app.py:455](/opt/codex-deploy/deployment-center/codex_deploy/app.py:455)
- [gateway/codex_tg/config_sync.py:166](/opt/codex-deploy/gateway/codex_tg/config_sync.py:166)

`config_payload()` 生成的公网 JSON 只有 `format`、`codex`、`claude`；版本放在两个引擎对象内部，没有顶层 `version`。Gateway `ConfigSync.fetch()` 却强制执行：

```python
re.fullmatch(r'\d+', str(payload.get('version', '')))
```

我直接对当前公网 `https://gpt.cevyo.com/codex-deploy/config-templates/latest.json` 运行了 Gateway 的 `ConfigSync.fetch()`，结果是：

```text
ConfigSyncError 配置模板版本格式无效
```

因此当前目标 VPS 的初始签名模板同步和 `/syncconfig` 实际都会失败。Gateway 启动时会把该错误记录为“Initial signed Agent config template sync skipped”，然后继续运行，造成配置同步功能看起来存在但实际没有生效。

影响：新模板不会自动同步到现有 VPS；`/syncconfig` 无法完成；`/configstatus` 可能长期显示未同步。Release 和 Gateway 主任务不受影响。

建议：在签名清单中加入明确的顶层全局版本，或把 Gateway 改为分别读取 `codex.version` 和 `claude.version`。更稳妥的格式是同时保留顶层 generation 和每个引擎的版本，并在校验时验证两者。应增加“真实公网清单 → `ConfigSync.fetch()` → `sync()`”回归测试；当前测试使用了手工带 `version` 的 fixture，所以没有发现这个集成错误。

### P1-02：Deployment Center 的 Telegram 绑定可以被任意 `/start` 用户抢占

位置：

- [deployment-center/codex_deploy/telegram.py:41](/opt/codex-deploy/deployment-center/codex_deploy/telegram.py:41)
- [deployment-center/codex_deploy/app.py:594](/opt/codex-deploy/deployment-center/codex_deploy/app.py:594)
- [deployment-center/codex_deploy/static/app.js:74](/opt/codex-deploy/deployment-center/codex_deploy/static/app.js:74)

网页验证 Bot Token 后，Deployment Center 轮询 Bot 的 `getUpdates`，只要看到任意私聊中以 `/start` 开头的消息，就把该消息的 User ID 和 Chat ID 写入当前网页会话的绑定。代码没有把网页会话和 Telegram 用户通过一次性 nonce 绑定，也没有要求 `/start <challenge>` 中包含网页显示的随机值。

触发条件：攻击者知道 Bot 用户名，并在网页操作者发送 `/start` 前后向该 Bot 发送 `/start`。若攻击者的消息被当前 `poll_start()` 读取，网页会显示绑定成功，后续生成的 Gateway 会把攻击者的 ID 作为唯一授权身份。

影响：部署出的 Gateway 可能授权错误的 Telegram 用户。Bot Token 本身不需要泄露，攻击者只需要能给公开 Bot 发消息并赢得轮询时序。这个问题直接影响“非授权 User ID + Chat ID 不能访问任何功能”的核心安全目标。

建议：验证 Bot 后生成一次性短 challenge，网页显示“向 Bot 发送 `/start <challenge>`”；服务端只接受匹配 challenge 的消息，并校验同一 Bot ID、私聊类型和未过期会话。更高安全性可以在网页显示待绑定 User ID 后要求人工确认，或要求用户在网页输入 Telegram 返回的短码。绑定完成后应停止继续消费该 Bot 的更新。

### P1-03：Telegram polling 在处理前持久化 offset，崩溃会永久丢消息

位置：[gateway/codex_tg/app.py:2140](/opt/codex-deploy/gateway/codex_tg/app.py:2140)

`poll()` 在调用 `handle_message()` 或 `handle_callback()` 之前，先写入 `telegram_offset`，并把 `telegram_processing_update` 只保存为 update ID。若进程在写 offset 后、任务入队前崩溃，重启时 Telegram 不会再次返回该 update；启动通知只告诉用户“没有自动重放”，但没有保存原始消息，也没有可恢复的任务。

这是当前代码明确选择的 at-most-once 策略，目的是避免重复执行破坏性任务，但代价是普通任务、文件上传、按钮操作都可能丢失。网络异常导致 `handle_message()` 抛错时也会先推进 offset。

建议增加持久化 inbound_updates 表：先以 `(update_id, chat_id)` 去重写入原始 update，再在同一数据库状态机中标记 RECEIVED/PROCESSING/DONE/FAILED；只有处理成功或明确记录不可处理后才推进 offset。对有副作用的任务使用任务唯一键，恢复时不重复执行但能明确告诉用户该 update 未提交。

### P1-04：多文件批次的“创建任务”和“批次已提交”不是一个事务，崩溃后可能重复执行

位置：[gateway/codex_tg/batches.py:115](/opt/codex-deploy/gateway/codex_tg/batches.py:115)

`Batches.process()` 在持有 Python lock 时调用 `submit_batch_task()`，后者会先向 `tasks` 写入任务；之后才执行：

```python
UPDATE upload_batches SET task_id=?, state='SUBMITTED'
```

如果 Gateway 在两步之间退出，批次仍然是 OPEN/SUBMIT、`task_id` 为空，但任务已经排队。重启后批处理循环会再次提交同一批文件，产生重复 Agent 任务和重复副作用。Python lock 不能跨重启保护这个窗口。

此外，提交期间持有批次 lock，并等待创建 Thread/Session、数据库和 Telegram 操作，可能阻塞同一批次的后续文件处理和普通命令。

建议把“批次转换为任务”和批次状态更新放进同一个 SQLite `BEGIN IMMEDIATE` 事务，或者先原子设置 `SUBMITTING` 并保存幂等键，再由恢复逻辑检查已有 task_id。提交操作应拆成短事务，不能在 Python lock 中等待外部网络或 Agent 初始化。

### P1-05：`/configexport` 的脱敏规则会泄露任意 Header/环境值

位置：[gateway/codex_tg/config_sync.py:254](/opt/codex-deploy/gateway/codex_tg/config_sync.py:254)

`export()` 只依据字段名中的 `token`、`secret`、`password`、`api_key` 等关键词脱敏。字段名为 `Authorization` 时，当前判断不是大小写不敏感的完整敏感字段判断，也不匹配这些子串；`http_headers.Authorization = "Bearer ..."` 会原样出现在导出文件。自定义 MCP 参数、任意 Header 值、URL query 中的凭据也可能使用普通字段名绕过规则。

我用临时 TOML 验证了：

```toml
[http_headers]
Authorization = "Bearer [fixture-secret]"
```

导出文件仍包含该值。文件随后通过 Telegram 发送，因此这不是只影响本地临时文件的风险。

建议使用递归的“字段名 + 值模式”联合脱敏：大小写不敏感匹配 `authorization`、`cookie`、`x-api-key`、`bearer`、`basic`、`private`、`credential`、`query` 等；对所有字符串扫描 Bot Token、OpenAI/Anthropic/Tavily 形式、Bearer/Basic 值和 URL 中的 userinfo/query；默认对 `env`、`headers`、`args`、`command` 采用更严格策略。导出前应加入独立的“无凭据断言”。

### P1-06：配置同步写入文件后不会让正在运行的引擎重新加载

位置：

- [gateway/codex_tg/app.py:1387](/opt/codex-deploy/gateway/codex_tg/app.py:1387)
- [gateway/codex_tg/config_sync.py:196](/opt/codex-deploy/gateway/codex_tg/config_sync.py:196)
- [gateway/codex_tg/claude_runtime.py:212](/opt/codex-deploy/gateway/codex_tg/claude_runtime.py:212)

`/syncconfig` 成功后只原子写入 `config.toml` 和 `settings.json`，没有重启或重建 Codex app-server，也没有刷新 Claude Session。Codex SDK 进程通常在 Gateway 启动时读取配置；已有进程不会因为文件变化而自动重新加载。Claude Gateway 明确使用 `--setting-sources ''`，因此 `settings.json` 不参与 Gateway 任务；模型、Effort、权限和 MCP 由命令行参数覆盖。

影响：Telegram 会收到“配置模板已同步”，但当前任务和后续任务可能继续使用旧的 app-server 配置；Claude 的配置模板对 Gateway 任务基本只对手动 CLI 有效。这个行为与“配置同步后实际生效”的产品语义不一致。

建议：同步结果明确区分 `文件已写入`、`需要重启才生效` 和 `当前 Session 不受影响`。对 Codex 在无任务、无队列、无交付时安全重建 Runtime；对 Claude 明确模板只作用于手动 CLI，或构造受控 `--settings` 文件并保留 bypass/deny 安全覆盖。任何自动重启都必须经活动锁和用户确认。

### P1-07：Installer 和 Gateway updater 的 Release 下载没有统一走大陆代理

位置：

- Installer 在 [installer/install.sh:412](/opt/codex-deploy/installer/install.sh:412) 开始下载 Release/Manifest，直到 [installer/install.sh:519](/opt/codex-deploy/installer/install.sh:519) 才配置 Telegram/Mihomo 网络。
- Updater 的 [gateway/bin/codex-tg-updater:270](/opt/codex-deploy/gateway/bin/codex-tg-updater:270) 使用 `urllib.request.urlopen()`，不接受 `scoped_install_network()` 生成的环境，也不会读取 `CODEX_TG_TELEGRAM_PROXY_URL`。

Installer 的确在 Gateway 依赖安装前启动代理，但清单、归档、wheelhouse 和 Claude 二进制已经在代理初始化前下载。Updater 的 Gateway Release、wheelhouse 和 Claude/SDK 相关下载也有同样问题；只有 `curl`/pip 子进程路径使用了临时代理配置。

在能直连 Deployment Center 的主机上不会触发；在需要代理才能访问 Release/PyPI 的大陆 VPS 上，安装或 `/update` 可能在前置下载阶段失败。这与项目声称的“代理支持安装和更新”不完全一致。

建议统一一个受限下载层：先启动/验证 Mihomo，再用显式 curl 配置下载所有 Release/Manifest/Claude/wheelhouse；或让 `download()` 接受经过校验的 `environment`/proxy 参数，禁止 urllib 默认环境和系统代理混用。新增无直连网络的安装和更新集成测试。

### P1-08：`/updatecodex` 的供应链信任边界弱于正常 Gateway Release

位置：[gateway/bin/codex-tg-updater:89](/opt/codex-deploy/gateway/bin/codex-tg-updater:89)

正常 `/update` 使用项目 Release 的 Ed25519 清单和项目私钥签名。`/updatecodex` 却直接查询 PyPI JSON，读取官方 `requires_dist`，下载指定 SDK/Runtime wheel，再用同一个 PyPI 元数据返回的 digest 校验 wheel。若 PyPI 元数据和 wheel 同时被篡改，二者仍然可以一致；这里没有项目 Release 签名或独立的可信 hash 清单。

Claude updater 也从官方稳定元数据和 manifest 获取 checksum，但没有项目侧签名。这个风险属于供应链边界，不是普通 HTTP 中间人（TLS 仍然有效），但 root 服务直接执行候选包，影响较大。

建议把 CLI/SDK 更新候选纳入 Deployment Center 签名清单，或由中心预先下载、独立审核并发布带项目签名的候选元数据；至少固定允许的包名、版本、平台和 hash，记录审核来源，避免 updater 在目标 VPS 上自行决定“最新”。

## 五、P2 风险和设计缺陷

### P2-01：Claude `max` Effort 会写入无效的 settings.json 值

Deployment Center 允许 `claude_effort` 使用 `max`，[deployment-center/codex_deploy/app.py:236](/opt/codex-deploy/deployment-center/codex_deploy/app.py:236)；Installer 又把它写成顶层 `settings.json.effortLevel`，[installer/install.sh:785](/opt/codex-deploy/installer/install.sh:785)。当前 Claude settings 规范的文件值是 `low`、`medium`、`high`、`xhigh`；`max` 是 CLI/运行时能力，不能按同样方式写成文件默认值。Gateway 任务的 `--effort max` 仍可能正常，因此问题主要表现为手动 CLI/配置审计显示无效或被忽略。

建议把 `max` 只保留在 SQLite/任务快照和 CLI 参数中；写入 settings 文件时保留原合法值或不写顶层 effort，并在页面提示“max 仅对 Gateway Turn 生效”。官方配置参考见 [Claude settings reference](https://code.claude.com/docs/en/settings-reference)。

### P2-02：配置同步会丢失 TOML 注释和格式，且同一秒的备份可能互相覆盖

位置：[gateway/codex_tg/config_sync.py:120](/opt/codex-deploy/gateway/codex_tg/config_sync.py:120)、[gateway/codex_tg/config_sync.py:188](/opt/codex-deploy/gateway/codex_tg/config_sync.py:188)。`tomlkit.loads(...).unwrap()` 转成普通 dict 后再 dumps，注释、部分布局和原始格式不可能保留；`path.name.before-sync-{int(time.time())}` 在同一秒多次同步会复用文件名并覆盖旧备份。

建议用 TOML AST 进行路径级修改，或明确把“注释和格式不保证保留”写进 UI；备份名改为纳入纳秒、随机 nonce 或版本号，并限制备份数量。

### P2-03：ConfigSync 拒绝符号链接，与 Installer 的既有配置保护策略冲突

Installer 支持 `/root/.codex/config.toml` 是符号链接并备份其真实目标；ConfigSync 的 `local()` 却直接把符号链接视为异常，[gateway/codex_tg/config_sync.py:143](/opt/codex-deploy/gateway/codex_tg/config_sync.py:143)。管理员使用 symlink 管理 Codex 配置时，Installer 可以升级但 `/syncconfig` 不能同步。

建议明确 symlink 策略：只允许解析到受信任的普通文件，并对目标路径做 `O_NOFOLLOW`/目录边界校验；或者在 ConfigSync 中报告“管理员托管，不能自动同步”，不要让状态看似可用。

### P2-04：任务、事件和 Release 没有生命周期清理策略

Gateway 的 `tasks`、`events`、`thread_counters`、批次记录和已交付 artifact 元数据没有统一保留策略；maintenance 只清理上传文件。长期运行会让 SQLite 和事件查询持续增长。Deployment Center 当前保留 177 个归档和 776 MB wheelhouse，未来每次发布都会继续增长。

建议增加可配置保留策略：任务/事件按时间和数量归档，保留当前会话相关任务；artifact 元数据和已交付副本分层清理；Release 保留最近 N 个可回滚版本、当前版本和显式 LTS，老 wheelhouse 移到离线归档。清理必须先验证没有目标 VPS 仍在使用对应版本。

### P2-05：Telegram 文件上传的网络超时仍可能导致重复文件

位置：[gateway/codex_tg/telegram.py:145](/opt/codex-deploy/gateway/codex_tg/telegram.py:145)。`sendDocument` 的连接/读取超时会进入普通重试；服务器可能已经接受文件但客户端没收到响应，此时最多会重复发送五次。代码只把非连接类异常标记为 `TelegramDeliveryUncertain`，但连接超时同样是结果不确定。

建议对有文件体的请求采用更保守策略：上传超时后标记 UNKNOWN，等待用户确认；或使用 Telegram 返回的可验证 file_id/业务幂等记录（如果 API 允许）避免自动重复。当前 `artifact_parts.message_id` 设计能降低已确认分片重复，但无法覆盖响应丢失窗口。

### P2-06：加密备份没有覆盖 Agent 实际配置文件和 ConfigSync 状态

位置：[gateway/codex_tg/backups.py:49](/opt/codex-deploy/gateway/codex_tg/backups.py:49)。备份包含 chats、Thread/Session 映射、credentials、Prompt、环境文件和代理文件，但没有 `config.toml`、Claude `settings.json`、`config-template-state` 或 ConfigSync state。恢复后 SQLite 里的模型/Endpoint 状态可能恢复，但本地配置文件和三方合并基线不一定回到同一版本。

建议把两个 Agent 配置和同步基线作为加密备份的版本化条目；恢复时先做新备份，原子写入后调用配置解析器，再报告需要重启的组件。

### P2-07：配置 URL 允许 HTTP，且订阅 DNS 校验存在 TOCTOU SSRF 窗口

Deployment Center 和 Gateway 的 Base URL 允许 `http://`，因此用户如果把真实 API Key 配到公网明文 HTTP，中转请求会泄露。代理订阅检查先解析并验证公网地址，再由后续请求重新 DNS 解析，存在 DNS rebinding 到内网的时间窗口；Mihomo refresh 还会由目标 VPS 直接拉取订阅 URL。

建议默认只允许 HTTPS，HTTP 仅允许明确的 loopback/private 开发地址；对订阅使用解析后的 IP 绑定、禁止重定向到非公网地址并在每次 refresh 重新校验，或由隔离下载器代理。

### P2-08：Gateway 的 `settings.json` 配置模板大部分不会影响实际 Claude Turn

这是当前权限安全策略带来的产品语义问题：Claude 使用 `--setting-sources ''`，同时 `--model`、`--effort`、`--permission-mode` 和 `--mcp-config` 全部由 Gateway 注入。Deployment Center 的 Claude settings 编辑器因此不能改变 Gateway 当前的权限、MCP、模型或 Effort；它只是手动 CLI 配置模板。模板页面和 `/syncconfig` 如果不明确这一点，会让用户误以为修改立即影响 TG Agent。

建议页面把“手动 Claude CLI 模板”和“Gateway 运行时覆盖”分成两栏；若未来要让模板控制 Gateway，必须采用白名单合并并保留不可覆盖的 root/bypass/MCP 安全边界。

### P2-09：文件交付上下文在异常退出后可能残留敏感任务上下文

位置：[gateway/codex_tg/app.py:2039](/opt/codex-deploy/gateway/codex_tg/app.py:2039)、[gateway/codex_tg/app.py:2132](/opt/codex-deploy/gateway/codex_tg/app.py:2132)。`delivery-context.json` 包含一次性 token、任务 ID、引擎和受保护 secret。正常任务 finally 会删除，但在 `SIGKILL`、宿主机断电或 Python 崩溃后文件会留在 data 目录，直到下一次任务覆盖。Store 状态会阻止旧任务调用，但 root 可读文件仍然包含旧凭据。

建议启动时删除无对应 RUNNING 任务的 context 文件，或按 task_id 使用独立 0600 context 并带过期时间；把保护 secret 改成引用/哈希，避免落盘原文。

## 六、安全审查结论

做得较好的部分：

- Gateway 只接受绑定的 Telegram User ID、Chat ID 和 private chat；回调也做相同校验。
- Center 的密码使用 Argon2id；会话、CSRF、安装 Token 和部署 Token 使用 HMAC hash；一次性 claim 密文使用 AES-GCM。
- API Key 不进入普通 SQLite 状态表，版本化保存在 0600 `credentials.json`；Installer/Updater/日志有多层脱敏。
- Release 使用 Ed25519 签名加 SHA-256；归档和 wheelhouse 有路径穿越检查。
- 文件交付拒绝 symlink、硬链接、设备、系统目录、常见凭据名和检测到的常见密钥；发送前建立不可变副本并验证 hash。
- Telegram、Tavily、Model Catalog、ConfigSync 和 Responses Adapter 明确使用 `trust_env=False`，不会继承系统级代理；Telegram 代理是显式传入的。
- Claude 权限参数显式使用 bypass/full-access，且通过 `--setting-sources ''` 隔离不受信任 settings/hooks。

仍然存在的安全边界：

- root/full-access 是最高风险设计。Prompt injection、恶意仓库内容、恶意文件和中转站返回的工具指令都可以要求 Agent 修改整台 VPS；Telegram 身份和 API Key 是唯一主要入口边界。
- `/start` 绑定没有 challenge，是当前最直接的身份绑定漏洞。
- 配置导出、任意 Header、MCP 参数和自定义 HTTP Endpoint 是敏感值泄露/SSRF 重点。
- `/updatecodex` 的动态 PyPI 信任链和目标 VPS 上的 root 候选执行需要独立供应链控制。
- Center 服务是非 root，但 systemd-analyze security 的总体暴露等级为 MEDIUM；这是网络服务和 Release 管理中心的合理起点，但仍可加入更严格的 capability、ProtectProc、SystemCallFilter 和 IP allowlist。Gateway root 服务不能简单套用同样的隔离，因为产品要求它管理整台 VPS。

## 七、稳定性、并发和性能评估

### 队列和恢复

单 Worker + SQLite `BEGIN IMMEDIATE` 保证了大多数任务 FIFO 和设置快照，`/stop` 连接真实 SDK interrupt/Claude process group，更新通过 activity lock 避免在任务/交付期间切换。这部分设计清晰。

主要缺口是 Telegram 入站 update 的 at-most-once 丢失、批次提交跨事务恢复和 ConfigSync 无运行时 reload。它们都属于崩溃/重启边界，不会在普通 happy path 测试中暴露。

### Async 资源

HTTP client、SDK、Claude process、MCP stdio 和后台任务均有 close/cancel 路径，现有测试覆盖了多个中断场景。风险在于：

- `Batches.process()` 持有 lock 时等待 Thread/Session 建立和 Telegram 发送，可能让同一 Chat 的文件更新和命令长时间排队。
- Diagnostics、模型目录和 Tavily 请求虽然有超时，但 `/tavilykey`、Endpoint 测试仍在 Telegram poll 任务中同步等待网络，长时间网络故障会延迟后续 updates。
- Gateway `tasks/events` 无上限，长期运行的内存主要可控，但 SQLite、WAL 和查询时间会持续增长。
- ConfigSync/backup/export 没有全局活动锁；多个后台操作可同时读写配置、模板状态和备份。

### 文件和磁盘

上传目录有 retention，Artifact vault 和分片可重发，Release 目录没有 retention。超大文件使用 48 MB 分片，内存占用合理，但大文件期间会同时保留原副本、vault 副本、分片、merge metadata 和 Telegram 上传缓冲，磁盘峰值可能达到原文件的 2 倍以上。清理策略应按 artifact 状态和任务保留策略统一规划。

### SQLite

Gateway Store 使用 WAL、busy timeout、外键和多处 `BEGIN IMMEDIATE`；Deployment Center Database 也使用 WAL、foreign_keys 和 busy timeout。中心数据库中没有为 `deployments.status`、`sessions.expires_at` 等高频清理/查询字段建立完整索引，当前数据量小不明显，长期部署后需要补索引和定期 checkpoint。Gateway 的事件和任务查询也缺少保留/归档机制。

## 八、架构与可维护性评价

合理之处：

- Codex SDK、Claude CLI、Telegram API、Mihomo、Updater 和 Deployment Center 之间的边界明确。
- 任务快照和版本化凭据解决了“改 Endpoint/模型影响已排队任务”的关键问题。
- 文件交付、ConfigSync、Backups、Responses Adapter 有独立模块和可测试的纯函数/协议层。
- 目标 VPS 不依赖 PyPI 安装常规 Gateway 依赖，离线 wheelhouse 适合受限网络环境。

需要收敛之处：

- `Gateway` 仍同时负责启动恢复、命令路由、认证输入、设置、队列、更新、诊断、文件、备份和会话，修改一个命令经常要跨越 2000 多行。
- `engine_endpoints` 内存字典、`Credentials` 当前 revision、SQLite task snapshot 和环境变量同时表示 Endpoint，容易出现“当前显示值”和“任务实际值”不一致。
- `claude_session_id` 与 `claude_conversation_key` 存储同一 Session 的两份映射；Codex 的 `chats.thread_id` 与 `thread_counters` 也需要多步同步。
- `Artifacts.begin/collect`、`tasks.output_dir/files_collected` 是旧的输出目录扫描遗留路径，当前 Worker 使用 MCP 直接注册文件，旧字段和代码没有被删除，增加审计面和迁移复杂度。
- Center 和 Gateway 各自实现一套 Claude/Codex 配置结构验证，未来规范变化时容易漂移。
- 版本号、Release manifest 版本、每个 Agent 配置模板版本、Gateway SQLite schema 版本缺少统一迁移矩阵。

可以删除或简化的候选项：

1. 在确认旧数据库已完成迁移后，移除未调用的 `Artifacts.begin/collect` 旧扫描路径和 `output_dir/files_collected` 字段，或把它们明确隔离到一次性兼容迁移模块。
2. 统一 Endpoint resolver：任务只保存 opaque revision，运行时通过一个 resolver 返回 base URL、认证方式和当前状态；删除 Gateway 内存 Endpoint 字典的重复状态。
3. 合并 Claude 的两个 Session 字段，保留一个 canonical session ID；历史迁移只保留兼容读取。
4. 把命令处理拆为 `CommandRouter`、`SettingsFlow`、`TaskFlow`、`MaintenanceFlow`，Gateway 只做生命周期和依赖注入。
5. 配置模板只保留一种版本语义；不要同时使用全局版本、引擎版本和 state 版本而没有清晰定义。

## 九、测试覆盖和缺口

当前测试对以下部分覆盖较好：

- 双引擎基础切换、Session/Thread、模型列表、Reasoning 快照。
- Codex Responses Adapter 的 instructions 顺序、字段保持、SSE 和取消。
- Claude CLI 本地协议 fixture、权限参数、MCP 和 Session resume。
- FIFO、Stop、批次、文件副本、Telegram acknowledgement、重发和大文件分片。
- ConfigSync 三方合并、冲突、签名、Installer 容器和 Updater 版本切换/健康回滚。
- Secret redaction、路径穿越、Release 归档安全和基础静态规则。

关键缺口：

- 没有真实 Telegram API 端到端测试；Bot Token 绑定抢占和 update 丢失没有故障注入测试。
- 没有真实目标 VPS reboot、systemd kill -9、断电恢复或 SQLite WAL 损坏测试。
- 没有 ConfigSync 真实公网清单测试；这正是当前已发现的顶层 `version` 缺失问题。
- 没有批次“任务已写入、批次状态尚未写入”崩溃测试。
- 没有 Telegram 文件上传“服务器已接收、客户端超时”重复交付测试。
- 没有 `/configexport` 对 Authorization、MCP args、Cookie、URL query 和任意 Header 的全面脱敏测试。
- 没有 `claude_effort=max` Installer/settings 实际加载测试。
- 没有真正的 `/syncconfig` 后当前 SDK/CLI 重新加载验证。
- 没有并发 `/syncconfig`、`/backup`、`/restore`、`/diagnose`、`/update` 竞态测试。
- 没有 coverage、类型检查、依赖 CVE 扫描、系统调用/权限审计的自动门禁。

## 十、推荐修复顺序

### 第一阶段：先修 P1 安全和正确性

1. 修复配置模板 manifest 顶层版本协议，并新增公网清单真实回归测试。
2. 用一次性 `/start <challenge>` 或网页确认修复 Telegram 绑定抢占。
3. 设计 durable inbound update 表和幂等任务键，解决 offset 先推进造成的消息丢失。
4. 将批次提交与任务创建改为事务/幂等状态机，补重启故障测试。
5. 完善 ConfigExport 的递归 secret redaction，覆盖 Header、Cookie、Bearer、MCP args、URL query、环境变量和值模式。
6. 统一 Installer/Updater 的下载代理层，先验证代理后下载全部 Release/SDK/Claude 资源。
7. 给 `/updatecodex` 增加项目签名候选清单或独立可信 hash，降低直接信任 PyPI 的风险。

### 第二阶段：修复运行时语义和恢复

1. 明确 ConfigSync 的“写入、重载、重启、当前 Session”状态；安全时重建 Codex Runtime，Claude 模板明确区分手动 CLI 和 Gateway。
2. 修正 `claude max` 文件写入语义。
3. 补齐 ConfigSync 备份命名、TOML AST 保留、symlink 策略和并发锁。
4. 修复上传超时后的 UNKNOWN/重试策略，避免 Telegram 重复文件。
5. 启动时清理无有效 RUNNING task 的 delivery-context，并让 context token 带过期时间。
6. 让加密备份覆盖 Agent 配置文件和 ConfigSync state。

### 第三阶段：长期可维护性

1. 建立私有 Git 仓库、签名 tag、CI Release gate 和可追踪的 schema migration。
2. 加入覆盖率、类型检查、Bandit/依赖扫描、故障注入和真实公网协议 fixture。
3. 设计 SQLite retention、WAL checkpoint 和 Release artifact retention。
4. 删除死代码和重复状态，拆分 Gateway 命令/编排模块。
5. 为 root/full-access 增加可选的受限部署模式，生产环境默认要求二次确认或额外绑定证明。

## 十一、整体质量评价

当前项目已经超过“能运行的脚本集合”：它有明确的双引擎模型、持久化任务快照、签名 Release、离线依赖、候选更新、文件副本校验和多层 Secret 保护，现有 231 个测试也覆盖了大量高风险 happy path。作为单用户、单 Chat、受控 VPS 的运维 Gateway，当前架构具备继续运行的基础。

但当前版本还不适合在“不需要人工观察”的多台生产 VPS 上长期无条件运行。原因不是日常任务路径不稳定，而是几个边界路径会造成安全或一致性问题：Telegram 绑定可被抢占、配置同步协议当前实测不可用、入站 update 和批次提交的崩溃恢复可能丢失或重复执行、配置导出可能泄露 Header 凭据、代理下载链路不完整，以及动态 SDK 更新直接信任 PyPI。先完成第一阶段后，系统才适合把“自动更新、自动同步和无人值守 root 运维”作为强保证；在此之前，建议把 `/syncconfig`、`/updatecodex` 和新 VPS 绑定视为需要人工复核的维护操作。

官方配置语义对照：Codex 自定义 Provider、Responses、上下文和权限配置参考 [Codex Advanced Configuration](https://learn.chatgpt.com/docs/config-file/config-advanced) 与 [Codex Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference)；Claude settings 的来源优先级、权限和模型设置参考 [Claude settings precedence](https://code.claude.com/docs/en/settings)、[Claude settings reference](https://code.claude.com/docs/en/settings-reference) 和 [Claude CLI reference](https://code.claude.com/docs/en/cli-reference)。
