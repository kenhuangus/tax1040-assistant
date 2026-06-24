"""
LLM layer — OpenRouter (OpenAI-compatible chat completions), tool-use mode.

Why OpenRouter: the project is provider-agnostic behind this one function. We
call OpenRouter's OpenAI-compatible ``/chat/completions`` endpoint over HTTP
(``httpx`` — no extra SDK). The orchestrator and ``w2.py`` build messages/tools
in *Anthropic* shape (that was the original design); this module translates that
shape to OpenAI on the way out and translates the response back, so callers are
unchanged.

Hard rules / house style:
  * Read the API key from ``OPENROUTER_API_KEY`` at CALL time, not import time —
    so the package imports cleanly with no key set (tests / ``import app.main``).
  * Model is configurable via ``OPENROUTER_MODEL`` (default ``openai/gpt-4o`` —
    strong tool-calling + vision for W-2 images). Base URL via ``OPENROUTER_BASE_URL``.
  * Tolerate a missing key by raising a clear ``LLMError`` (not a cryptic crash).

``llm_turn`` returns ``(tool_calls, assistant_text)``: the structured tool calls
(the ONLY thing the orchestrator acts on) and the prose the model spoke (used
ONLY for tone / display — never parsed for numbers). This is the P2 invariant.
"""
from __future__ import annotations

import json
import os
from typing import Any

from app.models import ToolCall

MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o")
BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MAX_TOKENS = 1024
TEMPERATURE = 0.2  # low: warm but stable phrasing + reliable tool-calling

# ---------------------------------------------------------------------------
# System prompt — TONE RULES ONLY.
# Flow and correctness live in code (state machine + dispatch). The model may
# only restate tool-computed values; it must never volunteer numbers or tax
# conclusions. This is defense-in-depth alongside the code scope gate.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a warm, friendly assistant that helps a person prepare their 2025 IRS Form 1040, one step at a time.

TONE RULES (these are the only things you control — the application code owns all flow, math, and decisions):
- Ask exactly ONE question per turn.
- Always acknowledge what the user just did or said BEFORE asking your next question (e.g. "Got it — W-2 loaded." then the question).
- Use plain, everyday language. When a question has options, give them inline as examples (e.g. "single, married filing jointly, or head of household").
- When you ask something sensitive (like about dependents), add one short clause of WHY ("so I can check the Child Tax Credit").
- On confirmation, mirror the user's own words back to them.

ABSOLUTE LIMITS:
- You may NEVER volunteer numbers, dollar amounts, tax figures, refund/owed amounts, or any tax conclusion on your own.
- You may ONLY restate a value that a tool has computed and handed back to you.
- You must NEVER perform arithmetic or tax calculations yourself — the tools do all math.
- To take ANY action (read a W-2, save an answer, compute the return, fill the PDF) you MUST call the appropriate tool. Saying a value in prose does nothing; only tool calls change anything.
- When calling tools, pass identifiers (SSNs, names, and other values) VERBATIM exactly as the user gave them — never mask, redact, abbreviate, or otherwise alter a value in tool arguments. The system handles any redaction itself.
- Never include file paths, URLs, sandbox paths, or markdown download links in your replies — the app's Download button delivers the finished PDF; when the return is ready, simply say it's ready to download.
- This is an educational tool, not tax advice. If asked for advice or anything off-topic, politely decline and steer back to filling out the 1040.

