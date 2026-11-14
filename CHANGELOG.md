# Changelog

## 2.4.1 - 2026-11-14

- Complete final acceptance against the 2.3.4 audit and keep all eight P1 and eight implemented P2 hardening fixes covered by the regression suite.
- Update the Gateway Python SDK and bundled Codex app-server/runtime pair to the current signed `0.169.0` release; keep the separately installed global Codex CLI at the same official version without coupling its lifecycle to Gateway.
- Update the bundled Claude Code binary and updater metadata to the official stable `2.1.311` channel, with native `bypassPermissions`, Session, Effort and MCP checks still enforced.
- Make public-clone CI deterministic by skipping intentionally omitted release binaries, removing deployment-directory assumptions, adding pip caching, read-only permissions, timeouts and cancellation for superseded runs.
- Update the Claude protocol fixture for deferred MCP `ToolSearch` in current CLI releases and remove misleading expected-failure output from Installer container scenarios.
- Remove superseded Deployment Center Release archives and wheelhouses after publishing 2.4.1; retain only the current signed manifest and assets required for new installs.

## 2.4.0 - 2026-11-14

- Make signed Agent config manifests compatible with Gateway ConfigSync by adding a global generation and preserving per-engine versions; verify the live public manifest in regression tests.
- Add encrypted durable Telegram inbox admission, idempotent task source references, explicit uncertain-update recovery, transactional multi-file batch submission, and SQLite retention/indexes.
- Bind Deployment Center Telegram setup with a one-time `/start <challenge>` instead of accepting an arbitrary private `/start` message.
- Harden configuration export, symlink-aware atomic config writes, Claude per-turn settings snapshots, backup coverage, and stale delivery-context cleanup.
- Route Installer and updater artifact downloads through the explicit scoped proxy path; use signed center-approved SDK/Runtime/Claude component metadata for update candidates.
- Pin subscription fetches to validated public addresses, use Mihomo file providers with atomic refresh rollback, and add a conservative Release retention planner.
- Add hardening, inbox, manifest, export, and proxy regression coverage; initialize a sanitized GitHub source baseline and CI metadata.


## 2.3.4 - 2026-11-14

- Validate Codex `config.toml` with the exact bundled app-server runtime during `/diagnose`, including provider shape, Responses protocol, retry/context limits, and fields that the runtime silently ignores.
- Correct the Claude Code template to use the official `permissions.defaultMode`, top-level `effortLevel`, and per-model `modelSettings` shape; reject the legacy `modelSettings.effortLevel` form before publishing a template.
- Merge deployment-specific Claude model/effort values into `settings.json`, migrate the legacy effort shorthand, and keep Codex's file-level provider aligned with the Gateway's `codex_tg` provider.
- Report the effective Gateway CLI permission and configuration overrides without making model calls; preserve provider capacity as unverified when the relay does not expose context metadata.

## 2.3.3 - 2026-11-14

- Redesign the Agent configuration page as a two-card configuration workspace with current-version badges, effective defaults, editor validation, history, diff preview, rollback controls, responsive navigation, and a clear explanation of new-VPS versus existing-VPS behavior.
- Seed the page with the actual Gateway Codex and Claude default configuration instead of empty placeholder documents.
- Include the current signed configuration templates in new deployment claims; Installer merges deployment-specific Endpoint/model values into the template while preserving custom fields and writes Claude settings safely.

## 2.3.2 - 2026-11-14

- Fix the authenticated Deployment Center Agent configuration page returning HTTP 500 because the shared Jinja renderer received `prefix` twice. Add an authenticated page regression test.

## 2.3.1 - 2026-11-14

- Skip initial signed template fetch on legacy VPS installations that do not yet have the pinned configuration public key, avoiding a startup network delay while preserving the explicit `/syncconfig` error path.

## 2.3.0 - 2026-11-14

