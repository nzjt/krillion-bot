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
from .storage import Storage

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
        replayed = self.service.migrate_ratings()
        if replayed:
            log.info("Replayed ratings for %d guild(s) with the current engine", replayed)
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
        scope = NOILLIRK_SCOPE if NOILLIRK_HEADER_RE.search(message.content) else GLOBAL_GUILD_ID
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

    async def _finalize_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for day in self.service.finalize_due(_now()):
                    await self._announce(day)
            except Exception:
                log.exception("Finalization failed")
            now = _now()
            wake = min(self.service.next_finalize_at(now), now + _POLL_CAP)
            await asyncio.sleep(max(1.0, (wake - now).total_seconds() + 1))

    async def _announce(self, day: FinalizedDay) -> None:
        results = self.service.storage.results_for(day.guild_id, day.puzzle_number)
        tiers = {r.user_id: r.tiers for r in results}
        game = "Noillirk" if day.guild_id == NOILLIRK_SCOPE else "Krillion"
        puzzle_date = self.service.calendar_for(day.guild_id).date_for(day.puzzle_number)
        # One board per source server: global rating, but each guild only sees
        # results shared in its own channels.
        by_guild: dict[int, list] = {}
        for r in results:
            by_guild.setdefault(r.source_guild_id, []).append(r)
        posted = 0
        for src, gres in by_guild.items():
            if self.config.leaderboard_channel_id is not None:
                channel_ids = (self.config.leaderboard_channel_id,)
            else:
                counts: dict[int, int] = {}
                for r in gres:
                    counts[r.channel_id] = counts.get(r.channel_id, 0) + 1
                channel_ids = (max(counts, key=lambda c: counts[c]),)
            guild = self.get_guild(src)
            mem = await guild_members(guild, {e.user_id for e in day.entries})
            uids = (
                {r.user_id for r in gres}
                if mem is None
                else {e.user_id for e in day.entries if e.user_id in mem}
            )
            entries = self.service.local_placements([e for e in day.entries if e.user_id in uids])
            board = final_table(
                day.puzzle_number,
                puzzle_date,
                entries,
                {u: day.players[u] for u in uids if u in day.players},
                {u: tiers[u] for u in uids},
                None,
                game=game,
            )
            for channel_id in channel_ids:
                channel = await self._messageable(channel_id)
                if channel is None:
                    continue
                # discord.File streams are single-use; rebuild per channel.
                await channel.send(
                    **board_message(board, f"{game.lower()}-{day.puzzle_number}.png")
                )
                posted += 1
        log.info("Posted %s #%d results to %d channel(s)", game, day.puzzle_number, posted)

    # -- weekly recap ----------------------------------------------------

    def week_recap(
        self,
        guild_id: int,
        anchor: date,
        today: date,
        source_guild: int | None = None,
    ) -> WeekRecap:
        storage = self.service.storage
        calendar = self.service.calendar
        low = calendar.number_for_date(anchor - timedelta(days=anchor.weekday() + 7))
        high = calendar.number_for_date(anchor + timedelta(days=6 - anchor.weekday()))
        return build_week_recap(
            [
                r
                for r in storage.results_between(guild_id, low, high)
                if source_guild is None or r.user_id in source_guild
            ],
            storage.history_between(guild_id, low, high),
            calendar,
            anchor,
            today,
        )

    async def week_image(
        self, guild_id: int, recap: WeekRecap, viewer: int | None = None
    ) -> discord.File:
        """The recap dashboard, names coloured by each diver's current rank."""
        players = self.service.storage.players(guild_id)
        names = {p.user_id: p.display_name for p in players}
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
    )
    return KrillionBot(config, service)
