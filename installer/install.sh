#!/usr/bin/env bash
set -Eeuo pipefail

PUBLIC_BASE_URL='__PUBLIC_BASE_URL__'
CLAIM_TOKEN='__CLAIM_TOKEN__'
RELEASE_PUBLIC_KEY_B64='__RELEASE_PUBLIC_KEY_B64__'
INSTALL_ROOT='/opt/codex-tg'
CODEX_HOME_DIR='/root/.codex'
TMP_DIR=''
DEPLOYMENT_ID=''
INSTALL_TOKEN=''
BACKUP_DIR=''
CODEX_BACKUP_DIR=''
OLD_CURRENT=''
OLD_SERVICE_ACTIVE=0
DISPLACED_RELEASE=''
FINAL_RELEASE=''
CONFIG_WRITTEN=0
INSTALL_FILES_WRITTEN=0
SWITCHED_CURRENT=0
PROXY_FILES_WRITTEN=0
OLD_PROXY_ACTIVE=0
OLD_PROXY_TIMER_ACTIVE=0
SUCCESS=0
OLD_CLAUDE_CURRENT=''
CLAUDE_SWITCHED=0
CODEX_WRAPPER_WRITTEN=0
FAILURE_DETAIL=''

umask 077

say() { printf '%s\n' "$*"; }
die() {
    say "错误：$*" >&2
    FAILURE_DETAIL="$*"
    on_error 1 "$LINENO"
}

cleanup() {
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf -- "$TMP_DIR"
    fi
}

write_json() {
    local target="$1" stage="$2" detail="${3:-}"
    STAGE="$stage" DETAIL="$detail" TARGET="$target" python3 - <<'PY'
import json, os
with open(os.environ["TARGET"], "w", encoding="utf-8") as output:
    json.dump({"stage": os.environ["STAGE"], "detail": os.environ["DETAIL"]}, output)
PY
}

report() {
    [ -n "$DEPLOYMENT_ID" ] && [ -n "$INSTALL_TOKEN" ] || return 0
    local stage="$1" detail="${2:-}" body="$TMP_DIR/progress.json"
    write_json "$body" "$stage" "$detail"
    curl --config "$TMP_DIR/auth.curl" --request POST --data-binary "@$body" \
        "$PUBLIC_BASE_URL/api/install/$DEPLOYMENT_ID/progress" >/dev/null 2>&1 || true
}

finish_report() {
    [ -n "$DEPLOYMENT_ID" ] && [ -n "$INSTALL_TOKEN" ] || return 0
    local ok="$1" error="${2:-}" body="$TMP_DIR/finish.json"
    SUCCESS_VALUE="$ok" ERROR_VALUE="$error" TARGET="$body" python3 - <<'PY'
import json, os
with open(os.environ["TARGET"], "w", encoding="utf-8") as output:
    json.dump({"success": os.environ["SUCCESS_VALUE"] == "true", "error": os.environ["ERROR_VALUE"] or None}, output)
PY
    curl --config "$TMP_DIR/auth.curl" --request POST --data-binary "@$body" \
        "$PUBLIC_BASE_URL/api/install/$DEPLOYMENT_ID/finish" >/dev/null 2>&1 || true
}

sanitize_codex_install_log() {
    CLAIM_FILE="$TMP_DIR/claim.json" RAW_LOG="$TMP_DIR/codex-install.log" \
    SAFE_LOG="$INSTALL_ROOT/logs/codex-cli-install.log" python3 - <<'PY'
import json, os, re

raw = open(os.environ["RAW_LOG"], encoding="utf-8", errors="replace").read()
claim = json.load(open(os.environ["CLAIM_FILE"], encoding="utf-8"))
for key in (
    "api_key", "claude_api_key", "tavily_api_key", "telegram_bot_token", "installation_token",
    "proxy_subscription_url", "custom_proxy_url",
):
    secret = str(claim.get(key, ""))
    if len(secret) >= 6:
        raw = raw.replace(secret, "[REDACTED]")
raw = re.sub(r"Bearer\s+[A-Za-z0-9._~-]+", "Bearer [REDACTED]", raw, flags=re.I)
raw = re.sub(r"\b\d{6,15}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_BOT_TOKEN]", raw)
raw = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw)
raw = raw.replace("\x00", "")[-65536:]
safe_log = os.environ["SAFE_LOG"]
with open(safe_log, "w", encoding="utf-8") as output:
    output.write(raw)
os.chmod(safe_log, 0o600)
lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines() if line.strip()]
summary = " | ".join(lines[-4:]) if lines else "official Codex installer exited without output"
print(summary[-220:])
PY
}

sanitize_gateway_install_log() {
    CLAIM_FILE="$TMP_DIR/claim.json" PROXY_RESULT="$TMP_DIR/proxy-result.json" \
    RAW_LOG="$TMP_DIR/gateway-pip.log" SAFE_LOG="$INSTALL_ROOT/logs/gateway-pip-install.log" \
    python3 - <<'PY'
import json, os, re

raw = open(os.environ["RAW_LOG"], encoding="utf-8", errors="replace").read()
claim = json.load(open(os.environ["CLAIM_FILE"], encoding="utf-8"))
proxy = json.load(open(os.environ["PROXY_RESULT"], encoding="utf-8"))
for secret in (
    claim.get("api_key", ""),
    claim.get("claude_api_key", ""),
    claim.get("tavily_api_key", ""),
    claim.get("telegram_bot_token", ""),
    claim.get("proxy_subscription_url", ""),
    claim.get("custom_proxy_url", ""),
    proxy.get("proxy_url", ""),
    proxy.get("install_proxy_url", ""),
):
    if isinstance(secret, str) and len(secret) >= 6:
        raw = raw.replace(secret, "[REDACTED]")
raw = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", raw)
raw = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw).replace("\x00", "")[-65536:]
with open(os.environ["SAFE_LOG"], "w", encoding="utf-8") as output:
    output.write(raw)
os.chmod(os.environ["SAFE_LOG"], 0o600)
lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines() if line.strip()]
summary = " | ".join(lines[-4:]) if lines else "pip exited without output"
print(summary[-260:])
PY
}

sanitize_config_install_log() {
    CLAIM_FILE="$TMP_DIR/claim.json" PROXY_RESULT="$TMP_DIR/proxy-result.json" \
    RAW_LOG="$TMP_DIR/config-install.log" SAFE_LOG="$INSTALL_ROOT/logs/config-install.log" \
    python3 - <<'PY'
import json, os, re

raw = open(os.environ["RAW_LOG"], encoding="utf-8", errors="replace").read()
claim = json.load(open(os.environ["CLAIM_FILE"], encoding="utf-8"))
proxy = json.load(open(os.environ["PROXY_RESULT"], encoding="utf-8"))
for secret in (
    claim.get("api_key", ""),
    claim.get("claude_api_key", ""),
    claim.get("tavily_api_key", ""),
    claim.get("telegram_bot_token", ""),
    claim.get("proxy_subscription_url", ""),
    claim.get("custom_proxy_url", ""),
    proxy.get("proxy_url", ""),
):
    if isinstance(secret, str) and len(secret) >= 6:
        raw = raw.replace(secret, "[REDACTED]")
raw = re.sub(r"\b\d{6,15}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_BOT_TOKEN]", raw)
raw = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw).replace("\x00", "")[-65536:]
with open(os.environ["SAFE_LOG"], "w", encoding="utf-8") as output:
    output.write(raw)
