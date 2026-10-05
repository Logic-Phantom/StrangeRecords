"""Gemini 응답 텍스트에서 JSON 을 안전하게 추출한다."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


class JSONParseError(ValueError):
    pass


def _balanced_block(text: str) -> str | None:
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_json(text: str) -> Any:
    if not text or not text.strip():
        raise JSONParseError("빈 응답")
    candidates = [text.strip()]
    candidates += [m.strip() for m in _FENCE.findall(text)]
    block = _balanced_block(text)
    if block:
        candidates.append(block)

    last_error: Exception | None = None
    for candidate in candidates:
        for attempt in (candidate, _TRAILING_COMMA.sub(r"\1", candidate)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError as exc:
                last_error = exc
    raise JSONParseError(f"JSON 파싱 실패: {last_error}")
