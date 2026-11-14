from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
import uuid
from contextlib import contextmanager
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


PROJECT = Path(__file__).resolve().parents[1]


def load_updater():
    name = f"codex_tg_updater_{uuid.uuid4().hex}"
    path = PROJECT / "gateway/bin/codex-tg-updater"
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_release(directory: Path, version: str) -> tuple[Path, Path, Path, Path]:
    source = directory / "source"
    (source / "codex_tg").mkdir(parents=True)
    (source / "bin").mkdir()
    (source / "systemd").mkdir()
    (source / "VERSION").write_text(version + "\n", "ascii")
    (source / "requirements.lock").write_text("", "ascii")
    (source / "codex_tg/__init__.py").write_text(f'__version__ = "{version}"\n', "ascii")
    (source / "codex_tg/app.py").write_text("READY = True\n", "ascii")
    (source / "bin/codex-tg-updater").write_text("#!/bin/sh\n# new updater\n", "ascii")
    (source / "systemd/codex-tg.service").write_text("# new service\n", "ascii")
    (source / "systemd/codex-tg-update@.service").write_text("# new update service\n", "ascii")
    wheelhouse = directory / f"wheelhouse-{version}.tar.gz"
    with tarfile.open(wheelhouse, "w:gz"):
        pass
    cli_wheel = directory / "openai_codex_cli_bin-test-py3-none-manylinux_2_17_x86_64.whl"
    cli_wheel.write_bytes(b"test-cli-wheel")
    wheelhouse_entry = {
        "archive": wheelhouse.name,
        "sha256": hashlib.sha256(wheelhouse.read_bytes()).hexdigest(),
        "cli": {
            "file": cli_wheel.name,
            "sha256": hashlib.sha256(cli_wheel.read_bytes()).hexdigest(),
        },
    }
    (source / "WHEELHOUSE.json").write_text(
        json.dumps({"cp312-x86_64": wheelhouse_entry}), "utf-8"
    )
    archive = directory / f"codex-tg-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for item in sorted(source.rglob("*")):
            bundle.add(item, arcname=item.relative_to(source), recursive=False)
    manifest = {
        "version": version,
        "archive": archive.name,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "wheelhouses": {"cp312-x86_64": wheelhouse_entry},
    }
    manifest_path = directory / "latest.json"
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(manifest_bytes)
    private = Ed25519PrivateKey.generate()
    public = directory / "release.pub"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    signature = directory / "latest.json.sig"
    signature.write_bytes(private.sign(manifest_bytes))
    return archive, manifest_path, signature, public


def configure_updater(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str):
    updater = load_updater()
    root = tmp_path / "codex-tg"
    releases = root / "releases"
    data = root / "data"
    old = releases / "1.0.1"
    old.mkdir(parents=True)
    data.mkdir(parents=True)
    (old / "VERSION").write_text("1.0.1\n", "ascii")
    (data / "gateway.db").write_text("persistent-db", "ascii")
    (data / 'codex-pre-instructions.txt').write_bytes(b'  preserved\r\nexact prompt\n')
    (data / 'codex-pre-instructions.txt').chmod(0o600)
    (data / 'codex-pre-instructions.versions').mkdir(mode=0o700)
    (data / 'codex-pre-instructions.versions/retained.txt').write_text('queued snapshot')
    current = root / "current"
    current.symlink_to(old)
    service = tmp_path / "codex-tg.service"
    update_unit = tmp_path / "codex-tg-update@.service"
    updater_bin = tmp_path / "codex-tg-updater"
    service.write_text("# old service\n", "ascii")
    update_unit.write_text("# old update service\n", "ascii")
    updater_bin.write_text("# old updater\n", "ascii")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    archive, manifest, signature, public = make_release(fixtures, version)
    mapping = {
        "latest.json": manifest,
        "latest.json.sig": signature,
        archive.name: archive,
        f"wheelhouse-{version}.tar.gz": fixtures / f"wheelhouse-{version}.tar.gz",
        "openai_codex_cli_bin-test-py3-none-manylinux_2_17_x86_64.whl": (
            fixtures / "openai_codex_cli_bin-test-py3-none-manylinux_2_17_x86_64.whl"
        ),
    }

    def download(url: str, target: Path, _max_bytes: int = 0) -> None:
        target.write_bytes(mapping[url.rsplit("/", 1)[-1]].read_bytes())

    monkeypatch.setattr(updater, "ROOT", root)
    monkeypatch.setattr(updater, "CURRENT", current)
    monkeypatch.setattr(updater, "RELEASES", releases)
    monkeypatch.setattr(updater, "PUBLIC_KEY", public)
    monkeypatch.setattr(updater, "RELEASE_BASE", "https://release.invalid")
    monkeypatch.setattr(updater, "SERVICE_UNIT", service)
    monkeypatch.setattr(updater, "UPDATE_UNIT", update_unit)
    monkeypatch.setattr(updater, "UPDATER_BIN", updater_bin)
    monkeypatch.setattr(updater, "download", download)
    monkeypatch.setattr(updater, "notify", lambda _text: None)
    return updater, root, old, service, update_unit, updater_bin


