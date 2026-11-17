"""Discord-agnostic game logic: accept submissions, close days, run the rating."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from .parser import (
    NOILLIRK_HEADER_RE,
    NOILLIRK_SCOPE,
    ParsedResult,
    parse_noillirk,
    parse_result,
)
from .puzzle import PuzzleCalendar
from .rating import (
    DAMPING,
    DECAY_BASE,
    DECAY_GRACE,
    DECAY_MAX,
    STARTING_RATING,
    DayOutcome,
    close_day,
    placements,
)
from .models import GLOBAL_GUILD_ID
from .storage import RATING_ENGINE, Player, RatingEntry, Result, Storage


class SubmitStatus(Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    TOO_LATE = "too_late"
    NOT_YET = "not_yet"
    NOT_A_RESULT = "not_a_result"
    BANNED = "banned"


@dataclass(frozen=True)
class SubmitOutcome:
    status: SubmitStatus
    parsed: ParsedResult | None
    current_puzzle: int
    existing: Result | None = None


@dataclass(frozen=True)
class FinalizedDay:
    guild_id: int
    puzzle_number: int
    channel_ids: tuple[int, ...]
    entries: list[RatingEntry]
    players: dict[int, Player]
    decay: dict[int, float]
    """Rating lost by each absent player (shared out to the day's submitters)."""


@dataclass(frozen=True)
class Invalidation:
    removed: Result
    replayed_days: int
    """Closed days whose Elo was recomputed (0 if the puzzle was still open)."""


@dataclass(frozen=True)
class Deletion:
    removed: int
    recomputed: bool
    """Whether a closed day was touched, so every rating was rebuilt from scratch."""
    replayed_days: int


MAX_INACTIVE_DAYS = 30
"""Divers who haven't played this many puzzles drop off the ratings board."""


class KrillionService:
    def __init__(
        self,
        storage: Storage,
        calendar: PuzzleCalendar | None = None,
        *,
        grace: timedelta = timedelta(minutes=10),
        damping: float = DAMPING,
        decay_base: float = DECAY_BASE,
        decay_max: float = DECAY_MAX,
        decay_grace: int = DECAY_GRACE,
        noillirk_epoch: date | None = None,
        noillirk_tz: ZoneInfo | None = None,
    ) -> None:
        self.storage = storage
        self.calendar = calendar or PuzzleCalendar()
        self.noillirk_calendar = PuzzleCalendar(
            tz=noillirk_tz or ZoneInfo("UTC"),
            epoch=noillirk_epoch or date(2026, 9, 27),
        )
        self.grace = grace
        self.damping = damping
        self.decay_base = decay_base
        self.decay_max = decay_max
        self.decay_grace = decay_grace

    def engine_stale(self) -> bool:
        """Stored history came from an older rating engine and needs a replay."""
        return self.storage.get_meta("rating_engine") != RATING_ENGINE

    def migrate_ratings(self) -> int:
        """Replay every result scope in place if history came from an older
        engine (self-contained scopes only — per-server data is rebuilt by the
        bot once member lists are available)."""
        if not self.engine_stale():
            return 0
        guilds = self.storage.guild_ids()
        for guild_id in guilds:
            self.replay(guild_id)
        self.storage.set_meta("rating_engine", RATING_ENGINE)
        return len(guilds)

    # -- submissions -----------------------------------------------------

    @staticmethod
    def rating_scope(game_scope: int, guild_id: int) -> int:
        """Where a server's rating data for ``game_scope`` lives: the guild id
        for Krillion, its negation for Noillirk (guild ids are positive)."""
        return -guild_id if game_scope == NOILLIRK_SCOPE else guild_id

    @staticmethod
    def game_of(scope: int) -> int:
        """The game scope a rating scope belongs to."""
        return NOILLIRK_SCOPE if scope < 0 else GLOBAL_GUILD_ID

    def calendar_for(self, guild_id: int) -> PuzzleCalendar:
        return self.noillirk_calendar if guild_id < 0 else self.calendar

    def is_open(self, guild_id: int, puzzle_number: int, now: datetime) -> bool:
        """A puzzle accepts results from its start until reset + grace."""
        cal = self.calendar_for(guild_id)
        if now < cal.start(puzzle_number):
            return False
        return now < cal.end(puzzle_number) + self.grace

    def is_closed(self, guild_id: int, puzzle_number: int, now: datetime) -> bool:
        """The day's grace window has fully ended (future puzzles are not closed)."""
        cal = self.calendar_for(guild_id)
        return now >= cal.end(puzzle_number) + self.grace

    def submit(
        self,
        *,
        guild_id: int,
        user_id: int,
        display_name: str,
        text: str,
        channel_id: int,
        message_id: int | None,
        now: datetime,
        source_guild_id: int = 0,
    ) -> SubmitOutcome:
        noillirk = NOILLIRK_HEADER_RE.search(text) is not None
        scope = NOILLIRK_SCOPE if noillirk else guild_id
        cal = self.calendar_for(scope)
        current = cal.current(now)
        parsed = parse_noillirk(text) if noillirk else parse_result(text)
        if parsed is None:
            return SubmitOutcome(SubmitStatus.NOT_A_RESULT, None, current)
        return self._record(
            scope,
            user_id,
            display_name,
            parsed,
            channel_id,
            message_id,
            now,
            current,
            source_guild_id,
        )

    def _record(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        parsed: ParsedResult,
        channel_id: int,
        message_id: int | None,
        now: datetime,
        current: int,
        source_guild_id: int = 0,
    ) -> SubmitOutcome:
        n = parsed.puzzle_number
        if self.storage.is_banned(guild_id, user_id) or (
            source_guild_id
            and self.storage.is_banned(
                self.rating_scope(guild_id, source_guild_id), user_id
            )
        ):
            return SubmitOutcome(SubmitStatus.BANNED, parsed, current)
        if n > current:
            return SubmitOutcome(SubmitStatus.NOT_YET, parsed, current)
        if not self.is_open(guild_id, n, now) or self.storage.is_finalized(guild_id, n):
            return SubmitOutcome(SubmitStatus.TOO_LATE, parsed, current)
        self.storage.upsert_player(guild_id, user_id, display_name)
        result = Result(
            guild_id=guild_id,
            puzzle_number=n,
            user_id=user_id,
            score=parsed.score,
            tiers=parsed.tiers,
            channel_id=channel_id,
            message_id=message_id,
            submitted_at=now,
            source_guild_id=source_guild_id,
        )
        if not self.storage.add_result(result):
            existing = self.storage.get_result(guild_id, n, user_id)
            return SubmitOutcome(SubmitStatus.DUPLICATE, parsed, current, existing)
        return SubmitOutcome(SubmitStatus.ACCEPTED, parsed, current)

    @staticmethod
    def local_placements(entries: list[RatingEntry]) -> list[RatingEntry]:
        """Re-rank ``entries`` among themselves — per-server boards on a global rating."""
        places = placements({e.user_id: e.score for e in entries})
        return [replace(e, placement=places[e.user_id]) for e in entries]

    def ranked_players(self, guild_id: int, now: datetime, include_inactive: bool) -> list[Player]:
        """Public ratings board: by default nobody
        who has sat out the last :data:`MAX_INACTIVE_DAYS` puzzles."""
        cutoff = self.calendar_for(guild_id).current(now) - MAX_INACTIVE_DAYS
        # "Last played" follows the user's global results (the game scope);
        # self-contained scopes keep their results under the same id.
        last = self.storage.last_played(self.game_of(guild_id)) or self.storage.last_played(
            guild_id
        )
        return [
            p
            for p in self.storage.players(guild_id)
            if not self.storage.is_banned(guild_id, p.user_id)
            and (include_inactive or last.get(p.user_id, cutoff) >= cutoff)
        ]

    # -- finalization ----------------------------------------------------

    def rate_day(self, guild_id: int, puzzle_number: int, results: list[Result]) -> DayOutcome:
        """What closing ``puzzle_number`` with ``results`` does to everyone's rating."""
        players = {
            p.user_id: p for p in self.storage.players(guild_id, [r.user_id for r in results])
        }
        scores = {r.user_id: r.score for r in results}
        ratings = {
            uid: players[uid].rating if uid in players else STARTING_RATING
            for uid in scores
        }
        absent = self.storage.absentees(guild_id, puzzle_number, scores)
        return close_day(
            scores,
            ratings,
            absent,
            damping=self.damping,
            decay_base=self.decay_base,
            decay_max=self.decay_max,
            decay_grace=self.decay_grace,
        )

    def projected_deltas(
        self, guild_id: int, puzzle_number: int, results: list[Result]
    ) -> dict[int, float]:
        """Rating change each submitter would get if ``results`` closed the day now."""
        return self.rate_day(guild_id, puzzle_number, results).deltas

    def next_finalize_at(self, now: datetime) -> datetime:
        """Next instant a puzzle's grace ends, across both game calendars.

        The puzzle that just rolled over (``current - 1``) may still be inside
        its grace window, so its close must be considered too.
        """
        candidates = []
        for cal in {self.calendar, self.noillirk_calendar}:
            n = cal.current(now)
            for m in (n, n - 1):
                t = cal.end(m) + self.grace
                if t > now:
                    candidates.append(t)
        return min(candidates) if candidates else now

    def due_result_puzzles(self, now: datetime) -> list[tuple[int, int]]:
        """``(game scope, puzzle)`` pairs with results whose grace has ended."""
        return [
            (guild_id, n)
            for guild_id, n in self.storage.result_puzzles()
            if self.is_closed(guild_id, n, now)
        ]

    def finalize_guild_day(
        self,
        game_scope: int,
        guild_id: int,
        puzzle_number: int,
        member_uids: set[int] | None,
        now: datetime,
    ) -> FinalizedDay | None:
        """Close ``puzzle_number`` for one server from its members' results.

        Results are global (posted anywhere counts), but the day's round and
        the rating history it writes are per-server: only results from
        ``member_uids`` (and not banned in this server) are rated. ``member_uids``
        of ``None`` rates every result (self-contained scopes, admin replay).
        """
        rscope = (
            guild_id
            if guild_id == game_scope
            else self.rating_scope(game_scope, guild_id)
        )
        results = self.storage.results_for(game_scope, puzzle_number)
        if member_uids is not None:
            results = [r for r in results if r.user_id in member_uids]
        results = [
            r for r in results if not self.storage.is_banned(rscope, r.user_id)
        ]
        if not results or self.storage.is_finalized(rscope, puzzle_number):
            return None
        names = {
            p.user_id: p.display_name
            for p in self.storage.players(game_scope, [r.user_id for r in results])
        }
        for r in results:
            self.storage.upsert_player(
                rscope, r.user_id, names.get(r.user_id) or str(r.user_id)
            )
        players = {
            p.user_id: p for p in self.storage.players(rscope, [r.user_id for r in results])
        }
        scores = {r.user_id: r.score for r in results}
        ratings = {uid: players[uid].rating for uid in scores}
        outcome = self.rate_day(rscope, puzzle_number, results)
        places = placements(scores)
        entries = [
            RatingEntry(
                puzzle_number=puzzle_number,
                user_id=r.user_id,
                score=r.score,
                placement=places[r.user_id],
                rating_before=ratings[r.user_id],
                rating_after=ratings[r.user_id] + outcome.deltas[r.user_id],
                performance=outcome.performances[r.user_id],
            )
            for r in results
        ]
        self.storage.finalize(rscope, puzzle_number, entries, now, outcome.decay)
        # Candidate post channels: every channel the rated results were shared
        # in, busiest first; the bot picks the busiest one inside this server.
        channel_counts: dict[int, int] = {}
        for r in results:
            channel_counts[r.channel_id] = channel_counts.get(r.channel_id, 0) + 1
        channel_ids = tuple(sorted(channel_counts, key=lambda c: -channel_counts[c]))
        updated = {
            p.user_id: p for p in self.storage.players(rscope, [r.user_id for r in results])
        }
        return FinalizedDay(rscope, puzzle_number, channel_ids, entries, updated, outcome.decay)

    def finalize(
        self, guild_id: int, puzzle_number: int, now: datetime
    ) -> FinalizedDay | None:
        """Close ``puzzle_number`` treating ``guild_id`` as its own scope (all
        results under it rated together). Per-server closes use
        :meth:`finalize_guild_day`."""
        return self.finalize_guild_day(guild_id, guild_id, puzzle_number, None, now)

    # -- admin -----------------------------------------------------------

    def invalidate(self, guild_id: int, puzzle_number: int, user_id: int) -> Invalidation | None:
        """Drop a user's result. If that day was already closed, replay ratings from scratch."""
        removed = self.storage.remove_result(guild_id, puzzle_number, user_id)
        if removed is None:
            return None
        if not self.storage.is_finalized(guild_id, puzzle_number):
            return Invalidation(removed, 0)
        return Invalidation(removed, self.replay(guild_id))

    def add_manual(
        self,
        *,
        guild_id: int,
        user_id: int,
        display_name: str,
        puzzle_number: int,
        score: int,
        tiers: str,
        channel_id: int,
        now: datetime,
        source_guild_id: int = 0,
    ) -> Invalidation | None:
        """Admin insert of a result for any past or current puzzle.

        Returns ``None`` if the user already has a result for that puzzle. A closed
        day is (re)rated by replaying the guild.
        """
        self.storage.upsert_player(guild_id, user_id, display_name)
        result = Result(
            guild_id=guild_id,
            puzzle_number=puzzle_number,
            user_id=user_id,
            score=score,
            tiers=tiers,
            channel_id=channel_id,
            message_id=None,
            submitted_at=now,
            source_guild_id=source_guild_id,
        )
        if not self.storage.add_result(result):
            return None
        if self.is_open(guild_id, puzzle_number, now):
            return Invalidation(result, 0)
        return Invalidation(result, self.replay(guild_id, now))

    def import_result(
        self,
        *,
        guild_id: int,
        user_id: int,
        display_name: str,
        text: str,
        channel_id: int,
        message_id: int,
        created_at: datetime,
        source_guild_id: int = 0,
    ) -> bool:
        """Backfill a share from channel history, judged by when it was posted.

        Returns whether a new result was stored. Closed days are not rated here;
        the caller replays once the whole channel has been read.
        """
        parsed = parse_result(text)
        if parsed is None or self.storage.is_banned(guild_id, user_id):
            return False
        n = parsed.puzzle_number
        if not self.is_open(guild_id, n, created_at):
            return False
        self.storage.upsert_player(guild_id, user_id, display_name)
        return self.storage.add_result(
            Result(
                guild_id=guild_id,
                puzzle_number=n,
                user_id=user_id,
                score=parsed.score,
                tiers=parsed.tiers,
                channel_id=channel_id,
                message_id=message_id,
                submitted_at=created_at,
                source_guild_id=source_guild_id,
            )
        )

    def correct(
        self, guild_id: int, puzzle_number: int, user_id: int, score: int, tiers: str
    ) -> bool:
        """Overwrite a stored score; the caller replays once all corrections are in."""
        existing = self.storage.get_result(guild_id, puzzle_number, user_id)
        if existing is None or (existing.score, existing.tiers) == (score, tiers):
            return False
        return self.storage.update_result(guild_id, puzzle_number, user_id, score, tiers)

    def delete_puzzles(self, guild_id: int, low: int, high: int) -> Deletion:
        """Drop every result for puzzles ``low..high``, replaying ratings if any was closed."""
        closed = any(low <= n <= high for n, _ in self.storage.finalized_puzzles(guild_id))
        removed = self.storage.delete_results(guild_id, low, high)
        recomputed = bool(removed) and closed
        replayed = self.replay(guild_id) if recomputed else 0
        return Deletion(removed, recomputed, replayed)

    def replay_guild(
        self,
        game_scope: int,
        guild_id: int,
        member_uids: set[int] | None,
        now: datetime | None = None,
    ) -> int:
        """Recompute a server's rating history from the stored results, in order.

        Re-finalizes every closed day this server already rated plus (with
        ``now``) closed result days it never rated. ``member_uids`` filters
        which results count for the server; ``None`` rates everything.
        """
        rscope = (
            guild_id
            if guild_id == game_scope
            else self.rating_scope(game_scope, guild_id)
        )
        days = dict(self.storage.finalized_puzzles(rscope))
        if now is not None:
            for g, n in self.storage.result_puzzles():
                if g == game_scope and n not in days and self.is_closed(g, n, now):
                    days[n] = now
        self.storage.reset_ratings(rscope)
        return sum(
            self.finalize_guild_day(game_scope, guild_id, n, member_uids, days[n])
            is not None
            for n in sorted(days)
        )

    def replay(self, guild_id: int, now: datetime | None = None) -> int:
        """Recompute every closed day's rating in a self-contained scope."""
        return self.replay_guild(guild_id, guild_id, None, now)

    def finalize_due(self, now: datetime) -> list[FinalizedDay]:
        """Close every due game-scope day in its own scope (no member filter).

        The bot closes days per server instead; this covers self-contained
        scopes (tests, imports)."""
        done: list[FinalizedDay] = []
        for guild_id, n in self.due_result_puzzles(now):
            day = self.finalize_guild_day(guild_id, guild_id, n, None, now)
            if day is not None:
                done.append(day)
        return done