os.chmod(os.environ["SAFE_LOG"], 0o600)
lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines() if line.strip()]
print((" | ".join(lines[-4:]) if lines else "configuration transaction exited without output")[-260:])
PY
}

restore_managed_file() {
    local path="$1" backup_name="$2"
    if [ -L "$BACKUP_DIR/$backup_name.link" ]; then
        local real_target
        real_target=$(readlink -f -- "$path" 2>/dev/null || true)
        if [ -n "$real_target" ] && [ -f "$BACKUP_DIR/$backup_name.real" ]; then
            cp -a -- "$BACKUP_DIR/$backup_name.real" "$real_target"
        fi
    elif [ -e "$BACKUP_DIR/$backup_name.file" ]; then
        mkdir -p -- "$(dirname -- "$path")"
        cp -a -- "$BACKUP_DIR/$backup_name.file" "$path"
    elif [ -e "$BACKUP_DIR/$backup_name.absent" ]; then
        rm -f -- "$path"
    fi
}

rollback() {
    set +e
    if [ "$CODEX_WRAPPER_WRITTEN" -eq 1 ]; then
        restore_managed_file /root/.local/bin/codex codex-wrapper
    fi
    if [ "$CLAUDE_SWITCHED" -eq 1 ]; then
        if [ -n "$OLD_CLAUDE_CURRENT" ]; then
            ln -sfn -- "$OLD_CLAUDE_CURRENT" "$INSTALL_ROOT/claude/current"
        else
            unlink "$INSTALL_ROOT/claude/current" 2>/dev/null || true
        fi
    fi
    if [ "$SWITCHED_CURRENT" -eq 1 ]; then
        systemctl stop codex-tg.service >/dev/null 2>&1 || true
    fi
    if [ "$PROXY_FILES_WRITTEN" -eq 1 ]; then
        systemctl stop codex-tg-proxy.service >/dev/null 2>&1 || true
    fi
    if [ -n "${STAGING_RELEASE:-}" ] && [ -d "$STAGING_RELEASE" ]; then
        rm -rf -- "$STAGING_RELEASE"
    fi
    if [ "$CONFIG_WRITTEN" -eq 1 ] && [ -n "$BACKUP_DIR" ]; then
        restore_managed_file "$INSTALL_ROOT/data/credentials.json" credentials
        restore_managed_file "$INSTALL_ROOT/data/inbox.key" inbox-key
        restore_managed_file "$INSTALL_ROOT/data/config-template-state/state.json" config-state
        restore_managed_file "$INSTALL_ROOT/data/config-template-state/codex.toml" config-state-codex
        restore_managed_file "$INSTALL_ROOT/data/config-template-state/claude.json" config-state-claude
        restore_managed_file "$INSTALL_ROOT/data/codex-pre-instructions.txt" codex-pre-instructions
        if [ -f "$BACKUP_DIR/codex-prompt-state.json" ] && [ -f "$INSTALL_ROOT/data/gateway.db" ]; then
            python3 - "$INSTALL_ROOT/data/gateway.db" "$BACKUP_DIR/codex-prompt-state.json" <<'RESTOREPROMPT'
import json, sqlite3, sys
with sqlite3.connect(sys.argv[1]) as conn:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='codex_prompt_state'").fetchone():
        old = json.load(open(sys.argv[2]))
        conn.execute('DELETE FROM codex_prompt_state WHERE id=1')
        if old is not None:
            conn.execute('INSERT INTO codex_prompt_state(id,enabled,updated_at,length,sha256) VALUES(1,?,?,?,?)', old)
RESTOREPROMPT
        fi
        restore_managed_file /etc/codex-tg.env etc-env
        restore_managed_file "$CODEX_HOME_DIR/config.toml" config
        restore_managed_file "$INSTALL_ROOT/data/claude/settings.json" claude-settings
        restore_managed_file "$CODEX_HOME_DIR/.env" dot-env
    fi
    if [ "$INSTALL_FILES_WRITTEN" -eq 1 ] && [ -n "$BACKUP_DIR" ]; then
        restore_managed_file /etc/codex-tg-release.pub release-public-key
        restore_managed_file /etc/codex-tg-config.pub config-template-public-key
        restore_managed_file /etc/systemd/system/codex-tg.service service-unit
        restore_managed_file /etc/systemd/system/codex-tg-update@.service update-unit
        restore_managed_file /usr/local/bin/codex-tg-updater updater-bin
    fi
    if [ "$PROXY_FILES_WRITTEN" -eq 1 ] && [ -n "$BACKUP_DIR" ]; then
        restore_managed_file /etc/systemd/system/codex-tg-proxy.service proxy-unit
        restore_managed_file /etc/systemd/system/codex-tg-proxy-refresh.service proxy-refresh-unit
        restore_managed_file /etc/systemd/system/codex-tg-proxy-refresh.timer proxy-refresh-timer
        restore_managed_file /etc/systemd/system/codex-tg.service.d/telegram-proxy.conf proxy-dropin
        rm -rf -- /etc/codex-tg-proxy
        rm -rf -- /opt/codex-tg-proxy
        if [ -d "$BACKUP_DIR/proxy-config" ]; then
            cp -a -- "$BACKUP_DIR/proxy-config" /etc/codex-tg-proxy
        fi
        if [ -d "$BACKUP_DIR/proxy-install" ]; then
            cp -a -- "$BACKUP_DIR/proxy-install" /opt/codex-tg-proxy
        fi
    fi
    if [ -n "$DISPLACED_RELEASE" ] && [ -d "$DISPLACED_RELEASE" ] && [ -n "$FINAL_RELEASE" ]; then
        [ -d "$FINAL_RELEASE" ] && mv -- "$FINAL_RELEASE" "${FINAL_RELEASE}.failed.$(date +%s)"
        mv -- "$DISPLACED_RELEASE" "$FINAL_RELEASE"
    fi
    if [ -n "$OLD_CURRENT" ] && [ -d "$OLD_CURRENT" ]; then
        ln -sfn -- "$OLD_CURRENT" "$INSTALL_ROOT/.current-rollback"
        mv -Tf -- "$INSTALL_ROOT/.current-rollback" "$INSTALL_ROOT/current"
    elif [ "$SWITCHED_CURRENT" -eq 1 ]; then
        unlink "$INSTALL_ROOT/current" 2>/dev/null || true
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    if [ "$OLD_PROXY_ACTIVE" -eq 1 ]; then
        systemctl restart codex-tg-proxy.service >/dev/null 2>&1 || true
    fi
    if [ "$OLD_PROXY_TIMER_ACTIVE" -eq 1 ]; then
        systemctl start codex-tg-proxy-refresh.timer >/dev/null 2>&1 || true
    fi
    if [ "$OLD_SERVICE_ACTIVE" -eq 1 ]; then
        systemctl restart codex-tg.service >/dev/null 2>&1 || true
    fi
}

on_error() {
    local code="$1" line="$2"
    trap - ERR
    set +e
    say "部署失败：步骤位于脚本第 $line 行（退出码 $code）" >&2
    local summary="${FAILURE_DETAIL:-installer step failed at line $line}"
    report FAILED "$summary"
    rollback
    finish_report false "$summary"
    exit "$code"
}

