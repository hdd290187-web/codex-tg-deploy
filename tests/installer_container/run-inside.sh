#!/bin/bash
set -euo pipefail
trap 'printf "Container assertion failed at line %s (%s)\\n" "$LINENO" "$MODE" >&2' ERR

MODE="${1:-fresh}"
case "$MODE" in
    partial-*) export PARTIAL_CONFIG="${MODE#partial-}" ;;
    prompt-disabled) export PROMPT_DISABLED=1 ;;
    prompt-omitted) export PROMPT_OMITTED=1 ;;
esac
/usr/bin/apt-get update -qq
DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get install -y -qq python3 python3-venv openssl ca-certificates >/dev/null
install -d -m 0755 /mockbin
install -m 0755 /work/fake-curl /mockbin/curl
install -m 0755 /work/fake-systemctl /mockbin/systemctl
install -m 0755 /work/fake-sleep /mockbin/sleep
install -m 0755 /work/fake-ss /mockbin/ss
export PATH="/mockbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

if [ "$MODE" = proxy ]; then
    export FAKE_PROXY_MODE=subscription
    export REQUIRE_CODEX_PROXY=1
    export EXPECTED_INSTALL_PROXY=http://127.0.0.1:7890
elif [ "$MODE" = custom-socks ]; then
    export FAKE_PROXY_MODE=custom
    export REQUIRE_CODEX_PROXY=1
    export EXPECTED_INSTALL_PROXY=socks5://proxy.example:1080
elif [ "$MODE" = invalid-subscription ]; then
    export FAKE_PROXY_MODE=invalid
else
    export REQUIRE_CODEX_DIRECT=1
fi

mkdir -p /root/.codex/sessions
printf 'keep-session\n' >/root/.codex/sessions/existing.json
printf '{"existing":true}\n' >/root/.codex/auth.json
if [ "$MODE" = existing ] || [ "$MODE" = bad-signature ] || [ "$MODE" = codex-failure ] || [ "$MODE" = health-failure ]; then
    printf 'model = "old-model"\n' >/etc/existing-codex.toml
    ln -s /etc/existing-codex.toml /root/.codex/config.toml
    printf 'UNRELATED=value\nCODEX_TG_API_KEY="old-key"\n' >/root/.codex/.env
fi

if [ "$MODE" = health-failure ]; then
    mkdir -p /opt/codex-tg/releases/0.9.0 /opt/codex-tg/data
    printf 'old prompt preserved\r\n' >/opt/codex-tg/data/codex-pre-instructions.txt
    chmod 600 /opt/codex-tg/data/codex-pre-instructions.txt
    python3 - <<'OLDPROMPT'
import sqlite3
with sqlite3.connect('/opt/codex-tg/data/gateway.db') as conn:
    conn.execute('CREATE TABLE codex_prompt_state(id INTEGER PRIMARY KEY,enabled INTEGER,updated_at INTEGER,length INTEGER,sha256 TEXT)')
    conn.execute("INSERT INTO codex_prompt_state VALUES(1,0,1234,22,'old-hash')")
OLDPROMPT
    printf '0.9.0\n' >/opt/codex-tg/releases/0.9.0/VERSION
    ln -s /opt/codex-tg/releases/0.9.0 /opt/codex-tg/current
    printf 'OLD_ENV=preserved\n' >/etc/codex-tg.env
    printf '# old service\n' >/etc/systemd/system/codex-tg.service
    printf '# old update unit\n' >/etc/systemd/system/codex-tg-update@.service
    printf '# old updater\n' >/usr/local/bin/codex-tg-updater
    printf 'old-public-key\n' >/etc/codex-tg-release.pub
    export HEALTH_FAILURE=1
    set +e
    bash /fixtures/install.sh >/tmp/installer-output 2>&1
    status=$?
    set -e
    [ "$status" -ne 0 ]
    grep -q 'old-model' /etc/existing-codex.toml
    grep -q '^OLD_ENV=preserved$' /etc/codex-tg.env
    grep -q '^# old service$' /etc/systemd/system/codex-tg.service
    grep -q '^# old update unit$' /etc/systemd/system/codex-tg-update@.service
    grep -q '^# old updater$' /usr/local/bin/codex-tg-updater
    grep -q '^old-public-key$' /etc/codex-tg-release.pub
    [ "$(readlink -f /opt/codex-tg/current)" = /opt/codex-tg/releases/0.9.0 ]
    test -f /root/.codex/sessions/existing.json
    grep -q '^old prompt preserved' /opt/codex-tg/data/codex-pre-instructions.txt
    test "$(stat -c %a /opt/codex-tg/data/codex-pre-instructions.txt)" = 600
    python3 - <<'CHECKPROMPT'
