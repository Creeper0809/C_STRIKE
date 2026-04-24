import asyncio
import logging

import discord
from discord.ext import commands

try:
    import db
except ModuleNotFoundError as exc:
    if exc.name != "db":
        raise
    from .. import db


LOGGER = logging.getLogger("ops.bot.message")


def _schedule_message_log(**kwargs) -> None:
    """동기 psycopg2 호출을 스레드풀로 격리해 이벤트 루프를 막지 않는다.

    on_message 계열 리스너에서 직접 호출하면 slash command interaction의
    3초 timeout 안에 defer()가 실행되지 못해 `404 Unknown interaction`이
    발생한다. 로깅은 fire-and-forget으로 충분하므로 create_task로 분리한다.
    """

    async def _run() -> None:
        try:
            await asyncio.to_thread(db.create_message_log, **kwargs)
        except Exception:
            LOGGER.exception("message log write failed action=%s", kwargs.get("action"))

    asyncio.create_task(_run())


def _truncate(text: str | None, limit: int = 400) -> str:
    if not text:
        return "-"
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _message_context_text(content: str | None, attachments_count: int) -> str:
    parts: list[str] = []
    text = _truncate(content)
    if text != "-":
        parts.append(text)
    if attachments_count:
        parts.append(f"[attachments: {attachments_count}]")
    return " ".join(parts) if parts else "[empty]"


class MessageCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        detail = _message_context_text(message.content, len(message.attachments))
        _schedule_message_log(
            actor_id=str(message.author.id),
            actor_name=str(message.author),
            user_id=str(message.author.id),
            action="MESSAGE_SENT",
            channel_id=str(message.channel.id),
            context=detail,
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or after.guild is None:
            return
        if before.content == after.content and len(before.attachments) == len(after.attachments):
            return

        detail = (
            f"{_message_context_text(before.content, len(before.attachments))} -> "
            f"{_message_context_text(after.content, len(after.attachments))}"
        )
        _schedule_message_log(
            actor_id=str(after.author.id),
            actor_name=str(after.author),
            user_id=str(after.author.id),
            action="MESSAGE_EDITED",
            channel_id=str(after.channel.id),
            context=detail,
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author is None or message.author.bot or message.guild is None:
            return

        detail = _message_context_text(message.content, len(message.attachments))
        _schedule_message_log(
            actor_id=str(message.author.id),
            actor_name=str(message.author),
            user_id=str(message.author.id),
            action="MESSAGE_DELETED",
            channel_id=str(message.channel.id),
            context=detail,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MessageCog(bot))
