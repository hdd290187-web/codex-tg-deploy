from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installer_shell_syntax() -> None:
    subprocess.run(["bash", "-n", str(ROOT / "installer/install.sh")], check=True)


def test_installer_forces_official_codex_noninteractive_mode() -> None:
    installer = (ROOT / "installer/install.sh").read_text("utf-8")
    assert 'from codex_cli_bin import bundled_codex_path' in installer
    assert 'codex_tg.components --exec-codex' in installer
    assert 'https://chatgpt.com/codex/install.sh' not in installer
    assert "sanitize_codex_install_log" in installer


def test_installer_bootstraps_proxy_before_codex_and_scopes_network_config() -> None:
    installer = (ROOT / "installer/install.sh").read_text("utf-8")
    proxy_step = installer.index('say "[6/12] 检测并配置 Telegram 网络"')
    codex_step = installer.index('say "[7/12] 安装 Gateway 依赖与官方 Codex CLI"')
    assert proxy_step < codex_step
    assert '--no-index --find-links "$TMP_DIR/wheelhouse"' in installer
    assert '--config "$TMP_DIR/codex-install.curl"' in installer
    assert "env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY" in installer
    assert "export HTTP_PROXY" not in installer
    assert "export HTTPS_PROXY" not in installer
    assert "export ALL_PROXY" not in installer


def test_updater_never_assumes_a_venv_pip_executable_exists() -> None:
    updater = (ROOT / "gateway/bin/codex-tg-updater").read_text("utf-8")
    assert ".venv/bin/pip" not in updater
    assert 'str(python), "-m", "pip"' in updater
    assert "activate_runtime_venv" in updater
    assert "restore_runtime_venv" in updater


def test_gateway_lock_contains_python310_conditional_dependencies() -> None:
    requirements = (ROOT / "gateway/requirements.lock").read_text("utf-8")
    assert "exceptiongroup==1.3.1" in requirements


def test_default_password_is_not_in_frontend() -> None:
    frontend = "\n".join(
        path.read_text("utf-8")
        for path in (ROOT / "deployment-center/codex_deploy").rglob("*")
        if path.is_file() and path.suffix in {".html", ".js", ".css"}
    )
    assert "test-only-login-passphrase" not in frontend


def test_no_private_key_in_source_tree() -> None:
    for path in [ROOT / "gateway", ROOT / "installer", ROOT / "deployment-center"]:
        content = "\n".join(
            item.read_text("utf-8", errors="ignore") for item in path.rglob("*") if item.is_file()
        )
        assert "BEGIN PRIVATE KEY" not in content


def test_deployment_ui_preserves_verified_bot_token_until_deployment() -> None:
    script = (ROOT / "deployment-center/codex_deploy/static/app.js").read_text("utf-8")
    template = (ROOT / "deployment-center/codex_deploy/templates/index.html").read_text("utf-8")
    assert "verifiedToken" in script
    assert script.count('byId("bot-token").value = ""') == 1
    assert 'id="tavily-enabled" type="checkbox" checked' not in template
    assert 'tavily_enabled: byId("tavily-api-key").value.trim() ?' in script
    assert 'id="codex-pre-instructions"' in template
    assert 'id="codex-prompt-file"' in template
    assert 'codex_pre_instructions_enabled' in script
    assert 'codex_pre_instructions: codexPromptText()' in script


def test_installer_preserves_codex_pre_instructions_file_and_sets_adapter() -> None:
    installer = (ROOT / "installer/install.sh").read_text("utf-8")
    assert "CODEX_TG_CODEX_PROMPT_ENABLED" in installer
    assert "CODEX_TG_ADAPTER_PORT=0" in installer
    assert "codex-pre-instructions.txt" in installer