trap 'on_error $? $LINENO' ERR
trap cleanup EXIT

[ "${EUID:-$(id -u)}" -eq 0 ] || die "必须以 root 用户运行"
[ -r /etc/os-release ] || die "无法识别操作系统"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) ;;
    *) die "仅支持 Ubuntu 22.04/24.04 与 Debian 12/13（当前 ${ID:-unknown} ${VERSION_ID:-unknown}）" ;;
esac
case "$(uname -m)" in
    x86_64|aarch64|arm64) ;;
    *) die "仅支持 x86_64 与 arm64" ;;
esac

TMP_DIR=$(mktemp -d /tmp/codex-tg-install.XXXXXX)
chmod 700 "$TMP_DIR"

say "[1/12] 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git iproute2 openssl python3 python3-venv tar >/dev/null
SYSTEM_CURL=$(command -v curl)

say "[2/12] 领取一次性部署配置"
TARGET="$TMP_DIR/claim-request.json" OS_ID="${ID:-unknown}" OS_VERSION="${VERSION_ID:-unknown}" python3 - <<'PY'
import json, os, platform
with open(os.environ["TARGET"], "w", encoding="utf-8") as output:
    json.dump(
        {
            "hostname": platform.node()[:255],
            "os_id": os.environ["OS_ID"][:64],
            "os_version": os.environ["OS_VERSION"][:64],
            "architecture": platform.machine()[:64],
        },
        output,
    )
PY
curl -fsSL --request POST --header 'Content-Type: application/json' \
    --data-binary "@$TMP_DIR/claim-request.json" \
    "$PUBLIC_BASE_URL/api/install/claim/$CLAIM_TOKEN" >"$TMP_DIR/claim.json"
chmod 600 "$TMP_DIR/claim.json"
CLAIM_TOKEN=''

eval "$(CLAIM_FILE="$TMP_DIR/claim.json" python3 - <<'PY'
import json, os, shlex
data = json.load(open(os.environ["CLAIM_FILE"], encoding="utf-8"))
for key in ("deployment_id", "installation_token", "gateway_version", "release_manifest_url"):
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise SystemExit(f"claim response missing {key}")
print("DEPLOYMENT_ID=" + shlex.quote(data["deployment_id"]))
print("INSTALL_TOKEN=" + shlex.quote(data["installation_token"]))
print("GATEWAY_VERSION=" + shlex.quote(data["gateway_version"]))
print("RELEASE_MANIFEST_URL=" + shlex.quote(data["release_manifest_url"]))
PY
)"
AUTH_TOKEN="$INSTALL_TOKEN" TARGET="$TMP_DIR/auth.curl" python3 - <<'PY'
import os
token = os.environ["AUTH_TOKEN"]
with open(os.environ["TARGET"], "w", encoding="ascii") as output:
    output.write('silent\nshow-error\nfail\n')
    output.write(f'header = "Authorization: Bearer {token}"\n')
    output.write('header = "Content-Type: application/json"\n')
PY
chmod 600 "$TMP_DIR/auth.curl"
report CLAIMED "configuration claimed"

say "[3/12] 备份现有 Codex 与 Gateway 配置"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$CODEX_HOME_DIR/backups/$STAMP" "$INSTALL_ROOT/backups/$STAMP" "$INSTALL_ROOT/releases" "$INSTALL_ROOT/data/uploads" "$INSTALL_ROOT/logs"
chmod 700 "$CODEX_HOME_DIR" "$CODEX_HOME_DIR/backups" "$CODEX_HOME_DIR/backups/$STAMP" "$INSTALL_ROOT/backups/$STAMP" "$INSTALL_ROOT/data"
CODEX_BACKUP_DIR="$CODEX_HOME_DIR/backups/$STAMP"
BACKUP_DIR="$INSTALL_ROOT/backups/$STAMP"

backup_managed_file() {
    local path="$1" name="$2" destination="$3"
    if [ -L "$path" ]; then
        cp -a -- "$path" "$destination/$name.link"
        local resolved
        resolved=$(readlink -f -- "$path")
        [ -f "$resolved" ] || die "符号链接目标不是普通文件：$path"
        cp -a -- "$resolved" "$destination/$name.real"
    elif [ -e "$path" ]; then
        [ -f "$path" ] || die "配置路径不是普通文件：$path"
        cp -a -- "$path" "$destination/$name.file"
    else
        : >"$destination/$name.absent"
    fi
}

backup_managed_file "$CODEX_HOME_DIR/config.toml" config "$CODEX_BACKUP_DIR"
backup_managed_file "$INSTALL_ROOT/data/claude/settings.json" claude-settings "$BACKUP_DIR"
backup_managed_file "$CODEX_HOME_DIR/.env" dot-env "$CODEX_BACKUP_DIR"
if [ -e "$CODEX_HOME_DIR/auth.json" ] || [ -L "$CODEX_HOME_DIR/auth.json" ]; then
    cp -a -- "$CODEX_HOME_DIR/auth.json" "$CODEX_BACKUP_DIR/auth.json"
fi
backup_managed_file "$INSTALL_ROOT/data/credentials.json" credentials "$BACKUP_DIR"
backup_managed_file "$INSTALL_ROOT/data/inbox.key" inbox-key "$BACKUP_DIR"
backup_managed_file "$INSTALL_ROOT/data/config-template-state/state.json" config-state "$BACKUP_DIR"
backup_managed_file "$INSTALL_ROOT/data/config-template-state/codex.toml" config-state-codex "$BACKUP_DIR"
backup_managed_file "$INSTALL_ROOT/data/config-template-state/claude.json" config-state-claude "$BACKUP_DIR"
backup_managed_file /root/.local/bin/codex codex-wrapper "$BACKUP_DIR"
backup_managed_file "$INSTALL_ROOT/data/codex-pre-instructions.txt" codex-pre-instructions "$BACKUP_DIR"
python3 - "$INSTALL_ROOT/data/gateway.db" "$BACKUP_DIR/codex-prompt-state.json" <<'BACKUPPROMPT'
import json, pathlib, sqlite3, sys
old = None
if pathlib.Path(sys.argv[1]).exists():
    with sqlite3.connect(sys.argv[1]) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='codex_prompt_state'").fetchone():
            old = conn.execute('SELECT enabled,updated_at,length,sha256 FROM codex_prompt_state WHERE id=1').fetchone()
pathlib.Path(sys.argv[2]).write_text(json.dumps(old))
BACKUPPROMPT
backup_managed_file /etc/codex-tg.env etc-env "$BACKUP_DIR"
backup_managed_file /etc/codex-tg-release.pub release-public-key "$BACKUP_DIR"
backup_managed_file /etc/codex-tg-config.pub config-template-public-key "$BACKUP_DIR"
backup_managed_file /etc/systemd/system/codex-tg.service service-unit "$BACKUP_DIR"
backup_managed_file /etc/systemd/system/codex-tg-update@.service update-unit "$BACKUP_DIR"
backup_managed_file /usr/local/bin/codex-tg-updater updater-bin "$BACKUP_DIR"
backup_managed_file /etc/systemd/system/codex-tg-proxy.service proxy-unit "$BACKUP_DIR"
backup_managed_file /etc/systemd/system/codex-tg-proxy-refresh.service proxy-refresh-unit "$BACKUP_DIR"
backup_managed_file /etc/systemd/system/codex-tg-proxy-refresh.timer proxy-refresh-timer "$BACKUP_DIR"
backup_managed_file /etc/systemd/system/codex-tg.service.d/telegram-proxy.conf proxy-dropin "$BACKUP_DIR"
if [ -d /etc/codex-tg-proxy ]; then
    cp -a -- /etc/codex-tg-proxy "$BACKUP_DIR/proxy-config"
