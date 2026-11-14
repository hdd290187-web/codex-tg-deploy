# Codex TG Deployment Center

当前已部署地址：<https://gpt.cevyo.com/codex-deploy/>

这是一个面向自有 VPS 的一次性部署中心与 Codex Telegram Gateway 发布源。部署中心只负责生成短期一次性 Installer、提供签名 Release 和记录部署结果；已安装的目标 VPS 不需要持续连接部署中心，后续通过 Telegram 的 `/update` 拉取公开 Release。

## 目录

- `deployment-center/`：FastAPI 部署中心、模板、静态资源和 systemd/Nginx 集成文件。
- `gateway/`：Telegram Gateway、Codex Python SDK 运行层、systemd 模板、独立 updater。
- `installer/`：目标 VPS 使用的一行命令 Installer 模板。
- `release-tools/`：Ed25519 密钥初始化、测试、打包、SHA-256、签名和原子发布。
- `releases/`：公开 Release 归档、清单和签名。
- `tests/`：单元/集成、SDK 配置、Release 安全、浏览器和 Docker Installer 验收。

## 当前运维入口

```text
systemctl status codex-deploy
journalctl -u codex-deploy
curl -fsS https://gpt.cevyo.com/codex-deploy/health
```

发布新 Gateway 前，发布工具会先运行测试和静态检查：

```text
/opt/codex-deploy/.venv/bin/python /opt/codex-deploy/release-tools/release.py release --bump patch
```

只有测试通过，才会更新 `latest.json`。Release 私钥位于 `/opt/codex-deploy/keys/release-private.pem`，权限为 `0600`，不进入归档、不发送到目标 VPS。

## Telegram 运行时设置

每个获授权的 Telegram Chat 都在 SQLite 中保存独立的运行配置。`config.toml` 和环境变量仍提供首次安装时的系统默认值，Telegram 的 Chat 设置不会通过反复改写 `/root/.codex/config.toml` 实现。

```text
/model                         实时查询当前引擎中转站的模型，支持分页和重新检测
/model gpt-5.6-sol             验证并设置后续任务的模型
/sessions                      选择当前引擎的历史 Thread / Session
/reasoning                     查看当前模型实际支持的 Reasoning 等级
/reasoning high                设置后续任务的 model_reasoning_effort
/planreasoning                 查看当前模型实际支持的 Plan Reasoning 等级
/planreasoning high            设置后续任务的 plan_mode_reasoning_effort
/settings                      查看当前 Chat、Gateway、Codex、Thread 与任务配置
/codexprompt                   管理 Codex 前置 Instructions（不作用于 Claude）
```

设置在任务入队时形成快照。运行中的任务和已经排队的任务保持提交时的配置，新设置从下一条新提交任务生效。模型与普通 Reasoning 通过官方 SDK 的逐 Turn 参数应用；Plan Reasoning 通过 `thread_resume(..., config=...)` 应用到同一个 Thread，不会仅因切换设置新建会话。模型与等级按钮的 callback 继续要求 Telegram User ID、Chat ID 和私聊类型同时匹配。

`/model` 和模型按钮直接读取当前引擎配置的中转站目录：支持 OpenAI / Anthropic 的 `/v1/models`，以及保留中转站路径前缀的 `/models` 兼容接口。Claude 使用当前 Key 的 API Key / Bearer 鉴权，并支持目录分页。每次打开菜单或点击“重新检测”都会重新查询；更换 URL 或 Key 会清除目录缓存并使旧按钮失效。模型目录不调用生成接口，也不通过猜测固定模型名发送探测请求。无法获取时明确提示“无法自动获取模型列表”，保留 `/model <model_name>` 手动设置，绝不补入 SDK 内置目录或固定 Claude 别名。中转站目录声明可用不等于逐一完成模型生成测试，实际调用错误仍会返回 Telegram。

Codex 手动设置模型时，空闲情况下使用独立临时 SDK runtime 对当前中转站发送自然语言验证请求，不会使用仍绑定旧中转站的任务 runtime。Reasoning 按钮参考 SDK 能力元数据；手动 `/reasoning <等级>`、`/planreasoning <等级>` 只检查输入格式，直接保存，即使能力目录缺失或没有列出该值也不会阻止。Gateway 不为此发送验证请求；若 CLI 或中转站实际不接受该值，会在执行时返回错误，保存值不自动回退。运行中和已排队任务保持提交时快照。

修改 `/endpoint` 的 URL 或 Key 时，格式合法即先保存并刷新模型目录缓存，再单独测试。即使 Key 无可用模型、模型调用失败或网络超时，新配置仍保留并显示 ERROR；不会恢复之前的 Key。当前和已排队任务继续使用旧快照，后续新提交任务使用新配置。

