"""Optional LLM layer. Decisions with money attached are made by deterministic,
tested code; the LLM drafts copy, summarizes, and explains. Everything works
without an API key (LLM features are skipped)."""

from __future__ import annotations

import os


def available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def complete(system: str, prompt: str, model: str, max_tokens: int = 1500) -> str | None:
    if not available():
        return None
    import anthropic

    client = anthropic.Anthropic(max_retries=4, timeout=120)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
