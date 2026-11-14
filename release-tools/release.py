#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


PROJECT = Path(__file__).resolve().parents[1]
VERSION_FILE = PROJECT / "VERSION"
RELEASE_DIR = PROJECT / "releases"
PRIVATE_KEY_FILE = PROJECT / "keys/release-private.pem"
PUBLIC_KEY_FILE = PROJECT / "keys/release-public.pem"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
WHEEL_PLATFORMS = {
    "x86_64": ("linux-amd64", "manylinux_2_17_x86_64"),
    "aarch64": ("linux-arm64", "manylinux_2_17_aarch64"),
}
PYTHON_MINORS = (10, 11, 12, 13)


def bump(version: str, part: str) -> str:
    match = SEMVER.fullmatch(version)
    if not match:
        raise ValueError(f"invalid current version: {version}")
    major, minor, patch = map(int, match.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def signing_key() -> Ed25519PrivateKey:
    if not PRIVATE_KEY_FILE.exists() or not PUBLIC_KEY_FILE.exists():
        raise RuntimeError("release signing keys are missing; run bootstrap first")
    key = serialization.load_pem_private_key(PRIVATE_KEY_FILE.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("release private key is not Ed25519")
    return key


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(mode)
    temporary.replace(path)


def deterministic_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    if info.isfile() and info.name.startswith("bin/"):
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o644
    elif info.isdir():
        info.mode = 0o755
    return info


def build_archive(
    version: str,
    target: Path,
    wheelhouses: dict[str, dict[str, object]] | None = None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="codex-tg-release-") as name:
        staging = Path(name)
        shutil.copytree(
            PROJECT / "gateway/codex_tg",
            staging / "codex_tg",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        init_file = staging / "codex_tg/__init__.py"
        init_text = re.sub(
            r'^__version__\s*=\s*"[^"]+"$',
            f'__version__ = "{version}"',
            init_file.read_text("utf-8"),
            flags=re.MULTILINE,
        )
        init_file.write_text(init_text, "utf-8")
        shutil.copytree(
            PROJECT / "gateway/bin",
            staging / "bin",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copytree(PROJECT / "gateway/systemd", staging / "systemd")
        if (PROJECT / 'keys/config-template-public.pem').exists():
            shutil.copy2(PROJECT / 'keys/config-template-public.pem', staging / 'config-template-public.pem')
        shutil.copy2(PROJECT / "gateway/requirements.lock", staging / "requirements.lock")
        if wheelhouses:
            (staging / "WHEELHOUSE.json").write_text(
                json.dumps(wheelhouses, sort_keys=True, indent=2) + "\n",
                "utf-8",
            )
        if (PROJECT / 'release-tools/claude-assets.json').exists():
            shutil.copy2(PROJECT / 'release-tools/claude-assets.json', staging / 'CLAUDE.json')
        (staging / "VERSION").write_text(version + "\n", encoding="ascii")
        for executable in (staging / "bin").iterdir():
            executable.chmod(0o755)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as bundle:
            for path in sorted(staging.rglob("*")):
                bundle.add(
                    path,
                    arcname=path.relative_to(staging),
                    recursive=False,
                    filter=deterministic_filter,
                )
        atomic_write(target, buffer.getvalue())


def build_wheelhouses(version: str) -> dict[str, dict[str, object]]:
    python = PROJECT / ".venv/bin/python"
    result: dict[str, dict[str, object]] = {}
    cli_assets: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory(prefix="codex-tg-wheelhouse-") as name:
        root = Path(name)
        for architecture, (label, platform_tag) in WHEEL_PLATFORMS.items():
            for minor in PYTHON_MINORS:
                abi = f"cp3{minor}"
                target = root / f"{abi}-{architecture}"
                target.mkdir()
                subprocess.run(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "download",
                        "--quiet",
                        "--dest",
                        str(target),
                        "--only-binary=:all:",
                        "--platform",
                        platform_tag,
                        "--implementation",
                        "cp",
                        "--python-version",
                        f"3.{minor}",
                        "--abi",
                        abi,
                        "-r",
                        str(PROJECT / "gateway/requirements.lock"),
                    ],
                    check=True,
                )
                cli_wheels = list(target.glob("openai_codex_cli_bin-*.whl"))
                if len(cli_wheels) != 1:
                    raise RuntimeError(f"expected one Codex CLI wheel for {abi}-{architecture}")
                cli_wheel = cli_wheels[0]
                cli_bytes = cli_wheel.read_bytes()
                existing = cli_assets.get(architecture)
                if existing and existing["sha256"] != hashlib.sha256(cli_bytes).hexdigest():
                    raise RuntimeError(f"Codex CLI wheel changed across Python versions: {architecture}")
                cli_target = RELEASE_DIR / cli_wheel.name
                if cli_target.exists():
                    if cli_target.read_bytes() != cli_bytes:
                        raise RuntimeError(f"published CLI wheel content mismatch: {cli_target.name}")
                else:
                    atomic_write(cli_target, cli_bytes)
                cli_assets[architecture] = {
                    "file": cli_wheel.name,
                    "sha256": hashlib.sha256(cli_bytes).hexdigest(),
                    "size": len(cli_bytes),
                }
                cli_wheel.unlink()
                wheelhouse_name = f"codex-tg-wheelhouse-{version}-{abi}-{label}.tar.gz"
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as bundle:
                    for wheel in sorted(target.glob("*.whl")):
                        bundle.add(
                            wheel,
                            arcname=wheel.name,
                            recursive=False,
                            filter=deterministic_filter,
                        )
                wheelhouse_bytes = buffer.getvalue()
                atomic_write(RELEASE_DIR / wheelhouse_name, wheelhouse_bytes)
                result[f"{abi}-{architecture}"] = {
                    "archive": wheelhouse_name,
                    "sha256": hashlib.sha256(wheelhouse_bytes).hexdigest(),
                    "size": len(wheelhouse_bytes),
                    "cli": cli_assets[architecture],
                }
    return result


def validate_claude_assets(metadata: dict[str, object]) -> None:
    """Ensure every signed Claude asset is present before publishing a manifest.

    Claude binaries are served beside the Gateway archive (they are intentionally
    not embedded in the tarball).  A missing or mismatched binary would otherwise
    produce a validly signed release that cannot be installed on a target VPS.
    """
    version = metadata.get("version")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise RuntimeError("invalid Claude asset version")
    assets = metadata.get("assets")
    if not isinstance(assets, dict):
        raise RuntimeError("Claude assets are missing")
    for architecture in WHEEL_PLATFORMS:
        item = assets.get(architecture)
        if not isinstance(item, dict):
            raise RuntimeError(f"Claude asset is missing for {architecture}")
        name = item.get("file")
        digest = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not re.fullmatch(r"[A-Za-z0-9._-]+", name)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not isinstance(size, int)
            or size <= 0
        ):
            raise RuntimeError(f"invalid Claude asset metadata for {architecture}")
        asset = RELEASE_DIR / name
        if not asset.is_file():
            raise RuntimeError(f"Claude asset is missing: {asset.name}")
        actual_size = asset.stat().st_size
        actual_digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        if actual_size != size or actual_digest != digest:
            raise RuntimeError(f"Claude asset checksum mismatch: {asset.name}")


def run_quality_checks() -> None:
    python = PROJECT / ".venv/bin/python"
    ruff = PROJECT / ".venv/bin/ruff"
    subprocess.run([str(python), "-m", "compileall", "-q", str(PROJECT / "gateway")], check=True)
    if ruff.exists():
        subprocess.run([str(ruff), "check", str(PROJECT / "gateway"), str(PROJECT / "deployment-center")], check=True)
    subprocess.run([str(python), "-m", "pytest", "-q", str(PROJECT / "tests")], check=True)


def publish(version: str) -> None:
    if not SEMVER.fullmatch(version):
        raise ValueError("version must use semantic versioning")
    key = signing_key()
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    archive_name = f"codex-tg-{version}.tar.gz"
    archive_path = RELEASE_DIR / archive_name
    if archive_path.exists():
        raise RuntimeError(f"release {version} already exists")
    claude_metadata = json.loads((PROJECT / "release-tools/claude-assets.json").read_text())
    validate_claude_assets(claude_metadata)
    wheelhouses = build_wheelhouses(version)
    build_archive(version, archive_path, wheelhouses)
    archive_bytes = archive_path.read_bytes()
    manifest = {
        "version": version,
        "archive": archive_name,
        "sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "size": len(archive_bytes),
        "sdk": next(line for line in (PROJECT / 'gateway/requirements.lock').read_text().splitlines()
                    if line.startswith('openai-codex==')),
        "claude": claude_metadata,
        "mihomo": "1.19.31",
        "mihomo_assets": {
            "linux-amd64": "04cf9f09671704f839ddbee2e93069dc831a4123a75281e725d1d96ab9ac1afc",
            "linux-arm64": "9e0f11afbf38426b8bd88fdc594678f8161c57eccb4e1b77acb12b493904f1d4",
        },
        "pysocks_bootstrap": "2725bd0a9925919b9b51739eea5f9e2bae91e83288108a9ad338b2e3a4435ee5",
        "wheelhouses": wheelhouses,
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    version_manifest = RELEASE_DIR / f"codex-tg-{version}.json"
    atomic_write(version_manifest, manifest_bytes)
    atomic_write(version_manifest.with_suffix(".json.sig"), key.sign(manifest_bytes))
    atomic_write(RELEASE_DIR / "latest.json", manifest_bytes)
    atomic_write(RELEASE_DIR / "latest.json.sig", key.sign(manifest_bytes))
    print(f"published {archive_name} sha256={manifest['sha256']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test, build, sign and publish Codex TG Gateway")
    parser.add_argument("release", choices=("release",))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--version")
    group.add_argument("--bump", choices=("patch", "minor", "major"))
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args()
    current = VERSION_FILE.read_text("ascii").strip()
    version = args.version or bump(current, args.bump)
    if not args.skip_tests:
        run_quality_checks()
    publish(version)
    if version != current:
        atomic_write(VERSION_FILE, (version + "\n").encode("ascii"))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"release failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
