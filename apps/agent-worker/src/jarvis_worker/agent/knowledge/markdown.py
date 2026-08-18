"""Obsidian Markdown normalization at the Knowledge application boundary."""

from __future__ import annotations

import re

_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


def normalize_obsidian_markdown(content: str) -> str:
    """Normalize supported LaTeX delimiters without touching code or malformed pairs.

    Obsidian's native MathJax dialect uses ``$...$`` and ``$$...$$``. Models also
    commonly emit ``\\(...\\)`` and ``\\[...\\]``. Only balanced delimiter pairs
    outside fenced and inline code are rewritten.
    """

    protected = _protected_code_positions(content)
    openers: dict[str, list[int]] = {"(": [], "[": []}
    replacements: dict[int, str] = {}

    index = 0
    while index + 1 < len(content):
        if protected[index]:
            if index == 0 or not protected[index - 1]:
                openers = {"(": [], "[": []}
            index += 1
            continue
        if (
            content[index] == "\\"
            and not protected[index + 1]
            and _is_unescaped_backslash(content, index)
        ):
            marker = content[index + 1]
            if marker in openers:
                openers[marker].append(index)
                index += 2
                continue
            opener = "(" if marker == ")" else "[" if marker == "]" else None
            if opener is not None and openers[opener]:
                opening_index = openers[opener].pop()
                replacement = "$" if opener == "(" else "$$"
                replacements[opening_index] = replacement
                replacements[index] = replacement
                index += 2
                continue
        index += 1

    if not replacements:
        return content

    output: list[str] = []
    index = 0
    while index < len(content):
        replacement = replacements.get(index)
        if replacement is not None:
            output.append(replacement)
            index += 2
        else:
            output.append(content[index])
            index += 1
    return "".join(output)


def _protected_code_positions(content: str) -> list[bool]:
    protected = [False] * len(content)
    fence_char: str | None = None
    fence_length = 0
    offset = 0

    for line in content.splitlines(keepends=True):
        line_without_ending = line.rstrip("\r\n")
        opening = _FENCE_OPEN.match(line_without_ending)
        if fence_char is not None:
            _mark(protected, offset, offset + len(line))
            if _is_closing_fence(line_without_ending, fence_char, fence_length):
                fence_char = None
                fence_length = 0
        elif opening is not None:
            marker = opening.group(1)
            fence_char = marker[0]
            fence_length = len(marker)
            _mark(protected, offset, offset + len(line))
        offset += len(line)

    _mark_inline_code(protected, content)
    return protected


def _is_closing_fence(line: str, fence_char: str, minimum_length: int) -> bool:
    pattern = rf"^[ \t]{{0,3}}{re.escape(fence_char)}{{{minimum_length},}}[ \t]*$"
    return re.fullmatch(pattern, line) is not None


def _mark_inline_code(protected: list[bool], content: str) -> None:
    index = 0
    while index < len(content):
        if protected[index]:
            index += 1
            continue
        if content[index] != "`" or not _is_unescaped_character(content, index):
            index += 1
            continue
        run_end = index + 1
        while run_end < len(content) and content[run_end] == "`":
            run_end += 1
        closing = _find_closing_backtick_run(protected, content, run_end, run_end - index)
        if closing < 0:
            index = run_end
            continue
        closing_end = closing + (run_end - index)
        _mark(protected, index, closing_end)
        index = closing_end


def _find_closing_backtick_run(
    protected: list[bool], content: str, start: int, expected_length: int
) -> int:
    index = start
    while index < len(content):
        if protected[index]:
            return -1
        if content[index] != "`" or not _is_unescaped_character(content, index):
            index += 1
            continue
        run_end = index + 1
        while run_end < len(content) and content[run_end] == "`":
            run_end += 1
        if run_end - index == expected_length:
            return index
        index = run_end
    return -1


def _mark(protected: list[bool], start: int, end: int) -> None:
    protected[start:end] = [True] * (end - start)


def _is_unescaped_backslash(content: str, index: int) -> bool:
    return _is_unescaped_character(content, index)


def _is_unescaped_character(content: str, index: int) -> bool:
    preceding = 0
    cursor = index - 1
    while cursor >= 0 and content[cursor] == "\\":
        preceding += 1
        cursor -= 1
    return preceding % 2 == 0
