"""OpenCode Zen provider (API key, Responses API).

Reuses helpers from OpenAICodexProvider; only auth, URL, and body differ.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import httpx

from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.openai_codex_provider import (
    _consume_sse,
    _convert_messages,
    _convert_tools,
)

DEFAULT_ZEN_BASE = "https://opencode.ai/zen/v1"


class ZenProvider(LLMProvider):
    """Call Zen's Responses API with an API key."""

    def __init__(
        self,
        api_key: str,
        api_base: str | None = None,
        default_model: str = "zen/muse-spark-1.3",
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}
        base = (api_base or DEFAULT_ZEN_BASE).rstrip("/")
        self._url = base if base.endswith("/responses") else f"{base}/responses"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        system_prompt, input_items = _convert_messages(_alias_tool_ids(messages))
        # Stable session per chat: hash of first user message.
        first = next(
            (m.get("content") for m in messages if m.get("role") == "user" and m.get("content")),
            None,
        )
        if isinstance(first, str):
            session_id = "ses_" + hashlib.sha256(first.encode()).hexdigest()[:32]
        else:
            session_id = f"ses_{uuid.uuid4().hex}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "nanobot (python)",
            "session_id": session_id,
            "x-client-request-id": f"req_{uuid.uuid4().hex}",
            **self.extra_headers,
        }
        body: dict[str, Any] = {
            "model": _strip_model_prefix(model or self.default_model),
            "store": False,
            "stream": True,
            "instructions": system_prompt,
            "input": input_items,
            "max_output_tokens": max(1, max_tokens),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            **self.extra_body,
        }
        if tools:
            body["tools"] = _convert_tools(tools)

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", self._url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        text = await r.aread()
                        raise RuntimeError(f"HTTP {r.status_code}: {text.decode()[:500]}")
                    content, tool_calls, finish = await _consume_sse(r)
            return LLMResponse(content=content, tool_calls=tool_calls, finish_reason=finish)
        except Exception as e:
            return LLMResponse(content=f"Error calling Zen: {e}", finish_reason="error")

    def get_default_model(self) -> str:
        return self.default_model


def _alias_tool_ids(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shorten tool ids over Zen 64-char cap."""
    aliases: dict[str, str] = {}

    def short(cid: str) -> str:
        base = cid.split("|", 1)[0] if "|" in cid else cid
        if len(base) <= 60:
            return cid
        return aliases.setdefault(base, f"mc{len(aliases)}")

    out = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            msg = {
                **msg,
                "tool_calls": [{**tc, "id": short(tc.get("id") or "")} for tc in msg["tool_calls"]],
            }
        elif msg.get("role") == "tool" and msg.get("tool_call_id"):
            msg = {**msg, "tool_call_id": short(msg["tool_call_id"])}
        out.append(msg)
    return out


def _strip_model_prefix(model: str) -> str:
    if "/" in model:
        prefix, rest = model.split("/", 1)
        if prefix.lower().replace("-", "_") in ("zen", "opencode"):
            return rest
    return model
