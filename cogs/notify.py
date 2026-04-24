from datetime import datetime, timedelta, timezone
import logging

import discord
from discord import app_commands
from discord.ext import commands

try:
    from . import audit
except ModuleNotFoundError as exc:
    if exc.name not in {"audit", "cogs"}:
        raise
    import cogs.audit as audit

try:
    import config
except ModuleNotFoundError as exc:
    if exc.name != "config":
        raise
    from .. import config

try:
    import db
except ModuleNotFoundError as exc:
    if exc.name != "db":
        raise
    from .. import db


LOGGER = logging.getLogger("ops.bot.notify")
KST = timezone(timedelta(hours=9), name="KST")


def _now_kst_naive() -> datetime:
    return datetime.now(timezone.utc)


def _can_use_ops_command(user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    permissions = user.guild_permissions
    return permissions.administrator or permissions.manage_guild


def _color_for_category(category: str) -> discord.Color:
    normalized = (category or "").strip().lower()
    color_map = {
        "deploy_failure": discord.Color.red(),
        "judge_failure": discord.Color.orange(),
        "recovery": discord.Color.green(),
        "incident": discord.Color.red(),
        "warning": discord.Color.orange(),
        "notice": discord.Color.blurple(),
        "announcement": discord.Color.blurple(),
    }
    return color_map.get(normalized, discord.Color.blurple())


def _normalize_notification_title(title: str) -> str:
    normalized = (title or "").strip() or "notice"
    compact = normalized.replace(" ", "")
    if compact in {"오류발생", "오류"}:
        if normalized.startswith("🚨") and normalized.endswith("🚨"):
            return normalized
        return f"🚨 {normalized} 🚨"
    return normalized


def build_notification_embed(title: str, description: str, requested_by: str) -> discord.Embed:
    title = _normalize_notification_title(title)
    LOGGER.info("Building notification embed title=%s requested_by=%s", title, requested_by)
    embed = discord.Embed(
        title=title,
        description=description,
        color=_color_for_category(title),
        timestamp=_now_kst_naive(),
    )
    embed.set_footer(text=f"요청자: {requested_by}")
    return embed



def build_relay_embed(
    *,
    title: str,
    relay_text: str,
    requested_by: str,
    share: int | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="중계 공지",
        description=relay_text,
        color=discord.Color.gold(),
        timestamp=_now_kst_naive(),
    )
    embed.add_field(name="팀/이벤트", value=str(title or "-").strip() or "-", inline=False)
    if share is not None:
        embed.add_field(name="점유율", value=f"{share}%", inline=True)
    embed.set_footer(text=f"Relay | requested_by: {requested_by}")
    return embed


async def send_relay_notification(
    bot: commands.Bot,
    *,
    channel_id: str,
    title: str,
    relay_text: str,
    requested_by: str = "relay",
    share: int | None = None,
    action: str = "API_RELAY",
) -> int:
    guild = bot.get_guild(config.GUILD_ID)
    if guild is None:
        raise RuntimeError(f"Guild {config.GUILD_ID} not found")

    try:
        channel_id_int = int(str(channel_id).strip())
    except ValueError as exc:
        raise RuntimeError(f"Invalid channel_id: {channel_id}") from exc

    channel = guild.get_channel(channel_id_int)
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError(f"Target channel not found or not a text channel: {channel_id}")

    embed = build_relay_embed(title=title, relay_text=relay_text, requested_by=requested_by, share=share)
    await channel.send(embed=embed)

    audit_channel = guild.get_channel(config.AUDIT_LOG_CHANNEL_ID)
    actor = bot.user
    if actor is not None:
        detail = f"{title}: {relay_text} | channel_id={channel_id}"
        if share is not None:
            detail += f" | share={share}"
        audit.log(None, actor, action, detail)
        await audit.post_audit_embed(audit_channel, actor, action, detail=title)
    return 1


def target_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    channels: list[discord.TextChannel] = []
    for channel in guild.text_channels:
        name = channel.name.lower()
        if "ops" in name or "공지" in channel.name:
            channels.append(channel)
    return channels


async def broadcast_notification(
    bot: commands.Bot,
    title: str,
    description: str,
    *,
    requested_by: str,
    action: str = "API_NOTIFICATION",
    target_channel_id: str | None = None,
) -> int:
    guild = bot.get_guild(config.GUILD_ID)
    if guild is None:
        raise RuntimeError(f"Guild {config.GUILD_ID} not found")

    embed = build_notification_embed(title, description, requested_by)
    sent_count = 0

    if target_channel_id:
        try:
            channel_id_int = int(str(target_channel_id).strip())
        except ValueError as exc:
            raise RuntimeError(f"Invalid channel_id: {target_channel_id}") from exc

        channel = guild.get_channel(channel_id_int)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"Target channel not found or not a text channel: {target_channel_id}")
        await channel.send(embed=embed)
        sent_count = 1
    else:
        for channel in target_channels(guild):
            await channel.send(embed=embed)
            sent_count += 1

    audit_channel = guild.get_channel(config.AUDIT_LOG_CHANNEL_ID)
    actor = bot.user
    if actor is not None:
        if target_channel_id:
            detail = f"{title}: {description} | channel_id={target_channel_id}"
        else:
            detail = f"{title}: {description}"
        audit.log(None, actor, action, detail)
        await audit.post_audit_embed(audit_channel, actor, action, detail=title)

    return sent_count


