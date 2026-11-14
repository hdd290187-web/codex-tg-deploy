from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from codex_deploy.app import create_app
from codex_deploy.config import Settings
from codex_deploy.proxy import SubscriptionInfo, classify_subscription
from codex_deploy.security import redact, token_hash
from codex_deploy.telegram import TelegramAPI


PASSWORD = "test-only-login-passphrase"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    key = Ed25519PrivateKey.generate()
    public_path = tmp_path / "release-public.pem"
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    config_private = Ed25519PrivateKey.generate()
    config_private_path = tmp_path / 'config-template-private.pem'
    config_public_path = tmp_path / 'config-template-public.pem'
    config_private_path.write_bytes(config_private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    config_public_path.write_bytes(config_private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "latest.json").write_text(
        json.dumps({"version": "1.0.0", "archive": "codex-tg-1.0.0.tar.gz", "sha256": "a" * 64}),
        "utf-8",
    )
    installer = tmp_path / "install.sh"
    installer.write_text(
        "BASE='__PUBLIC_BASE_URL__'\nTOKEN='__CLAIM_TOKEN__'\nKEY='__RELEASE_PUBLIC_KEY_B64__'\n",
        "utf-8",
    )
    return Settings(
        password_hash=PasswordHasher().hash(PASSWORD),
        master_key=b"m" * 32,
        token_pepper=b"p" * 32,
        public_base_url="http://testserver/codex-deploy",
        database_path=tmp_path / "deploy.db",
        release_dir=releases,
        installer_template=installer,
        signing_public_key_path=public_path,
        session_seconds=3600,
        deployment_seconds=600,
        install_session_seconds=1800,
        secure_cookie=False,
        config_signing_private_key_path=config_private_path,
        config_public_key_path=config_public_path,
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings), base_url="http://testserver") as test_client:
        yield test_client


