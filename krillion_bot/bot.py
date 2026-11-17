from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

import discord
from discord import app_commands

from . import commands, commands_admin, commands_noillirk
from .analytics import WeekRecap, build_week_recap
from .config import Config
from .discord_util import board_message, guild_members, png_file, utcnow
from .formatting import final_table
from .models import GLOBAL_GUILD_ID
from .parser import NOILLIRK_HEADER_RE, NOILLIRK_SCOPE
from .plot_week import week_plot
from .puzzle import PuzzleCalendar
from .service import FinalizedDay, KrillionService, SubmitStatus
from .storage import RATING_ENGINE, Storage

log = logging.getLogger(__name__)

_POLL_CAP = timedelta(minutes=15)


def _now() -> datetime:
    return utcnow()


class KrillionBot(discord.Client):
    def __init__(self, config: Config, service: KrillionService) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)
        self.config = config
        self.service = service
        self.tree = app_commands.CommandTree(self)
        group = commands.register(self)
        commands_admin.register(self, group)
        commands_noillirk.register(self)
        self._scheduler: asyncio.Task[None] | None = None

    @staticmethod
    def now() -> datetime:
        return _now()

    # -- lifecycle -------------------------------------------------------

    async def setup_hook(self) -> None:
        await self.tree.sync()
        self._scheduler = asyncio.create_task(self._finalize_loop(), name="finalize-loop")

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info(
            "Logged in as %s (%s); current puzzle #%d",
            self.user,
            self.user.id,
            self.service.calendar.current(_now()),
        )

    async def close(self) -> None:
        if self._scheduler is not None:
            self._scheduler.cancel()
        await super().close()

    def admin_ids(self, guild_id: int) -> set[int]:
        return set(self.config.admin_user_ids) | self.service.storage.admins(guild_id)

    def is_admin(self, user: discord.abc.User, guild_id: int) -> bool:
        return user.id in self.admin_ids(guild_id)

    async def _messageable(self, channel_id: int) -> discord.abc.Messageable | None:
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                log.error("Cannot find channel %s", channel_id)
                return None
        if not isinstance(channel, discord.abc.Messageable):
            log.error("Channel %s is not messageable", channel_id)
            return None
        return channel

    # -- results ingestion ----------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        wanted = self.config.results_channel_id
        if wanted is not None and message.channel.id != wanted:
            return
        scope = (
            NOILLIRK_SCOPE
            if NOILLIRK_HEADER_RE.search(message.content)
            else GLOBAL_GUILD_ID
        )
        game = "Noillirk" if scope == NOILLIRK_SCOPE else "Krillion"
        emo = "🚀" if scope == NOILLIRK_SCOPE else "🦐"
        outcome = self.service.submit(
            guild_id=scope,
            user_id=message.author.id,
            display_name=message.author.display_name,
            text=message.content,
            channel_id=message.channel.id,
            message_id=message.id,
            now=message.created_at,
            source_guild_id=message.guild.id,
        )
        if outcome.status is SubmitStatus.NOT_A_RESULT:
            if "krillion" in message.content.lower():
                log.info(
                    "Message %s in %s mentions Krillion but did not parse as a result",
                    message.id,
                    message.channel.id,
                )
            return
        assert outcome.parsed is not None
        n = outcome.parsed.puzzle_number
        log.info(
            "%s #%d from %s in guild %s: %s (%s)",
            game,
            n,
            message.author.id,
            message.guild.id,
            outcome.status.value,
            outcome.parsed.score,
        )
        try:
            if outcome.status is SubmitStatus.ACCEPTED:
                await message.add_reaction(emo)
                await message.reply(
                    f"Received {game} #{n} score from **{message.author.display_name}**: "
                    f"{outcome.parsed.score} {emo}",
                    mention_author=False,
                )
            elif outcome.status is SubmitStatus.DUPLICATE:
                assert outcome.existing is not None
                await message.add_reaction("⚠️")
                await message.reply(
                    f"Already have your {game} #{n} result ({outcome.existing.score}) — "
                    "keeping the first one.",
                    mention_author=False,
                )
            elif outcome.status is SubmitStatus.TOO_LATE:
                await message.add_reaction("⏰")
                await message.reply(
                    f"{game} #{n} has already closed — today's puzzle is "
                    f"#{outcome.current_puzzle}.",
                    mention_author=False,
                )
            elif outcome.status is SubmitStatus.NOT_YET:
                await message.add_reaction("❓")
                await message.reply(
                    f"{game} #{n} isn't out yet — today's puzzle is #{outcome.current_puzzle}.",
                    mention_author=False,
                )
            elif outcome.status is SubmitStatus.BANNED:
                await message.add_reaction("🚫")
        except discord.HTTPException:
            log.exception("Failed to respond to submission in %s", message.channel.id)

    # -- daily close -----------------------------------------------------

    async def _member_uids(self, guild: discord.Guild) -> set[int] | None:
        """Everyone in ``guild`` — from the member cache, or per-user lookups
        over the players who actually posted, when the cache is empty."""
        members = {m.id for m in guild.members}
        if members:
            return members
        return await guild_members(guild, self.service.storage.result_user_ids())

    async def _migrate_scopes(self) -> None:
        """One-time: rebuild per-server rating data from the global results.

        Older data lives under the game scopes; ratings are per-server now, so
        each guild replays its member-filtered history once. Also replays when
        the rating engine version changed."""
        storage = self.service.storage
        legacy = storage.legacy_rating_rows()
        stale = self.service.engine_stale()
        if not legacy and not stale:
            return
        log.info(
            "Rebuilding per-server ratings (legacy rows=%s, engine stale=%s)",
            legacy,
            stale,
        )
        for guild in self.guilds:
            members = await self._member_uids(guild)
            for game_scope in (GLOBAL_GUILD_ID, NOILLIRK_SCOPE):
                self.service.replay_guild(game_scope, guild.id, members, _now())
        storage.clear_scope_ratings(GLOBAL_GUILD_ID)
        storage.clear_scope_ratings(NOILLIRK_SCOPE)
        storage.set_meta("rating_engine", RATING_ENGINE)

    async def replay_guilds(
        self,
        uids: set[int] | None = None,
        scopes: tuple[int, ...] = (GLOBAL_GUILD_ID, NOILLIRK_SCOPE),
    ) -> int:
        """Re-rate every guild whose members intersect ``uids`` (all if ``None``)."""
        total = 0
        for guild in self.guilds:
            members = await self._member_uids(guild)
            if uids is not None and members is not None and not (uids & members):
                continue
            for game_scope in scopes:
                total += self.service.replay_guild(
                    game_scope, guild.id, members, _now()
                )
        return total

    async def _finalize_due(
        self, now: datetime
    ) -> list[tuple[discord.Guild, FinalizedDay]]:
        """Close every due game-scope day for each guild, member-filtered."""
        done: list[tuple[discord.Guild, FinalizedDay]] = []
        storage = self.service.storage
        for game_scope, n in self.service.due_result_puzzles(now):
            uids = {r.user_id for r in storage.results_for(game_scope, n)}
            for guild in self.guilds:
                rscope = self.service.rating_scope(game_scope, guild.id)
                if storage.is_finalized(rscope, n):
                    continue
                mem = await guild_members(guild, uids)
                day = self.service.finalize_guild_day(
                    game_scope, guild.id, n, mem, now
                )
                if day is not None:
                    done.append((guild, day))
        return done

    async def _finalize_loop(self) -> None:
        await self.wait_until_ready()
        await self._migrate_scopes()
        while not self.is_closed():
            try:
                for guild, day in await self._finalize_due(_now()):
                    await self._announce(guild, day)
            except Exception:
                log.exception("Finalization failed")
            now = _now()
            wake = min(self.service.next_finalize_at(now), now + _POLL_CAP)
            await asyncio.sleep(max(1.0, (wake - now).total_seconds() + 1))

    async def _board_channel(
        self, guild: discord.Guild, day: FinalizedDay
    ) -> discord.abc.Messageable | None:
        """Where this guild's board posts: busiest channel inside the guild
        where rated results were shared, else the configured leaderboard
        channel (if it belongs to the guild), else the system channel."""
        for channel_id in day.channel_ids:
            channel = await self._messageable(channel_id)
            cguild = getattr(channel, "guild", None)
            if cguild is not None and cguild.id == guild.id:
                return channel
        lb = self.config.leaderboard_channel_id
        if lb is not None:
            channel = await self._messageable(lb)
            cguild = getattr(channel, "guild", None) if channel is not None else None
            if cguild is not None and cguild.id == guild.id:
                return channel
        if guild.system_channel is not None:
            return guild.system_channel
        return next(iter(guild.text_channels), None)

    async def _announce(self, guild: discord.Guild, day: FinalizedDay) -> None:
        game_scope = self.service.game_of(day.guild_id)
        game = "Noillirk" if game_scope == NOILLIRK_SCOPE else "Krillion"
        puzzle_date = self.service.calendar_for(day.guild_id).date_for(day.puzzle_number)
        uids = {e.user_id for e in day.entries}
        tiers = {
            r.user_id: r.tiers
            for r in self.service.storage.results_for(game_scope, day.puzzle_number)
            if r.user_id in uids
        }
        channel = await self._board_channel(guild, day)
        if channel is None:
            log.warning(
                "No channel to post %s #%d results in guild %s",
                game,
                day.puzzle_number,
                guild.id,
            )
            return
        board = final_table(
            day.puzzle_number,
            puzzle_date,
            day.entries,
            day.players,
            tiers,
            None,
            game=game,
        )
        await channel.send(
            **board_message(board, f"{game.lower()}-{day.puzzle_number}.png")
        )
        log.info(
            "Posted %s #%d results to %s #%s",
            game,
            day.puzzle_number,
            guild.id,
            channel.id,
        )

    # -- weekly recap ----------------------------------------------------

    def week_recap(
        self,
        game_scope: int,
        rscope: int,
        anchor: date,
        today: date,
        members: set[int] | None = None,
    ) -> WeekRecap:
        storage = self.service.storage
        calendar = self.service.calendar
        low = calendar.number_for_date(anchor - timedelta(days=anchor.weekday() + 7))
        high = calendar.number_for_date(anchor + timedelta(days=6 - anchor.weekday()))
        return build_week_recap(
            [
                r
                for r in storage.results_between(game_scope, low, high)
                if members is None or r.user_id in members
            ],
            storage.history_between(rscope, low, high),
            calendar,
            anchor,
            today,
        )

    async def week_image(
        self, game_scope: int, rscope: int, recap: WeekRecap, viewer: int | None = None
    ) -> discord.File:
        """The recap dashboard, names coloured by each diver's current rank."""
        players = self.service.storage.players(rscope)
        names = {
            p.user_id: p.display_name for p in self.service.storage.players(game_scope)
        }
        names.update({p.user_id: p.display_name for p in players})
        ratings = {p.user_id: p.rating for p in players}
        png = await asyncio.to_thread(week_plot, recap, names, ratings, viewer=viewer)
        return png_file(png, f"krillion-week-{recap.start}.png")


def build(config: Config) -> KrillionBot:
    storage = Storage(config.database_path)
    calendar = PuzzleCalendar(tz=config.puzzle_tz, epoch=config.epoch_date)
    service = KrillionService(
        storage,
        calendar,
        grace=timedelta(minutes=config.late_grace_minutes),
        damping=config.rating_damping,
        decay_base=config.decay_base,
        decay_max=config.decay_max,
        decay_grace=config.decay_grace,
        noillirk_epoch=config.noillirk_epoch_date,
        noillirk_tz=config.noillirk_tz,
    )
    return KrillionBot(config, service)