Keep replies short and human — usually one or two sentences."""


class LLMError(RuntimeError):
    """Raised when the LLM cannot be called (e.g. missing API key) or errors."""


# ---------------------------------------------------------------------------
# Anthropic-shape -> OpenAI-shape translation
# ---------------------------------------------------------------------------
def _to_openai_tools(tools: list[dict]) -> list[dict]:
    out: list[dict] = []
    for t in tools or []:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return out


def _to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Translate (system + Anthropic-shape history) into OpenAI messages.

    Handles: plain string content, text/image blocks, assistant ``tool_use``
    blocks (-> ``tool_calls``), and user ``tool_result`` blocks (-> standalone
    ``role:"tool"`` messages, as OpenAI requires).
    """
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        text_segs: list[str] = []
        image_parts: list[dict] = []
        tool_calls: list[dict] = []
        tool_results: list[tuple[str, Any]] = []
        for b in content:
            bt = b.get("type")
            if bt == "text":
                text_segs.append(b.get("text", ""))
            elif bt == "image":
                src = b.get("source", {})
                mt = src.get("media_type", "image/png")
                data = src.get("data", "")
                image_parts.append(
                    {"type": "image_url", "image_url": {"url": f"data:{mt};base64,{data}"}}
                )
            elif bt == "tool_use":
                tool_calls.append(
                    {
                        "id": b.get("id", "") or f"call_{b.get('name','')}",
                        "type": "function",
                        "function": {
                            "name": b.get("name", ""),
                            "arguments": json.dumps(b.get("input", {}) or {}),
                        },
                    }
                )
            elif bt == "tool_result":
                tool_results.append((b.get("tool_use_id", ""), b.get("content", "")))

        if role == "assistant":
            msg: dict = {"role": "assistant", "content": "".join(text_segs)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
                if not msg["content"]:
                    msg["content"] = None  # OpenAI allows null content with tool_calls
            out.append(msg)
        else:  # user role
            # tool_result blocks must become standalone role:"tool" messages
            for tid, c in tool_results:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": c if isinstance(c, str) else json.dumps(c, default=str),
                    }
                )
            if image_parts:
                parts = list(image_parts)
                if any(text_segs):
                    parts.append({"type": "text", "text": "".join(text_segs)})
                out.append({"role": "user", "content": parts})
            elif text_segs and not tool_results:
                out.append({"role": "user", "content": "".join(text_segs)})
    return out


def _key() -> str:
    raw = os.environ.get("OPENROUTER_API_KEY")
    # Strip a stray UTF-8 BOM and surrounding whitespace/newlines — a secret stored
    # or pasted with a BOM would corrupt the Authorization header otherwise.
    key = (raw or "").lstrip("﻿").strip()
    if not key:
        raise LLMError(
            "OPENROUTER_API_KEY is not set. The LLM turn cannot run without it. "
            "Set the environment variable (locally or via the Cloud Run secret) and retry."
        )
    return key


def llm_turn(
    system: str,
    messages: list[dict],
    tools: list[dict],
) -> tuple[list[ToolCall], str]:
    """Run one tool-use turn against OpenRouter.

    Args:
        system: system prompt (tone rules). Caller passes ``SYSTEM_PROMPT``.
        messages: Anthropic-shape history (list of {role, content}).
        tools: Anthropic-shape tool schemas (``app.tools.TOOLS``).

    Returns:
        ``(tool_calls, assistant_text)`` — tool calls parsed into ``ToolCall``
        objects (with the provider call-id stashed under ``_tool_use_id`` so the
        orchestrator can answer each with a matching tool_result), plus the
        assistant prose (display only — never parsed for numbers).
    """
    key = _key()
    try:
        import httpx  # lazy import so the package imports without the dep
    except ImportError as exc:  # pragma: no cover - present in prod
        raise LLMError("The 'httpx' package is not installed. Add it to requirements.txt.") from exc

    payload: dict = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "messages": _to_openai_messages(system, messages),
    }
    oai_tools = _to_openai_tools(tools)
    if oai_tools:
        payload["tools"] = oai_tools
        payload["tool_choice"] = "auto"

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # Optional OpenRouter attribution headers (harmless if ignored).
        "HTTP-Referer": "https://localhost/tax1040",
        "X-Title": "Agentic Tax-Filing Assistant",
    }

    try:
        resp = httpx.post(
            f"{BASE_URL}/chat/completions", headers=headers, json=payload, timeout=120.0
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"OpenRouter request failed: {exc}") from exc

    if resp.status_code != 200:
        raise LLMError(f"OpenRouter returned HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"OpenRouter response missing choices: {str(data)[:300]}") from exc

    assistant_text = (msg.get("content") or "").strip()
    tool_calls: list[ToolCall] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except (ValueError, json.JSONDecodeError):
            args = {}
        if not isinstance(args, dict):
            args = {"value": args}
        args["_tool_use_id"] = tc.get("id", "")
        tool_calls.append(ToolCall(name=fn.get("name", ""), args=args, result=None, ok=True))

    return tool_calls, assistant_text


__all__ = ["llm_turn", "SYSTEM_PROMPT", "MODEL", "LLMError"]