- Merge Telegram media groups and short consecutive attachment bursts into one durable task; add explicit `/batch`, `/run`, and `/cancel` controls with progress, duplicate-update protection, and per-member download errors.
- Use Codex SDK native `thread/compact/start` and persisted `contextCompaction` confirmation for `/compact`; report Claude headless compact as unsupported when no reliable native RPC is exposed.
- Add signed, versioned Deployment Center Agent config templates, a Chinese editor/history/rollback page, and Gateway `/syncconfig`, `/configstatus`, and `/configexport` commands.
- Add three-way TOML/JSON config merging with conflict choices, atomic writes, 0600 backups, signed manifest verification, and safe rollback on sync errors. Preserve local keys, environment variables, CLI overrides and non-conflicting VPS changes.

## 2.2.1 - 2026-10-20

- Stop appending Gateway file-delivery/search instructions to task text or Claude system prompts. Preserve explicitly configured Codex pre-instructions, which remain empty and disabled by default.
- Expose authorized `telegram_send_file` and `telegram_file_status` MCP tools to both engines so requested files can be sent directly from their working directory; stop allocating/scanning a delivery directory for every new task.
- Save manually entered Reasoning/Effort and Plan Reasoning values without provider capability checks or model probes, preserving running/queued snapshots.
- Persist valid Endpoint/Key changes before testing; report failed tests while retaining the new configuration, invalidate model caches, and prevent older tasks from overwriting newer endpoint status.
- Verify both official engine processes calling the file tool, exact user text, independent new-thread history, credential failure persistence, queue snapshots, and unauthorized tool access.

## 2.2.0 - 2026-10-13

- Add shared Codex/Claude task output directories, immutable delivery snapshots, persistent per-file/per-part Telegram acknowledgements, bounded upload retries, ambiguous-result handling, and resumable large-file splitting with SHA-256 merge verification.
- Add `/files`, `/sendfile`, `/tasks`, `/cd`, `/pwd`, `/diagnose`, `/backup` and `/restore`, including authorized buttons, explicitly confirmed task retries, per-engine working directories, and AES-GCM encrypted configuration backups.
- Preserve task execution outcomes independently from delivery errors; recover uncollected completed outputs after restart without rerunning Agent tasks, and keep in-flight uploads referenced by queued tasks.
- Update the bundled Codex Python SDK and Runtime together to 0.162.1. Codex CLI uses the same bundled runtime; existing npm installations remain intact.
- Rework `/updatecodex` to resolve official stable SDK/runtime pairs, verify official wheel SHA-256 values, build an isolated candidate, exercise local SDK/app-server APIs without model probes, then switch and roll back both runtime and CLI entrypoint together.
- Separate Claude/Codex preflight tests, isolate candidate data from production settings, repair relocated venv shebangs, coordinate updates with Agent/file workers, and retain detailed sanitized progress and rollback results.
- Install the matching CLI from signed offline wheelhouses instead of running an unrelated global CLI installer. Add encrypted-backup dependencies to all supported Python/architecture release assets.

## 2.1.0 - 2026-09-30

- Add a loopback Codex-only Responses Adapter that injects configured pre-instructions immediately before the upstream request while preserving other request fields, SSE streaming and tool calls.
- Add `/codexprompt` management with status, show, edit, enable, disable and confirmed clear actions; direct multiline text and UTF-8 `.txt` uploads are supported.
- Freeze the prompt revision when each Codex task is queued so changing it cannot alter a running Turn or an already queued task; Claude requests never pass through the Adapter.
- Add Deployment Center fields for optional Codex pre-instructions, enable state, multiline input, UTF-8 text upload and character count.
- Persist prompt metadata in SQLite and root-only prompt files, preserve revisions across Gateway updates/reboots, and add Installer backup/restore handling.

## 2.0.3 - 2026-09-27

