from __future__ import annotations

import base64
import gzip
import hashlib
import importlib.util
import json
import os
import subprocess
import uuid
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_proxy_setup():
    name = f"codex_tg_proxy_setup_{uuid.uuid4().hex}"
    path = ROOT / "gateway/bin/codex-tg-proxy-setup"
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_proxy_refresh():
    name = f"codex_tg_proxy_refresh_{uuid.uuid4().hex}"
    path = ROOT / "gateway/bin/codex-tg-proxy-refresh"
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("content", "expected_format", "expected_nodes"),
    [
        (b"proxies:\n- name: one\n  type: ss\n  server: example.com\n", "yaml", 1),
        (b"ss://YWVzLTI1Ni1nY206cGFzc0BleGFtcGxlLmNvbTo0NDM=#one\n", "uri", 1),
        (
            base64.b64encode(
                b"vless://00000000-0000-0000-0000-000000000000@example.com:443#one\n"
            ),
            "base64",
            1,
        ),
    ],
)
def test_installer_proxy_setup_recognizes_native_mihomo_formats(
    content: bytes, expected_format: str, expected_nodes: int
) -> None:
    module = load_proxy_setup()
    assert module.subscription_format(content) == (expected_format, expected_nodes)


def test_installer_proxy_setup_rejects_unknown_subscription() -> None:
    module = load_proxy_setup()
    with pytest.raises(module.ProxySetupError, match="unsupported subscription format"):
        module.subscription_format(b"this is not a proxy subscription")


def test_auto_mode_keeps_direct_when_target_getme_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_proxy_setup()
    monkeypatch.setattr(module, "telegram_direct_test", lambda _token: (True, "ok"))
    assert module.choose_mode("auto", "https://subscription.example/list", "token") == "direct"


def test_auto_mode_falls_back_only_for_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_proxy_setup()
    monkeypatch.setattr(
        module,
        "telegram_direct_test",
        lambda _token: (False, "network:Telegram direct connection failed"),
    )
    assert (
        module.choose_mode("auto", "https://subscription.example/list", "token")
        == "subscription"
    )
    monkeypatch.setattr(
        module,
        "telegram_direct_test",
        lambda _token: (False, "api:Unauthorized"),
    )
    with pytest.raises(module.ProxySetupError, match="Bot Token"):
        module.choose_mode("auto", "https://subscription.example/list", "token")


def test_proxy_refresh_accepts_all_native_subscription_formats() -> None:
    module = load_proxy_refresh()
    uri = b"trojan://password@example.com:443#one\n"
    assert module.valid_subscription(uri)
    assert module.valid_subscription(base64.b64encode(uri))
    assert module.valid_subscription(b"proxies:\n- name: one\n  type: trojan\n")
    assert not module.valid_subscription(b"not a subscription")


def test_generated_mihomo_config_is_local_health_checked_and_auto_failover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_proxy_setup()
    subscription = b"proxies:\n- name: one\n  type: ss\n  server: example.com\n"
    monkeypatch.setattr(module, "download", lambda _url, _limit: subscription)
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append([str(item) for item in command])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    root = tmp_path / "proxy"
    mihomo = tmp_path / "mihomo"
    mihomo.write_text("binary", "ascii")
    format_name, count = module.prepare_subscription(
        "https://subscription.example/list?token=sensitive",
        root,
        mihomo,
    )
    assert (format_name, count) == ("yaml", 1)
    config = json.loads((root / "config.yaml").read_text("utf-8"))
    assert config["mixed-port"] == 7890
    assert config["allow-lan"] is False
    assert config["bind-address"] == "127.0.0.1"
    assert config["external-controller"] == "127.0.0.1:9090"
    assert config["proxy-providers"]["subscription"]["interval"] == 3600
    assert config["proxy-providers"]["subscription"]["proxy"] == "DIRECT"
    assert config["proxy-providers"]["subscription"]["health-check"]["enable"] is True
    assert config["proxy-groups"][0]["type"] == "fallback"
    assert config["proxy-groups"][0]["lazy"] is False
    assert calls[0][1:3] == ["-t", "-d"]
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "config.yaml").stat().st_mode & 0o777 == 0o600
    assert (root / "providers/subscription.yaml").stat().st_mode & 0o777 == 0o600


def test_mihomo_bootstrap_uses_center_mirror_and_verifies_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_proxy_setup()
    compressed = gzip.compress(b"verified-mihomo-binary")
    digest = hashlib.sha256(compressed).hexdigest()
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module, "MIHOMO_ASSETS", {"x86_64": ("mihomo-test.gz", digest)})
    urls: list[str] = []

    def download(url: str, _limit: int) -> bytes:
        urls.append(url)
        return compressed

    monkeypatch.setattr(module, "download", download)
    target = tmp_path / "mihomo"
    module.install_mihomo(target, "https://center.example/releases")
    assert urls == ["https://center.example/releases/mihomo-test.gz"]
    assert target.read_bytes() == b"verified-mihomo-binary"
    assert target.stat().st_mode & 0o777 == 0o755


def test_proxy_systemd_and_installer_never_set_global_proxy_environment() -> None:
    installer = (ROOT / "installer/install.sh").read_text("utf-8")
    gateway_unit = (ROOT / "gateway/systemd/codex-tg.service").read_text("utf-8")
    proxy_unit = (ROOT / "gateway/systemd/codex-tg-proxy.service").read_text("utf-8")
    combined = installer + gateway_unit + proxy_unit
    for name in ("HTTP_PROXY=", "HTTPS_PROXY=", "ALL_PROXY="):
        assert name not in combined
    assert "127.0.0.1:7890" in installer
    assert "Requires=codex-tg-proxy.service" in installer
    assert "RestartSec=10" in proxy_unit
    assert "StartLimitBurst=5" in proxy_unit
    assert "/opt/codex-tg-proxy/mihomo" in proxy_unit
    assert "/usr/local/bin/mihomo" not in combined
    helper = (ROOT / "gateway/bin/codex-tg-proxy-setup").read_text("utf-8")
    assert '"proxy_url": "socks5://127.0.0.1:7890"' in helper
    assert '"install_proxy_url": "http://127.0.0.1:7890"' in helper


def test_proxy_secrets_are_not_shell_command_arguments() -> None:
    installer = (ROOT / "installer/install.sh").read_text("utf-8")
    assert "proxy_subscription_url" not in "\n".join(
        line for line in installer.splitlines() if line.lstrip().startswith("curl ")
    )
    assert "custom_proxy_url" not in "\n".join(
        line for line in installer.splitlines() if line.lstrip().startswith("curl ")
    )
    helper = load_proxy_setup()
    assert helper.MIHOMO_VERSION == "1.19.31"
    assert os.path.basename(str(ROOT)) == "codex-deploy"
