from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.shell import ExecTool


@pytest.mark.asyncio
async def test_exec_tool_returns_output_by_default() -> None:
    tool = ExecTool(timeout=5)

    result = await tool.execute("printf 'hello'")

    assert result == "hello"


@pytest.mark.asyncio
async def test_exec_tool_raw_output_sends_to_channel() -> None:
    send_callback = AsyncMock()
    tool = ExecTool(timeout=5, send_callback=send_callback)
    tool.set_context("telegram", "12345", "99")

    result = await tool.execute("printf 'hello from exec'", raw_output=True)

    assert result == (
        "Raw output sent to telegram:12345\n\nhello from exec"
        "\n\nalready sent to the user because raw_output=True, "
        "DO NOT SEND DETAILS AGAIN"
    )
    send_callback.assert_awaited_once()
    outbound = send_callback.await_args.args[0]
    assert outbound.channel == "telegram"
    assert outbound.chat_id == "12345"
    assert outbound.content == "=== raw ===\nhello from exec"
    assert outbound.metadata.get("raw_output") is True
    assert outbound.metadata.get("message_id") == "99"


@pytest.mark.asyncio
async def test_exec_tool_raw_output_requires_context() -> None:
    send_callback = AsyncMock()
    tool = ExecTool(timeout=5, send_callback=send_callback)

    result = await tool.execute("printf 'hello'", raw_output=True)

    assert result == "Error: raw_output requested but no target channel/chat is available"
    send_callback.assert_not_called()


@pytest.mark.asyncio
async def test_exec_tool_allows_per_call_timeout_seconds() -> None:
    tool = ExecTool(timeout=5)

    result = await tool.execute(
        'python3 -c "import time; time.sleep(2)"',
        timeout_seconds=1,
    )

    assert result == "Error: Command timed out after 1 seconds"


@pytest.mark.asyncio
async def test_exec_tool_rejects_non_positive_timeout_seconds() -> None:
    tool = ExecTool(timeout=5)

    result = await tool.execute("printf 'hello'", timeout_seconds=0)

    assert result == "Error: timeout_seconds must be greater than 0"
