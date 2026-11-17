"""Shared conservative code-fence boundaries for outgoing chat text.

Recognize backtick and tilde runs, including incomplete model output. A closing
fence must use the same character and at least the opening run's length on its
own line. Backtick openers may follow prose/inline planning markers for
compatibility with model output; tilde openers are recognized only at the
start of a physical line (0-3 leading spaces), because ``~~~``/``~~~~`` are
common tone marks in ordinary chat and must not swallow the rest of a reply
(2026-11-17 PR#89 review). This protects code, rather than implementing
Markdown.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

# Backtick runs are recognized anywhere (existing model-output compatibility).
# Tilde runs only at a physical line start with 0-3 spaces of indent; the
# fence character/length must come from the captured run, never from the
# optional indent, so the groups below carry the real run.
_OPEN_FENCE_RE = re.compile(r"(`{3,})|^[ ]{0,3}(~{3,})", re.MULTILINE)


def fenced_parts(text: str) -> Iterator[tuple[bool, str]]:
    """Yield (is_code, content) without changing any input characters."""
    position = 0
    while match := _OPEN_FENCE_RE.search(text, position):
        if match.start() > position:
            yield False, text[position:match.start()]
        fence = match.group(1) or match.group(2)
        closing = re.compile(
            rf"(?m)^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*\r?$")
        end_match = closing.search(text, match.end())
        end = end_match.end() if end_match else len(text)
        yield True, text[match.start():end]
        position = end
    if position < len(text):
        yield False, text[position:]


def has_code_fence(text: str) -> bool:
    return _OPEN_FENCE_RE.search(text) is not None


def map_outside_fences(text: str, fn: Callable[[str], str]) -> str:
    return "".join(part if code else fn(part) for code, part in fenced_parts(text))


def split_outside_fences(text: str, separator: re.Pattern[str]) -> list[str]:
    """Split outside code, preserving each complete code region in one part."""
    result = [""]
    for code, part in fenced_parts(text):
        if code:
            result[-1] += part
        else:
            chunks = separator.split(part)
            result[-1] += chunks[0]
            result.extend(chunks[1:])
    return result
