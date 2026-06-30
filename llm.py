"""
Minimal OpenAI-compatible chat client backed by `requests`.

Replaces the `openai` SDK so the project has NO Rust-built dependencies (jiter / pydantic-core),
which don't build on Termux/Android. Implements only the surface this project uses:

    client.chat.completions.create(model=, messages=, tools=?, response_format=?)

returning an object that quacks like the SDK's: .choices[0].message.content and
.choices[0].message.tool_calls (each tool_call: .id, .function.name, .function.arguments).
"""

from __future__ import annotations

import types

import requests


class _Message:
    """Quacks like the openai SDK message object; keeps the raw dict for re-sending."""

    def __init__(self, raw: dict):
        self._raw = raw
        self.role = raw.get("role")
        self.content = raw.get("content")
        self.tool_calls = [
            types.SimpleNamespace(
                id=tc.get("id"), type=tc.get("type", "function"),
                function=types.SimpleNamespace(
                    name=tc["function"]["name"], arguments=tc["function"].get("arguments", "")))
            for tc in (raw.get("tool_calls") or [])] or None


def _to_dict(m):
    """Serialise a message (plain dict, our _Message, or any role/content/tool_calls object)."""
    if isinstance(m, dict):
        return m
    if isinstance(m, _Message):
        return m._raw
    d = {"role": getattr(m, "role", None), "content": getattr(m, "content", None)}
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        d["tool_calls"] = [{"id": tc.id, "type": getattr(tc, "type", "function"),
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                           for tc in tcs]
    return d


class _Completions:
    def __init__(self, client):
        self._c = client

    def create(self, model, messages, tools=None, response_format=None, timeout=600):
        payload = {"model": model, "messages": [_to_dict(m) for m in messages]}
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["response_format"] = response_format
        r = requests.post(
            f"{self._c.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self._c.api_key}", "Content-Type": "application/json"},
            json=payload, timeout=timeout)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=_Message(msg))])


class OllamaClient:
    """Drop-in replacement for openai.OpenAI for the subset this project uses."""

    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.api_key = api_key
        self.chat = types.SimpleNamespace(completions=_Completions(self))
