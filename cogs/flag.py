from __future__ import annotations

import json
import logging

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

try:
    from . import audit
except ModuleNotFoundError as exc:
    if exc.name not in {"audit", "cogs"}:
        raise
    import cogs.audit as audit


LOGGER = logging.getLogger("ops.bot.flag")


def _user_friendly_flag_error(exc: Exception) -> str:
    message = str(exc).strip()
    if message == "active session not found for discord user":
        return "디스코드 인증부터 해주세요."
    return f"FLAG 제출 중 오류가 발생했습니다: {message}"


async def submit_flag(discord_user_id: str, problem_no: str, submitted_flag: str) -> dict:
    # ops-backend의 BotFlagSubmitRequest 스키마에 맞춰 필드명 정렬.
    # competition_id는 omit하면 서버가 현재 running 대회를 자동 선택한다.
    payload = {
        "discord_user_id": str(discord_user_id),
        "submitted_flag": str(submitted_flag).strip(),
        "problem_no": str(problem_no).strip() or None,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Bot-API-Key": config.FLAG_API_KEY,
    }
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(config.FLAG_API_URL, json=payload, headers=headers) as resp:
            raw_text = (await resp.text()).strip()
            data: dict | None = None

            if raw_text:
                try:
                    parsed = json.loads(raw_text)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    data = parsed

            if resp.status >= 400:
                if data is not None:
                    raise RuntimeError(str(data.get("error") or data.get("message") or f"HTTP {resp.status}"))
                preview = raw_text[:200] if raw_text else "empty response body"
                raise RuntimeError(f"FLAG 제출 실패: HTTP {resp.status} ({preview})")

            if data is None:
                preview = raw_text[:200] if raw_text else "empty response body"
                raise RuntimeError(f"FLAG 서버 응답이 JSON 형식이 아닙니다: {preview}")

            return data


class FlagCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="flag", description="문제 번호와 FLAG를 제출합니다.")
    @app_commands.describe(problem_no="문제 번호", submitted_flag="예: flag{...}")
    async def flag_submit(
        self,
        interaction: discord.Interaction,
        problem_no: str,
        submitted_flag: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await submit_flag(str(interaction.user.id), problem_no, submitted_flag)
        except Exception as exc:
            LOGGER.exception("FLAG submit failed user_id=%s problem_no=%s", interaction.user.id, problem_no)
            await interaction.followup.send(_user_friendly_flag_error(exc), ephemeral=True)
            return

        status = str(result.get("status", "")).strip()
        message = str(result.get("message", "")).strip() or "처리 결과를 확인해주세요."
        score = result.get("score")
        awarded = result.get("awarded_score")

        if status == "correct":
            audit.log(
                None,
                interaction.user,
                "FLAG_SUBMIT_CORRECT",
                f"problem_no={problem_no}; awarded_score={awarded}; total_score={score}",
            )
            await interaction.followup.send(
                f"{message}\n문제 번호: `{problem_no}`\n획득 점수: `{awarded}`\n현재 점수: `{score}`",
                ephemeral=True,
            )
            return

        action_name = {
            "already_solved": "FLAG_SUBMIT_ALREADY_SOLVED",
            "invalid_format": "FLAG_SUBMIT_INVALID_FORMAT",
            "wrong_answer": "FLAG_SUBMIT_WRONG",
            "problem_not_found": "FLAG_SUBMIT_UNKNOWN_PROBLEM",
        }.get(status, "FLAG_SUBMIT_FAILED")
        audit.log(None, interaction.user, action_name, f"problem_no={problem_no}; status={status}")
        await interaction.followup.send(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    cog = FlagCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.flag_submit, override=True)
