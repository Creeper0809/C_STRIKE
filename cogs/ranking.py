from __future__ import annotations

import logging
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

try:
    import config
except ModuleNotFoundError as exc:
    if exc.name != "config":
        raise
    from .. import config


LOGGER = logging.getLogger("ops.bot.ranking")


def _api_base() -> str:
    return str(getattr(config, "SCOREBOARD_API_BASE_URL", "") or "").rstrip("/")


async def _get_json(path: str) -> dict[str, Any]:
    base = _api_base()
    if not base:
        raise RuntimeError("SCOREBOARD_API_BASE_URL is empty")
    url = f"{base}{path}"
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            if response.status >= 400:
                body = await response.text()
                raise RuntimeError(f"GET {url} failed: {response.status} {body[:300]}")
            payload = await response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"GET {url} returned non-object payload")
    return payload


async def _resolve_competition_id() -> str:
    configured = str(getattr(config, "SCOREBOARD_COMPETITION_ID", "") or "").strip()
    if configured:
        return configured

    payload = await _get_json("/active-competitions")
    competitions = payload.get("competitions")
    if not isinstance(competitions, list) or not competitions:
        raise RuntimeError("active competition not found")
    competition_id = str((competitions[0] or {}).get("id") or "").strip()
    if not competition_id:
        raise RuntimeError("active competition id is empty")
    return competition_id


def _medal(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "🏅")


def _rank_delta(value: object) -> str:
    try:
        change = int(value or 0)
    except Exception:
        change = 0
    if change > 0:
        return f" ▲{change}"
    if change < 0:
        return f" ▼{abs(change)}"
    return ""


def _num(value: object) -> str:
    try:
        return f"{float(value or 0):,.0f}"
    except Exception:
        return "0"


def _ranking_embed(payload: dict[str, Any]) -> discord.Embed:
    rankings = payload.get("rankings")
    if not isinstance(rankings, list):
        rankings = []

    round_number = payload.get("round_number") or 0
    updated_at = str(payload.get("updated_at") or "").strip()
    embed = discord.Embed(
        title="🏆 사이버공방전 | 팀 랭킹",
        description=f"🔄 라운드: `{round_number}`\n상위 20개 팀 기준 누적 순위입니다.",
        color=discord.Color.gold(),
    )

    if not rankings:
        embed.add_field(name="안내", value="현재 표시할 랭킹 데이터가 없습니다.", inline=False)
        return embed

    lines: list[str] = []
    for idx, item in enumerate(rankings[:20], start=1):
        rank = int(item.get("rank") or idx)
        team_name = str(item.get("team_name") or "-").strip()
        total = _num(item.get("total_score"))
        attack = _num(item.get("attack_score"))
        defense = _num(item.get("defense_score"))
        flags = int(item.get("flags_captured") or 0)
        lines.append(
            f"{_medal(rank)} **{rank}위 · {team_name}**{_rank_delta(item.get('rank_change'))}\n"
            f"> **총점** `{total}`\n"
            f"> ⚔️ 공격 `{attack}`  🛡️ 방어 `{defense}`  🚩 플래그 `{flags}`"
        )

    embed.add_field(name="📊 순위", value="\n\n".join(lines)[:4096], inline=False)
    if updated_at:
        embed.set_footer(text=f"업데이트: {updated_at[:19]}")
    return embed


class RankingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="랭킹", description="스코어보드 공개 API 기준 팀 랭킹을 조회합니다.")
    async def ranking(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            competition_id = await _resolve_competition_id()
            payload = await _get_json(f"/{competition_id}/rankings")
        except Exception as exc:
            LOGGER.exception("Failed to fetch scoreboard rankings")
            embed = discord.Embed(
                title="랭킹 조회 실패",
                description=f"스코어보드 랭킹을 가져오지 못했습니다.\n`{str(exc)[:900]}`",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await interaction.followup.send(embed=_ranking_embed(payload), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RankingCog(bot))
