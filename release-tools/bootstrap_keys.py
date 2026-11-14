#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "keys/release-private.pem"
PUBLIC = ROOT / "keys/release-public.pem"


def write_exclusive(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)


def main() -> None:
    PRIVATE.parent.mkdir(parents=True, exist_ok=True)
    if PRIVATE.exists() or PUBLIC.exists():
        raise SystemExit("signing keys already exist; refusing to replace them")
    private = Ed25519PrivateKey.generate()
    write_exclusive(
        PRIVATE,
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o600,
    )
    write_exclusive(
        PUBLIC,
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
        0o644,
    )
    print(f"created {PRIVATE} and {PUBLIC}")


if __name__ == "__main__":
    main()
