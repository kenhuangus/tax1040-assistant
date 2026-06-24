"""
LLM layer — Anthropic ``claude-opus-4-8``, tool-use mode.

Hard rules (from the contract + house style):
  * Model id is ``claude-opus-4-8``.
  * NEVER send ``temperature`` (this model rejects it).
  * NEVER prefill an assistant turn.
  * Read the API key from ``ANTHROPIC_API_KEY`` at CALL time, not import time —
    so the package imports cleanly with no key set (tests / CI / `import app.main`).
  * Tolerate a missing key by raising a clear ``RuntimeError`` (not crashing at
    import, and not a cryptic SDK error).

``llm_turn`` returns ``(tool_calls, assistant_text)``: the structured
``tool_use`` blocks (the ONLY thing the orchestrator acts on) and the prose the
model spoke (used ONLY for tone / display — never parsed for numbers).
"""
from __future__ import annotations

import os
from typing import Optional

from app.models import ToolCall

MODEL = "claude-opus-4-8"
MAX_TOKENS = 1024

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
- This is an educational tool, not tax advice. If asked for advice or anything off-topic, politely decline and steer back to filling out the 1040.

Keep replies short and human — usually one or two sentences."""


class LLMError(RuntimeError):
    """Raised when the LLM cannot be called (e.g. missing API key)."""


def _client():
    """Construct an Anthropic client, reading the key from env at call time."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError(
            "ANTHROPIC_API_KEY is not set. The LLM turn cannot run without it. "
            "Set the environment variable (locally or via the Cloud Run secret) "
            "and retry."
        )
    try:
        import anthropic  # imported lazily so the package imports without the SDK
    except ImportError as exc:  # pragma: no cover - dependency always present in prod
        raise LLMError(
            "The 'anthropic' package is not installed. Add it to requirements.txt."
        ) from exc
    return anthropic.Anthropic(api_key=api_key)


def llm_turn(
    system: str,
    messages: list[dict],
    tools: list[dict],
) -> tuple[list[ToolCall], str]:
    """Run one tool-use turn.

    Args:
        system: the system prompt (tone rules). Caller passes ``SYSTEM_PROMPT``.
        messages: anthropic-format message history (list of {role, content}).
        tools: anthropic tool schemas (``app.tools.TOOLS``).

    Returns:
        ``(tool_calls, assistant_text)`` — structured ``tool_use`` blocks parsed
        into ``ToolCall`` objects, plus the concatenated assistant prose.

    NOTE: we deliberately do NOT pass ``temperature`` and never prefill an
    assistant message. The model decides which tools to call; the orchestrator
    acts only on ``tool_calls`` and uses ``assistant_text`` for display only.
    """
    client = _client()

    kwargs: dict = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
    # No temperature. No assistant prefill. (claude-opus-4-8 contract.)

    resp = client.messages.create(**kwargs)

    tool_calls: list[ToolCall] = []
    text_parts: list[str] = []
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    name=block.name,
                    args=dict(block.input or {}),
                    # Store the SDK's tool_use id so the orchestrator can return
                    # a matching tool_result on the follow-up turn.
                    result=None,
                    ok=True,
                )
            )
            # Stash the tool_use id on the args under a private key the
            # orchestrator strips before dispatch (keeps ToolCall schema clean).
            tool_calls[-1].args.setdefault("_tool_use_id", getattr(block, "id", ""))
        elif btype == "text":
            text_parts.append(block.text)

    assistant_text = "".join(text_parts).strip()
    return tool_calls, assistant_text


__all__ = ["llm_turn", "SYSTEM_PROMPT", "MODEL", "LLMError"]