`/codexprompt` 只作用于 Codex。Gateway 将 Codex Provider 指向本机 127.0.0.1 上需要随机 Bearer Token 的 Responses Adapter；Adapter 只修改 Responses 请求体顶层 `instructions`，在启用且有内容时按 `自定义内容 + "\n\n" + Codex 原始 instructions` 注入，其他请求字段原样保留。流式响应和工具调用直接透传。Prompt 在入队时保存不可变 revision，因此当前 Turn 与已排队任务不会受到之后修改影响；Claude Code 仍直接访问自己的 Endpoint。当前 Prompt 文件位于目标机 `/opt/codex-tg/data/codex-pre-instructions.txt`，权限为 `0600`，普通状态表只保存 enabled、时间、长度和 SHA-256。

`/sessions`、`/resume` 和“历史会话”按钮按当前 `/engine` 分流：Codex 显示 SDK 历史 Thread；Claude 显示 Gateway 独立 Claude 目录 `projects/*/*.jsonl` 中可恢复的主 Session，忽略子 Agent、空会话和损坏记录。菜单支持分页和选择；Claude 选择后通过官方 CLI `--resume` 继续原上下文，并在 SQLite 中恢复 Session 映射与原任务编号。两个引擎的选择与 `/new` 操作互不覆盖；有运行中或排队任务时不能切换历史会话。

`/updatecodex` 检查官方 PyPI 的最新稳定 `openai-codex` 与其声明的固定 Runtime，下载 wheel 后逐个核对官方 SHA-256，在旁路候选 venv 中完成 `pip check`、CLI 版本、SDK import、app-server Thread 与能力目录调用。自检使用独立数据库和 Codex Home，不调用模型，不依赖 Claude、Tavily 或 Telegram 连通性。通过后才短暂停服务并切换；失败同时恢复旧 venv 和 CLI 入口，报告实际回滚健康结果。当前发布默认 SDK/Runtime 为 `0.162.1`。`/root/.local/bin/codex` 使用 Gateway 的同一 bundled Runtime，原 npm/native 安装不会卸载。更新日志在 `/opt/codex-tg/logs/updater.log`。`/update` 与 `/update force` 仍只更新 Gateway；`/updateclaude` 独立更新 Claude 官方稳定版。

## 文件交付、工作目录与诊断

Gateway 不再向任务文字添加文件交付、Tavily 或其他系统自定义前置说明。默认自定义 Instructions 为空且关闭；只有用户显式配置并开启 `/codexprompt` 时才会注入其原文。引擎自身的系统指令、用户已有项目指令和工具定义照常工作。

两个引擎均提供 `telegram_send_file` 和 `telegram_file_status` MCP 工具。用户说“把那个文件发给我”时，Agent 可直接将普通工作目录中的文件路径交给工具；无需另存专用交付目录，Gateway 不扫描任务目录自动发文件。工具将请求的文件保存为内部不可变副本并交给独立 FIFO 发送队列。工具绑定当前授权 User ID、Chat ID 和运行中任务，不能指定其他收件人；系统/凭据路径、符号链接、硬链接、疑似密钥内容和包含敏感文件的 ZIP 仍会拒绝。只有 Bot API 返回文件消息回执后才标记交付成功，上传失败不会重跑 Agent。旧版已生成的待交付文件和历史记录仍保留。

- `/files`：查看请求发送过的文件和交付状态，点击重发。结果不明的发送需要先核对 Telegram，再确认重发。
- `/sendfile /opt/report.pdf`：显式发送一个已有成品，仍执行身份、路径和内容检查。
- `/tasks`、`/tasks <ID>`：查看执行结果与文件数量；失败/中断任务可显式确认重试，启动和重启时不会自动重跑。
- `/cd /opt/project`、`/pwd`：每个引擎独立保存工作目录；当前/已排队任务使用原快照，后续任务使用新目录。Codex 逐 Turn 传递 cwd；Claude 在指定 cwd 启动并用完整 transcript 路径恢复原 Session。
- `/diagnose`：本地组件、systemd、目录权限、数据库、会话、磁盘、Telegram getMe、代理、Release 签名和已有交付回执检查。不发送模型探测请求，未实测模型/搜索能力明确标记未验证。只对目录和代理服务提供确认后的修复。

云端 Bot API 的单文件限制为 50 MB。大于 48 MB（48,000,000 字节）的成品会拆分为 `.part001` 等分片，同时交付 `.merge.json` 和 `.merge.py`；放到同一目录后运行 Python 合并并核对完整 SHA-256。文件工具每次接受最多 40 个路径，每文件最多 2 GiB；更大的成品应由 Agent 拆分后发送。传输过程中已有确认回执的分片不会重复上传。无法确认是否发送成功的超时保留为 UNKNOWN，避免盲目自动重发。