fi
if [ -d /opt/codex-tg-proxy ]; then
    cp -a -- /opt/codex-tg-proxy "$BACKUP_DIR/proxy-install"
fi
cp -a -- "$CODEX_BACKUP_DIR/config."* "$BACKUP_DIR/" 2>/dev/null || true
cp -a -- "$CODEX_BACKUP_DIR/dot-env."* "$BACKUP_DIR/" 2>/dev/null || true
[ -L "$INSTALL_ROOT/current" ] && OLD_CURRENT=$(readlink -f -- "$INSTALL_ROOT/current" || true)
systemctl is-active --quiet codex-tg.service 2>/dev/null && OLD_SERVICE_ACTIVE=1 || true
systemctl is-active --quiet codex-tg-proxy.service 2>/dev/null && OLD_PROXY_ACTIVE=1 || true
systemctl is-active --quiet codex-tg-proxy-refresh.timer 2>/dev/null && OLD_PROXY_TIMER_ACTIVE=1 || true
systemctl stop codex-tg.service 2>/dev/null || true
systemctl stop codex-tg-proxy-refresh.timer 2>/dev/null || true
systemctl stop codex-tg-proxy.service 2>/dev/null || true
report CODEX_BACKUP "existing configuration backed up"

# HTTPS installer bootstrap is self-contained so subscription setup happens
# before the large signed Release/wheelhouse/Claude downloads.
printf '%s' '__PROXY_BOOTSTRAP_B64__' | base64 -d >"$TMP_DIR/proxy-bootstrap"
printf '%s' '__PROXY_UNIT_B64__' | base64 -d >"$TMP_DIR/proxy-bootstrap.service"
chmod 700 "$TMP_DIR/proxy-bootstrap"
say "[6/12] 检测并配置 Telegram 网络"
PROXY_FILES_WRITTEN=1
if ! "$TMP_DIR/proxy-bootstrap" \
    --claim "$TMP_DIR/claim.json" \
    --output "$TMP_DIR/proxy-result.json" \
    >"$TMP_DIR/proxy-setup.log" 2>&1; then
    PROXY_ERROR=$(tail -n 1 "$TMP_DIR/proxy-setup.log" | sed 's/^PROXY_SETUP_FAILED: //' | cut -c1-180)
    FAILURE_DETAIL="Telegram 网络配置失败：${PROXY_ERROR:-请检查直连、订阅格式或自定义代理}"
    say "$FAILURE_DETAIL" >&2
    false
fi
chmod 600 "$TMP_DIR/proxy-result.json"
eval "$(PROXY_RESULT="$TMP_DIR/proxy-result.json" python3 - <<'PY'
import json, os, shlex
data = json.load(open(os.environ["PROXY_RESULT"], encoding="utf-8"))
for key in ("network", "kind", "proxy_url", "install_proxy_url"):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise SystemExit(f"invalid proxy result: {key}")
    print("TELEGRAM_" + key.upper() + "=" + shlex.quote(value))
PY
)"
report PROXY "$TELEGRAM_NETWORK ($TELEGRAM_KIND)"

if [ "$TELEGRAM_KIND" = subscription ]; then
    install -o root -g root -m 0644 "$TMP_DIR/proxy-bootstrap.service" /etc/systemd/system/codex-tg-proxy.service
    systemctl daemon-reload
    systemctl start codex-tg-proxy.service
    for _ in $(seq 1 30); do
        if systemctl is-active --quiet codex-tg-proxy.service && \
           ss -ltnH 'sport = :7890' | grep -q '127\.0\.0\.1:7890'; then
            break
        fi
        sleep 1
    done
    systemctl is-active --quiet codex-tg-proxy.service || die "Codex 安装前无法启动本地代理"
fi

NETWORK_RESULT="$TMP_DIR/proxy-result.json" CURL_CONFIG="$TMP_DIR/codex-install.curl" \
PIP_CONFIG="$TMP_DIR/pip.conf" python3 - <<'PY'
import json, os

network = json.load(open(os.environ["NETWORK_RESULT"], encoding="utf-8"))
proxy = network.get("install_proxy_url") or network["proxy_url"]

def curl_quote(value):
    if any(ch in value for ch in "\r\n\x00"):
        raise SystemExit("invalid proxy URL")
    return value.replace("\\", "\\\\").replace('"', '\\"')

with open(os.environ["CURL_CONFIG"], "w", encoding="utf-8") as output:
    if proxy:
        output.write(f'proxy = "{curl_quote(proxy)}"\n')
    else:
        output.write('noproxy = "*"\n')
with open(os.environ["PIP_CONFIG"], "w", encoding="utf-8") as output:
    output.write("[global]\ndisable-pip-version-check = true\n")
    if proxy:
        output.write("proxy = " + proxy.replace("%", "%%") + "\n")
PY
chmod 600 "$TMP_DIR/codex-install.curl" "$TMP_DIR/pip.conf"
install -d -m 0700 "$TMP_DIR/codex-net-bin"
cat >"$TMP_DIR/codex-net-bin/curl" <<EOF
#!/bin/sh
exec "$SYSTEM_CURL" --disable --config "$TMP_DIR/codex-install.curl" "\$@"
EOF
chmod 700 "$TMP_DIR/codex-net-bin/curl"

release_curl() { "$TMP_DIR/codex-net-bin/curl" "$@"; }

say "[4/12] 验证并下载 Gateway Release v$GATEWAY_VERSION"
printf '%s' "$RELEASE_PUBLIC_KEY_B64" | base64 -d >"$TMP_DIR/release-public.pem"
openssl pkey -pubin -in "$TMP_DIR/release-public.pem" -noout >/dev/null
release_curl -fsSL "$RELEASE_MANIFEST_URL" -o "$TMP_DIR/manifest.json"
release_curl -fsSL "${RELEASE_MANIFEST_URL}.sig" -o "$TMP_DIR/manifest.json.sig"
openssl pkeyutl -verify -pubin -inkey "$TMP_DIR/release-public.pem" -rawin \
    -in "$TMP_DIR/manifest.json" -sigfile "$TMP_DIR/manifest.json.sig" >/dev/null
eval "$(MANIFEST_FILE="$TMP_DIR/manifest.json" EXPECTED_VERSION="$GATEWAY_VERSION" \
MACHINE="$(uname -m)" python3 - <<'PY'
import json, os, re, shlex
data = json.load(open(os.environ["MANIFEST_FILE"], encoding="utf-8"))
version = str(data.get("version", ""))
archive = str(data.get("archive", ""))
digest = str(data.get("sha256", ""))
if version != os.environ["EXPECTED_VERSION"] or not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise SystemExit("release version mismatch")
if archive != os.path.basename(archive) or not archive.endswith(".tar.gz"):
    raise SystemExit("invalid archive name")
