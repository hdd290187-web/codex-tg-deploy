#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    (output / "public.pem").write_bytes(public_pem)

    release_root = output / "release-root"
    (release_root / "codex_tg").mkdir(parents=True)
    (release_root / "bin").mkdir()
    (release_root / "systemd").mkdir()
    (release_root / "VERSION").write_text("1.0.0\n", "ascii")
    (release_root / "requirements.lock").write_text("", "ascii")
    (release_root / "codex_tg/__init__.py").write_text("__version__ = '1.0.0'\n", "ascii")
    shutil.copy2(ROOT / "gateway/codex_tg/credentials.py", release_root / "codex_tg/credentials.py")
    shutil.copy2(ROOT / "keys/config-template-public.pem", release_root / "config-template-public.pem")
    for module in ('pre_instructions.py', 'store.py', 'components.py'):
        shutil.copy2(ROOT / 'gateway/codex_tg' / module, release_root / 'codex_tg' / module)
    (release_root / 'codex_cli_bin.py').write_text('from pathlib import Path\ndef bundled_codex_path():\n return Path(__file__).parent / "bin/codex-runtime"\n')
    (release_root / 'bin/codex-runtime').write_text('''#!/bin/sh
if [ "${CODEX_INSTALL_FAILURE:-0}" = 1 ]; then
    echo 'Bundled Codex runtime verification failed' >&2
    exit 37
fi
echo 'codex-cli 0.162.1'
''')
    (release_root / "codex_tg/app.py").write_text("VALUE = 'container-test'\n", "ascii")
    (release_root / "bin/codex-tg-selftest").write_text(
        "#!/usr/bin/env python3\nprint('CODEX_TG_SELFTEST_OK')\n", "ascii"
    )
    (release_root / "bin/codex-tg-updater").write_text("#!/bin/sh\nexit 0\n", "ascii")
    (release_root / "bin/codex-tg-proxy-setup").write_text(
        '''#!/bin/sh
set -eu
output=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --output) output="$2"; shift 2 ;;
        *) shift ;;
    esac
done
case "${FAKE_PROXY_MODE:-direct}" in
    subscription)
        install -d -m 0700 /etc/codex-tg-proxy /etc/codex-tg-proxy/providers
        printf '{"mixed-port":7890,"allow-lan":false,"bind-address":"127.0.0.1"}\n' >/etc/codex-tg-proxy/config.yaml
        printf 'proxies:\n- name: fixture\n  type: direct\n' >/etc/codex-tg-proxy/providers/subscription.yaml
        chmod 600 /etc/codex-tg-proxy/config.yaml /etc/codex-tg-proxy/providers/subscription.yaml
        install -d -m 0700 /opt/codex-tg-proxy
        printf '#!/bin/sh\necho Mihomo v1.19.31\n' >/opt/codex-tg-proxy/mihomo
        chmod 755 /opt/codex-tg-proxy/mihomo
        printf '{"network":"PROXY","kind":"subscription","proxy_url":"socks5://127.0.0.1:7890","install_proxy_url":"http://127.0.0.1:7890"}\n' >"$output"
        ;;
    custom)
        printf '{"network":"PROXY","kind":"custom","proxy_url":"socks5://proxy.example:1080","install_proxy_url":"socks5://proxy.example:1080"}\n' >"$output"
        ;;
    invalid) echo 'PROXY_SETUP_FAILED: unsupported subscription format' >&2; exit 1 ;;
    *) printf '{"network":"DIRECT","kind":"none","proxy_url":"","install_proxy_url":""}\n' >"$output" ;;
esac
chmod 600 "$output"
''',
        "ascii",
    )
    shutil.copy2(ROOT / "gateway/systemd/codex-tg.service", release_root / "systemd/codex-tg.service")
    shutil.copy2(
        ROOT / "gateway/systemd/codex-tg-update@.service",
        release_root / "systemd/codex-tg-update@.service",
    )
    shutil.copy2(
        ROOT / "gateway/systemd/codex-tg-proxy.service",
        release_root / "systemd/codex-tg-proxy.service",
    )
    shutil.copy2(
        ROOT / "gateway/systemd/codex-tg-proxy-refresh.service",
        release_root / "systemd/codex-tg-proxy-refresh.service",
    )
    shutil.copy2(
        ROOT / "gateway/systemd/codex-tg-proxy-refresh.timer",
        release_root / "systemd/codex-tg-proxy-refresh.timer",
    )
    for item in (release_root / "bin").iterdir():
        item.chmod(0o755)
    archive = output / "codex-tg-1.0.0.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for item in sorted(release_root.rglob("*")):
            bundle.add(item, arcname=item.relative_to(release_root), recursive=False)
    wheelhouse = output / "codex-tg-wheelhouse-1.0.0-test.tar.gz"
    with tarfile.open(wheelhouse, "w:gz"):
        pass
    cli_wheel = output / "openai_codex_cli_bin-0.147.0-py3-none-manylinux_2_17_x86_64.whl"
    cli_wheel.write_bytes(b"container-cli-wheel")
    wheelhouse_entry = {
        "archive": wheelhouse.name,
        "sha256": hashlib.sha256(wheelhouse.read_bytes()).hexdigest(),
        "size": wheelhouse.stat().st_size,
        "cli": {
            "file": cli_wheel.name,
            "sha256": hashlib.sha256(cli_wheel.read_bytes()).hexdigest(),
            "size": cli_wheel.stat().st_size,
        },
    }
    claude = output / 'claude-2.1.283-test'
    claude.write_text('#!/bin/sh\necho "2.1.283 (Claude Code) --effort --session-id"\n')
    claude.chmod(0o755)
    manifest = {
        'claude': {'version': '2.1.283', 'assets': {'x86_64': {'file': claude.name, 'sha256': hashlib.sha256(claude.read_bytes()).hexdigest(), 'size': claude.stat().st_size}}},
        "version": "1.0.0",
        "archive": archive.name,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "size": archive.stat().st_size,
        "wheelhouses": {
            "cp310-x86_64": wheelhouse_entry,
            "cp311-x86_64": wheelhouse_entry,
            "cp312-x86_64": wheelhouse_entry,
            "cp313-x86_64": wheelhouse_entry,
        },
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    (output / "codex-tg-1.0.0.json").write_bytes(manifest_bytes)
    (output / "codex-tg-1.0.0.json.sig").write_bytes(private.sign(manifest_bytes))
    (output / "bad.sig").write_bytes(b"invalid-signature")
    shutil.copy2(ROOT / "releases/PySocks-1.7.1-py3-none-any.whl", output)
    claim = {
        "deployment_id": "container-test",
        "installation_token": "install-session-token",
        "release_base_url": "https://center.invalid/codex-deploy/releases",
        "release_manifest_url": "https://center.invalid/codex-deploy/releases/codex-tg-1.0.0.json",
        "base_url": "https://relay.invalid/v1",
        "api_key": "container-api-secret",
        "claude_base_url": "https://claude-relay.invalid",
        "claude_api_key": "container-claude-secret",
        "claude_model": "sonnet",
        "claude_effort": "high",
        "tavily_api_key": "container-tavily-secret",
        "tavily_enabled": True,
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "plan_reasoning_effort": "xhigh",
        "codex_pre_instructions_enabled": True,
        "codex_pre_instructions": "Container prompt first line\nContainer prompt second line",
        "telegram_bot_token": "123456:container-telegram-secret",
        "allowed_user_id": 10001,
        "allowed_chat_id": 10001,
        "gateway_version": "1.0.0",
        "telegram_network_mode": "auto",
        "proxy_subscription_url": "https://subscription.invalid/list?token=container-proxy-secret",
        "custom_proxy_url": "",
        "mihomo_base_url": "https://center.invalid/codex-deploy/releases",
    }
    (output / "claim.json").write_text(json.dumps(claim), "utf-8")
    installer = (ROOT / "installer/install.sh").read_text("utf-8")
    installer = installer.replace("__PUBLIC_BASE_URL__", "https://center.invalid/codex-deploy")
    installer = installer.replace("__CLAIM_TOKEN__", "container-claim-token")
    installer = installer.replace(
        "__RELEASE_PUBLIC_KEY_B64__", base64.b64encode(public_pem).decode("ascii")
    )
    (output / "install.sh").write_text(installer, "utf-8")
    (output / "install.sh").chmod(0o755)


if __name__ == "__main__":
    main()