def intercept_systemctl(monkeypatch: pytest.MonkeyPatch, updater):
    calls: list[list[str]] = []
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        normalized = [str(item) for item in command]
        if normalized and normalized[0] == "systemctl":
            calls.append(normalized)
            return subprocess.CompletedProcess(normalized, 0)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(updater.subprocess, "run", run)
    return calls


def test_health_failure_restores_release_units_updater_and_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater, root, old, service, update_unit, updater_bin = configure_updater(
        monkeypatch, tmp_path, "1.0.2"
    )
    calls = intercept_systemctl(monkeypatch, updater)
    health_versions: list[str] = []

    def health(version: str) -> None:
        health_versions.append(version)
        if version == "1.0.2":
            raise RuntimeError("simulated health failure")

    monkeypatch.setattr(updater, "wait_healthy", health)
    with pytest.raises(RuntimeError, match="rolled back to 1.0.1"):
        updater.install_release(False)
    assert (root / "current").resolve() == old
    assert service.read_text("ascii") == "# old service\n"
    assert update_unit.read_text("ascii") == "# old update service\n"
    assert updater_bin.read_text("ascii") == "# old updater\n"
    assert (root / "data/gateway.db").read_text("ascii") == "persistent-db"
    assert (root / 'data/codex-pre-instructions.txt').read_bytes() == b'  preserved\r\nexact prompt\n'
    assert (root / 'data/codex-pre-instructions.txt').stat().st_mode & 0o777 == 0o600
    assert (root / 'data/codex-pre-instructions.versions/retained.txt').read_text() == 'queued snapshot'
    assert health_versions == ["1.0.2", "1.0.1"]
    assert ["systemctl", "restart", "codex-tg.service"] in calls


def test_force_reinstalls_same_version_without_touching_shared_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater, root, _old, service, update_unit, updater_bin = configure_updater(
        monkeypatch, tmp_path, "1.0.1"
    )
    calls = intercept_systemctl(monkeypatch, updater)
    monkeypatch.setattr(updater, "wait_healthy", lambda version: None)
    updater.install_release(True)
    assert (root / "current/VERSION").read_text("ascii").strip() == "1.0.1"
    assert (root / "data/gateway.db").read_text("ascii") == "persistent-db"
    assert (root / 'data/codex-pre-instructions.txt').read_bytes() == b'  preserved\r\nexact prompt\n'
    assert (root / 'data/codex-pre-instructions.txt').stat().st_mode & 0o777 == 0o600
    assert (root / 'data/codex-pre-instructions.versions/retained.txt').read_text() == 'queued snapshot'
    assert service.read_text("ascii") == "# new service\n"
    assert update_unit.read_text("ascii") == "# new update service\n"
    assert "new updater" in updater_bin.read_text("ascii")
    assert ["systemctl", "daemon-reload"] in calls


def test_health_failure_includes_actual_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    updater = load_updater()
    monkeypatch.setenv("CODEX_TG_HEALTH_PORT", "17654")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"gateway":"ok","db":true,"codex_runtime":true,"version":"1.0.1"}'

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(updater.subprocess, "run", lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})())
    monkeypatch.setattr(updater.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match='"version":"1.0.1"'):
        updater.wait_healthy("1.0.5")


def test_safe_extract_rejects_fifo(tmp_path: Path) -> None:
    archive = tmp_path / "fifo.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("unsafe-fifo")
        info.type = tarfile.FIFOTYPE
        bundle.addfile(info, io.BytesIO())
    updater = load_updater()
    with pytest.raises(RuntimeError, match="unsupported special entry"):
        updater.safe_extract(archive, tmp_path / "out")