`/backup` 在 VPS 上创建 AES-GCM 加密的轻量配置备份，包含中转站凭据、模型/推理、Tavily、代理配置、Codex Instructions 和会话映射。`/backup list` 或 `/restore` 可选择并确认恢复；恢复前再次备份，任务、成品和真实 Codex/Claude 会话文件不会被覆盖。加密文件位于 `data/backups/`，密钥为独立 `data/backup.key`，均为 root 0600；普通 Telegram 消息不包含 Key。跨 VPS 灾难恢复需单独安全保管该密钥，并另行备份真实会话目录和成品；此功能是配置备份，不是整机备份。Gateway/CLI/SDK 更新前自动备份，更新不会删除这些目录。

新建 Thread 的第一条任务直接使用 SDK `thread_start()` 返回的内存 Thread，不会在首个 Turn 前调用 `thread_resume()`。如果 Gateway 恰好在空 Thread 产生首个 rollout 前重启，SDK 返回 `no rollout found` 时 Gateway 会仅替换这个不可恢复的空 Thread、更新 SQLite 映射并继续任务；已经产生 Turns 的长期 Thread 仍按原 ID resume。Updater 会把状态原子写入 `/opt/codex-tg/data/update-status.json`，直接 Telegram 通知失败时由 Gateway 后台补发，`/status` 也会显示最近更新结果；完整输出保存在 `/opt/codex-tg/logs/updater.log`。

## 中国大陆 VPS Telegram 网络

Deployment Center 的 Telegram 网络模式默认为“自动”：Installer 在目标 VPS 使用 Bot Token 执行不经过环境代理的真实 `getMe`。直连可用时选择 `DIRECT`，不下载代理核心；直连发生网络错误且提供了订阅 URL 时，才切换到订阅代理。Telegram 返回 401/404 等 Token 错误时不会误判为网络封锁，也不会通过安装代理掩盖凭据错误。

支持以下模式：

- `自动`：目标机真实直连成功时使用 DIRECT，否则使用填写的订阅。
- `直连`：要求目标机 Telegram 直连成功。
- `订阅代理`：安装并校验 Mihomo，使用订阅自动选取可用节点。
- `SOCKS5 / HTTP`：使用用户提供的代理 URL，不安装 Mihomo。

订阅代理使用 **Mihomo v1.19.31**，固定验证官方 Linux amd64/arm64 归档 SHA-256。Deployment Center 镜像这两个官方压缩包，避免大陆 VPS 在代理启动前依赖 GitHub；目标 VPS 下载镜像后仍使用内置的官方 SHA-256 独立校验。Mihomo 当前原生支持的 provider 输入包括 Mihomo/Clash YAML、逐行 URI 和整体 Base64 URI 订阅。Deployment Center 的“测试订阅”只返回格式和可识别节点数量，不返回节点名称、服务器或密码；最终兼容性仍由目标机上的 Mihomo `-t` 和真实 Telegram `getMe` 验证。

目标机布局：

```text
/opt/codex-tg-proxy/mihomo
/etc/codex-tg-proxy/config.yaml
/etc/codex-tg-proxy/providers/subscription.yaml
/etc/codex-tg-proxy/refresh-status.json
codex-tg-proxy.service
codex-tg-proxy-refresh.timer
```

Installer 的系统 Python 阶段只使用 Ubuntu/Debian 自带标准库，不依赖 `tomllib` 或尚未安装的 `tomli`。固定 Codex TOML 模板中的所有外部值都经过控制字符拒绝和 TOML 字符串转义，最终仍由 Codex selftest 做真实解析与模型请求验证。配置事务 traceback 会先脱敏再写入 `/opt/codex-tg/logs/config-install.log` 并返回 Deployment Center 摘要。

代理目录为 `root:root 0700`，配置、订阅缓存和刷新状态为 `0600`。mixed port 和 controller 分别只监听 `127.0.0.1:7890`、`127.0.0.1:9090`。fallback 组每 300 秒检查节点；provider 每小时更新，独立 timer 同时记录刷新状态。刷新失败时 Mihomo 保留最后有效缓存，Gateway 的 `/status` 和本地 health endpoint 会区分本地代理停止、订阅刷新失败、订阅无节点、所有节点不可用和 Bot Token 错误。

