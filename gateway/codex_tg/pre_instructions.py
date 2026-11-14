from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 128 * 1024


class PromptValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PromptState:
    enabled: bool
    configured: bool
    length: int
    sha256: str
    updated_at: int | None
    revision: str


def validate_prompt(value: str) -> str:
    if not isinstance(value, str):
        raise PromptValidationError("前置 Instructions 必须是文本")
    if any(ord(c) < 32 and c not in '\t\r\n' for c in value) or '\x7f' in value:
        raise PromptValidationError("前置 Instructions 包含非文本控制字符")
    try:
        encoded = value.encode('utf-8')
    except UnicodeError:
        raise PromptValidationError('前置 Instructions 必须是有效 UTF-8 文本') from None
    if len(encoded) > MAX_BYTES:
        raise PromptValidationError(f"前置 Instructions 不能超过 {MAX_BYTES // 1024} KiB")
    return value


class PromptStore:
    """Root-only current prompt plus immutable revisions for queued tasks."""

    def __init__(self, path: Path):
        self.path = path
        self.versions = path.parent / "codex-pre-instructions.versions"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.versions.mkdir(parents=True, exist_ok=True)
        self._ensure_mode(self.versions)

    @staticmethod
    def _ensure_mode(path: Path) -> None:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        if os.geteuid() == 0:
            os.chown(path, 0, 0)

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def read(self) -> str:
        try:
            if self.path.is_symlink():
                raise PromptValidationError('前置 Instructions 不能是符号链接')
            self._ensure_mode(self.path)
            return self._read(self.path)
        except FileNotFoundError:
            return ""

    @staticmethod
    def _read(path: Path) -> str:
        with path.open('rb') as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise PromptValidationError('前置 Instructions 文件超过 128 KiB')
        try:
            # Preserve BOM, CRLF, whitespace and trailing newlines verbatim.
            return validate_prompt(raw.decode('utf-8'))
        except UnicodeError:
            raise PromptValidationError('前置 Instructions 文件不是有效 UTF-8') from None

    def save(self, value: str) -> str:
        value = validate_prompt(value)
        revision = self.digest(value) if value else ""
        if value:
            self._atomic_write(self.versions / f"{revision}.txt", value)
        self._atomic_write(self.path, value)
        return revision

    def load_revision(self, revision: str | None) -> str:
        if not revision:
            return ""
        if len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
            raise PromptValidationError('前置 Instructions 快照无效')
        try:
            value = self._read(self.versions / f'{revision}.txt')
        except FileNotFoundError:
            raise PromptValidationError('前置 Instructions 快照缺失，请重新提交任务') from None
        if self.digest(value) != revision:
            raise PromptValidationError('前置 Instructions 快照校验失败')
        return value

    def prune(self, retained: set[str]) -> None:
        for path in self.versions.glob('*.txt'):
            if path.stem not in retained:
                path.unlink(missing_ok=True)

    def state(self, enabled: bool, updated_at: int | None) -> PromptState:
        value = self.read()
        revision = self.digest(value) if value else ""
        return PromptState(bool(enabled and value), bool(value), len(value), revision, updated_at, revision)

    def _atomic_write(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(name, 0o600)
            if os.geteuid() == 0:
                os.chown(name, 0, 0)
            os.replace(name, path)
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def now() -> int:
        return int(time.time())