def login(client: TestClient) -> str:
    response = client.post(
        "/codex-deploy/api/login",
        json={"password": PASSWORD},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    page = client.get("/codex-deploy/")
    assert page.status_code == 200
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
    assert match
    return match.group(1)


def test_config_template_versions_signed_and_rollback(client: TestClient, settings: Settings) -> None:
    csrf = login(client)
    headers = {'X-CSRF-Token': csrf}
    codex = 'model = "fixture"\n\n[model_providers.fixture]\nbase_url = "https://example.invalid/v1"\n'
    saved = client.post('/codex-deploy/api/config/templates', json={'engine': 'codex', 'content': codex}, headers=headers)
    assert saved.status_code == 200 and saved.json()['version'] == 1
    claude = '{"effortLevel":"high","modelSettings":{},"mcpServers":{}}\n'
    saved = client.post('/codex-deploy/api/config/templates', json={'engine': 'claude', 'content': claude}, headers=headers)
    assert saved.status_code == 200 and saved.json()['version'] == 1
    manifest = client.get('/codex-deploy/config-templates/latest.json')
    signature = client.get('/codex-deploy/config-templates/latest.json.sig')
    assert manifest.status_code == signature.status_code == 200
    public = serialization.load_pem_public_key(settings.config_public_key_path.read_bytes())
    public.verify(signature.content, manifest.content)
    assert manifest.json()['codex']['content'] == codex
    assert client.get('/codex-deploy/api/config/templates').json()['codex'][0]['version'] == 1
    rollback = client.post('/codex-deploy/api/config/templates/codex/1/rollback', json={}, headers=headers)
    assert rollback.status_code == 200 and rollback.json()['version'] == 2
    invalid = client.post('/codex-deploy/api/config/templates', json={'engine': 'claude', 'content': '{bad'}, headers=headers)
    assert invalid.status_code == 422


def test_authenticated_agent_config_page_renders(client: TestClient) -> None:
    login(client)
    response = client.get("/codex-deploy/config")
    assert response.status_code == 200
    assert "code-editor" in response.text
    assert "配置模板" in response.text


def test_login_failure_session_and_csrf(client: TestClient) -> None:
    wrong = client.post(
        "/codex-deploy/api/login",
        json={"password": "wrong"},
        headers={"Origin": "http://testserver"},
    )
    assert wrong.status_code == 401
    csrf = login(client)
    valid_token = "123456:" + "A" * 30
    assert client.post(
        "/codex-deploy/api/telegram/verify", json={"bot_token": valid_token}
    ).status_code == 403
    logout = client.post("/codex-deploy/api/logout", headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 200
    assert "max-age=0" in logout.headers["set-cookie"].lower()


def test_origin_and_brute_force_limit(client: TestClient) -> None:
    rejected = client.post(
        "/codex-deploy/api/login",
        json={"password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert rejected.status_code == 403
    for _ in range(8):
        client.post(
            "/codex-deploy/api/login",
            json={"password": "wrong"},
            headers={"Origin": "http://testserver"},
        )
    limited = client.post(
        "/codex-deploy/api/login",
        json={"password": PASSWORD},
        headers={"Origin": "http://testserver"},
    )
    assert limited.status_code == 429


def test_telegram_binding_claim_once_and_secret_destruction(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_prepare(_self):
        return {"id": 1234567, "username": "codex_test_bot", "first_name": "Codex Test"}, 9, True

    async def fake_poll(_self, offset):
        return offset + 1, 777, 777

    monkeypatch.setattr(TelegramAPI, "prepare_binding", fake_prepare)
    monkeypatch.setattr(TelegramAPI, "poll_start", fake_poll)
    csrf = login(client)
    headers = {"X-CSRF-Token": csrf}
    bot_token = "1234567:" + "A" * 30
    verified = client.post(
        "/codex-deploy/api/telegram/verify", json={"bot_token": bot_token}, headers=headers
    )
    assert verified.status_code == 200
    assert verified.json()["webhook_removed"] is True
    bound = client.get("/codex-deploy/api/telegram/bind")
    assert bound.json()["bound"] is True
    api_key = "sk-test-not-logged"
    subscription_url = "https://subscription.example/api?token=subscription-secret"
    created = client.post(
        "/codex-deploy/api/deployments",
        json={
            "base_url": "https://relay.example/v1/",
            "api_key": api_key,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "plan_reasoning_effort": None,
            "telegram_network_mode": "subscription",
            "proxy_subscription_url": subscription_url,
        },
        headers=headers,
    )
    assert created.status_code == 200
    result = created.json()
    assert api_key not in result["command"]
    assert bot_token not in result["command"]
    assert subscription_url not in result["command"]
    raw_token = result["command"].split("/i/", 1)[1].split()[0]
    installer = client.get(f"/codex-deploy/i/{raw_token}")
    assert installer.status_code == 200
    assert "__CLAIM_TOKEN__" not in installer.text
    claimed = client.post(f"/codex-deploy/api/install/claim/{raw_token}", json={})
    assert claimed.status_code == 200
    claim = claimed.json()
    assert claim["api_key"] == api_key
    assert claim["telegram_bot_token"] == bot_token
    assert claim["plan_reasoning_effort"] == "xhigh"
    assert claim["telegram_network_mode"] == "subscription"
    assert claim["proxy_subscription_url"] == subscription_url
    assert claim["custom_proxy_url"] == ""
    assert 'codex_config_template' in claim and 'model_provider = "codex_tg"' in claim['codex_config_template']
    assert 'claude_config_template' in claim and 'modelSettings' in claim['claude_config_template']
    assert claim["mihomo_base_url"] == "http://testserver/codex-deploy/releases"
    assert claim["release_manifest_url"].endswith("/codex-tg-1.0.0.json")
    assert client.post(f"/codex-deploy/api/install/claim/{raw_token}", json={}).status_code == 410
    row = client.app.state.db.deployment(result["id"])
    assert row["secret_encrypted"] is None
    assert subscription_url not in json.dumps(dict(row))
    install_headers = {"Authorization": f"Bearer {claim['installation_token']}"}
    progress = client.post(
        f"/codex-deploy/api/install/{result['id']}/progress",
        json={"stage": "SYSTEM", "detail": "Ubuntu"},
        headers=install_headers,
    )
    assert progress.status_code == 200
    finished = client.post(
        f"/codex-deploy/api/install/{result['id']}/finish",
        json={"success": True},
        headers=install_headers,
    )
    assert finished.status_code == 200
    status = client.get(f"/codex-deploy/api/deployments/{result['id']}")
    assert status.json()["status"] == "SUCCESS"
    assert client.post(
        f"/codex-deploy/api/install/{result['id']}/progress",
        json={"stage": "LATE"},
        headers=install_headers,
    ).status_code == 401


def test_expired_token_is_destroyed(client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_prepare(_self):
        return {"id": 1, "username": "test_bot", "first_name": "Test"}, 1, False

    async def fake_poll(_self, offset):
        return offset + 1, 2, 2

    monkeypatch.setattr(TelegramAPI, "prepare_binding", fake_prepare)
    monkeypatch.setattr(TelegramAPI, "poll_start", fake_poll)
    csrf = login(client)
    headers = {"X-CSRF-Token": csrf}
    client.post(
        "/codex-deploy/api/telegram/verify",
        json={"bot_token": "123456:" + "A" * 30},
        headers=headers,
    )
    client.get("/codex-deploy/api/telegram/bind")
    created = client.post(
        "/codex-deploy/api/deployments",
        json={
            "base_url": "https://relay.example/v1",
            "api_key": "secret-key",
            "model": "model-1",
            "reasoning_effort": "high",
        },
        headers=headers,
    ).json()
    token = created["command"].split("/i/", 1)[1].split()[0]
    with client.app.state.db.connect() as conn:
        conn.execute("UPDATE deployments SET expires_at=? WHERE id=?", (int(time.time()) - 1, created["id"]))
    client.app.state.db.cleanup()
    assert client.get(f"/codex-deploy/i/{token}").status_code == 410
    row = client.app.state.db.deployment(created["id"])
    assert row["status"] == "EXPIRED"
    assert row["secret_encrypted"] is None


def test_invalid_effort_and_base_url_are_rejected(client: TestClient) -> None:
    csrf = login(client)
    response = client.post(
        "/codex-deploy/api/deployments",
        json={
            "base_url": "https://user:pass@example.com/v1?leak=yes",
            "api_key": "secret-key",
            "model": "good-model",
            "reasoning_effort": "max",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422


def test_subscription_formats_are_classified_without_exposing_nodes() -> None:
    yaml_result = classify_subscription(
        b'proxies:\n  - name: "secret-node"\n    type: ss\n    server: 203.0.113.1\n'
    )
    assert yaml_result == SubscriptionInfo("Mihomo / Clash YAML", 1)
    uri = "ss://YWVzLTI1Ni1nY206cGFzc0BleGFtcGxlLmNvbTo0NDM=#node"
    assert classify_subscription(uri.encode()).format == "URI list"
    encoded = __import__("base64").b64encode((uri + "\n" + uri).encode())
    assert classify_subscription(encoded) == SubscriptionInfo("Base64 URI list", 2)


def test_subscription_test_returns_only_format_and_count(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_inspect(_client, url):
        assert "sensitive-token" in url
        return SubscriptionInfo("Mihomo / Clash YAML", 7)

    monkeypatch.setattr("codex_deploy.app.inspect_subscription", fake_inspect)
    csrf = login(client)
    response = client.post(
        "/codex-deploy/api/proxy/subscription/test",
        json={"subscription_url": "https://sub.example/list?token=sensitive-token"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "format": "Mihomo / Clash YAML",
        "node_count": 7,
    }
    assert "sensitive-token" not in response.text


def test_subscription_test_blocks_private_network_ssrf(client: TestClient) -> None:
    csrf = login(client)
    response = client.post(
        "/codex-deploy/api/proxy/subscription/test",
        json={"subscription_url": "http://127.0.0.1:8787/private-subscription"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert "内网或本机" in response.json()["detail"]


@pytest.mark.parametrize(
    ("mode", "subscription", "custom"),
    [
        ("subscription", None, None),
        ("custom", None, None),
        ("invalid", None, None),
        ("custom", None, "ftp://proxy.example:21"),
    ],
)
def test_invalid_proxy_mode_configuration_is_rejected(
    client: TestClient, mode: str, subscription: str | None, custom: str | None
) -> None:
    csrf = login(client)
    response = client.post(
        "/codex-deploy/api/deployments",
        json={
            "base_url": "https://relay.example/v1",
            "api_key": "secret-key",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "telegram_network_mode": mode,
            "proxy_subscription_url": subscription,
            "custom_proxy_url": custom,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422


def test_token_hash_is_keyed() -> None:
    assert token_hash("same", b"a" * 32) != token_hash("same", b"b" * 32)


def test_claimed_install_session_expiry_destroys_secret(client: TestClient) -> None:
    now = int(time.time())
    with client.app.state.db.connect() as conn:
        conn.execute(
            """INSERT INTO deployments(
                id,token_hash,secret_encrypted,status,gateway_version,created_at,expires_at,
                claimed_at,install_session_hash,install_session_expires_at,progress_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "abandoned",
                "token-hash",
                "encrypted-secret-placeholder",
                "CLAIMED",
                "1.0.1",
                now - 3600,
                now - 3000,
                now - 3500,
                "session-hash",
                now - 1,
                "[]",
            ),
        )
    client.app.state.db.cleanup(now)
    row = client.app.state.db.deployment("abandoned")
    assert row["status"] == "FAILED"
    assert row["secret_encrypted"] is None
    assert row["install_session_hash"] is None
    assert row["error_summary"] == "installation session expired"


def test_cleanup_does_not_fail_fresh_claimed_install_session(client: TestClient) -> None:
    now = int(time.time())
    with client.app.state.db.connect() as conn:
        conn.execute(
            """INSERT INTO deployments(
                id,token_hash,secret_encrypted,status,gateway_version,created_at,expires_at,
                claimed_at,install_session_hash,install_session_expires_at,progress_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "fresh-claimed",
                "fresh-token-hash",
                None,
                "CLAIMED",
                "1.0.3",
                now - 10,
                now + 600,
                now - 5,
                "fresh-session-hash",
                now + 1800,
                "[]",
            ),
        )
    client.app.state.db.cleanup(now)
    row = client.app.state.db.deployment("fresh-claimed")
    assert row["status"] == "CLAIMED"
    assert row["install_session_hash"] == "fresh-session-hash"
    assert row["install_session_expires_at"] == now + 1800


def test_generic_error_redaction_covers_tokens() -> None:
    text = (
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz "
        "bot=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234 "
        "key=sk-abcdefghijklmnopqrstuvwxyz"
    )
    result = redact(text)
    assert "abcdefghijklmnopqrstuvwxyz" not in result
    assert "123456:" not in result
    assert "sk-" not in result


@pytest.mark.parametrize('mask', range(8))
def test_partial_configuration_claim_roundtrip(client, monkeypatch, mask):
    async def prepare(_self):
        return {'id': 1234567, 'username': 'fixture_bot', 'first_name': 'fixture'}, 0, False
    async def poll(_self, offset):
        return 1, 777, 777
    monkeypatch.setattr(TelegramAPI, 'prepare_binding', prepare)
    monkeypatch.setattr(TelegramAPI, 'poll_start', poll)
    csrf = login(client)
    headers = {'X-CSRF-Token': csrf}
    client.post('/codex-deploy/api/telegram/verify', json={'bot_token': '1234567:' + 'A'*30}, headers=headers)
    assert client.get('/codex-deploy/api/telegram/bind').json()['bound']
    body = {}
    if mask & 1:
        body.update(base_url='https://codex.invalid/v1', api_key='codex-private-key')
    if mask & 2:
        body.update(claude_base_url='https://claude.invalid', claude_api_key='claude-private-key')
    if mask & 4:
        body.update(tavily_api_key='tavily-private-key')
    result = client.post('/codex-deploy/api/deployments', json=body, headers=headers)
    assert result.status_code == 200
    data = result.json()
    row = dict(client.app.state.db.deployment(data['id']))
    for key in ('codex-private-key', 'claude-private-key', 'tavily-private-key'):
        assert key not in json.dumps(row)
        assert key not in result.text
    token = data['command'].split('/i/')[1].split()[0]
    claim = client.post('/codex-deploy/api/install/claim/' + token, json={}).json()
    assert bool(claim['api_key']) == bool(mask & 1)
    assert bool(claim['claude_api_key']) == bool(mask & 2)
    assert bool(claim['tavily_api_key']) == bool(mask & 4)
    assert claim['tavily_enabled'] is bool(mask & 4)
    assert client.app.state.db.deployment(data['id'])['secret_encrypted'] is None


def test_validation_does_not_echo_secret_inputs(client):
    csrf = login(client)
    response = client.post('/codex-deploy/api/deployments', json={'claude_api_key':'sensitive-secret\n'}, headers={'X-CSRF-Token':csrf})
    # Leading/trailing whitespace is normalized; internal control characters reject.
    response = client.post('/codex-deploy/api/deployments', json={'claude_api_key':'sensitive-secret\ninside'}, headers={'X-CSRF-Token':csrf})
    assert response.status_code == 422
    assert 'sensitive-secret' not in response.text


@pytest.mark.parametrize('enabled', [True, False])
def test_codex_pre_instructions_optional_claim_roundtrip(client, monkeypatch, enabled):
    async def prepare(_self):
        return {'id': 1234567, 'username': 'fixture_bot', 'first_name': 'fixture'}, 0, False

    async def poll(_self, offset):
        return offset + 1, 777, 777

    monkeypatch.setattr(TelegramAPI, 'prepare_binding', prepare)
    monkeypatch.setattr(TelegramAPI, 'poll_start', poll)
    csrf = login(client)
    headers = {'X-CSRF-Token': csrf}
    client.post('/codex-deploy/api/telegram/verify', json={'bot_token': '1234567:' + 'A' * 30}, headers=headers)
    assert client.get('/codex-deploy/api/telegram/bind').json()['bound']
    prompt = '第一行\n第二行; 原样保留，不包装。'
    created = client.post('/codex-deploy/api/deployments', json={
        'codex_pre_instructions_enabled': enabled,
        'codex_pre_instructions': prompt,
    }, headers=headers)
    assert created.status_code == 200
    token = created.json()['command'].split('/i/', 1)[1].split()[0]
    claim = client.post('/codex-deploy/api/install/claim/' + token, json={}).json()
    assert claim['codex_pre_instructions_enabled'] is enabled
    assert claim['codex_pre_instructions'] == prompt
    assert prompt not in created.text


def test_codex_pre_instructions_rejects_nul_and_oversized_content(client):
    csrf = login(client)
    headers = {'X-CSRF-Token': csrf}
    response = client.post('/codex-deploy/api/deployments', json={'codex_pre_instructions': 'before\x00after'}, headers=headers)
    assert response.status_code == 422
    assert 'before' not in response.text
    response = client.post('/codex-deploy/api/deployments', json={'codex_pre_instructions': 'x' * 131073}, headers=headers)
    assert response.status_code == 422