if not re.fullmatch(r"[0-9a-f]{64}", digest):
    raise SystemExit("invalid release digest")
architecture = os.environ["MACHINE"].lower()
if architecture == "arm64":
    architecture = "aarch64"
python_key = f"cp{os.sys.version_info.major}{os.sys.version_info.minor}-{architecture}"
wheelhouse = (data.get("wheelhouses") or {}).get(python_key)
if not isinstance(wheelhouse, dict):
    raise SystemExit(f"release does not support Python/architecture {python_key}")
cli = wheelhouse.get("cli")
if not isinstance(cli, dict):
    raise SystemExit("release wheelhouse is missing Codex CLI runtime")
for label, name in (
    ("wheelhouse", wheelhouse.get("archive")),
    ("CLI wheel", cli.get("file")),
):
    if not isinstance(name, str) or name != os.path.basename(name):
        raise SystemExit(f"invalid {label} file name")
for label, value in (
    ("wheelhouse", wheelhouse.get("sha256")),
    ("CLI wheel", cli.get("sha256")),
):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SystemExit(f"invalid {label} digest")
claude = data.get('claude', {})
asset = claude.get('assets', {}).get(architecture, {})
if not re.fullmatch(r'\d+\.\d+\.\d+', claude.get('version', '')):
    raise SystemExit('missing Claude version in signed manifest')
if not asset.get('file') or os.path.basename(asset['file']) != asset['file'] or not re.fullmatch(r'[a-f0-9]{64}', asset.get('sha256', '')):
    raise SystemExit('invalid signed Claude asset')
