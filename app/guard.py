"""Customer-facing output guard: plain text, Kakao length limit, no leaked secrets or prompt."""

from __future__ import annotations

import re
from collections.abc import Iterable

SAFE_REPLY = "그 내용은 안내하는 항목이 아니에요. 문의하신 내용을 알려 주시면 도와드릴게요."

MAX_REPLY_LENGTH = 500
_TARGET_LENGTH = 480
_ELLIPSIS = "..."
_MIN_SECRET_LENGTH = 8
_PROMPT_OVERLAP = 80

_LEAK_PATTERNS = (re.compile(r"aitop100_[A-Za-z0-9]{6,}"), re.compile(r"sk-ant-"))

# Pictographs, arrows, technical and geometric symbols, dingbats, variation selector and joiner.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF), (0x2190, 0x21FF), (0x2300, 0x23FF), (0x25A0, 0x27BF),
    (0x2900, 0x297F), (0x2B00, 0x2BFF), (0x3030, 0x3030), (0x303D, 0x303D),
    (0xFE0F, 0xFE0F), (0x200D, 0x200D),
)
_EMOJI = re.compile("[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _EMOJI_RANGES) + "]")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_LINE_PREFIX = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|>+[ \t]?|[-*•][ \t]+|\d+[.)][ \t]+)")
_EMPHASIS = re.compile(r"\*\*|__|`")
_SPACE_RUNS = re.compile(r"[ \t]{2,}")
_BLANK_RUNS = re.compile(r"\n\s*\n+")
_SENTENCE_ENDS = (".", "?", "!", "\n")


def clean_reply(text: str, *, system_text: str, secrets: Iterable[str]) -> str:
    plain = _plain(text)
    secret_values = [s for s in secrets if s and len(s) >= _MIN_SECRET_LENGTH]
    # The raw text is checked too: markup between characters must not hide a leak from the match.
    if any(_leaks(candidate, system_text, secret_values) for candidate in (text, plain)):
        return SAFE_REPLY
    if not plain:
        return SAFE_REPLY
    return _fit(plain)


def _plain(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if _TABLE_SEPARATOR.match(line) and "-" in line:
            continue
        line = _LINE_PREFIX.sub("", line)
        if "|" in line:
            line = " ".join(cell.strip() for cell in line.strip().strip("|").split("|") if cell.strip())
        lines.append(_SPACE_RUNS.sub(" ", _EMPHASIS.sub("", _EMOJI.sub("", line))).rstrip())
    return _BLANK_RUNS.sub("\n", "\n".join(lines)).strip()


def _leaks(text: str, system_text: str, secret_values: list[str]) -> bool:
    return (
        any(value in text for value in secret_values)
        or any(pattern.search(text) for pattern in _LEAK_PATTERNS)
        or _overlaps(text, system_text, _PROMPT_OVERLAP)
    )


def _overlaps(text: str, reference: str, length: int) -> bool:
    """True when ``text`` shares a run of ``length`` characters with ``reference``.

    A shared run of ``length`` contains one of the half-length windows taken at half-length
    strides, so only those are searched; each hit is then confirmed at full length.
    """
    half = length // 2
    if len(text) < length or len(reference) < length:
        return False
    for start in range(0, len(text) - half + 1, half):
        if text[start : start + half] in reference:
            first = max(0, start - half)
            last = min(len(text) - length, start + half)
            if any(text[i : i + length] in reference for i in range(first, last + 1)):
                return True
    return False


def _fit(text: str) -> str:
    if len(text) <= _TARGET_LENGTH:
        return text
    window = text[:_TARGET_LENGTH]
    end = max(window.rfind(mark) for mark in _SENTENCE_ENDS)
    if end >= 0:
        fitted = (window[:end] if window[end] == "\n" else window[: end + 1]).rstrip()
        if fitted:
            return fitted
    return window[: _TARGET_LENGTH - len(_ELLIPSIS)] + _ELLIPSIS