class NoticeScheduleCog(commands.GroupCog, group_name="공지", group_description="예약 공지 관리"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="전파", description="운영 공지를 전파합니다.")
    @app_commands.default_permissions(administrator=True, manage_guild=True)
    async def broadcast(self, interaction: discord.Interaction, title: str, description: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("길드에서만 사용할 수 있습니다.", ephemeral=True)
            return
        if not _can_use_ops_command(interaction.user):
            await interaction.response.send_message("사용할 수 없는 권한입니다.", ephemeral=True)
            return

        embed = build_notification_embed(title, description, str(interaction.user))
        for channel in target_channels(interaction.guild):
            await channel.send(embed=embed)

        audit_channel = interaction.guild.get_channel(config.AUDIT_LOG_CHANNEL_ID)
        audit.log(None, interaction.user, "INCIDENT_BROADCAST", f"{title}: {description}")
        await audit.post_audit_embed(
            audit_channel,
            interaction.user,
            "INCIDENT_BROADCAST",
            detail=title,
        )
        await interaction.response.send_message("운영 공지를 전파했습니다.", ephemeral=True)

    @app_commands.command(name="목록", description="현재 예약된 공지 목록을 보여줍니다.")
    async def list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("길드에서만 사용할 수 있습니다.", ephemeral=True)
            return
        if not _can_use_ops_command(interaction.user):
            await interaction.response.send_message("사용할 수 없는 권한입니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = db.list_scheduled_announcements(str(interaction.guild.id), limit=50)
        if not rows:
            await interaction.followup.send("현재 예약된 공지가 없습니다.", ephemeral=True)
            return

        lines: list[str] = []
        for idx, row in enumerate(rows, start=1):
            scheduled_at = str(row.get("scheduled_at", "-")).replace("T", " ")[:16]
            title = str(row.get("title", "untitled")).strip() or "untitled"
            channel_name = str(row.get("channel_name", "")).strip()
            channel_id = str(row.get("channel_id", "")).strip()
            if channel_name:
                target = f"#{channel_name}"
            elif channel_id:
                target = f"<#{channel_id}>"
            else:
                target = "(all ops channels)"
            lines.append(f"{idx}. [{scheduled_at}] {target} - {title}")
            if len(lines) >= 20:
                break

        await interaction.followup.send(
            "예약 공지 목록\n" + "\n".join(lines),
            ephemeral=True,
        )

    @app_commands.command(name="삭제", description="지정한 번호의 예약 공지를 삭제합니다.")
    @app_commands.describe(number="목록에서 확인한 번호")
    async def delete(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 200]) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("길드에서만 사용할 수 있습니다.", ephemeral=True)
            return
        if not _can_use_ops_command(interaction.user):
            await interaction.response.send_message("사용할 수 없는 권한입니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = str(interaction.guild.id)
        row = db.get_scheduled_announcement_by_number(guild_id, int(number))
        if not row:
            await interaction.followup.send("해당 번호의 예약 공지가 없습니다.", ephemeral=True)
            return

        announcement_id = int(row["id"])
        deleted = db.soft_delete_scheduled_announcement(guild_id, announcement_id)
        if not deleted:
            await interaction.followup.send("이미 삭제되었거나 처리할 수 없는 공지입니다.", ephemeral=True)
            return

        title = str(row.get("title", "untitled")).strip() or "untitled"
        scheduled_at = str(row.get("scheduled_at", "-")).replace("T", " ")[:16]
        job_id = str(row.get("job_id", "")).strip()
        extra = f", job_id={job_id}" if job_id else ""
        detail = f"number={number}, announcement_id={announcement_id}, title={title}, scheduled_at={scheduled_at}{extra}"
        audit.log(None, interaction.user, "NOTICE_SCHEDULE_DELETE", detail)

        await interaction.followup.send(
            f"예약 공지 삭제 완료: #{number} (ID: {announcement_id}, 예정시각: {scheduled_at})",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(NoticeScheduleCog(bot))
