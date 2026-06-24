"""Regression guard for app.llm._key().

A secret stored or pasted with a UTF-8 BOM (or stray whitespace/newline) once
corrupted the OpenRouter Authorization header in prod. _key() must normalize it.
"""
import pytest

from app import llm


def test_key_strips_bom_and_whitespace(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "﻿  sk-or-v1-abc123  \n")
    assert llm._key() == "sk-or-v1-abc123"


def test_key_strips_trailing_newline(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-xyz\n")
    assert llm._key() == "sk-or-v1-xyz"


def test_key_plain_unchanged(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-clean")
    assert llm._key() == "sk-or-v1-clean"


def test_key_missing_raises(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(llm.LLMError):
        llm._key()
