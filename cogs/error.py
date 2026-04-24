import asyncio
import logging
import sys
import traceback
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

try:
    import db
except ModuleNotFoundError as exc:
    if exc.name != "db":
        raise
    from .. import db

LOGGER = logging.getLogger("ops.bot.error")


def _truncate(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _format_exception(error: BaseException) -> str:
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    return _truncate(rendered.strip() or repr(error))


def _base_error(error: BaseException) -> BaseException:
    for attr_name in ("original", "__cause__", "__context__"):
        nested = getattr(error, attr_name, None)
        if isinstance(nested, BaseException):
            return nested
    return error


def _log_exception(message: str, error: BaseException) -> None:
    LOGGER.error(
        message,
        exc_info=(type(error), error, error.__traceback__),
    )


def _actor_payload(
    *,
    actor: discord.abc.User | None,
) -> tuple[str, str]:
    if actor is None:
        return "system", "ops_bot"
    return str(actor.id), str(actor)


def _serialize_interaction_options(interaction: discord.Interaction) -> str:
    data = interaction.data if isinstance(interaction.data, dict) else {}
    options = data.get("options")
    if not isinstance(options, list):
        return ""

    pairs: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        name = str(option.get("name", "")).strip()
        value = option.get("value")
        if not name:
            continue
        pairs.append(f"{name}={value}")
    return ", ".join(pairs)


class ErrorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._previous_on_error = None
        self._previous_on_command_error = None
        self._previous_tree_on_error = None

    async def cog_load(self) -> None:
        self._previous_on_error = self.bot.on_error
        self._previous_on_command_error = self.bot.on_command_error
        self._previous_tree_on_error = self.bot.tree.on_error

        self.bot.on_error = self._bot_on_error
        self.bot.on_command_error = self._bot_on_command_error
        self.bot.tree.on_error = self._tree_on_error

    async def cog_unload(self) -> None:
        if self._previous_on_error is not None:
            self.bot.on_error = self._previous_on_error
        if self._previous_on_command_error is not None:
            self.bot.on_command_error = self._previous_on_command_error
        if self._previous_tree_on_error is not None:
            self.bot.tree.on_error = self._previous_tree_on_error

    def _write_error_log(
        self,
        *,
        action: str,
        detail: str,
        actor: discord.abc.User | None = None,
        ticket_id: str | None = None,
    ) -> None:
        actor_id, actor_name = _actor_payload(actor=actor)
        db.create_audit_log(
            ticket_id=ticket_id,
            actor_id=actor_id,
            actor_name=actor_name,
            action=action,
            detail=_truncate(detail),
        )

    def _schedule_audit_log(
        self,
        *,
        action: str,
        detail: str,
        actor: discord.abc.User | None = None,
        ticket_id: str | None = None,
    ) -> None:
        """Audit 로깅을 스레드풀에 던져 이벤트 루프를 막지 않도록 한다.

        psycopg2 호출은 동기라 on_interaction 리스너에서 직접 호출 시 3초 interaction
        timeout 안에 defer()가 실행되지 못해 `404 Unknown interaction`이 발생한다.
        로그 기록은 fire-and-forget이어도 무방하므로 create_task + to_thread로 격리한다.
        """

        async def _run() -> None:
            try:
                await asyncio.to_thread(
                    self._write_error_log,
                    action=action,
                    detail=detail,
                    actor=actor,
                    ticket_id=ticket_id,
                )
            except Exception:
                LOGGER.exception("audit log write failed action=%s", action)

        asyncio.create_task(_run())

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.application_command:
            return

        command_name = interaction.command.qualified_name if interaction.command else "<unknown>"
        option_text = _serialize_interaction_options(interaction)
        detail = (
            f"command={command_name}; "
            f"channel_id={interaction.channel_id}; "
            f"guild_id={interaction.guild_id}; "
            f"user_id={getattr(interaction.user, 'id', None)}"
        )
        if option_text:
            detail += f"; options={option_text}"

        self._schedule_audit_log(
            action="BOT_APP_COMMAND_INVOKED",
            detail=detail,
            actor=interaction.user,
        )

    @commands.Cog.listener()
    async def on_command_completion(self, context: commands.Context) -> None:
        command_name = context.command.qualified_name if context.command else "<unknown>"
        detail = (
            f"command={command_name}; "
            f"channel_id={getattr(context.channel, 'id', None)}; "
            f"guild_id={getattr(context.guild, 'id', None)}"
        )
        self._schedule_audit_log(
            action="BOT_PREFIX_COMMAND_INVOKED",
            detail=detail,
            actor=context.author,
        )

    async def _bot_on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        current_error = kwargs.get("error") or sys.exc_info()[1]
        error = _base_error(current_error or RuntimeError(f"Unhandled event error: {event_method}"))
        actor = None

        for item in args:
            if isinstance(item, discord.Interaction):
                actor = item.user
                break
            author = getattr(item, "author", None)
            if isinstance(author, discord.abc.User):
                actor = author
                break

        detail = (
            f"event={event_method}; "
            f"args={len(args)}; "
            f"error_type={type(error).__name__}; "
            f"error={error}; "
            f"traceback={_format_exception(error)}"
        )
        self._write_error_log(action="BOT_EVENT_ERROR", detail=detail, actor=actor)
        _log_exception(f"Unhandled Discord event error: {event_method}", error)

    async def _bot_on_command_error(
        self,
        context: commands.Context,
        exception: commands.CommandError,
        /,
    ) -> None:
        error = _base_error(exception)
        command_name = context.command.qualified_name if context.command else "<unknown>"
        detail = (
            f"command={command_name}; "
            f"channel_id={getattr(context.channel, 'id', None)}; "
            f"guild_id={getattr(context.guild, 'id', None)}; "
            f"error_type={type(error).__name__}; "
            f"error={error}; "
            f"traceback={_format_exception(error)}"
        )
        self._write_error_log(
            action="BOT_COMMAND_ERROR",
            detail=detail,
            actor=context.author,
        )
        _log_exception(f"Prefix command error: {command_name}", error)

    async def _tree_on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
        /,
    ) -> None:
        base_error = _base_error(error)
        command_name = interaction.command.qualified_name if interaction.command else "<unknown>"
        detail = (
            f"command={command_name}; "
            f"channel_id={interaction.channel_id}; "
            f"guild_id={interaction.guild_id}; "
            f"user_id={getattr(interaction.user, 'id', None)}; "
            f"error_type={type(base_error).__name__}; "
            f"error={base_error}; "
            f"traceback={_format_exception(base_error)}"
        )
        self._write_error_log(
            action="BOT_APP_COMMAND_ERROR",
            detail=detail,
            actor=interaction.user,
        )
        _log_exception(f"App command error: {command_name}", base_error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ErrorCog(bot))