import sqlite3
with sqlite3.connect('/opt/codex-tg/data/gateway.db') as conn:
    assert conn.execute('SELECT enabled,updated_at,length,sha256 FROM codex_prompt_state WHERE id=1').fetchone() == (0,1234,22,'old-hash')
CHECKPROMPT
    printf '%s: config, units, updater, key and current release rolled back\n' "$MODE"
    exit 0
fi

if [ "$MODE" = invalid-subscription ]; then
    set +e
    bash /fixtures/install.sh >/tmp/installer-output 2>&1
    status=$?
    set -e
    [ "$status" -ne 0 ]
    grep -q 'unsupported subscription format' /tmp/installer-output
    ! grep -q 'container-proxy-secret' /tmp/installer-output
    [ ! -e /etc/codex-tg.env ]
    printf '%s: unsupported subscription rejected without secret disclosure\n' "$MODE"
    exit 0
fi

if [ "$MODE" = codex-failure ]; then
    export CODEX_INSTALL_FAILURE=1
    set +e
    bash /fixtures/install.sh >/tmp/installer-output 2>&1
    status=$?
    set -e
    [ "$status" -ne 0 ]
    grep -q 'Bundled Codex runtime verification failed' /tmp/installer-output
    grep -q 'Bundled Codex runtime verification failed' /opt/codex-tg/logs/codex-cli-install.log
    ! grep -q 'container-api-secret' /tmp/installer-output
    ! grep -q 'container-telegram-secret' /tmp/installer-output
    ! grep -q 'container-proxy-secret' /tmp/installer-output
    ! grep -q 'container-api-secret' /opt/codex-tg/logs/codex-cli-install.log
    ! grep -q 'container-telegram-secret' /opt/codex-tg/logs/codex-cli-install.log
    ! grep -q 'container-proxy-secret' /opt/codex-tg/logs/codex-cli-install.log
    [ "$(stat -c %a /opt/codex-tg/logs/codex-cli-install.log)" = 600 ]
    grep -q 'old-model' /etc/existing-codex.toml
    [ ! -e /etc/codex-tg.env ]
    printf '%s: detailed error was redacted, persisted, and reported\n' "$MODE"
    exit 0
fi

if [ "$MODE" = bad-signature ]; then
    export BAD_SIGNATURE=1
    set +e
    bash /fixtures/install.sh >/tmp/installer-output 2>&1
    status=$?
    set -e
    [ "$status" -ne 0 ]
    grep -q 'old-model' /etc/existing-codex.toml
    [ ! -e /etc/codex-tg.env ]
    printf '%s: expected signature failure preserved old configuration\n' "$MODE"
    exit 0
fi

if ! bash /fixtures/install.sh >/tmp/installer-output 2>&1; then
    ! grep -q 'container-api-secret' /tmp/installer-output
    ! grep -q 'container-telegram-secret' /tmp/installer-output
    cat /tmp/installer-output
    exit 1
fi
grep -q 'Codex TG 部署成功' /tmp/installer-output
! grep -q 'container-api-secret' /tmp/installer-output
! grep -q 'container-telegram-secret' /tmp/installer-output
! grep -q 'container-proxy-secret' /tmp/installer-output
if [ "${PARTIAL_CONFIG:-codex}" = codex ]; then
grep -q 'model = "gpt-5.6-sol"' "$(readlink -f /root/.codex/config.toml)"
grep -q 'approval_policy = "never"' "$(readlink -f /root/.codex/config.toml)"
grep -q 'sandbox_mode = "danger-full-access"' "$(readlink -f /root/.codex/config.toml)"
fi
grep -q '^UNRELATED=value$' /root/.codex/.env 2>/dev/null || \
    [ "$MODE" = fresh ] || [ "$MODE" = fresh-py310 ] || \
    [ "$MODE" = proxy ] || [ "$MODE" = custom-socks ] || [ -n "${PARTIAL_CONFIG:-}" ] || [[ "$MODE" = prompt-* ]]
test -f /root/.codex/auth.json
test -f /root/.codex/sessions/existing.json
test -L /opt/codex-tg/current
test -x /opt/codex-tg/claude/current
/opt/codex-tg/claude/current --version | grep -q 2.1.283
test -f /opt/codex-tg/data/codex-pre-instructions.txt
if [ -n "${PROMPT_OMITTED:-}" ]; then
    test ! -s /opt/codex-tg/data/codex-pre-instructions.txt
