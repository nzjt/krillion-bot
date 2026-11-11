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
    info,
    reply,
    resolve_puzzle,
    send_plot,
)
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

    def today() -> date:
        return calendar.date_for(calendar.current(bot.now()))

    @group.command(
        description="Noillirk leaderboard — live today, or the final table for a past day."
    )
    @app_commands.describe(puzzle="Puzzle number", date="Day as YYYY-MM-DD")
    async def leaderboard(
        interaction: discord.Interaction, puzzle: int | None = None, date: str | None = None
    ) -> None:
        now = bot.now()
        n, error = resolve_puzzle(calendar, now, puzzle, date)
        if n is None:
            await reply(interaction, alert(error or "Bad puzzle."), ephemeral=True)
            return
        await interaction.response.defer()
        all_res = storage.results_for(SCOPE, n)
        mem = await guild_members(getattr(interaction, "guild", None), {r.user_id for r in all_res})
        gres = [r for r in all_res if mem is None or r.user_id in mem]
        if storage.is_finalized(SCOPE, n):
            uids = {r.user_id for r in gres}
            entries = service.local_placements(
                [e for e in storage.history_for(SCOPE, n) if e.user_id in uids]
            )
            players = {p.user_id: p for p in storage.players(SCOPE, [e.user_id for e in entries])}
            tiers = {r.user_id: r.tiers for r in gres}
            board = final_table(n, calendar.date_for(n), entries, players, tiers, game=GAME)
            await interaction.followup.send(**board_message(board, f"noillirk-{n}.png"))
            return
        results = gres
        if not results:
            await interaction.followup.send(embed=info(f"No results for Noillirk #{n} yet."))
            return
        players = {p.user_id: p for p in storage.players(SCOPE, [r.user_id for r in results])}
        reset_unix = (
            int((calendar.end(n) + service.grace).timestamp())
            if n == calendar.current(now)
            else None
        )
        outcome = service.rate_day(SCOPE, n, results)
        board = live_table(
            n,
            results,
            players,
            deltas=outcome.deltas,
            performances=outcome.performances,
            reset_unix=reset_unix,
            game=GAME,
        )
        await interaction.followup.send(**board_message(board, f"noillirk-{n}-live.png"))

    @group.command(description="Noillirk ratings, strongest first.")
    @app_commands.describe(inactive="Show divers inactive for 30+ days")
    async def ratings(interaction: discord.Interaction, inactive: bool = False) -> None:
        players = service.ranked_players(SCOPE, bot.now(), inactive)
        table = ratings_table(players, storage.games_played(SCOPE))
        if table is None:
            await reply(interaction, info("Nobody has played Noillirk yet."))
            return
        await interaction.response.send_message(**board_message(table, "noillirk-ratings.png"))

    @group.command(description="Noillirk stats for you or another climber.")
    @app_commands.describe(member="Whose stats (default: you)")
    async def stats(interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        player = storage.get_player(SCOPE, target.id)
        rows = storage.results_for_user(SCOPE, target.id)
        if player is None or not rows:
            await reply(
                interaction,
                alert(f"`{target.display_name}` hasn't shared a Noillirk result yet."),
                ephemeral=True,
            )
            return
        summary = analytics.summarize(
            target.id,
            storage.results_between(SCOPE),
            storage.history_for_user(SCOPE, target.id),
            calendar,
            calendar.current(bot.now()),
        )
        await send_plot(
            interaction,
            "noillirk-stats.png",
            stats_plot,
            player.display_name,
            summary,
            rows,
            calendar,
            today(),
        )

    bot.tree.add_command(group)