运行期只有 Gateway 内的 Telegram HTTP 客户端显式使用 `CODEX_TG_TELEGRAM_PROXY_URL`，并设置 `trust_env=False`。Gateway Python 依赖不再由目标 VPS 访问 PyPI：每个 Release 都为 Python 3.10/3.11/3.12/3.13 和 amd64/arm64 发布签名清单引用的离线 wheelhouse，Installer 按目标 ABI/架构下载、校验 SHA-256 后使用 `pip --no-index` 安装。官方 Codex CLI 下载在代理模式下仍使用临时 `0600` curl 配置；自定义 SOCKS5 下 `/updatecodex` 会先校验并离线安装 PySocks bootstrap wheel。临时目录在 Installer/Updater 退出时删除。Installer 不写系统级 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`；Codex runtime 启动前还会移除 Telegram 代理 URL，因此安装结束后的 Codex Base URL、apt、Git、Docker 和其他 VPS 流量不会被此功能重定向。

## 安全边界

- 部署中心密码只以 Argon2id 哈希保存于 `/etc/codex-deploy.env`。
- API Key 与 Telegram Bot Token 以 AES-256-GCM 临时密文保存；一次性 claim 解密后立即清除。
- Installer Token 有效期 10 分钟且只能成功 claim 一次；安装 session 另行短期签发。
- Gateway 仅接受绑定的 Telegram User ID + Chat ID，其他身份不会得到状态、文件或 Codex 能力。
- `/settings`、`/status` 和设置按钮不会回显 API Key 或 Telegram Bot Token。
- 代理订阅 URL 和带凭据的自定义代理 URL 与 API Key、Bot Token 一样，仅在 Deployment Center 的 AES-256-GCM 一次性密文中短期保存；领取或过期后销毁，不进入命令、查询参数、部署记录或日志。
- 目标 VPS 使用 `User=root`、Codex `approval_policy=never` 和 `Sandbox.full_access`，这是产品的明确授权边界。
- Gateway 更新必须同时通过 Ed25519 清单签名和归档 SHA-256 校验，健康检查失败会切回旧 Release。

## 未在当前服务器执行的外部步骤

当前没有用户提供的真实 Telegram Bot Token、Codex 中转站 API Key 或目标 VPS 安装地址，因此没有向外部 Bot 发送测试消息，也没有在当前长期服务器上安装一个未绑定配置的 Gateway。页面输入真实配置后，Installer 会在目标 VPS 上完成 claim、备份、配置、systemd、Telegram 与 Codex 自检。

## 多文件批次、上下文压缩与 Agent 配置模板

Telegram 相册或连续附件会先进入一个短暂批次缓冲；同一 `media_group_id` 的图片、TXT、ZIP、PDF 和代码文件只形成一个任务，caption/说明文字也会合并。没有相册 ID 的连续附件在约 3 秒静默后合并。`/batch` 开启手动批次，连续发送文件和说明后用 `/run` 提交，`/cancel` 取消；手动模式不会把文件或说明提前放入普通任务队列。每个批次会显示接收、下载完成和失败数量，重复 Telegram update 由 `(chat_id,message_id)` 去重。

`/compact` 使用当前引擎的原生能力。Codex 调用官方 SDK 的 `thread/compact/start`，等待持久化 `contextCompaction` 项后才报告完成，并保留同一个 Thread ID；如果 SDK 提供 TokenUsage 事件，显示真实数值，否则显示未知。正在运行的任务、队列或文件交付完成前，压缩请求会排队。当前 Claude Code 官方 headless `stream-json` 接口没有可确认的原生 compact RPC/flag，Gateway 明确报告不支持，不发送“请总结之前对话”来伪造压缩。

Deployment Center 的“Agent 配置管理”页面保存独立版本化的 Codex `config.toml` 和 Claude `settings.json` 模板。模板由单独的 Ed25519 配置签名密钥签名，Gateway 通过 `/syncconfig` 拉取并验证。三方合并使用上一次模板、本地实际文件、新模板：本地新增项和无冲突修改保留，模板变更只应用到本地未改字段，同一字段双方都改时列出冲突；`/syncconfig local` 或 `/syncconfig template` 选择冲突策略。写入前保存 0600 备份，校验后原子替换，失败恢复原文件。`/configstatus` 查看模板版本、本地修改和冲突，`/configexport` 导出脱敏后的当前文件。Key、Token、环境变量和命令行覆盖不进入公共模板。

`/diagnose` 会读取目标 VPS 当前的两个配置文件，并用 Gateway 实际携带的 Codex bundled app-server 和 Claude CLI 参数做本地校验，不调用模型。Codex 配置中被当前 runtime 忽略的更新版字段会显示为警告；Claude 的 `modelSettings` 必须按模型名映射，运行中的 Gateway 使用 `--model`、`--effort`、`bypassPermissions` 和显式 MCP 参数覆盖文件中的同名设置。中转站没有提供上下文窗口元数据时，诊断会标为“未验证”，不会假报已匹配。
