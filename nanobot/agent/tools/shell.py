"""Shell execution tool."""

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        send_callback=None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
        default_session_key: str | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",  # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",  # del /f, del /q
            r"\brmdir\s+/s\b",  # rmdir /s
            r"(?:^|[;&|]\s*)format\b",  # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",  # disk operations
            r"\bdd\s+if=",  # dd
            r">\s*/dev/sd",  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",  # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._default_session_key = default_session_key

    def set_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """Set routing context for optional raw output delivery."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id
        self._default_session_key = session_key

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "raw_output": {
                    "type": "boolean",
                    "description": "If true, send command output directly to the current chat channel",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Optional timeout for this command in seconds",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        raw_output: bool = False,
        timeout_seconds: int | None = None,
        **kwargs: Any,
    ) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        if timeout_seconds is not None and timeout_seconds <= 0:
            return "Error: timeout_seconds must be greater than 0"
        effective_timeout = timeout_seconds if timeout_seconds is not None else self.timeout
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                # Wait for the process to fully terminate so pipes are
                # drained and file descriptors are released.
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return f"Error: Command timed out after {effective_timeout} seconds"

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")

            full_result = "\n".join(output_parts) if output_parts else "(no output)"

            # Truncate very long output for tool return/session storage.
            max_len = 5000
            result_for_return = self._truncate_output_for_return(full_result, max_len)

            if raw_output:
                # Raw mode sends the complete output to the channel, while the
                # tool return remains truncated-safe for session history.
                status = await self._send_raw_output(full_result)
                note = (
                    "\n\nalready sent to the user because raw_output=True, "
                    "DO NOT SEND DETAILS AGAIN"
                )
                return f"{status}\n\n{result_for_return}{note}"

            return result_for_return

        except Exception as e:
            return f"Error executing command: {str(e)}"

    async def _send_raw_output(self, output: str) -> str:
        """Send tool output straight to chat when requested by the caller."""
        if not self._default_channel or not self._default_chat_id:
            return "Error: raw_output requested but no target channel/chat is available"
        if not self._send_callback:
            return "Error: raw_output requested but message sending is not configured"

        msg = OutboundMessage(
            channel=self._default_channel,
            chat_id=self._default_chat_id,
            content=f"=== raw ===\n{output}",
            metadata={
                "message_id": self._default_message_id,
                "raw_output": True,
                "_session_key": self._default_session_key,
            },
        )
        try:
            await self._send_callback(msg)
        except Exception as e:
            return f"Error: Failed to send raw output: {e}"
        return f"Raw output sent to {self._default_channel}:{self._default_chat_id}"

    def _truncate_output_for_return(self, full_result: str, max_len: int) -> str:
        """Bound tool output while still surfacing truncation metadata and tail context."""
        if len(full_result) <= max_len:
            return full_result

        tail_lines = full_result.splitlines()[-3:]
        tail_block = "\n".join(tail_lines)
        # Use at most the last 3 lines, additionally capped to the last 1000 chars.
        max_tail_chars = 1000
        if len(tail_block) > max_tail_chars:
            tail_block = f"...{tail_block[-(max_tail_chars - 3) :]}"
        tail_section_raw = (
            f"\n--- tail (last 3 lines, max 1000 chars) ---\n{tail_block}" if tail_block else ""
        )

        truncation_note = "\n\n... (truncated, 0 more lines)"
        # Recompute until note length and head cutoff agree.
        for _ in range(3):
            max_tail_section_len = max(0, max_len - len(truncation_note))
            tail_section = tail_section_raw
            if len(tail_section) > max_tail_section_len:
                tail_section = tail_section[-max_tail_section_len:]

            head_budget = max(0, max_len - len(truncation_note) - len(tail_section))
            remaining_lines = len(full_result[head_budget:].splitlines())
            line_label = "line" if remaining_lines == 1 else "lines"
            updated_note = f"\n\n... (truncated, {remaining_lines} more {line_label})"
            if updated_note == truncation_note:
                break
            truncation_note = updated_note

        max_tail_section_len = max(0, max_len - len(truncation_note))
        tail_section = tail_section_raw
        if len(tail_section) > max_tail_section_len:
            tail_section = tail_section[-max_tail_section_len:]
        head_budget = max(0, max_len - len(truncation_note) - len(tail_section))
        head = full_result[:head_budget]
        return head + truncation_note + tail_section

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            # Only match absolute paths — avoid false positives on relative
            # paths like ".venv/bin/python" where "/bin/python" would be
            # incorrectly extracted by the old pattern.
            posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", cmd)

            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw.strip()).resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None
