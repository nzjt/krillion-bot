"""``/noillirk`` slash-command group: minimal command surface for Noillirk.

Same game plumbing as :mod:`commands` but bound to the Noillirk scope —
separate results, ratings and leaderboard from Krillion.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from . import analytics
from .discord_util import (
    alert,
    board_message,
    guild_members,
    guild_of,
    info,
    member_gate,
    reply,
    resolve_puzzle,
    send_plot,
)
from .models import GLOBAL_GUILD_ID, Player
from .rating import STARTING_RATING
from .formatting import final_table, live_table, ratings_table
from .parser import NOILLIRK_SCOPE
from .plot_stats import stats_plot

if TYPE_CHECKING:
    from .bot import KrillionBot

SCOPE = NOILLIRK_SCOPE
GAME = "Noillirk"


def register(bot: KrillionBot) -> None:
    service = bot.service
    storage = service.storage
    calendar = service.calendar_for(SCOPE)
    group = app_commands.Group(
        name="noillirk",
        description="Noillirk daily puzzle: results, ratings and stats.",
        guild_only=True,
    )

    def players_view(rscope: int, uids) -> dict[int, Player]:
        """The guild's rated players plus 1200-placeholders for members who
        haven't finished a rated day in this server yet."""
        out = {p.user_id: p for p in storage.players(rscope, uids)}
        names = {
            p.user_id: p.display_name for p in storage.players(SCOPE, uids)
        }
        for uid in uids:
            if uid not in out:
                out[uid] = Player(
                    guild_id=rscope,
                    user_id=uid,
                    display_name=names.get(uid, f"<@{uid}>"),
                    rating=STARTING_RATING,
                )
        return out

    def today() -> date:
        return calendar.date_for(calendar.current(bot.now()))

    @group.command(description="Noillirk leaderboard — live today, or the final table for a past day.")
    @app_commands.describe(puzzle="Puzzle number", date="Day as YYYY-MM-DD")
    async def leaderboard(
        interaction: discord.Interaction, puzzle: int | None = None, date: str | None = None
    ) -> None:
        rscope = service.rating_scope(SCOPE, guild_of(interaction))
        now = bot.now()
        n, error = resolve_puzzle(calendar, now, puzzle, date)
        if n is None:
            await reply(interaction, alert(error or "Bad puzzle."), ephemeral=True)
            return
        await interaction.response.defer()
        all_res = storage.results_for(SCOPE, n)
        mem = await guild_members(getattr(interaction, "guild", None), {r.user_id for r in all_res})
        gres = [
            r
            for r in all_res
            if (mem is None or r.user_id in mem)
            and not storage.is_banned(rscope, r.user_id)
        ]
        if storage.is_finalized(rscope, n):
            uids = {r.user_id for r in gres}
            entries = service.local_placements(
                [e for e in storage.history_for(rscope, n) if e.user_id in uids]
            )
            players = players_view(rscope, [e.user_id for e in entries])
            tiers = {r.user_id: r.tiers for r in gres}
            board = final_table(
                n, calendar.date_for(n), entries, players, tiers, game=GAME
            )
            await interaction.followup.send(
                **board_message(board, f"noillirk-{n}.png")
            )
            return
        results = gres
        if not results:
            await interaction.followup.send(
                embed=info(f"No results for Noillirk #{n} yet.")
            )
            return
        players = players_view(rscope, [r.user_id for r in results])
        reset_unix = (
            int((calendar.end(calendar.current(now)) + service.grace).timestamp())
            if n == calendar.current(now)
            else None
        )
        outcome = service.rate_day(rscope, n, results)
        board = live_table(
            n,
            results,
            players,
            deltas=outcome.deltas,
            performances=outcome.performances,
            reset_unix=reset_unix,
            game=GAME,
        )
        await interaction.followup.send(
            **board_message(board, f"noillirk-{n}-live.png")
        )

    @group.command(description="Noillirk ratings, strongest first.")
    @app_commands.describe(inactive="Show divers inactive for 30+ days")
    async def ratings(interaction: discord.Interaction, inactive: bool = False) -> None:
        rscope = service.rating_scope(SCOPE, guild_of(interaction))
        await interaction.response.defer()
        players = service.ranked_players(rscope, bot.now(), inactive)
        mem = await guild_members(
            getattr(interaction, "guild", None), {p.user_id for p in players}
        )
        players = [p for p in players if mem is None or p.user_id in mem]
        table = ratings_table(players, storage.games_played(rscope))
        if table is None:
            await reply(interaction, info("Nobody has played Noillirk yet."))
            return
        await interaction.followup.send(
            **board_message(table, "noillirk-ratings.png")
        )

    @group.command(description="Noillirk stats for you or another climber.")
    @app_commands.describe(member="Whose stats (default: you)")
    async def stats(interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        rscope = service.rating_scope(SCOPE, guild_of(interaction))
        target = member or interaction.user
        if not await member_gate(interaction, target):
            return
        player = storage.get_player(rscope, target.id)
        rows = storage.results_for_user(SCOPE, target.id)
        if not rows:
            await reply(
                interaction,
                alert(f"`{target.display_name}` hasn't shared a Noillirk result yet."),
                ephemeral=True,
            )
            return
        summary = analytics.summarize(
            target.id,
            storage.results_between(SCOPE),
            storage.history_for_user(rscope, target.id),
            calendar,
            calendar.current(bot.now()),
        )
        await send_plot(
            interaction,
            "noillirk-stats.png",
            stats_plot,
            player.display_name if player else target.display_name,
            summary,
            rows,
            calendar,
            today(),
        )

    bot.tree.add_command(group)
