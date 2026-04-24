import logging

import discord

_CONFIG_IMPORT_NAMES = {"config"}
_AUDIT_IMPORT_NAMES = {"audit", "cogs"}

try:
    import config
except ModuleNotFoundError as exc:
    if exc.name not in _CONFIG_IMPORT_NAMES:
        raise
    from .. import config

try:
    from . import audit
except ModuleNotFoundError as exc:
    if exc.name not in _AUDIT_IMPORT_NAMES:
        raise
    import cogs.audit as audit


LOGGER = logging.getLogger("ops.bot.ticket_log")


async def emit_ticket_audit(
    guild: discord.Guild | None,
    actor: discord.Member | discord.User,
    action: str,
    ticket_id: str | None,
    detail: str,
) -> None:
    if guild is None:
        return

    try:
        audit_channel = guild.get_channel(config.AUDIT_LOG_CHANNEL_ID)
        audit.log(ticket_id, actor, action, detail)
        await audit.post_audit_embed(
            audit_channel,
            actor,
            action,
            ticket_id,
            detail,
        )
    except Exception as exc:
        LOGGER.exception(
            "Ticket audit failed: action=%s ticket_id=%s",
            action,
            ticket_id,
        )
