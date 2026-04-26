from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

try:
    import config
except ModuleNotFoundError as exc:
    if exc.name != "config":
        raise
    from .. import config

try:
    from db import problem as db_problem
    from db import team as db_team
except ModuleNotFoundError as exc:
    if exc.name not in {"db", "problem", "team"}:
        raise
    from ..db import problem as db_problem
    from ..db import team as db_team


LOGGER = logging.getLogger("ops.bot.problem")


def _problem_no(problem: dict[str, Any]) -> str:
    return str(problem.get("problem_no") or "").strip().upper()


def _category(problem: dict[str, Any]) -> str:
    return str(problem.get("category") or "MISC").strip().upper()


def _difficulty(problem: dict[str, Any]) -> str:
    return str(problem.get("difficulty") or "Easy").strip().title()


def _score(problem: dict[str, Any]) -> int:
    try:
        return int(problem.get("score") or 0)
    except Exception:
        return 0


def _competition_id() -> str | None:
    value = str(getattr(config, "BOT_COMPETITION_ID", "") or "").strip()
    return value or None


def _member_role_ids(interaction: discord.Interaction) -> list[str]:
    member = interaction.user
    roles = list(getattr(member, "roles", []) or [])
    roles.sort(key=lambda role: int(getattr(role, "position", 0) or 0), reverse=True)
    return [str(role.id) for role in roles if str(getattr(role, "id", "") or "").strip()]


def _team_from_role_ids(guild_id: str, role_ids: list[str]) -> dict[str, Any] | None:
    competition_id = _competition_id()
    for role_id in role_ids:
        team = db_team.get_team_by_role(guild_id, role_id)
        if not team:
            continue
        if competition_id and str(team.get("competition_id") or "").strip() != competition_id:
            continue
        return team
    return None


def _button_url(value: object) -> str | None:
    raw_url = str(value or "").strip()
    if not raw_url:
        return None
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https", "discord"}:
        return None
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        return None
    return raw_url


def _display_link(value: object) -> str:
    link = str(value or "").strip()
    return link if link else "`NULL`"


def _display_action_link(value: object, label: str) -> str:
    link = _button_url(value)
    return f"[{label}]({link})" if link else "`NULL`"


def _chunk_lines(lines: list[str], limit: int = 1000) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        next_len = len(line) + 1
        if current and current_len + next_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += next_len
    if current:
        chunks.append("\n".join(current))
    return chunks or ["`NULL`"]


def _problem_list_embed(problems: list[dict[str, Any]]) -> discord.Embed:
    total_score = sum(_score(problem) for problem in problems)
    embed = discord.Embed(
        title="⚔️ 사이버공방전 | 문제 목록",
        description="배부 방식: `공통 배부`\n`/문제 [문제번호]`로 상세 설명과 팀별 링크를 확인하세요.",
        color=discord.Color.dark_teal(),
    )
    embed.add_field(name="공개 문제", value=f"`{len(problems)}`개", inline=True)
    embed.add_field(name="총점", value=f"`{total_score}`점", inline=True)
    if not problems:
        embed.add_field(name="안내", value="현재 공개된 문제가 없습니다.", inline=False)
        return embed

    for category in sorted({_category(problem) for problem in problems}):
        rows = [problem for problem in problems if _category(problem) == category]
        lines = [
            f"`{_problem_no(problem):<16}` `{_score(problem):>3}점` `{_difficulty(problem)}` | **{problem.get('title') or '-'}**"
            for problem in rows
        ]
        embed.add_field(name=f"{category} ({len(rows)}문제)", value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="상세 보기: /문제 [문제번호]")
    return embed


def _problem_detail_embed(problem: dict[str, Any], team_links: list[dict[str, Any]]) -> discord.Embed:
    description = str(problem.get("description") or "").strip() or "문제 설명이 없습니다."
    embed = discord.Embed(
        title=f"⚔️ 문제 정보 - [{_problem_no(problem)}] {problem.get('title') or '-'}",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.add_field(name="🔹 카테고리", value=_category(problem), inline=True)
    embed.add_field(name="🔹 난이도", value=_difficulty(problem), inline=True)
    embed.add_field(name="🔹 점수", value=f"{_score(problem)}점", inline=True)
    embed.add_field(name="🔹 제출 형식", value="`flag{...}`", inline=False)

    if not team_links:
        embed.add_field(name="✨ 팀별 링크", value="등록된 팀 역할이 없습니다.", inline=False)
        return embed

    lines = []
    for item in team_links:
        team_name = str(item.get("team_name") or item.get("team_code") or "unknown").strip()
        lines.append(
            f"**✨ {team_name}**\n"
            f"> 🔗 접속: {_display_action_link(item.get('access_url'), '바로가기')}\n"
            f"> 📦 다운로드: {_display_action_link(item.get('download_url'), '파일 받기')}"
        )
    for idx, chunk in enumerate(_chunk_lines(lines), start=1):
        suffix = f" {idx}" if idx > 1 else ""
        embed.add_field(name=f"✨ 팀별 링크{suffix}", value=chunk, inline=False)
    return embed


class ProblemCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        db_problem.ensure_problem_table()

    @app_commands.command(name="문제목록", description="현재 공개된 공통 문제 목록을 조회합니다.")
    async def problem_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        visibility = await asyncio.to_thread(
            db_problem.get_problem_visibility_state,
            competition_id=_competition_id(),
        )
        if not visibility.get("visible", True):
            await interaction.followup.send(
                str(visibility.get("reason") or "현재는 문제를 공개하지 않습니다."),
                ephemeral=True,
            )
            return
        problems = await asyncio.to_thread(
            db_problem.list_visible_problems,
            team_id=None,
            competition_id=_competition_id(),
            limit=500,
        )
        LOGGER.info("Problem list returned count=%s", len(problems))
        await interaction.followup.send(embed=_problem_list_embed(problems), ephemeral=True)

    @app_commands.command(name="문제", description="문제 번호로 공개된 문제 상세 정보와 팀별 링크를 조회합니다.")
    @app_commands.describe(problem_no="예: WEB-101")
    async def problem_detail(self, interaction: discord.Interaction, problem_no: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        visibility = await asyncio.to_thread(
            db_problem.get_problem_visibility_state,
            competition_id=_competition_id(),
        )
        if not visibility.get("visible", True):
            await interaction.followup.send(
                str(visibility.get("reason") or "현재는 문제를 공개하지 않습니다."),
                ephemeral=True,
            )
            return
        guild_id = str(interaction.guild_id or "")
        role_ids = _member_role_ids(interaction)
        team = await asyncio.to_thread(_team_from_role_ids, guild_id, role_ids)
        team_id = str(team.get("id") or "").strip() if team else None
        if not team or not team_id:
            await interaction.followup.send("현재 소속 팀을 찾을 수 없습니다.", ephemeral=True)
            return

        problem = await asyncio.to_thread(
            db_problem.get_visible_problem_by_no,
            problem_no=problem_no,
            team_id=team_id,
            competition_id=_competition_id(),
        )
        if problem is None:
            await interaction.followup.send("해당 문제를 찾을 수 없거나 아직 공개되지 않았습니다.", ephemeral=True)
            return

        team_links = await asyncio.to_thread(
            db_problem.list_problem_team_links,
            _problem_no(problem),
            _competition_id(),
        )
        embed = _problem_detail_embed(problem, team_links)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProblemCog(bot))
