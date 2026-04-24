import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord

try:
    import db
except ModuleNotFoundError as exc:
    if exc.name != "db":
        raise
    from .. import db

try:
    import config
except ModuleNotFoundError as exc:
    if exc.name != "config":
        raise
    from .. import config


KST = timezone(timedelta(hours=9), name="KST")
LOGGER = logging.getLogger("ops.bot.audit")


def _now_kst_naive() -> datetime:
    return datetime.now(timezone.utc)


def log(
    ticket_id: str | None,
    actor: discord.User | discord.Member,
    action: str,
    detail: str = "",
):
    """Record operational actions in the audit_logs table.

    psycopg2는 동기 드라이버라 interaction handler 안에서 직접 호출하면 이벤트 루프를
    막아 slash command의 3초 timeout을 깰 수 있다. 실행 중인 이벤트 루프가 있으면
    create_task + asyncio.to_thread로 fire-and-forget 실행하고, 루프 밖에서 호출된
    경우(테스트 등)에만 동기 폴백으로 떨어진다.
    """
    actor_id = str(actor.id)
    actor_name = str(actor)
    ts = _now_kst_naive()

    kwargs = {
        "ticket_id": ticket_id,
        "actor_id": actor_id,
        "actor_name": actor_name,
        "action": action,
        "detail": detail,
        "timestamp": ts,
    }

    async def _run() -> None:
        try:
            await asyncio.to_thread(db.create_audit_log, **kwargs)
        except Exception:
            LOGGER.exception("audit log write failed action=%s", action)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        db.create_audit_log(**kwargs)
        return

    loop.create_task(_run())


async def post_audit_embed(
    channel: discord.abc.Messageable | None,
    actor: discord.Member | discord.User,
    action: str,
    ticket_id: str | None = None,
    detail: str = "",
):
    if channel is None:
        return

    embed = discord.Embed(
        title="Audit Log",
        color=discord.Color.blurple(),
        timestamp=_now_kst_naive(),
    )
    embed.add_field(name="Actor", value=actor.mention, inline=True)
    embed.add_field(name="Action", value=action, inline=True)

    if ticket_id:
        embed.add_field(name="Ticket ID", value=f"`{ticket_id}`", inline=True)
    if detail:
        embed.add_field(name="Detail", value=detail, inline=False)

    await channel.send(embed=embed)


def _truncate(text: str | None, limit: int = 400) -> str:
    if not text:
        return "-"
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _member_summary(member: discord.Member) -> str:
    joined_at = member.joined_at.isoformat() if member.joined_at else "-"
    return f"user={member} id={member.id} display_name={member.display_name} joined_at={joined_at}"


def register_audit_handlers(bot, audit_channel_id: int) -> None:
    async def _write_audit_log(
        guild: discord.Guild | None,
        actor: discord.Member | discord.User,
        action: str,
        detail: str,
        *,
        post_to_channel: bool,
    ) -> None:
        log(None, actor, action, detail)

        if not post_to_channel or guild is None:
            return

        audit_channel = guild.get_channel(audit_channel_id)
        await post_audit_embed(audit_channel, actor, action, None, detail)

    @bot.listen("on_member_join")
    async def _on_member_join(member: discord.Member):
        await _write_audit_log(
            member.guild,
            member,
            "MEMBER_JOINED",
            _member_summary(member),
            post_to_channel=True,
        )

    @bot.listen("on_member_remove")
    async def _on_member_remove(member: discord.Member):
        await _write_audit_log(
            member.guild,
            member,
            "MEMBER_LEFT",
            _member_summary(member),
            post_to_channel=True,
        )

    @bot.listen("on_member_update")
    async def _on_member_update(before: discord.Member, after: discord.Member):
        changes: list[str] = []

        if before.display_name != after.display_name:
            changes.append(f"display_name: {before.display_name} -> {after.display_name}")
        if before.nick != after.nick:
            changes.append(f"nick: {before.nick or '-'} -> {after.nick or '-'}")

        before_roles = {role.id: role.name for role in before.roles if role.name != "@everyone"}
        after_roles = {role.id: role.name for role in after.roles if role.name != "@everyone"}
        added_roles = [after_roles[role_id] for role_id in sorted(after_roles.keys() - before_roles.keys())]
        removed_roles = [before_roles[role_id] for role_id in sorted(before_roles.keys() - after_roles.keys())]

        if added_roles:
            changes.append(f"roles_added={', '.join(added_roles)}")
        if removed_roles:
            changes.append(f"roles_removed={', '.join(removed_roles)}")

        if not changes:
            return

        await _write_audit_log(
            after.guild,
            after,
            "MEMBER_UPDATED",
            "; ".join(changes),
            post_to_channel=True,
        )

    @bot.listen("on_audit_log_entry_create")
    async def _on_audit_log_entry_create(entry: discord.AuditLogEntry):
        if entry.user is None:
            return

        target = entry.target
        target_value = getattr(target, "mention", None) or getattr(target, "name", None) or str(target)
        detail_parts = [f"target={target_value}"]
        if entry.reason:
            detail_parts.append(f"reason={entry.reason}")

        changes = _truncate(str(entry.changes), 800)
        if changes and changes != "AuditLogDiff()":
            detail_parts.append(f"changes={changes}")

        await _write_audit_log(
            entry.guild,
            entry.user,
            f"AUDIT_{str(entry.action).upper()}",
            "; ".join(detail_parts),
            post_to_channel=True,
        )


async def setup(bot):
    register_audit_handlers(bot, config.AUDIT_LOG_CHANNEL_ID)
