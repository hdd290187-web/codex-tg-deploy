#!/usr/bin/env python3
from __future__ import annotations

import base64
import os
from pathlib import Path

from argon2 import PasswordHasher


TARGET = Path("/etc/codex-deploy.env")


def encoded_random(size: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(size)).decode("ascii")


def main() -> None:
    if TARGET.exists():
        raise SystemExit(f"{TARGET} already exists; refusing to overwrite it")
    password = os.environ.pop("CODEX_DEPLOY_BOOTSTRAP_PASSWORD", "")
    if not password:
        raise SystemExit("CODEX_DEPLOY_BOOTSTRAP_PASSWORD is required")
    password_hash = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4).hash(password)
    lines = [
        f'CODEX_DEPLOY_PASSWORD_HASH="{password_hash}"',
        f'CODEX_DEPLOY_MASTER_KEY="{encoded_random()}"',
        f'CODEX_DEPLOY_TOKEN_PEPPER="{encoded_random()}"',
        'PUBLIC_BASE_URL="https://gpt.cevyo.com/codex-deploy"',
        'CODEX_DEPLOY_DATABASE="/opt/codex-deploy/data/deploy.db"',
        'CODEX_DEPLOY_RELEASES="/opt/codex-deploy/releases"',
        'CODEX_DEPLOY_INSTALLER="/opt/codex-deploy/installer/install.sh"',
        'CODEX_DEPLOY_PUBLIC_KEY="/opt/codex-deploy/keys/release-public.pem"',
    ]
    descriptor = os.open(TARGET, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
        output.flush()
        os.fsync(output.fileno())
    print(f"created {TARGET} with Argon2id password hash and random 256-bit keys")


if __name__ == "__main__":
    main()
