from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from codex_tg.config_sync import ConfigSync


def signed_transport(private, payload):
    body = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    signature = private.sign(body)
    async def handler(request):
        return httpx.Response(200, content=signature if request.url.path.endswith('.sig') else body)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_three_way_config_sync_preserves_local_additions_and_reports_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'home'))
    private = Ed25519PrivateKey.generate()
    public = tmp_path / 'config.pub'
    public.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    codex_path = tmp_path / 'home/config.toml'
    codex_path.parent.mkdir()
    codex_path.write_text('model = "local-model"\nlocal_added = true\n\n[provider]\nbase_url = "https://local"\n')
    sync = ConfigSync(tmp_path, 'https://center.invalid/releases', public)
    base = 'model = "base-model"\n\n[provider]\nbase_url = "https://base"\n'
    (tmp_path / 'config-template-state').mkdir()
    (tmp_path / 'config-template-state/codex.toml').write_text(base)
    template = 'model = "template-model"\nnew_field = 3\n\n[provider]\nbase_url = "https://template"\n'
    payload = {'format': 1, 'version': '2', 'codex': {'content': template, 'sha256': hashlib.sha256(template.encode()).hexdigest()},
               'claude': {'content': '{}\n', 'sha256': hashlib.sha256(b'{}\n').hexdigest()}}
    real = httpx.AsyncClient
    monkeypatch.setattr('codex_tg.config_sync.httpx.AsyncClient', lambda **kwargs: real(transport=signed_transport(private, payload), **kwargs))
    first = await sync.sync()
    assert any(x['path'] == 'model' for x in first['conflicts'])
    assert codex_path.read_text().startswith('model = "local-model"')
    resolved = await sync.sync('template')
    assert not resolved['conflicts']
    merged = codex_path.read_text()
    assert 'template-model' in merged and 'local_added' in merged and 'https://template' in merged
    state = sync.status('codex')
    assert state['template_version'] == '2' and state['local_modified']


def test_config_export_redacts_secrets_and_toml_roundtrip(tmp_path, monkeypatch):
    sync = ConfigSync(tmp_path, 'https://center.invalid/releases', tmp_path / 'missing.pub')
    path = tmp_path / 'home/config.toml'
    path.parent.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'home'))
    path.write_text('api_key = "secret-value"\nmodel = "m"\n[env]\nANTHROPIC_API_KEY = "another-secret"\n')
    output = sync.export('codex')
    text = output.read_text()
    assert 'secret-value' not in text and 'another-secret' not in text and '[REDACTED]' in text
    assert output.stat().st_mode & 0o777 == 0o600