def test_codex_update_uses_scoped_proxy_files_without_global_proxy_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater = load_updater()
    root = tmp_path / "codex-tg"
    (root / "data").mkdir(parents=True)
    monkeypatch.setattr(updater, "ROOT", root)
    monkeypatch.setattr(updater.shutil, "which", lambda _name: "/usr/bin/curl")
    monkeypatch.setenv(
        "CODEX_TG_TELEGRAM_PROXY_URL",
        "socks5://proxy-user:proxy-password@127.0.0.1:7890",
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://global-proxy.invalid:8080")
    with updater.scoped_install_network() as (environment, network_root):
        assert "HTTPS_PROXY" not in environment
        assert environment["PATH"].startswith(str(network_root / "bin"))
        assert environment["PIP_CONFIG_FILE"] == str(network_root / "pip.conf")
        assert "proxy-user:proxy-password" in (network_root / "curl.conf").read_text("utf-8")
        assert "proxy-user:proxy-password" in (network_root / "pip.conf").read_text("utf-8")
        wrapper = (network_root / "bin/curl").read_text("utf-8")
        assert "proxy-user" not in wrapper
        assert "--config" in wrapper
        assert (network_root / "curl.conf").stat().st_mode & 0o777 == 0o600


def test_subscription_proxy_uses_mixed_port_http_for_pip_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater = load_updater()
    root = tmp_path / "codex-tg"
    (root / "data").mkdir(parents=True)
    monkeypatch.setattr(updater, "ROOT", root)
    monkeypatch.setattr(updater.shutil, "which", lambda _name: "/usr/bin/curl")
    monkeypatch.setenv("CODEX_TG_TELEGRAM_PROXY_KIND", "subscription")
    monkeypatch.setenv("CODEX_TG_TELEGRAM_PROXY_URL", "socks5://127.0.0.1:7890")
    with updater.scoped_install_network() as (_environment, network_root):
        assert 'proxy = "http://127.0.0.1:7890"' in (
            network_root / "curl.conf"
        ).read_text("utf-8")
        assert "proxy = http://127.0.0.1:7890" in (
            network_root / "pip.conf"
        ).read_text("utf-8")


def test_runtime_venv_activation_repairs_missing_environment(tmp_path: Path) -> None:
    updater = load_updater()
    release = tmp_path / "release"
    candidate = release / ".venv.candidate"
    candidate.mkdir(parents=True)
    (candidate / "marker").write_text("new", "ascii")
    backup = updater.activate_runtime_venv(release, candidate)
    assert backup is None
    assert (release / ".venv/marker").read_text("ascii") == "new"
    updater.restore_runtime_venv(release, backup)
    assert not (release / '.venv').exists()
    assert list(release.glob('.venv.failed-*'))


def test_runtime_venv_activation_rolls_back_existing_environment(tmp_path: Path) -> None:
    updater = load_updater()
    release = tmp_path / "release"
    active = release / ".venv"
    candidate = release / ".venv.candidate"
    active.mkdir(parents=True)
    candidate.mkdir()
    (active / "marker").write_text("old", "ascii")
    (candidate / "marker").write_text("new", "ascii")
    (candidate / 'bin').mkdir()
    (candidate / 'bin/pip').write_text(f'#!{candidate}/bin/python\nprint("entrypoint")\n')
    backup = updater.activate_runtime_venv(release, candidate)
    assert backup is not None
    assert (active / "marker").read_text("ascii") == "new"
    assert (active / 'bin/pip').read_text().startswith(f'#!{active}/bin/python')
    updater.restore_runtime_venv(release, backup)
    assert (active / "marker").read_text("ascii") == "old"


def test_codex_update_does_not_stop_gateway_before_candidate_is_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater = load_updater()
    release = tmp_path / "release"
    (release / "bin").mkdir(parents=True)
    (release / "WHEELHOUSE.json").write_text("{}", "utf-8")
    monkeypatch.setattr(updater, "CURRENT", release)
    monkeypatch.setattr(updater, "notify", lambda _text: None)

    @contextmanager
    def network():
        yield {}, tmp_path

    monkeypatch.setattr(updater, "scoped_install_network", network)
    monkeypatch.setattr(updater, 'latest_codex', lambda *_args: ('0.162.1', '0.162.1'))
    monkeypatch.setattr(
        updater.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "codex-cli 0.154.0\n",
    )
    commands: list[list[str]] = []

    def run(command, *args, **kwargs):
        commands.append([str(item) for item in command])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(updater.subprocess, "run", run)
    monkeypatch.setattr(updater, "prepare_wheelhouse", lambda *_args: tmp_path)
    monkeypatch.setattr(
        updater,
        "build_runtime_venv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("candidate failed")),
    )
    with pytest.raises(RuntimeError, match="candidate failed"):
        updater.update_codex()
    assert ["systemctl", "stop", "codex-tg.service"] not in commands


def test_update_status_is_atomic_and_root_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater = load_updater()
    target = tmp_path / "data/update-status.json"
    monkeypatch.setattr(updater, "UPDATE_STATUS", target)
    updater.write_update_status(
        "update-1",
        "codex",
        "FAILED",
        "safe failure",
        notified=False,
    )
    assert json.loads(target.read_text("utf-8")) == {
        "id": "update-1",
        "mode": "codex",
        "state": "FAILED",
        "detail": "safe failure",
        "notified": False,
        "updated_at": pytest.approx(__import__("time").time(), abs=5),
    }
    assert target.stat().st_mode & 0o777 == 0o600


def test_candidate_selftest_error_is_detailed_and_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater = load_updater()
    token = "123456:" + "A" * 30
    proxy = "socks5://user:password@127.0.0.1:7890"
    result = subprocess.CompletedProcess(
        ["python", "selftest"],
        1,
        stdout="",
        stderr=f"SELFTEST_FAILED: Telegram {token} via {proxy} timed out\n",
    )
    monkeypatch.setattr(updater.subprocess, "run", lambda *_args, **_kwargs: result)
    with pytest.raises(RuntimeError) as raised:
        updater.run_candidate_selftest(
            tmp_path / "python",
            tmp_path / "selftest",
            {
                "TELEGRAM_BOT_TOKEN": token,
                "CODEX_TG_TELEGRAM_PROXY_URL": proxy,
            },
        )
    message = str(raised.value)
    assert "candidate selftest failed" in message
    assert "timed out" in message
    assert token not in message
    assert proxy not in message


@pytest.mark.parametrize('fail', [False, True])
def test_claude_switch_and_health_rollback_preserve_configuration(monkeypatch, tmp_path, fail):
    import codex_tg.native as native
    updater = load_updater()
    root = tmp_path / 'gateway'
    release = root / 'current'
    release.mkdir(parents=True)
    (root / 'data').mkdir()
    (root / 'data/credentials.json').write_text('{"preserved":true}')
    claude = root / 'claude'
    (claude / 'versions').mkdir(parents=True)
    old, candidate = claude / 'versions/2.0.0', claude / 'versions/2.1.283'
    old.write_text('old')
    candidate.write_text('candidate')
    (claude / 'current').symlink_to(old)
    monkeypatch.setattr(updater, 'ROOT', root)
    monkeypatch.setattr(updater, 'CURRENT', release)
    monkeypatch.setattr(updater, 'notify', lambda _text: False)
    monkeypatch.setattr(updater, 'current_version', lambda: '2.2.0')
    @contextmanager
    def network():
        yield {}, tmp_path
    monkeypatch.setattr(updater, 'scoped_install_network', network)
    monkeypatch.setattr(native, 'latest_official', lambda *_args: ({'version': '2.1.283'}, 'https://official.invalid'))
    monkeypatch.setattr(native, 'prepare', lambda *_args: candidate)
    monkeypatch.setattr(updater.subprocess, 'check_output', lambda *_args, **_kwargs: '2.0.0 (Claude Code)')
    commands = []
    def run(command, *_args, **_kwargs):
        commands.append([str(x) for x in command])
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
    monkeypatch.setattr(updater.subprocess, 'run', run)
    calls = []
    def health(version):
        calls.append(version)
        if fail and len(calls) == 1:
            raise RuntimeError('isolated health failure')
    monkeypatch.setattr(updater, 'wait_healthy', health)
    if fail:
        with pytest.raises(RuntimeError, match='已回滚且健康检查通过'):
            updater.update_claude()
        assert (claude / 'current').resolve() == old
        assert len(calls) == 2
    else:
        updater.update_claude()
        assert (claude / 'current').resolve() == candidate
    assert '--model-call' not in str(commands)
    assert (root / 'data/credentials.json').read_text() == '{"preserved":true}'