print('CLAUDE_ASSET=' + shlex.quote(asset['file']))
print('CLAUDE_SHA256=' + shlex.quote(asset['sha256']))
print('CLAUDE_VERSION=' + shlex.quote(claude['version']))
print("ARCHIVE_NAME=" + shlex.quote(archive))
print("ARCHIVE_SHA256=" + shlex.quote(digest))
print("WHEELHOUSE_ARCHIVE=" + shlex.quote(wheelhouse["archive"]))
print("WHEELHOUSE_SHA256=" + shlex.quote(wheelhouse["sha256"]))
print("CLI_WHEEL=" + shlex.quote(cli["file"]))
print("CLI_WHEEL_SHA256=" + shlex.quote(cli["sha256"]))
PY
)"
RELEASE_BASE_URL=${RELEASE_MANIFEST_URL%/*}
release_curl -fsSL "$RELEASE_BASE_URL/$ARCHIVE_NAME" -o "$TMP_DIR/$ARCHIVE_NAME"
printf '%s  %s\n' "$ARCHIVE_SHA256" "$TMP_DIR/$ARCHIVE_NAME" | sha256sum -c - >/dev/null
release_curl --fail --show-error --location --retry 5 --retry-all-errors \
    --connect-timeout 15 --max-time 1800 \
    "$RELEASE_BASE_URL/$WHEELHOUSE_ARCHIVE" -o "$TMP_DIR/$WHEELHOUSE_ARCHIVE"
release_curl --fail --show-error --location --retry 5 --retry-all-errors \
    --connect-timeout 15 --max-time 1800 \
    "$RELEASE_BASE_URL/$CLI_WHEEL" -o "$TMP_DIR/$CLI_WHEEL"
printf '%s  %s\n' "$WHEELHOUSE_SHA256" "$TMP_DIR/$WHEELHOUSE_ARCHIVE" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$CLI_WHEEL_SHA256" "$TMP_DIR/$CLI_WHEEL" | sha256sum -c - >/dev/null
mkdir -p "$TMP_DIR/wheelhouse"
WHEELHOUSE_ROOT="$TMP_DIR/wheelhouse" WHEELHOUSE_ARCHIVE_PATH="$TMP_DIR/$WHEELHOUSE_ARCHIVE" \
python3 - <<'PY'
import os, pathlib, tarfile
root = pathlib.Path(os.environ["WHEELHOUSE_ROOT"]).resolve()
with tarfile.open(os.environ["WHEELHOUSE_ARCHIVE_PATH"], "r:gz") as archive:
    for member in archive.getmembers():
        target = (root / member.name).resolve()
        if target.parent != root or not member.isfile() or not member.name.endswith(".whl"):
            raise SystemExit("unsafe wheelhouse entry")
    archive.extractall(root)
PY
cp -- "$TMP_DIR/$CLI_WHEEL" "$TMP_DIR/wheelhouse/$CLI_WHEEL"
release_curl --fail --show-error --location --retry 5 --retry-all-errors \
    --connect-timeout 15 --max-time 1800 "$RELEASE_BASE_URL/$CLAUDE_ASSET" -o "$TMP_DIR/claude"
printf '%s  %s\n' "$CLAUDE_SHA256" "$TMP_DIR/claude" | sha256sum -c - >/dev/null
report RELEASE "signature and SHA-256 verified"

say "[5/12] 解压 Gateway Release"
STAGING_RELEASE="$INSTALL_ROOT/releases/.${GATEWAY_VERSION}.$$"
FINAL_RELEASE="$INSTALL_ROOT/releases/$GATEWAY_VERSION"
[ ! -e "$STAGING_RELEASE" ] || die "临时 Release 目录冲突"
mkdir -p "$STAGING_RELEASE"
STAGING="$STAGING_RELEASE" ARCHIVE="$TMP_DIR/$ARCHIVE_NAME" python3 - <<'PY'
import os, pathlib, tarfile
root = pathlib.Path(os.environ["STAGING"]).resolve()
with tarfile.open(os.environ["ARCHIVE"], "r:gz") as archive:
    for member in archive.getmembers():
        target = (root / member.name).resolve()
        if target != root and root not in target.parents:
            raise SystemExit("unsafe release archive path")
        if member.issym() or member.islnk() or member.isdev():
            raise SystemExit("unsupported release archive entry")
    archive.extractall(root)
PY
[ "$(tr -d '\r\n' <"$STAGING_RELEASE/VERSION")" = "$GATEWAY_VERSION" ] || die "Release 内部版本不匹配"

say "[7/12] 安装 Gateway 依赖与官方 Codex CLI"
python3 -m venv "$STAGING_RELEASE/.venv"
if "$STAGING_RELEASE/.venv/bin/python" -m pip install -q \
    --no-index --find-links "$TMP_DIR/wheelhouse" \
    -r "$STAGING_RELEASE/requirements.lock" \
    >"$TMP_DIR/gateway-pip.log" 2>&1; then
    sanitize_gateway_install_log >/dev/null
else
    PIP_SUMMARY=$(sanitize_gateway_install_log)
    FAILURE_DETAIL="Gateway Python 依赖安装失败：$PIP_SUMMARY"
    say "$FAILURE_DETAIL" >&2
    false
fi
if env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
    PYTHONPATH="$STAGING_RELEASE" "$STAGING_RELEASE/.venv/bin/python" - <<'RUNTIMEPY' >"$TMP_DIR/codex-install.log" 2>&1
from codex_cli_bin import bundled_codex_path
import subprocess,sys
try:
    result=subprocess.run([str(bundled_codex_path()), '--version'], capture_output=True, text=True, timeout=20)
except (OSError,subprocess.TimeoutExpired) as exc:
    print('Bundled Codex runtime verification failed: '+str(exc),file=sys.stderr)
    raise SystemExit(1)
print(result.stdout.strip())
if result.returncode or not result.stdout.startswith('codex-cli '):
    print('Bundled Codex runtime verification failed: '+result.stderr.strip()[-1500:],file=sys.stderr)
    raise SystemExit(result.returncode or 1)
RUNTIMEPY
then
    CODEX_INSTALL_STATUS=0
else
    CODEX_INSTALL_STATUS=$?
fi
if [ "$CODEX_INSTALL_STATUS" -ne 0 ]; then
    FAILURE_DETAIL="Codex CLI：$(sanitize_codex_install_log)"
    say "$FAILURE_DETAIL" >&2
    false
fi
sanitize_codex_install_log >/dev/null
report CODEX_CLI "official SDK bundled Codex runtime verified"
chmod 755 "$TMP_DIR/claude"
"$TMP_DIR/claude" --version >"$TMP_DIR/claude-version"
grep -Fq "$CLAUDE_VERSION" "$TMP_DIR/claude-version" || die "Claude CLI 版本验证失败"
install -d -m 0755 "$INSTALL_ROOT/claude/versions"
[ ! -L "$INSTALL_ROOT/claude/current" ] || OLD_CLAUDE_CURRENT=$(readlink -f "$INSTALL_ROOT/claude/current")
install -m 0755 "$TMP_DIR/claude" "$INSTALL_ROOT/claude/versions/$CLAUDE_VERSION"
ln -sfn "$INSTALL_ROOT/claude/versions/$CLAUDE_VERSION" "$INSTALL_ROOT/claude/.current-new"
mv -Tf "$INSTALL_ROOT/claude/.current-new" "$INSTALL_ROOT/claude/current"
CLAUDE_SWITCHED=1
report CLAUDE_CLI "official Claude Code CLI installed"
"$STAGING_RELEASE/.venv/bin/python" -m compileall -q "$STAGING_RELEASE/codex_tg"
if [ -d "$FINAL_RELEASE" ]; then
    DISPLACED_RELEASE="$INSTALL_ROOT/releases/.${GATEWAY_VERSION}.previous.$STAMP"
    mv -- "$FINAL_RELEASE" "$DISPLACED_RELEASE"
fi
mv -- "$STAGING_RELEASE" "$FINAL_RELEASE"
python3 - "$STAGING_RELEASE/.venv" "$FINAL_RELEASE/.venv" <<'RELOCATEPY'
import pathlib,sys
old,new=sys.argv[1:]
for script in (pathlib.Path(new)/'bin').glob('*'):
    if script.is_file() and not script.is_symlink() and script.stat().st_size < 1024*1024:
        raw=script.read_bytes()
        if raw.startswith(b'#!') and old.encode() in raw:
            script.write_bytes(raw.replace(old.encode(),new.encode()))
RELOCATEPY
report GATEWAY "Gateway release installed"

say "[8/12] 原子写入 Codex 与 Gateway 配置"
CONFIG_WRITTEN=1
if ! CLAIM_FILE="$TMP_DIR/claim.json" CODEX_HOME_DIR="$CODEX_HOME_DIR" \
TARGET_ENV="/etc/codex-tg.env.new" RELEASE_PUBLIC_KEY="$TMP_DIR/release-public.pem" \
PROXY_RESULT="$TMP_DIR/proxy-result.json" "$FINAL_RELEASE/.venv/bin/python" - 2>"$TMP_DIR/config-install.log" <<'PY'
import json, os, pathlib, tempfile

claim = json.load(open(os.environ["CLAIM_FILE"], encoding="utf-8"))
proxy = json.load(open(os.environ["PROXY_RESULT"], encoding="utf-8"))
home = pathlib.Path(os.environ["CODEX_HOME_DIR"])
home.mkdir(parents=True, exist_ok=True)

def quote(value):
    value = str(value)
    if any(ch in value for ch in "\r\n\x00"):
        raise SystemExit("secret/config value contains invalid control characters")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

def real_target(path):
    return path.resolve(strict=True) if path.is_symlink() else path

def atomic_write(path, text, mode=0o600):
    target = real_target(path) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, mode)
        os.replace(name, target)
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass

env_lines = [
    "TELEGRAM_BOT_TOKEN=" + quote(claim["telegram_bot_token"]),
    "ALLOWED_USER_ID=" + quote(claim["allowed_user_id"]),
    "ALLOWED_CHAT_ID=" + quote(claim["allowed_chat_id"]),
    "CODEX_TG_API_KEY=" + quote(claim.get("api_key", "")),
    "CODEX_TG_BASE_URL=" + quote(claim.get("base_url", "")),
    "ANTHROPIC_BASE_URL=" + quote(claim.get("claude_base_url", "")),
    "ANTHROPIC_API_KEY=" + quote(claim.get("claude_api_key", "")),
    "CLAUDE_CODE_MODEL=" + quote(claim.get("claude_model", "sonnet")),
    "CLAUDE_CODE_EFFORT=" + quote(claim.get("claude_effort", "high")),
    "TAVILY_API_KEY=" + quote(claim.get("tavily_api_key", "")),
    "TAVILY_ENABLED=" + quote('1' if claim.get("tavily_api_key") and claim.get("tavily_enabled", True) else '0'),
    "CODEX_TG_CODEX_PROMPT_ENABLED=" + quote('1' if claim.get("codex_pre_instructions") and claim.get("codex_pre_instructions_enabled", False) else '0'),
    "CODEX_TG_ADAPTER_PORT=0",
    "CODEX_TG_MODEL=" + quote(claim["model"]),
    "CODEX_TG_REASONING_EFFORT=" + quote(claim["reasoning_effort"]),
    "CODEX_TG_PLAN_REASONING_EFFORT=" + quote(claim["plan_reasoning_effort"]),
    "CODEX_TG_TELEGRAM_NETWORK=" + quote(proxy["network"]),
    "CODEX_TG_TELEGRAM_PROXY_KIND=" + quote(proxy["kind"]),
    "CODEX_TG_TELEGRAM_PROXY_URL=" + quote(proxy["proxy_url"]),
    "CODEX_TG_RELEASE_BASE_URL=" + quote(claim["release_base_url"]),
    "CODEX_TG_RELEASE_PUBLIC_KEY=/etc/codex-tg-release.pub",
    "CODEX_TG_DATABASE=/opt/codex-tg/data/gateway.db",
    "CODEX_TG_UPLOAD_DIR=/opt/codex-tg/data/uploads",
    "CODEX_TG_LOG_DIR=/opt/codex-tg/logs",
    "CODEX_TG_UPLOAD_RETENTION_DAYS=7",
    "CODEX_TG_HEALTH_PORT=17654",
    "CODEX_TG_VERSION=" + quote(claim["gateway_version"]),
]
atomic_write(pathlib.Path(os.environ["TARGET_ENV"]), "\n".join(env_lines) + "\n")

config = f'''model_provider = "codex_tg"
model = {quote(claim["model"])}
model_reasoning_effort = {quote(claim["reasoning_effort"])}
plan_mode_reasoning_effort = {quote(claim["plan_reasoning_effort"])}
model_reasoning_summary = "detailed"
model_verbosity = "high"
model_context_window = 1050000
model_auto_compact_token_limit = 850000
web_search = "live"
personality = "pragmatic"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.codex_tg]
name = "Codex TG Relay"
base_url = {quote(claim.get("base_url") or "https://api.openai.com/v1")}
env_key = "CODEX_TG_API_KEY"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 2
stream_max_retries = 2
stream_idle_timeout_ms = 600000
supports_websockets = false
'''
template = claim.get('codex_config_template') or ''
if template:
    import tomlkit
    try:
        document = tomlkit.parse(template)
        # The Gateway always starts Codex with this provider and overlays the
        # live endpoint through the SDK.  Keep the file-level selector aligned
        # so a manually launched CLI cannot point at a stale/missing provider.
        document['model_provider'] = 'codex_tg'
        document['model'] = claim['model']
        document['model_reasoning_effort'] = claim['reasoning_effort']
        document['plan_mode_reasoning_effort'] = claim['plan_reasoning_effort']
        providers = document.get('model_providers')
        if providers is None:
            providers = tomlkit.table()
            document['model_providers'] = providers
        provider = providers.get('codex_tg')
        if provider is None:
            provider = tomlkit.table()
            providers['codex_tg'] = provider
        provider['name'] = provider.get('name', 'Codex TG Relay')
        provider['base_url'] = claim.get('base_url') or provider.get('base_url', 'https://api.openai.com/v1')
        provider['env_key'] = 'CODEX_TG_API_KEY'
        provider['wire_api'] = 'responses'
        provider['requires_openai_auth'] = False
        provider['supports_websockets'] = False
        config = tomlkit.dumps(document)
    except Exception as exc:
        raise SystemExit('Codex 配置模板无效：' + type(exc).__name__)
if template or (claim.get("base_url") and claim.get("api_key")):
    atomic_write(home / "config.toml", config)

claude_template = claim.get('claude_config_template') or ''
if claude_template:
    import json as _json
    try:
        document = _json.loads(claude_template)
        if not isinstance(document, dict):
            raise ValueError('root must be an object')
        # Keep the deployment form and the file used by a manually launched
        # Claude CLI consistent.  Gateway tasks still pass --model/--effort,
        # which intentionally take precedence for the running Turn.
        document['model'] = claim.get('claude_model') or document.get('model', 'sonnet')
        if claim.get('claude_effort') in {'low', 'medium', 'high', 'xhigh'}:
            document['effortLevel'] = claim['claude_effort']
        elif document.get('effortLevel') == 'max':
            document.pop('effortLevel', None)
        permissions = document.get('permissions')
        if permissions is None:
            permissions = {}
            document['permissions'] = permissions
        if not isinstance(permissions, dict):
            raise ValueError('permissions must be an object')
        permissions.setdefault('defaultMode', 'bypassPermissions')
        # Older Gateway templates used modelSettings.effortLevel, which is
        # invalid: modelSettings must map model IDs to objects.  Preserve
        # valid per-model entries while migrating that legacy shorthand.
        model_settings = document.get('modelSettings')
        if model_settings is None:
            document['modelSettings'] = {}
        elif not isinstance(model_settings, dict):
            raise ValueError('modelSettings must be an object')
        elif 'effortLevel' in model_settings:
            legacy_effort = model_settings.pop('effortLevel')
            if isinstance(legacy_effort, str) and legacy_effort in {'low', 'medium', 'high', 'xhigh'}:
                document.setdefault('effortLevel', legacy_effort)
        claude_text = _json.dumps(document, ensure_ascii=False, indent=2) + '\n'
    except Exception as exc:
        raise SystemExit('Claude 配置模板无效：' + type(exc).__name__)
    claude_settings = pathlib.Path('/opt/codex-tg/data/claude/settings.json')
    atomic_write(claude_settings, claude_text)

dotenv_path = home / ".env"
dotenv_target = real_target(dotenv_path) if dotenv_path.is_symlink() else dotenv_path
old_lines = dotenv_target.read_text("utf-8").splitlines() if dotenv_target.exists() else []
kept = [line for line in old_lines if not line.startswith("CODEX_TG_API_KEY=")]
kept.append("CODEX_TG_API_KEY=" + quote(claim.get("api_key", "")))
if claim.get("base_url") and claim.get("api_key"):
    atomic_write(dotenv_path, "\n".join(kept) + "\n")
PY
then
    CONFIG_SUMMARY=$(sanitize_config_install_log)
    FAILURE_DETAIL="Gateway 配置写入失败：$CONFIG_SUMMARY"
    say "$FAILURE_DETAIL" >&2
    false
fi
install -o root -g root -m 0600 /etc/codex-tg.env.new /etc/codex-tg.env
rm -f /etc/codex-tg.env.new
install -o root -g root -m 0644 "$TMP_DIR/release-public.pem" /etc/codex-tg-release.pub
if [ -f "$FINAL_RELEASE/config-template-public.pem" ]; then
    install -o root -g root -m 0644 "$FINAL_RELEASE/config-template-public.pem" /etc/codex-tg-config.pub
fi
CONFIG_WRITTEN=1
CLAIM_FILE="$TMP_DIR/claim.json" PYTHONPATH="$FINAL_RELEASE" "$FINAL_RELEASE/.venv/bin/python" - <<'CONFIGPY'
import json, os
from pathlib import Path
from codex_tg.credentials import Credentials
from codex_tg.pre_instructions import PromptStore
from codex_tg.store import Store
from codex_tg.credentials import atomic_json
claim = json.load(open(os.environ['CLAIM_FILE']))
state_root = Path('/opt/codex-tg/data/config-template-state')
state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
state = json.loads((state_root / 'state.json').read_text()) if (state_root / 'state.json').exists() else {}
for engine, suffix in [('codex', 'toml'), ('claude', 'json')]:
    text = claim.get(engine + '_config_template')
    if text is not None and engine not in state:
        path = state_root / (engine + '.' + suffix)
        path.write_text(text)
        path.chmod(0o600)
        state[engine] = {'version': str(claim.get(engine + '_config_template_version', 0)), 'conflicts': []}
atomic_json(state_root / 'state.json', state)
vault = Credentials(Path('/opt/codex-tg/data/credentials.json'))
for engine, prefix in [('codex', ''), ('claude', 'claude_')]:
    if claim.get(prefix + 'api_key') or claim.get(prefix + 'base_url'):
        vault.save(engine, claim.get(prefix + 'base_url', ''), claim.get(prefix + 'api_key', ''))
if claim.get('tavily_api_key'):
    vault.save('tavily', '', claim['tavily_api_key'])
prompt_store = PromptStore(Path('/opt/codex-tg/data/codex-pre-instructions.txt'))
prompt = claim.get('codex_pre_instructions', '')
if prompt or not prompt_store.path.exists():
    revision = prompt_store.save(prompt)
    Store(Path('/opt/codex-tg/data/gateway.db')).set_codex_prompt_state(
        enabled=bool(prompt and claim.get('codex_pre_instructions_enabled', False)),
        updated_at=prompt_store.now(), length=len(prompt), sha256=revision)
CONFIGPY
INSTALL_FILES_WRITTEN=1
report CONFIG "configuration written atomically"

say "[9/12] 安装 systemd、代理依赖与独立 Updater"
install -o root -g root -m 0644 "$FINAL_RELEASE/systemd/codex-tg.service" /etc/systemd/system/codex-tg.service
install -o root -g root -m 0644 "$FINAL_RELEASE/systemd/codex-tg-update@.service" /etc/systemd/system/codex-tg-update@.service
install -o root -g root -m 0755 "$FINAL_RELEASE/bin/codex-tg-updater" /usr/local/bin/codex-tg-updater
if [ "$TELEGRAM_KIND" = subscription ]; then
    install -o root -g root -m 0644 "$FINAL_RELEASE/systemd/codex-tg-proxy.service" /etc/systemd/system/codex-tg-proxy.service
    install -o root -g root -m 0644 "$FINAL_RELEASE/systemd/codex-tg-proxy-refresh.service" /etc/systemd/system/codex-tg-proxy-refresh.service
    install -o root -g root -m 0644 "$FINAL_RELEASE/systemd/codex-tg-proxy-refresh.timer" /etc/systemd/system/codex-tg-proxy-refresh.timer
    install -d -o root -g root -m 0755 /etc/systemd/system/codex-tg.service.d
    cat >/etc/systemd/system/codex-tg.service.d/telegram-proxy.conf <<'EOF'
[Unit]
Requires=codex-tg-proxy.service
After=codex-tg-proxy.service
EOF
    chmod 0644 /etc/systemd/system/codex-tg.service.d/telegram-proxy.conf
else
    systemctl disable --now codex-tg-proxy-refresh.timer >/dev/null 2>&1 || true
    systemctl disable --now codex-tg-proxy.service >/dev/null 2>&1 || true
    rm -f -- /etc/systemd/system/codex-tg-proxy.service
    rm -f -- /etc/systemd/system/codex-tg-proxy-refresh.service
    rm -f -- /etc/systemd/system/codex-tg-proxy-refresh.timer
    rm -f -- /etc/systemd/system/codex-tg.service.d/telegram-proxy.conf
    rmdir /etc/systemd/system/codex-tg.service.d 2>/dev/null || true
    rm -rf -- /etc/codex-tg-proxy
    rm -rf -- /opt/codex-tg-proxy
fi
ln -sfn -- "$FINAL_RELEASE" "$INSTALL_ROOT/.current-new"
mv -Tf -- "$INSTALL_ROOT/.current-new" "$INSTALL_ROOT/current"
SWITCHED_CURRENT=1
install -d -m 0755 /root/.local/bin
cat >"$TMP_DIR/codex-cli-wrapper" <<'CODEXWRAPPER'
#!/bin/sh
# Codex TG managed: CLI and Gateway use the same bundled runtime.
export PYTHONPATH=/opt/codex-tg/current
exec /opt/codex-tg/current/.venv/bin/python -m codex_tg.components --exec-codex "$@"
CODEXWRAPPER
CODEX_WRAPPER_WRITTEN=1
install -o root -g root -m 0755 "$TMP_DIR/codex-cli-wrapper" /root/.local/bin/codex
/root/.local/bin/codex --version >/dev/null
systemctl daemon-reload
systemctl enable codex-tg.service >/dev/null
if [ "$TELEGRAM_KIND" = subscription ]; then
    systemctl enable codex-tg-proxy.service >/dev/null
    systemctl enable --now codex-tg-proxy-refresh.timer >/dev/null
    systemctl restart codex-tg-proxy.service
    for _ in $(seq 1 30); do
        if systemctl is-active --quiet codex-tg-proxy.service && \
           ss -ltnH 'sport = :7890' | grep -q '127\.0\.0\.1:7890'; then
            break
        fi
        sleep 1
    done
    systemctl is-active --quiet codex-tg-proxy.service || die "codex-tg-proxy.service 未启动"
    PROXY_LISTENERS=$(ss -ltnH 'sport = :7890')
    printf '%s\n' "$PROXY_LISTENERS" | grep -q '127\.0\.0\.1:7890' || die "本地代理未监听 127.0.0.1:7890"
    if printf '%s\n' "$PROXY_LISTENERS" | grep -Eq '0\.0\.0\.0:7890|\[::\]:7890|\*:7890'; then
        die "本地代理意外暴露到公网监听地址"
    fi
fi
report SYSTEMD "service files installed"

say "[10/12] 执行 Telegram、已配置引擎与 Tavily 自检"
python3 - "$FINAL_RELEASE" "$CODEX_HOME_DIR" <<'SELFTESTPY'
import os, pathlib, shlex, sys
for line in pathlib.Path('/etc/codex-tg.env').read_text().splitlines():
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        parts = shlex.split(value)
        os.environ[key] = parts[0] if parts else ''
os.environ['CODEX_HOME'] = sys.argv[2]
release = pathlib.Path(sys.argv[1])
python = str(release / '.venv/bin/python')
os.execve(python, [python, str(release / 'bin/codex-tg-selftest'), '--model-call'], os.environ)
SELFTESTPY
report TELEGRAM "getMe passed"
report CODEX "installed; configured endpoint tested, otherwise UNCONFIGURED"
report CLAUDE "installed; configured endpoint tested, otherwise UNCONFIGURED"
report TAVILY "enabled when a valid Key is configured; native search fallback available"
report CODEX_PROMPT "configured when supplied; Codex-only Responses instructions adapter"

say "[11/12] 启动 Gateway 并检查健康状态"
systemctl restart codex-tg.service
for _ in $(seq 1 30); do
    if systemctl is-active --quiet codex-tg.service && \
       curl -fsS http://127.0.0.1:17654/health | EXPECTED_VERSION="$GATEWAY_VERSION" python3 -c 'import json,os,sys; d=json.load(sys.stdin); assert d["gateway"]=="ok" and d["db"] is True and d["codex_runtime"] is True and d["version"]==os.environ["EXPECTED_VERSION"]'; then
        break
    fi
    sleep 2
done
systemctl is-active --quiet codex-tg.service || die "codex-tg.service 未启动"
curl -fsS http://127.0.0.1:17654/health | EXPECTED_VERSION="$GATEWAY_VERSION" python3 -c 'import json,os,sys; d=json.load(sys.stdin); assert d["gateway"]=="ok" and d["db"] is True and d["codex_runtime"] is True and d["version"]==os.environ["EXPECTED_VERSION"]'
report SUCCESS "all checks passed"
finish_report true
SUCCESS=1

say "[12/12] 部署完成"
say ""
say "========================"
say "Codex TG 部署成功"
say "========================"
say "Gateway：正常"
say "Telegram：正常"
say "Telegram 网络：$TELEGRAM_NETWORK"
if [ "$TELEGRAM_KIND" = subscription ]; then
    say "代理：Mihomo v1.19.31 / 127.0.0.1:7890"
elif [ "$TELEGRAM_KIND" = custom ]; then
    say "代理：自定义代理"
fi
say "Codex / Claude：已安装（未填写 API 的引擎为 UNCONFIGURED）"
say "Tavily：已集成，失败自动回退原生搜索"
say "当前引擎：Codex（已有部署保留选择）"
say "systemd：正常"
say "当前版本：$GATEWAY_VERSION"
say "========================"
