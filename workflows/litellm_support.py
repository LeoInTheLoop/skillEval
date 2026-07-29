"""Shared LiteLLM helpers for user-facing CLI flows.

We suppress LiteLLM's support banner and debug chatter on failures so the
useful remediation from skillEval remains visible.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from typing import Any


def quiet_completion(litellm: Any, **kwargs: Any):
    """Call LiteLLM while hiding its repeated support banner on failures."""
    marker = object()
    previous = getattr(litellm, "suppress_debug_info", marker)
    litellm.suppress_debug_info = True
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return litellm.completion(**kwargs)
    finally:
        if previous is marker:
            try:
                delattr(litellm, "suppress_debug_info")
            except AttributeError:
                pass
        else:
            litellm.suppress_debug_info = previous