- Add Claude history pagination, selection and CLI resume, keeping Codex Thread and Claude Session mappings and task numbering independent across restarts.
- Query each configured provider's model listing over HTTP instead of showing fixed Claude aliases or the Codex SDK catalog; support Anthropic pagination, authentication compatibility and prefixed `/models` routes.
- Refresh model menus on every open, invalidate caches and old buttons after URL/Key changes, and provide manual input with an explicit discovery error when no catalog is available.
- Support long provider model IDs with short, engine- and endpoint-bound callback tokens, and validate Codex model changes using the current endpoint in an isolated runtime.
- Verify both official engines against local protocol servers, including selecting a previous conversation after creating another and restoring its original context.

## 2.0.2 - 2026-09-27

- Run Claude Code with unattended `bypassPermissions` under the root Gateway service, and isolate settings and MCP configuration so tool permissions are not narrowed by user/project hooks or deny rules.
- Replace short Codex and Claude endpoint validation prompts with natural-language requests; keep routine Gateway health checks model-free and explain relay short-input rejection in Telegram.
- Render `/settings` as one status message with its inline controls instead of three messages.
- Persist pending Telegram credential-input flows across Gateway restarts, suppress replayed secret messages, and prevent recognized bare API keys from entering the Agent queue.
- Remove a stale Tavily environment-write call that caused a false failure reply after the Key had already been saved.
- Keep Claude's built-in Write and WebSearch tools available; the CLI's `--bare` mode disables them, so the Gateway isolates setting sources without enabling bare mode.

## 2.0.1 - 2026-09-27

- Interim signed release of the permission and Telegram input fixes before the final Claude built-in tool validation.

## 2.0.0 - 2026-09-27

- Consolidate Telegram controls around inline menus for engines, models, reasoning/effort, endpoints, Tavily, queues, and updates.
- Add Claude model and effort selectors and a unified settings center while keeping compatibility command aliases.
- Make Tavily disabled by default without a key; a successfully configured key enables it automatically.
- Preserve the verified Telegram Bot Token in the Deployment Center until deployment succeeds, and invalidate the binding only when the token is edited.
- Improve Deployment Center loading/error states and optional configuration defaults for Codex, Claude Code, and Tavily.

## 1.3.10 - 2026-09-27

- Add Codex and Claude Code dual-engine execution with independent durable sessions, model/effort settings, endpoint rotation, and real stop handling.
- Add optional Tavily-first search through MCP with native-search fallback when Tavily is unavailable.
- Install and hash-verify the official Claude Code CLI for amd64 and arm64, with isolated `/updateclaude` rollback handling.
- Allow optional Codex, Claude Code, and Tavily deployment configuration while retaining Telegram-only post-install setup.
- Store endpoint secrets in a root-only versioned credentials file and keep task credential snapshots stable across rotations.

## 1.3.8 - 2026-09-17

- Remove the pre-configuration `tomllib`/`tomli` bootstrap dependency that broke Ubuntu 22.04 system Python 3.10 after Gateway installation.
- Add a complete Ubuntu 22.04/Python 3.10 fresh and repeat-Installer acceptance scenario.
- Capture, redact, persist, and report configuration transaction tracebacks instead of returning only the shell here-doc line number.

## 1.3.7 - 2026-09-17

- Add `exceptiongroup==1.3.1` to every offline wheelhouse so Python 3.10 satisfies AnyIO's conditional dependency without PyPI.
- Upgrade the signed Gateway SDK and bundled Codex runtime together from 0.147.0 to 0.154.0 after full Gateway/SDK compatibility tests.
- Retry Telegram and model-call self-tests three times and return their final redacted stdout/stderr instead of a generic subprocess exit code.
- Keep candidate validation and venv rollback behavior while making `/updatecodex` failures actionable from Telegram and updater status.

## 1.3.6 - 2026-09-17

- Keep a newly created SDK Thread in memory for its first Turn instead of resuming it before Codex has created a rollout.
- Detect `no rollout found for thread id`, replace only the unavailable empty Thread, persist the replacement mapping, and preserve per-Thread task numbering.
- Add atomic root-only updater status, direct Telegram notification retries, Gateway fallback notification, `/status` update visibility, and persistent updater logs.
- Make updater systemd ordering aware of the local Telegram proxy without requiring it on DIRECT installations.

