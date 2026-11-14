#!/usr/bin/env python3
"""Plan/apply conservative Release retention without deleting rollback targets by accident."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / 'releases'
SEMVER = re.compile(r'^codex-tg-(\d+)\.(\d+)\.(\d+)\.json$')


def versions():
    values = []
    for path in RELEASES.glob('codex-tg-*.json'):
        match = SEMVER.fullmatch(path.name)
        if match:
            values.append((tuple(map(int, match.groups())), path))
    return sorted(values, reverse=True)


def plan(keep=5):
    rows = versions()
    latest = json.loads((RELEASES / 'latest.json').read_text())['version']
    retained = {latest} | {'.'.join(map(str, version)) for version, _ in rows[:keep]}
    names = set()
    for version in retained:
        names.update({f'codex-tg-{version}.json', f'codex-tg-{version}.json.sig', f'codex-tg-{version}.tar.gz'})
        names.update(path.name for path in RELEASES.glob(f'codex-tg-wheelhouse-{version}-*.tar.gz'))
        # The signed manifest references one architecture-specific Codex CLI
        # wheel for every wheelhouse. Keep those files with the retained
        # release; deleting them leaves a valid manifest whose Installer URLs
        # return 404.
        manifest = RELEASES / f'codex-tg-{version}.json'
        if manifest.is_file():
            try:
                value = json.loads(manifest.read_text())
            except (OSError, ValueError):
                value = {}
            for wheelhouse in (value.get('wheelhouses') or {}).values():
                if isinstance(wheelhouse, dict):
                    cli = wheelhouse.get('cli')
                    if isinstance(cli, dict) and isinstance(cli.get('file'), str):
                        names.add(Path(cli['file']).name)
    remove = []
    for path in RELEASES.iterdir():
        if path.name in names or path.name in {'latest.json', 'latest.json.sig'}:
            continue
        if path.name.startswith(('codex-tg-', 'openai_codex_cli_bin-')) and path.is_file():
            remove.append(path)
    return retained, remove


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--keep', type=int, default=5)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    retained, remove = plan(max(1, args.keep))
    print('Retaining:', ', '.join(sorted(retained)))
    print('Candidates:', len(remove))
    for path in remove:
        print(path)
    if args.apply:
        for path in remove:
            path.unlink()
        print('Applied. Re-run manifest/signature verification before publishing.')


if __name__ == '__main__':
    main()
