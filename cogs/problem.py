from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

try:
    from db import problem as db_problem
except ModuleNotFoundError as exc:
    if exc.name not in {"db", "problem"}:
        raise
    from ..db import problem as db_problem


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


def _category_icon(category: str) -> str:
    return {
        "WEB": "🌐",
        "PWN": "💣",
        "REV": "🧩",
        "CRYPTO": "🔐",
        "MISC": "🧰",
        "FORENSIC": "🕵️",
    }.get(category, "📁")


def _difficulty_badge(difficulty: str) -> str:
    normalized = str(difficulty or "").strip().lower()
    if normalized == "easy":
        return "🟢 Easy"
    if normalized == "medium":
        return "🟡 Medium"
    if normalized == "hard":
        return "🔴 Hard"
    return f"⚪ {difficulty or 'Unknown'}"


def _detail_embed_color(difficulty: str) -> discord.Color:
    normalized = str(difficulty or "").strip().lower()
    if normalized == "easy":
        return discord.Color.green()
    if normalized == "medium":
        return discord.Color.gold()
    if normalized == "hard":
        return discord.Color.red()
    return discord.Color.blurple()


def _problem_list_embed(problems: list[dict[str, Any]]) -> discord.Embed:
    total_score = sum(_score(problem) for problem in problems)
    embed = discord.Embed(
        title="C-STRIKE 사이버공방전",
        description=f"총 `{len(problems)}`문제 | 총 `{total_score}`점",
        color=discord.Color.blurple(),
    )
    if not problems:
        embed.add_field(name="안내", value="등록된 문제가 없습니다.", inline=False)
        return embed

    categories = sorted({_category(problem) for problem in problems})
    for category in categories:
        rows = [problem for problem in problems if _category(problem) == category]
        lines = [
            (
                f"• `{_problem_no(problem):<10}` "
                f"🏆 `{_score(problem):>3}점` "
                f"{_difficulty_badge(_difficulty(problem))} | "
                f"**{problem.get('title') or '-'}**"
            )
            for problem in rows
        ]
        embed.add_field(
            name=f"{_category_icon(category)} {category} ({len(rows)}문제)",
            value="\n".join(lines)[:1024],
            inline=False,
        )
    embed.set_footer(text="🧾 제출 형식: CSTRIKE{...}")
    return embed


def _problem_detail_embed(problem: dict[str, Any]) -> discord.Embed:
    category = _category(problem)
    difficulty = _difficulty(problem)
    description = str(problem.get("description") or "").strip() or "문제 설명이 없습니다."
    access_url = str(problem.get("access_url") or "").strip() or "없음"
    download_url = str(problem.get("download_url") or "").strip() or "없음"

    embed = discord.Embed(
        title=f"🧩 문제 정보 - [{_problem_no(problem)}] {problem.get('title') or '-'}",
        description=description,
        color=_detail_embed_color(difficulty),
    )
    embed.add_field(name="📚 카테고리", value=f"{_category_icon(category)} {category}", inline=True)
    embed.add_field(name="난이도", value=_difficulty_badge(difficulty), inline=True)
    embed.add_field(name="🏆 점수", value=f"{_score(problem)}점", inline=True)
    embed.add_field(name="🔗 접속 링크", value=access_url, inline=False)
    embed.add_field(name="📦 다운로드 링크", value=download_url, inline=False)
    embed.add_field(name="🧾 제출 형식", value="`CSTRIKE{...}`", inline=False)
    return embed


class ProblemLinkView(discord.ui.View):
    def __init__(self, problem: dict[str, Any]):
        super().__init__(timeout=None)
        access_url = str(problem.get("access_url") or "").strip()
        download_url = str(problem.get("download_url") or "").strip()
        if access_url:
            self.add_item(discord.ui.Button(label="접속", url=access_url))
        if download_url:
            self.add_item(discord.ui.Button(label="다운로드", url=download_url))


class ProblemCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="문제목록", description="등록된 문제 목록을 조회합니다.")
    async def problem_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        problems = await asyncio.to_thread(db_problem.list_problems, 500, True)
        await interaction.followup.send(embed=_problem_list_embed(problems), ephemeral=True)

    @app_commands.command(name="문제", description="문제 번호로 상세 정보를 조회합니다.")
    @app_commands.describe(문제번호="예: WEB01")
    async def problem_detail(self, interaction: discord.Interaction, 문제번호: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        problem = await asyncio.to_thread(db_problem.get_problem_by_no, 문제번호)
        if problem is None:
            await interaction.followup.send("해당 문제를 찾을 수 없습니다.", ephemeral=True)
            return
        embed = _problem_detail_embed(problem)
        view = ProblemLinkView(problem)
        await interaction.followup.send(embed=embed, view=view if view.children else None, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    cog = ProblemCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.problem_list, override=True)
    bot.tree.add_command(cog.problem_detail, override=True)