else
    grep -q '^Container prompt first line$' /opt/codex-tg/data/codex-pre-instructions.txt
    grep -q '^Container prompt second line$' /opt/codex-tg/data/codex-pre-instructions.txt
fi
test "$(stat -c %a /opt/codex-tg/data/codex-pre-instructions.txt)" = 600
if [ -n "${PROMPT_OMITTED:-}${PROMPT_DISABLED:-}" ]; then
    grep -q '^CODEX_TG_CODEX_PROMPT_ENABLED="0"$' /etc/codex-tg.env
else
    grep -q '^CODEX_TG_CODEX_PROMPT_ENABLED="1"$' /etc/codex-tg.env
fi
grep -q '^CODEX_TG_ADAPTER_PORT=0$' /etc/codex-tg.env
! grep -q 'container-claude-secret' /tmp/installer-output
! grep -q 'container-tavily-secret' /tmp/installer-output
test -f /etc/systemd/system/codex-tg.service
test -f /etc/codex-tg-config.pub
test -x /usr/local/bin/codex-tg-updater
test "$(stat -c %a /etc/codex-tg.env)" = 600
! grep -Eq '^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY)=' /etc/codex-tg.env
if [ "$MODE" = proxy ]; then
    grep -q '^CODEX_TG_TELEGRAM_NETWORK="PROXY"$' /etc/codex-tg.env
    grep -q '^CODEX_TG_TELEGRAM_PROXY_KIND="subscription"$' /etc/codex-tg.env
    grep -q '^CODEX_TG_TELEGRAM_PROXY_URL="socks5://127.0.0.1:7890"$' /etc/codex-tg.env
    test -f /etc/systemd/system/codex-tg-proxy.service
    test -x /opt/codex-tg-proxy/mihomo
    grep -q '^Requires=codex-tg-proxy.service$' /etc/systemd/system/codex-tg.service.d/telegram-proxy.conf
    test "$(stat -c %a /etc/codex-tg-proxy/config.yaml)" = 600
    test "$(stat -c %a /etc/codex-tg-proxy/providers/subscription.yaml)" = 600
elif [ "$MODE" = custom-socks ]; then
    grep -q '^CODEX_TG_TELEGRAM_NETWORK="PROXY"$' /etc/codex-tg.env
    grep -q '^CODEX_TG_TELEGRAM_PROXY_KIND="custom"$' /etc/codex-tg.env
    grep -q '^CODEX_TG_TELEGRAM_PROXY_URL="socks5://proxy.example:1080"$' /etc/codex-tg.env
    [ ! -e /opt/codex-tg-proxy/mihomo ]
else
    grep -q '^CODEX_TG_TELEGRAM_NETWORK="DIRECT"$' /etc/codex-tg.env
    [ ! -e /opt/codex-tg-proxy/mihomo ]
    [ ! -e /etc/systemd/system/codex-tg-proxy.service ]
fi

# Simulate /codexprompt off, then reinstall with an omitted optional prompt.
# The file and the persisted OFF state must both survive.
cp /opt/codex-tg/data/codex-pre-instructions.txt /tmp/prompt-before-repeat
python3 - <<'OFFPROMPT'
import sqlite3
with sqlite3.connect('/opt/codex-tg/data/gateway.db') as conn:
    conn.execute('UPDATE codex_prompt_state SET enabled=0,updated_at=5678 WHERE id=1')
OFFPROMPT
export PROMPT_OMITTED=1
if ! bash /fixtures/install.sh >/tmp/installer-output-second 2>&1; then
    ! grep -q 'container-api-secret' /tmp/installer-output-second
    ! grep -q 'container-telegram-secret' /tmp/installer-output-second
    cat /tmp/installer-output-second
    exit 1
fi
grep -q 'Codex TG 部署成功' /tmp/installer-output-second
cmp /tmp/prompt-before-repeat /opt/codex-tg/data/codex-pre-instructions.txt
test "$(stat -c %a /opt/codex-tg/data/codex-pre-instructions.txt)" = 600
python3 - <<'PRESERVEDPROMPT'
import sqlite3
with sqlite3.connect('/opt/codex-tg/data/gateway.db') as conn:
    assert conn.execute('SELECT enabled,updated_at FROM codex_prompt_state WHERE id=1').fetchone() == (0,5678)
PRESERVEDPROMPT
test "$(find /root/.codex/backups -mindepth 1 -maxdepth 1 -type d | wc -l)" -ge 1
printf '%s: install and repeat install passed\n' "$MODE"
