#!/usr/bin/env python3
"""Scan the staged source tree; print locations only, never matched secrets."""
import pathlib
import re
import subprocess
import sys

paths = subprocess.check_output(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z']).decode().split('\0')
patterns = [rb'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----',
            rb'gh[pousr]_[A-Za-z0-9]{30,}', rb'github_pat_[A-Za-z0-9_]{40,}',
            rb'AKIA[A-Z0-9]{16}', rb'\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}']
blocked = {'keys', 'data', 'logs', 'backups', 'releases', 'node_modules', 'artifacts', 'sessions', 'uploads', '.venv'}
failures = []
for name in filter(None, paths):
    path = pathlib.PurePosixPath(name)
    if blocked.intersection(path.parts) or path.name.startswith('.env') or path.suffix in {'.db', '.pem', '.key', '.whl', '.log', '.ctgbackup'}:
        failures.append((name, 'prohibited path'))
        continue
    raw = subprocess.check_output(['git', 'show', ':' + name])
    if len(raw) > 2 * 1024 * 1024 or b'\0' in raw:
        failures.append((name, 'binary/oversized file'))
    for pattern in patterns:
        if re.search(pattern, raw):
            failures.append((name, 'credential pattern'))
for name, reason in failures:
    print(f'{name}: {reason}')
print(f'Staged secret scan: {len(list(filter(None, paths)))} files; {len(failures)} findings')
sys.exit(bool(failures))
