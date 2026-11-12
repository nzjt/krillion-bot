"""Parse the share text Krillion.io puts on the clipboard.

Example::

    Krillion #58 🦐
    340

    🦑🦑🦑🦑🦑🐟🫧
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ROUNDS_PER_DAY = 7
MAX_DAY_SCORE = 700

TIER_SCORES = {"🌟": 100, "🏮": 85, "🦑": 60, "🐟": 30, "🤡": 15, "🫧": 10, "⬛": 0}
TIER_EMOJI = frozenset(TIER_SCORES)

_HEADER_RE = re.compile(r"krillion\s*#\s*(\d{1,5})\b", re.IGNORECASE)
_SCORE_RE = re.compile(r"(?<![\d#])(\d{1,4})(?!\d)")


NOILLIRK_SCOPE = -1
"""Scope id for the Noillirk game (Krillion is ``GLOBAL_GUILD_ID`` = 0);
negative so it can never collide with a real or test guild id."""

NOILLIRK_GAME = "Noillirk"
SQUARE_SCORES = {"🟨": 100, "🟥": 64, "🟧": 32, "🟩": 16, "🟦": 8, "⬜": 4, "⬛": 0}
NOILLIRK_URL = "https://playdaily.org/noillirk"
NOILLIRK_NAME_RE = re.compile(r"noillirk", re.IGNORECASE)
_NOILLIRK_NUMBER_RE = re.compile(r"\bno\.\s*(\d{1,5})\b", re.IGNORECASE)
_NOILLIRK_SQUARES_RE = re.compile(r"(?<![🟨🟥🟧🟩🟦⬜⬛])([🟨🟥🟧🟩🟦⬜⬛]{7})(?![🟨🟥🟧🟩🟦⬜⬛])")


@dataclass(frozen=True)
class ParsedResult:
    puzzle_number: int
    score: int
    tiers: str  # the emoji row, "" if absent


def _strip_markdown(line: str) -> str:
    return line.replace("*", "").replace("_", "").replace("`", "").replace(">", "").strip()


def _emoji_row(line: str) -> str:
    chars = [c for c in line if c in TIER_EMOJI]
    return "".join(chars) if chars else ""


def parse_result(text: str) -> ParsedResult | None:
    """Return the parsed result or ``None`` if ``text`` is not a Krillion share."""
    lines = [_strip_markdown(line) for line in text.splitlines()]
    for idx, line in enumerate(lines):
        m = _HEADER_RE.search(line)
        if not m:
            continue
        puzzle_number = int(m.group(1))
        score: int | None = None
        tiers = ""
        for follow in lines[idx + 1 : idx + 6]:
            if not follow:
                continue
            if score is None:
                sm = _SCORE_RE.search(follow)
                if sm:
                    score = int(sm.group(1))
                    continue
                return None
            row = _emoji_row(follow)
            if row:
                tiers = row
                break
        if (
            score is None
            or score < 0
            or score > MAX_DAY_SCORE
            or len(tiers) != ROUNDS_PER_DAY
            or sum(TIER_SCORES[c] for c in tiers) != score
        ):
            return None
        return ParsedResult(puzzle_number=puzzle_number, score=score, tiers=tiers)
    return None


def parse_noillirk(text: str) -> ParsedResult | None:
    """Extract a Noillirk share from its name, puzzle number, square run and URL.

    Layout and printed height are ignored. The service checks the extracted
    puzzle number against the current UTC puzzle before recording a submission.
    """
    if NOILLIRK_URL not in text:
        return None
    if NOILLIRK_NAME_RE.search(text.replace(NOILLIRK_URL, "")) is None:
        return None
    number = _NOILLIRK_NUMBER_RE.search(text)
    squares = _NOILLIRK_SQUARES_RE.search(text)
    if number is None or squares is None:
        return None
    tiers = squares.group(1)
    return ParsedResult(
        puzzle_number=int(number.group(1)),
        score=sum(SQUARE_SCORES[c] for c in tiers),
        tiers=tiers,
    )