## 1.3.5 - 2026-09-17

- Eliminate target-VPS PyPI access by publishing signed, hash-pinned offline wheelhouses for Python 3.10-3.13 on amd64 and arm64.
- Split the large Codex runtime wheel by architecture and reuse it across Python versions to keep target downloads bounded.
- Select the exact wheelhouse from the signed manifest using the target Python ABI and architecture, then install with `pip --no-index`.
- Preserve automatic DIRECT behavior on VPS hosts that can reach Telegram without a proxy; offline dependency assets come from Deployment Center without enabling Mihomo.

## 1.3.4 - 2026-09-17

- Use Mihomo's local HTTP mixed-port protocol for pip bootstrap while keeping Gateway Telegram traffic on SOCKS5, avoiding pip's pre-install SOCKS dependency cycle.
- Mirror and SHA-256-verify PySocks 1.7.1 for custom SOCKS proxy bootstrap before pip accesses the network.
- Return the last redacted pip error lines in Installer and Deployment Center progress while retaining a root-only diagnostic log.
- Apply the HTTP mixed-port bootstrap choice to `/update`, `/update force`, and `/updatecodex`.

## 1.3.3 - 2026-09-17

- Rebuild `/updatecodex` in a complete candidate virtual environment before stopping the Gateway; atomically activate it only after import and live self-tests pass.
- Repair a missing or incomplete `current/.venv` instead of assuming `current/.venv/bin/pip` exists.
- Invoke pip through `python -m pip`, retain the previous venv for rollback, and leave a newly repaired venv active when no previous environment exists.
- Use the scoped mainland proxy path for normal Gateway update dependency installation as well as Codex CLI/SDK updates.

## 1.3.2 - 2026-09-17

- Reorder mainland-VPS bootstrap so the signed Gateway proxy helper and Mihomo start before Codex CLI or PyPI access.
- Mirror the two official Mihomo v1.19.31 Linux assets in Deployment Center and retain independent target-side SHA-256 verification, avoiding a pre-proxy GitHub dependency.
- Route only Installer child-process downloads for Gateway dependencies, `chatgpt.com/codex/install.sh`, and `releases.openai.com` through root-only temporary curl/pip configuration.
- Apply the same scoped network path to `/updatecodex` without setting system-wide proxy environment variables.
- Redact proxy credentials from Gateway dependency installation failures and remove all temporary network configuration when Installer exits.

## 1.3.1 - 2026-09-16

- Install Mihomo under the dedicated `/opt/codex-tg-proxy` tree so an unrelated system-wide `/usr/local/bin/mihomo` is never overwritten or removed.

## 1.3.0 - 2026-09-16

- Add automatic, direct, subscription, and custom SOCKS5/HTTP Telegram network modes to Deployment Center and Installer.
- Treat subscription and custom proxy URLs as encrypted one-time deployment secrets and exclude them from commands, logs, progress, and deployment history.
- Install SHA-256-pinned Mihomo v1.19.31 only when the target VPS needs subscription proxying.
- Support native Mihomo/Clash YAML, URI-list, and Base64 URI-list subscriptions with local-only validation and no third-party converter.
- Bind the Mihomo mixed port and controller exclusively to `127.0.0.1`, add provider health checks, fallback node selection, hourly refresh monitoring, and last-known-good cache behavior.
- Route only Gateway Telegram API, polling, send, image, and document requests through the explicit proxy client; keep Codex and system traffic on their existing paths.
- Add proxy-aware Telegram self-tests, updater notifications, `/status`, `/settings`, and health diagnostics without exposing proxy credentials.
- Preserve and roll back existing Gateway, Codex, Thread, SQLite, proxy configuration, systemd units, and binaries during repeat installation failures.

