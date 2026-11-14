"""Opt-in live relay audit. Never prints credentials or request bodies."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))

from codex_tg.app import Gateway  # noqa: E402
from codex_tg.config import Config  # noqa: E402
from codex_tg.store import ChatSettings  # noqa: E402
import codex_tg.responses_adapter as adapter_module  # noqa: E402


async def main():
    root_config = tomllib.loads(Path('/root/.codex/config.toml').read_text())
    root_auth = json.loads(Path('/root/.codex/auth.json').read_text())
    provider = root_config['model_providers'][root_config['model_provider']]
    endpoint = {'base_url': provider['base_url'], 'api_key': root_auth['OPENAI_API_KEY']}
    model = root_config['model']
    first_prompt = ('For this verification, preserve the requested audit markers exactly in your report.\r\n'
                    'Perform file operations only inside the temporary audit directory named in the user request.\n')
    next_prompt = 'Use the existing conversation context to report the operational verification accurately.\n'
    rewrites = []
    originals_present = []
    forwarded = []
    active_prompt = first_prompt
    original_rewrite = adapter_module.prepend_instructions
    original_proxy = adapter_module.ResponsesAdapter._proxy

    def checked_rewrite(raw, prompt):
        before = json.loads(raw)
        result = original_rewrite(raw, prompt)
        after = json.loads(result)
        expected = prompt + '\n\n' + before['instructions'] if before.get('instructions') else prompt
        assert after['instructions'] == expected
        assert after['instructions'].count(prompt) == 1
        assert {k: v for k, v in before.items() if k != 'instructions'} == {k: v for k, v in after.items() if k != 'instructions'}
        rewrites.append(True)
        originals_present.append(bool(before.get('instructions')))
        return result

    async def checked_proxy(self, connection, writer, method, url, headers, body):
        if method == 'POST' and url.endswith('/responses'):
            instructions = json.loads(body).get('instructions', '')
            assert (instructions == active_prompt or instructions.startswith(active_prompt + '\n\n')) if active_prompt else not (
                instructions.startswith(first_prompt) or instructions.startswith(next_prompt))
            forwarded.append(bool(active_prompt))
        return await original_proxy(self, connection, writer, method, url, headers, body)

    adapter_module.prepend_instructions = checked_rewrite
    adapter_module.ResponsesAdapter._proxy = checked_proxy
    with tempfile.TemporaryDirectory(prefix='codex-pre-instructions-live-') as directory:
        base = Path(directory)
        codex_home = base / 'home'
        codex_home.mkdir()
        os.environ['CODEX_HOME'] = str(codex_home)
        config = Config(telegram_token='fixture:never-contact-telegram', allowed_user_id=7, allowed_chat_id=9,
                        model=model, reasoning_effort='high', plan_reasoning_effort='high',
                        database_path=base / 'data/gateway.db', upload_dir=base / 'uploads', log_dir=base / 'logs',
                        release_base_url='https://gpt.cevyo.com/codex-deploy/releases', health_port=0,
                        telegram_network='DIRECT', telegram_proxy_kind='none', telegram_proxy_url=None,
                        release_public_key_path=Path(__file__).resolve().parents[1] / 'keys/release-public.pem',
                        max_upload_bytes=1024, upload_retention_days=7, version='2.1.0')
        gateway = Gateway(config)
        gateway.store.ensure_chat(7, 9, ChatSettings(model, 'high', 'high'))
        gateway.credentials.save('codex', endpoint['base_url'], endpoint['api_key'])
        gateway.save_codex_prompt(first_prompt, enabled=True)
        target = base / 'audit-marker.txt'
        marker = 'CODEX_INSTRUCTIONS_LIVE_PASS'
        changed = False

        async def progress(_text):
            nonlocal changed
            if not changed:
                changed = True
                gateway.save_codex_prompt(next_prompt, enabled=True)

        async def quiet(_text):
            pass

        try:
            await gateway.runtime.configure(endpoint)
            gateway.responses_adapter.set_snapshot(first_prompt)
            thread = await gateway.runtime.new_thread(model)
            result = await asyncio.wait_for(gateway.runtime.run(thread,
                f'Perform an operational verification in this temporary audit directory: {base}. '
                f'Use a shell tool to run id -u, create {target} containing exactly {marker}, then read the file. '
                'Report the effective uid and the exact marker. Do not modify any other files.',
                [], progress, model, 'high'), 150)
            assert not result.interrupted and marker in result.response
            assert target.read_text().strip() == marker and changed
            gateway.store.set_thread(9, thread.id)
            assert len(rewrites) >= 2 and all(forwarded)
            await gateway.runtime.close()
            await gateway.telegram.close()

            gateway = Gateway(config)
            assert gateway.codex_prompt_state().enabled and gateway.prompt_store.read() == next_prompt
            active_prompt = next_prompt
            await gateway.runtime.configure(endpoint)
            gateway.responses_adapter.set_snapshot(gateway.prompt_store.load_revision(gateway.codex_prompt_revision()))
            thread = await gateway.runtime.get_thread(gateway.store.thread_id(9), model, 'high', apply_settings=True)
            resumed = await asyncio.wait_for(gateway.runtime.run(thread,
                'Using the previous conversation only, recall the exact file verification marker and effective uid. '
                'Respond in one sentence without using tools.', [], quiet, model, 'high'), 90)
            assert marker in resumed.response

            gateway.set_codex_prompt_enabled(False)
            gateway.responses_adapter.clear_snapshot()
            active_prompt = ''
            await asyncio.wait_for(gateway.runtime.run(thread,
                'Confirm in one concise natural-language sentence that the file and root-identity verification completed. '
                'Use the previous conversation, without calling tools.', [], quiet, model, 'high'), 90)
            assert forwarded[-1] is False
            gateway.set_codex_prompt_enabled(True)
            assert gateway.prompt_store.read() == next_prompt
            active_prompt = next_prompt
            gateway.responses_adapter.set_snapshot(gateway.prompt_store.load_revision(gateway.codex_prompt_revision()))
            fresh = await gateway.runtime.new_thread(model)
            await asyncio.wait_for(gateway.runtime.run(fresh,
                'Reply with one concise natural-language sentence confirming that this new conversation can process '
                'an operational verification request. Do not call tools.', [], quiet, model, 'high'), 90)
            assert forwarded[-1] is True
            report = {'real_upstream': True, 'model': model, 'root_shell_and_file': True,
                      'instructions_exact_order': True, 'other_fields_unchanged': True,
                      'no_duplicate_injection': True, 'turn_snapshot': True, 'restart_and_resume': True,
                      'off_on_and_new_thread': True, 'responses_forwarded': len(forwarded), 'injected': len(rewrites)}
            report['codex_original_instructions_present'] = any(originals_present)
            artifact = Path(__file__).resolve().parent / 'artifacts/codex-prompt-live-audit.json'
            artifact.write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report))
        except Exception as exc:
            print('Live audit failed: ' + type(exc).__name__ + ': '
                  + gateway.redact_codex_prompt(gateway.sanitize(str(exc)))[:500])
            raise SystemExit(1) from None
        finally:
            await gateway.runtime.close()
            await gateway.telegram.close()
            adapter_module.prepend_instructions = original_rewrite
            adapter_module.ResponsesAdapter._proxy = original_proxy


if __name__ == '__main__':
    asyncio.run(main())
