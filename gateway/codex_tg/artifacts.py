from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path

CHUNK_SIZE = 48_000_000  # Below both decimal and binary interpretations of 50 MB.
MAX_FILE = 2 * 1024 * 1024 * 1024
MAX_FILES = 40
EXTENSIONS = set('doc docx odt xls xlsx ods ppt pptx odp pdf txt csv tsv json jsonl xml yaml yml zip gz tar tgz 7z rar png jpg jpeg gif webp svg bmp ico mp4 mov mkv webm avi mp3 wav ogg flac py js jsx ts tsx html css scss sh bash sql md rst c cpp h hpp go rs java kt swift rb php lua toml ini log ipynb'.split())
PRIVATE_NAMES = {'.env', 'credentials.json', 'auth.json', 'gateway.db', 'shadow', 'passwd', 'id_rsa', 'id_ed25519',
                 'config.json', 'backup.key', 'release-private.pem'}
SECRET_RE = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|\b\d{6,15}:[A-Za-z0-9_-]{20,}\b|\b(?:sk-ant-|sk-|tvly-)[A-Za-z0-9_-]{20,}')


class ArtifactError(ValueError):
    pass


def validate_directory(value: str, current: str = '/') -> str:
    if not value or any(ord(c) < 32 for c in value):
        raise ValueError('请输入有效目录路径')
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(current) / path
    path = path.resolve(strict=True)
    if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
        raise ValueError('目录不存在或无法进入')
    return str(path)