## 1.2.1 - 2026-09-15

- Preserve the verified state when an existing pre-1.2 Chat receives its default model during SQLite migration.

## 1.2.0 - 2026-09-15

- Add `/model`, `/reasoning`, `/planreasoning`, and `/settings` Telegram commands with authorized inline keyboards.
- Persist model, normal reasoning effort, Plan reasoning effort, and model-verification state per Telegram Chat in SQLite.
- Snapshot settings when each task is queued so running and already queued Turns cannot be mutated by later settings changes.
- Read models and supported reasoning levels from the official Codex SDK catalog, including future levels such as `max` and `ultra`, instead of using a fixed list.
- Validate model changes with an ephemeral read-only provider call; reject explicit unsupported errors and report unverifiable transient failures without crashing the Gateway.
- Apply model and normal reasoning per Turn, and reapply Plan reasoning while resuming the same persistent Codex Thread.
- Show persisted settings and active-task snapshots in `/status`, and real Gateway/Codex/runtime details in `/settings` without exposing secrets.
- Preserve Chat settings during SQLite migration, Gateway restart, signed updates, and Installer reruns.

## 1.1.0 - 2026-09-09

- Add `/sessions` and `/resume` Telegram history selectors backed by official Codex SDK `thread_list()` and `thread_resume()`.
- Add authorized inline-keyboard callbacks with pagination and current-thread indication.
- Number tasks independently per Codex Thread; a new Thread starts at task `#1`, while a resumed Thread continues from its existing Turn count.
- Pin every queued task to the Thread selected when it was accepted, preventing conversation switches from redirecting queued work.
- Migrate existing Gateway SQLite databases in place without deleting tasks or Thread mappings.

## 1.0.6 - 2026-09-07

- Make the release `current/VERSION` file the authoritative Gateway health version source; stale `CODEX_TG_VERSION` environment values no longer cause false rollback.
- Include the actual health JSON in updater timeout errors for direct diagnosis.

## 1.0.5 - 2026-09-07

- Close the `/stop` race between queue claim and SDK turn registration by latching interrupt requests until the real Turn handle exists.
- Stop a newly started Gateway during first-install rollback when no previous Gateway existed.

## 1.0.4 - 2026-09-07

- Include the complete second-acceptance remediation set in the Gateway Release: real error status, queue detail, log-file delivery, upload retention, stronger redaction, DB health, updater rollback restoration, force-update behavior, and corrected installation-session cleanup.

## 1.0.3 - 2026-09-07

- Ensure explicit Installer validation failures use the same rollback, progress reporting, and redaction path as shell `ERR` failures.

## 1.0.2 - 2026-09-07

- Add upload retention cleanup and safe TXT log delivery for Gateway `/log`.
- Make Gateway status report real `ERROR` state and queue details.
- Harden secret redaction across Gateway and Deployment Center errors.
- Expire abandoned installation sessions and destroy their encrypted secrets.
- Make updater rollback restore the service units, updater binary, public key, current symlink, and verify old-version health.
- Make `update force` reinstall the same version while preserving shared data.
- Extend Installer rollback and health checks, including DB health and release version checks.
- Add isolated tests for real SDK resume/interrupt/image input, FIFO worker behavior, file retention, updater rollback, and all Installer failure modes.

## 1.0.1 - 2026-09-07

- Force the official Codex CLI installer into non-interactive mode so existing package-manager installations and the final launch prompt cannot block one-line deployment.
- Capture the official Codex installer output, redact deployment secrets, persist a root-only diagnostic log, and report a concise root cause to Deployment Center progress.
- Add container coverage for Codex installer failure reporting, redaction, permissions, and rollback-before-configuration behavior.
- Stamp the Python package version inside each generated Gateway Release.

## 1.0.0 - 2026-09-07

- Initial Deployment Center, Telegram Gateway, one-line Installer, signed Release and rollback-capable updater.
