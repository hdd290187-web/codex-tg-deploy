"""Opt-in live Codex relay task; Telegram acknowledgements are isolated fixtures."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))

from codex_tg.app import Gateway  # noqa: E402
from test_gateway import make_config, FakeTelegram  # noqa: E402
from test_dual_engine import message  # noqa: E402


async def main():
    config = tomllib.loads(Path('/root/.codex/config.toml').read_text())
    auth = json.loads(Path('/root/.codex/auth.json').read_text())
    endpoint = config['model_providers'][config['model_provider']]['base_url']
    with tempfile.TemporaryDirectory(prefix='codex-files-live-audit-') as directory:
        root = Path(directory)
        home = root / 'home'
        home.mkdir()
        os.environ['CODEX_HOME'] = str(home)
        workspace = root / 'project'
        workspace.mkdir()
        g = Gateway(make_config(root / 'data'))
        g.store.ensure_chat(7, 9)
        g.credentials.save('codex', endpoint, auth['OPENAI_API_KEY'])
        g.engine_endpoints['codex'] = g.credentials.get('codex')
        g.store.update_chat_settings(9, model=config['model'], reasoning_effort='high')
        delivered = []
        class Telegram(FakeTelegram):
            async def send_document(self, chat_id, path, caption=''):
                delivered.append((path.name, path.read_bytes()))
                return {'message_id': len(delivered), 'document': {'file_id': 'isolated-ack-' + str(len(delivered))}}
        await g.telegram.close()
        g.telegram = Telegram()
        await g.runtime.configure(g.engine_endpoints['codex'])
        await g.handle_message(message('/cd ' + str(workspace), 41))
        await g.handle_message(message(
            'Create a small employee payroll example with two fictitious employees (Alice 1000 and Bob 1200). '
            'Deliver three actual finished files: payroll.xlsx, payroll.pdf and verification.txt. '
            'The TXT must include the real current working directory reported by Python os.getcwd(). '
            'Use local tools to create the files, with no network data needed. '
            'Keep all operations inside the current temporary project. '
            'Send the finished files to me in Telegram. Do not include any credentials.', 42))
        worker, delivery = asyncio.create_task(g.worker()), asyncio.create_task(g.delivery_worker())
        try:
            for _ in range(3600):
                items = g.store.artifacts(9)
                if len(items) >= 3 and all(x['status'] == 'DELIVERED' for x in items):
                    break
                last = g.store.last_task()
                if last and last['status'] in {'FAILED', 'INTERRUPTED'}:
                    raise RuntimeError(last['error'])
                await asyncio.sleep(.05)
            files = dict(delivered)
            assert {'payroll.xlsx', 'payroll.pdf', 'verification.txt'} <= files.keys(), list(files)
            assert files['payroll.pdf'].startswith(b'%PDF')
            assert str(workspace).encode() in files['verification.txt']
            xlsx = next(x for x in g.store.artifacts(9) if x['name'] == 'payroll.xlsx')
            with zipfile.ZipFile(xlsx['stored_path']) as archive:
                assert 'xl/workbook.xml' in archive.namelist()
                assert archive.testzip() is None
            report = {'real_model': config['model'], 'real_codex_runtime': '0.162.1',
                      'txt_excel_pdf_generated': True, 'cwd_verified': True,
                      'automatic_delivery_pipeline': True, 'telegram_acknowledgement': 'isolated fixture; live Telegram not configured',
                      'file_count': len(delivered)}
            (ROOT / 'tests/artifacts/files-live-audit.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report))
        except Exception as exc:
            print('LIVE_DELIVERY_FAILED ' + g.sanitize(str(exc))[:500])
            raise SystemExit(1) from None
        finally:
            g.stop_event.set()
            g.worker_event.set()
            g.delivery_event.set()
            worker.cancel()
            delivery.cancel()
            await asyncio.gather(worker, delivery, return_exceptions=True)
            await g.runtime.close()


if __name__ == '__main__':
    asyncio.run(main())
