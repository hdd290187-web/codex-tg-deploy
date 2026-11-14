from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path


PREFIX = "/codex-deploy"
MODEL_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
PLAN_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")


@dataclass(frozen=True, slots=True)
class Settings:
    password_hash: str
    master_key: bytes
    token_pepper: bytes
    public_base_url: str
    database_path: Path
    release_dir: Path
    installer_template: Path
    signing_public_key_path: Path
    session_seconds: int = 8 * 60 * 60
    deployment_seconds: int = 10 * 60
    install_session_seconds: int = 30 * 60
    secure_cookie: bool = True
    config_signing_private_key_path: Path = Path('/opt/codex-deploy/keys/config-template-private.pem')
    config_public_key_path: Path = Path('/opt/codex-deploy/keys/config-template-public.pem')

    @classmethod
    def from_env(cls) -> "Settings":
        required = {
            "CODEX_DEPLOY_PASSWORD_HASH": os.getenv("CODEX_DEPLOY_PASSWORD_HASH", ""),
            "CODEX_DEPLOY_MASTER_KEY": os.getenv("CODEX_DEPLOY_MASTER_KEY", ""),
            "CODEX_DEPLOY_TOKEN_PEPPER": os.getenv("CODEX_DEPLOY_TOKEN_PEPPER", ""),
            "PUBLIC_BASE_URL": os.getenv("PUBLIC_BASE_URL", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"missing required settings: {', '.join(missing)}")
        try:
            master_key = base64.urlsafe_b64decode(required["CODEX_DEPLOY_MASTER_KEY"])
            pepper = base64.urlsafe_b64decode(required["CODEX_DEPLOY_TOKEN_PEPPER"])
        except Exception as exc:
            raise RuntimeError("invalid base64 key material") from exc
        if len(master_key) != 32 or len(pepper) < 32:
            raise RuntimeError("master key and token pepper must be at least 256 bits")
        base_url = required["PUBLIC_BASE_URL"].rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            raise RuntimeError("PUBLIC_BASE_URL must be an absolute HTTP(S) URL")
        return cls(
            password_hash=required["CODEX_DEPLOY_PASSWORD_HASH"],
            master_key=master_key,
            token_pepper=pepper,
            public_base_url=base_url,
            database_path=Path(os.getenv("CODEX_DEPLOY_DATABASE", "/opt/codex-deploy/data/deploy.db")),
            release_dir=Path(os.getenv("CODEX_DEPLOY_RELEASES", "/opt/codex-deploy/releases")),
            installer_template=Path(
                os.getenv("CODEX_DEPLOY_INSTALLER", "/opt/codex-deploy/installer/install.sh")
            ),
            signing_public_key_path=Path(
                os.getenv("CODEX_DEPLOY_PUBLIC_KEY", "/opt/codex-deploy/keys/release-public.pem")
            ),
            secure_cookie=base_url.startswith("https://"),
            config_signing_private_key_path=Path(os.getenv('CODEX_DEPLOY_CONFIG_PRIVATE_KEY', '/opt/codex-deploy/keys/config-template-private.pem')),
            config_public_key_path=Path(os.getenv('CODEX_DEPLOY_CONFIG_PUBLIC_KEY', '/opt/codex-deploy/keys/config-template-public.pem')),
        )
