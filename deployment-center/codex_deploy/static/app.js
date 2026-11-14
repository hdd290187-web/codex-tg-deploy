(() => {
  "use strict";
  const prefix = document.documentElement.dataset.prefix;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const byId = (id) => document.getElementById(id);
  const state = { bound: false, verifiedToken: "", bindingTimer: null, deploymentTimer: null, deploymentId: null };

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = csrf;
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch(`${prefix}${path}`, { ...options, headers });
    let data;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (response.status === 401) { location.reload(); throw new Error("会话已过期"); }
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join("；") : data.detail;
      throw new Error(detail || `请求失败 (${response.status})`);
    }
    return data;
  }

  function botStatus(text, mode = "ok") {
    const box = byId("bot-status");
    box.className = `status-box ${mode === "ok" ? "" : mode}`;
    box.textContent = text;
  }

  function networkMode() {
    return document.querySelector('input[name="telegram-network"]:checked').value;
  }

  function updateNetworkFields() {
    const mode = networkMode();
    byId("subscription-fields").classList.toggle("hidden", !["auto", "subscription"].includes(mode));
    byId("custom-proxy-fields").classList.toggle("hidden", mode !== "custom");
    byId("subscription-url").required = mode === "subscription";
    byId("custom-proxy-url").required = mode === "custom";
    if (!["auto", "subscription"].includes(mode)) byId("subscription-status").classList.add("hidden");
  }

  document.querySelectorAll('input[name="telegram-network"]').forEach((input) => {
    input.addEventListener("change", updateNetworkFields);
  });
  updateNetworkFields();

  byId("test-subscription").addEventListener("click", async () => {
    const value = byId("subscription-url").value.trim();
    const box = byId("subscription-status");
    if (!value) {
      box.className = "status-box failed";
      box.textContent = "请填写代理订阅 URL";
      return;
    }
    const button = byId("test-subscription");
    button.disabled = true;
    box.className = "status-box waiting";
    box.textContent = "正在读取并识别订阅…";
    try {
      const data = await api("/api/proxy/subscription/test", {
        method: "POST",
        body: JSON.stringify({ subscription_url: value }),
      });
      box.className = "status-box";
      box.textContent = `✓ 订阅可读取\n✓ 格式支持  ${data.format}\n✓ 已识别节点  ${data.node_count}`;
    } catch (exc) {
      box.className = "status-box failed";
      box.textContent = exc.message;
    } finally {
      button.disabled = false;
    }
  });

  async function pollBinding() {
    try {
      const data = await api("/api/telegram/bind");
      if (data.bound) {
        state.bound = true;
        clearInterval(state.bindingTimer);
        state.bindingTimer = null;
        botStatus(`✓ Telegram Bot 验证成功  @${data.bot.username}\n✓ User ID 已识别  ${data.user_id}\n✓ Chat ID 已识别  ${data.chat_id}`);
        byId("generate").disabled = false;
      } else if (data.verified) {
        botStatus(`✓ @${data.bot.username} (${data.bot.id})\n→ 请在 Telegram 中给 @${data.bot.username} 发送 ${data.start_command}`, "waiting");
      }
    } catch (exc) {
      clearInterval(state.bindingTimer);
      state.bindingTimer = null;
      botStatus(exc.message, "failed");
    }
  }

  byId("verify-bot").addEventListener("click", async () => {
    const token = byId("bot-token").value.trim();
    if (!token) { botStatus("请输入 Telegram Bot Token", "failed"); return; }
    const button = byId("verify-bot");
    button.disabled = true;
    state.bound = false;
    byId("generate").disabled = true;
    botStatus("正在验证 Telegram Bot…", "waiting");
    try {
      const data = await api("/api/telegram/verify", {
        method: "POST",
        body: JSON.stringify({ bot_token: token }),
      });
      state.verifiedToken = token;
      botStatus(`✓ ${data.bot.name}  @${data.bot.username}\n✓ Bot ID  ${data.bot.id}\n→ 请在 Telegram 中发送 ${data.start_command}`, "waiting");
      clearInterval(state.bindingTimer);
      state.bindingTimer = setInterval(pollBinding, 2200);
      await pollBinding();
    } catch (exc) {
      botStatus(exc.message, "failed");
    } finally {
      button.disabled = false;
    }
  });

  byId("bot-token").addEventListener("input", (event) => {
    if (event.target.value.trim() !== state.verifiedToken) {
      state.bound = false;
      byId("generate").disabled = true;
      if (state.verifiedToken) botStatus("Bot Token 已改变，请重新验证。", "waiting");
    }
  });

  byId("tavily-api-key").addEventListener("input", (event) => {
    if (event.target.value.trim()) byId("tavily-enabled").checked = true;
  });

  let uploadedPrompt = null;
  function codexPromptText() {
    return uploadedPrompt === null ? byId("codex-pre-instructions").value : uploadedPrompt;
  }
  function updateCodexPromptCount() {
    const text = codexPromptText();
    const bytes = new TextEncoder().encode(text).length;
    byId("codex-prompt-count").textContent = `${Array.from(text).length} 字符 · ${bytes} / 131072 字节`;
    byId("codex-pre-instructions").setCustomValidity(bytes > 131072 ? "内容不能超过 128 KiB" : "");
  }
  byId("codex-pre-instructions").addEventListener("input", () => {
    uploadedPrompt = null;
    updateCodexPromptCount();
  });
  byId("codex-prompt-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".txt") || file.size > 131072) {
      event.target.value = "";
      byId("form-error").textContent = "只接受不超过 128 KiB 的 .txt 文件";
      return;
    }
    try {
      const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
      const value = decoder.decode(await file.arrayBuffer());
      if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value)) throw new Error("TXT 包含非文本控制字符");
      if (value === "" || value === "\ufeff") throw new Error("TXT 文件为空，原内容未修改");
      uploadedPrompt = value;
      byId("codex-pre-instructions").value = value;
      updateCodexPromptCount();
      byId("form-error").textContent = "";
    } catch (exc) {
      event.target.value = "";
      byId("form-error").textContent = `TXT 读取失败：${exc.message}`;
    }
  });
  updateCodexPromptCount();

  byId("custom-plan").addEventListener("change", (event) => {
    byId("plan-row").classList.toggle("hidden", !event.target.checked);
  });

  byId("effort").addEventListener("change", () => {
    if (!byId("custom-plan").checked) byId("plan-effort").value = byId("effort").value;
  });

  byId("deploy-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = byId("form-error");
    error.textContent = "";
    if (!state.bound) { error.textContent = "请先完成 Telegram 绑定"; return; }
    const button = byId("generate");
    button.disabled = true;
    try {
      const data = await api("/api/deployments", {
        method: "POST",
        body: JSON.stringify({
          base_url: byId("base-url").value,
          api_key: byId("api-key").value,
          model: byId("model").value || "gpt-5.6-sol",
          reasoning_effort: byId("effort").value,
          plan_reasoning_effort: byId("custom-plan").checked ? byId("plan-effort").value : null,
          codex_pre_instructions_enabled: byId("codex-prompt-enabled").checked,
          codex_pre_instructions: codexPromptText(),
          claude_base_url: byId("claude-base-url").value,
          claude_api_key: byId("claude-api-key").value,
          claude_model: byId("claude-model").value || "sonnet",
          claude_effort: byId("claude-effort").value,
          tavily_api_key: byId("tavily-api-key").value,
          tavily_enabled: byId("tavily-api-key").value.trim() ? byId("tavily-enabled").checked : false,
          telegram_network_mode: networkMode(),
          proxy_subscription_url: byId("subscription-url").value || null,
          custom_proxy_url: byId("custom-proxy-url").value || null,
        }),
      });
      byId("api-key").value = "";
      byId("claude-api-key").value = "";
      byId("tavily-api-key").value = "";
      byId("codex-pre-instructions").value = "";
      uploadedPrompt = null;
      byId("codex-prompt-file").value = "";
      updateCodexPromptCount();
      byId("bot-token").value = "";
      state.verifiedToken = "";
      byId("subscription-url").value = "";
      byId("custom-proxy-url").value = "";
      state.deploymentId = data.id;
      byId("deployment-id").textContent = `#${data.id}`;
      byId("command").textContent = data.command;
      byId("deployment-expiry").textContent = `有效期至 ${new Date(data.expires_at * 1000).toLocaleString()}`;
      byId("deployment").classList.remove("hidden");
      byId("deployment").scrollIntoView({ behavior: "smooth", block: "start" });
      clearInterval(state.deploymentTimer);
      state.deploymentTimer = setInterval(pollDeployment, 2000);
      await pollDeployment();
    } catch (exc) {
      error.textContent = exc.message;
      button.disabled = false;
    }
  });

  const stageNames = {
    CLAIMED: "已领取配置", SYSTEM: "系统检测", DEPENDENCIES: "系统依赖", CODEX_BACKUP: "备份旧 Codex",
    CLAUDE_CLI: "安装 Claude Code CLI", CLAUDE: "Claude 自检", TAVILY: "Tavily 自检", CODEX_PROMPT: "Codex Instructions", CODEX_CLI: "安装 Codex CLI", RELEASE: "验证 Gateway Release", CONFIG: "写入配置",
    GATEWAY: "安装 Gateway", PROXY: "Telegram 网络", SYSTEMD: "启动 systemd", TELEGRAM: "Telegram 检查",
    CODEX: "Codex 自检", SUCCESS: "部署完成", FAILED: "部署失败"
  };

  async function pollDeployment() {
    if (!state.deploymentId) return;
    try {
      const data = await api(`/api/deployments/${state.deploymentId}`);
      const list = byId("progress-list");
      list.replaceChildren(...data.progress.map((item) => {
        const li = document.createElement("li");
        li.className = "done";
        li.textContent = `${stageNames[item.stage] || item.stage}${item.detail ? ` · ${item.detail}` : ""}`;
        return li;
      }));
      byId("deployment-state").textContent = `状态：${data.status}${data.error ? ` · ${data.error}` : ""}`;
      if (["SUCCESS", "FAILED", "EXPIRED"].includes(data.status)) {
        clearInterval(state.deploymentTimer);
        state.deploymentTimer = null;
      }
    } catch (exc) {
      byId("deployment-state").textContent = exc.message;
    }
  }

  byId("copy-command").addEventListener("click", async () => {
    await navigator.clipboard.writeText(byId("command").textContent);
    byId("copy-command").textContent = "已复制";
    setTimeout(() => { byId("copy-command").textContent = "复制"; }, 1500);
  });

  byId("logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" });
    location.reload();
  });

  document.querySelectorAll("[data-time]").forEach((node) => {
    node.textContent = new Date(Number(node.dataset.time) * 1000).toLocaleString();
  });
})();
