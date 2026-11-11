"""Row types shared by the storage layer and its consumers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

GLOBAL_GUILD_ID = 0
"""Single shared scope: results posted in any server feed one global leaderboard."""


@dataclass(frozen=True)
class Result:
    guild_id: int
    puzzle_number: int
    user_id: int
    score: int
    tiers: str
    channel_id: int
    message_id: int | None
    submitted_at: datetime
    source_guild_id: int = 0
    """Real Discord server the share was posted in; boards are filtered by it."""


@dataclass(frozen=True)
class Player:
    guild_id: int
    user_id: int
    display_name: str
    rating: float


@dataclass(frozen=True)
class RatingEntry:
    puzzle_number: int
    user_id: int
    score: int
    placement: int
    rating_before: float
    rating_after: float
    performance: float | None = None
    """Rating at which this finish would have been par; ``None`` on a solo day."""

    @property
    def delta(self) -> float:
        return self.rating_after - self.rating_before


@dataclass(frozen=True)
class PlayerStats:
    games: int
    wins: int
    best_score: int | None
    average_score: float | None