class Artifacts:
    def __init__(self, data: Path, store, secrets):
        self.data, self.store, self.secrets = data, store, secrets
        self.outboxes = data / 'task-output'
        self.vault = data / 'delivered-files'
        for folder in (self.outboxes, self.vault):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.chmod(0o700)

    def begin(self, task_id: int) -> Path:
        folder = self.outboxes / f'{task_id}-{uuid.uuid4().hex}'
        folder.mkdir(mode=0o700)
        self.store.set_task_directory(task_id, str(folder))
        return folder

    @staticmethod
    def private_name(name: str) -> bool:
        return name.startswith('.') or name.lower() in PRIVATE_NAMES or name.lower().endswith(('.key', '.pem', '.p12', '.pfx', '.ctgbackup'))

    def checked_path(self, path: Path, *, outbox: Path | None = None) -> Path:
        if not path.is_absolute() or any(ord(c) < 32 for c in str(path)):
            raise ArtifactError('文件路径必须是有效绝对路径')
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ArtifactError('不交付符号链接；请使用真实成品文件')
        resolved = path.resolve(strict=True)
        if outbox is not None and not resolved.is_relative_to(outbox.resolve()):
            raise ArtifactError('文件不在本任务交付目录')
        if self.private_name(path.name):
            raise ArtifactError('不交付凭据或私有配置文件')
        denied = [Path('/proc'), Path('/sys'), Path('/dev'), Path('/etc'), Path('/root/.ssh'),
                  Path('/root/.codex'), Path('/root/.claude'), Path('/opt/codex-deploy/keys')]
        if any(resolved.is_relative_to(x) for x in denied):
            raise ArtifactError('该路径属于系统或凭据目录，禁止交付')
        if (resolved.is_relative_to(self.data.resolve()) and outbox is None
                and not any(resolved.is_relative_to(folder.resolve()) for folder in (self.outboxes, self.vault))):
            raise ArtifactError('Gateway 内部数据不可直接发送，请从 /files 选择成品')
        if path.suffix.lower().lstrip('.') not in EXTENSIONS:
            raise ArtifactError('该文件类型不能直接交付，请将成品打包为 ZIP')
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ArtifactError('只允许普通文件，不允许设备、管道或硬链接')
        if info.st_size > MAX_FILE:
            raise ArtifactError('单个成品超过 2 GiB，请拆分后交付；原文件仍保留在 VPS')
        return resolved

    def _check_bytes(self, data: bytes) -> None:
        if SECRET_RE.search(data) or any(value.encode() in data for value in self.secrets() if len(value) >= 6):
            raise ArtifactError('文件疑似包含密钥或凭据，已阻止自动交付')

    def _check_zip(self, path: Path) -> None:
        if not zipfile.is_zipfile(path):
            return
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(i.file_size for i in entries) > MAX_FILE:
                raise ArtifactError('压缩包过大，无法安全检查，请拆分成品')
            for item in entries:
                if item.is_dir():
                    continue
                parts = Path(item.filename).parts
                if Path(item.filename).is_absolute() or '..' in parts or any(self.private_name(n) and n != '.rels' for n in parts):
                    raise ArtifactError('压缩包含私有配置或异常路径')
                if stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                    raise ArtifactError('压缩包包含链接或加密内容，无法检查')
                with archive.open(item) as source:
                    tail = b''
                    while chunk := source.read(1024 * 1024):
                        self._check_bytes(tail + chunk)
                        tail = chunk[-8192:]

    def register(self, path: Path, chat_id: int, task_id: int | None = None, outbox: Path | None = None) -> int:
        source = self.checked_path(path, outbox=outbox)
        folder = self.vault / uuid.uuid4().hex
        folder.mkdir(mode=0o700)
        target = folder / source.name
        try:
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as incoming, target.open('xb') as outgoing:
                before = os.fstat(incoming.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise ArtifactError('文件类型已改变，停止交付')
                digest, total, tail = hashlib.sha256(), 0, b''
                while chunk := incoming.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_FILE:
                        raise ArtifactError('文件超过交付限制')
                    self._check_bytes(tail + chunk)
                    tail = chunk[-8192:]
                    digest.update(chunk)
                    outgoing.write(chunk)
                after = os.fstat(incoming.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ArtifactError('文件仍在写入，请完成后使用 /sendfile')
                outgoing.flush()
                os.fsync(outgoing.fileno())
            target.chmod(0o600)
            self._check_zip(target)
            record = self.store.add_artifact(task_id, chat_id, source.name, str(source), str(target), total, digest.hexdigest())
            if self.store.artifact(record, chat_id)['stored_path'] != str(target):
                shutil.rmtree(folder)
            return record
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def collect(self, folder: Path, chat_id: int, task_id: int) -> tuple[list[int], list[str]]:
        records, errors = [], []
        candidates = []
        # Traverse only the newly allocated task directory, never the VPS tree.
        for root, dirs, files in os.walk(folder, followlinks=False):
            dirs[:] = [n for n in dirs if not n.startswith('.') and not (Path(root) / n).is_symlink()]
            if len(Path(root).relative_to(folder).parts) >= 4:
                dirs.clear()
            candidates.extend(Path(root) / n for n in files)
            if len(candidates) > MAX_FILES:
                errors.append(f'成品超过 {MAX_FILES} 个，请将剩余文件打包后 /sendfile')
                candidates = candidates[:MAX_FILES]
                break
        for path in sorted(candidates):
            try:
                records.append(self.register(path, chat_id, task_id, folder))
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                errors.append(f'{path.name[:100]}：{exc}')
        return records, errors

    def parts(self, artifact: dict) -> list[dict]:
        source = Path(artifact['stored_path'])
        if source.is_symlink() or not source.resolve().is_relative_to(self.vault.resolve()):
            raise ArtifactError('成品副本路径异常')
        with source.open('rb') as incoming:
            digest = hashlib.file_digest(incoming, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else self._digest(incoming)
        if source.stat().st_size != artifact['size'] or digest != artifact['sha256']:
            raise ArtifactError('成品副本校验失败，停止发送')
        if self.store.artifact_parts(artifact['id']):
            return self.store.artifact_parts(artifact['id'])
        pending = []
        if artifact['size'] <= CHUNK_SIZE:
            pending.append((0, str(source), digest, artifact['size']))
        else:
            with source.open('rb') as incoming:
                number = 1
                pieces = []
                while chunk := incoming.read(CHUNK_SIZE):
                    part = source.with_name(f'{source.name}.part{number:03}')
                    part.write_bytes(chunk)
                    part.chmod(0o600)
                    sha = hashlib.sha256(chunk).hexdigest()
                    pieces.append({'file': part.name, 'sha256': sha, 'size': len(chunk)})
                    pending.append((number, str(part), sha, len(chunk)))
                    number += 1
            manifest = source.with_name(source.name + '.merge.json')
            manifest.write_text(json.dumps({'file': source.name, 'sha256': digest, 'size': artifact['size'], 'parts': pieces}, ensure_ascii=False, indent=2))
            manifest.chmod(0o600)
            merge = source.with_name(source.name + '.merge.py')
            merge.write_text('''# Put all parts, merge.json and this script in the same folder; run with Python 3.
import hashlib, json, pathlib
root = pathlib.Path(__file__).parent
meta = json.loads(pathlib.Path(__file__).with_suffix('.json').read_text())
destination = root / meta['file']
digest = hashlib.sha256()
with destination.open('xb') as output:
    for part in meta['parts']:
        data = (root / part['file']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == part['sha256'], 'Part checksum mismatch'
        output.write(data)
        digest.update(data)
assert digest.hexdigest() == meta['sha256'], 'File checksum mismatch'
print('Restored:', destination.name)
''')
            merge.chmod(0o600)
            for path in (manifest, merge):
                raw = path.read_bytes()
                pending.append((number, str(path), hashlib.sha256(raw).hexdigest(), len(raw)))
                number += 1
        self.store.save_artifact_parts(artifact['id'], pending)
        return self.store.artifact_parts(artifact['id'])

    @staticmethod
    def _digest(source) -> str:
        digest = hashlib.sha256()
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
