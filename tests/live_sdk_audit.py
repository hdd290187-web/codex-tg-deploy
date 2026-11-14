#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import time
import tomllib
from pathlib import Path
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, LocalImageInput, Sandbox, TextInput
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    CommandExecutionThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    MessagePhase,
    TurnCompletedNotification,
)


ARTIFACT = Path("/opt/codex-deploy/tests/artifacts/live-sdk-audit.json")
IMAGE = Path("/opt/codex-deploy/tests/artifacts/center-mobile.png")
CONFIG_PATH = Path("/root/.codex/config.toml")


async def collect(handle: Any, command_started: asyncio.Event | None = None) -> dict[str, Any]:
    methods: list[str] = []
    final = ""
    status = "missing"
    async for event in handle.stream():
        methods.append(event.method)
        payload = event.payload
        if isinstance(payload, ItemStartedNotification):
            item = payload.item.root if hasattr(payload.item, "root") else payload.item
            if command_started is not None and isinstance(item, CommandExecutionThreadItem):
                command_started.set()
        elif isinstance(payload, ItemCompletedNotification):
            item = payload.item.root if hasattr(payload.item, "root") else payload.item
            if isinstance(item, AgentMessageThreadItem):
                if item.phase == MessagePhase.final_answer or item.phase is None:
                    final = item.text
        elif isinstance(payload, TurnCompletedNotification):
            status = payload.turn.status.value
    return {
        "status": status,
        "final": final,
        "event_count": len(methods),
        "event_methods": sorted(set(methods)),
    }


async def main() -> None:
    config_data = tomllib.loads(CONFIG_PATH.read_text("utf-8"))
    model = config_data["model"]
    provider = config_data["model_provider"]
    environment = dict(os.environ)
    sdk_config = CodexConfig(cwd="/", env=environment)
    marker = f"TG_AUDIT_{int(time.time())}"
    result: dict[str, Any] = {
        "model": model,
        "provider": provider,
        "approval_mode": ApprovalMode.deny_all.value,
        "sandbox": Sandbox.full_access.value,
    }

    async with AsyncCodex(sdk_config) as client:
        thread = await client.thread_start(
            approval_mode=ApprovalMode.deny_all,
            cwd="/",
            model=model,
            model_provider=provider,
            sandbox=Sandbox.full_access,
        )
        result["thread_id"] = thread.id
        first = await thread.turn(
            "Use the shell tool to run `id -u && pwd && test -r /etc/shadow && echo ROOT_FS_OK`. "
            f"Then reply with the exact marker {marker} and a concise summary of the command results.",
            approval_mode=ApprovalMode.deny_all,
            cwd="/",
            sandbox=Sandbox.full_access,
        )
        result["root_turn"] = await collect(first)

    async with AsyncCodex(sdk_config) as resumed_client:
        resumed = await resumed_client.thread_resume(
            result["thread_id"],
            approval_mode=ApprovalMode.deny_all,
            cwd="/",
            model=model,
            model_provider=provider,
            sandbox=Sandbox.full_access,
        )
        resume_turn = await resumed.turn(
            "Without using tools, return the audit marker from the previous turn and nothing else.",
            approval_mode=ApprovalMode.deny_all,
            sandbox=Sandbox.full_access,
        )
        result["resume_turn"] = await collect(resume_turn)
        image_turn = await resumed.turn(
            [
                TextInput(
                    "Inspect this image. State the visible page title and whether it is a mobile layout. "
                    "Do not use shell or other tools."
                ),
                LocalImageInput(str(IMAGE)),
            ],
            approval_mode=ApprovalMode.deny_all,
            sandbox=Sandbox.full_access,
        )
        result["image_turn"] = await collect(image_turn)
        await resumed_client.thread_archive(result["thread_id"])

        interrupt_thread = await resumed_client.thread_start(
            approval_mode=ApprovalMode.deny_all,
            cwd="/",
            ephemeral=True,
            model=model,
            model_provider=provider,
            sandbox=Sandbox.full_access,
        )
        interrupt_handle = await interrupt_thread.turn(
            "Use the shell tool to execute exactly `sleep 120`. Do not finish the turn before it completes.",
            approval_mode=ApprovalMode.deny_all,
            sandbox=Sandbox.full_access,
        )
        command_started = asyncio.Event()
        consume_task = asyncio.create_task(collect(interrupt_handle, command_started))
        await asyncio.wait_for(command_started.wait(), timeout=45)
        await interrupt_handle.interrupt()
        result["interrupt_turn"] = await asyncio.wait_for(consume_task, timeout=30)

    root_final = result["root_turn"]["final"]
    assert result["root_turn"]["status"] == "completed"
    assert marker in root_final and "ROOT_FS_OK" in root_final and "0" in root_final
    assert "item/started" in result["root_turn"]["event_methods"]
    assert result["resume_turn"]["status"] == "completed"
    assert marker in result["resume_turn"]["final"]
    assert result["image_turn"]["status"] == "completed"
    assert result["image_turn"]["final"].strip()
    assert result["interrupt_turn"]["status"] == "interrupted"
    ARTIFACT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    ARTIFACT.chmod(0o600)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
