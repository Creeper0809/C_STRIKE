import discord
from discord import app_commands
from discord.ext import commands

_CONFIG_IMPORT_NAMES = {"config"}
_TICKET_SERVICE_IMPORT_NAMES = {"ticket_service", "cogs"}
_TICKET_VIEWS_IMPORT_NAMES = {"ticket_views", "cogs"}
_DB_IMPORT_NAMES = {"db"}

try:
    import config
except ModuleNotFoundError as exc:
    if exc.name not in _CONFIG_IMPORT_NAMES:
        raise
    from .. import config

try:
    import db
except ModuleNotFoundError as exc:
    if exc.name not in _DB_IMPORT_NAMES:
        raise
    from .. import db

try:
    from . import ticket_service
except ModuleNotFoundError as exc:
    if exc.name not in _TICKET_SERVICE_IMPORT_NAMES:
        raise
    import cogs.ticket_service as ticket_service

try:
    from .ticket_views import TicketCloseView, TicketEntryView, TicketTypeView
except ModuleNotFoundError as exc:
    if exc.name not in _TICKET_VIEWS_IMPORT_NAMES:
        raise
    from cogs.ticket_views import TicketCloseView, TicketEntryView, TicketTypeView


def _can_manage_ticket_alert(user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    perms = user.guild_permissions
    return bool(perms.administrator or perms.manage_guild)


class TicketCog(commands.GroupCog, group_name="티켓", group_description="티켓 생성/알림 관리"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._persistent_view_registered = False

    def _find_member_by_nickname(self, guild: discord.Guild, nickname: str) -> discord.Member | None:
        needle = str(nickname or "").strip().lower()
        if not needle:
            return None
        exact: list[discord.Member] = []
        partial: list[discord.Member] = []
        for member in guild.members:
            names = [str(member.display_name or ""), str(member.name or "")]
            lowered = [name.lower() for name in names if name]
            if needle in lowered:
                exact.append(member)
                continue
            if any(needle in value for value in lowered):
                partial.append(member)
        if exact:
            return exact[0]
        if partial:
            return partial[0]
        return None

    @app_commands.command(name="알림", description="티켓 승인/거절 DM을 받을 운영자를 지정합니다.")
    @app_commands.describe(nickname="DM 받을 디스코드 닉네임")
    async def set_ticket_notifier(self, interaction: discord.Interaction, nickname: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return
        if not _can_manage_ticket_alert(interaction.user):
            await interaction.response.send_message("권한이 없습니다. (관리자/서버 관리 필요)", ephemeral=True)
            return

        member = self._find_member_by_nickname(interaction.guild, nickname)
        if member is None:
            await interaction.response.send_message(f"닉네임 `{nickname}` 사용자를 찾지 못했습니다.", ephemeral=True)
            return

        db.set_ticket_notifier(str(interaction.guild.id), str(member.id), member.display_name)
        await interaction.response.send_message(
            f"티켓 알림 대상이 `{member.display_name}`({member.mention})로 설정되었습니다.",
            ephemeral=True,
        )

    @app_commands.command(name="닫기", description="현재 티켓 스레드를 닫습니다.")
    async def close_ticket_thread(self, interaction: discord.Interaction) -> None:
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message("티켓 스레드 안에서만 사용할 수 있습니다.", ephemeral=True)
            return

        ticket = db.get_latest_ticket_by_thread_id(str(thread.id))
        if not ticket or str(ticket.get("status") or "").lower() in {"closed", "deleted", "error"}:
            await interaction.response.send_message("열려 있는 티켓 스레드가 아닙니다.", ephemeral=True)
            return

        try:
            await ticket_service.close_ticket(interaction, thread, None)
        except Exception as exc:
            if getattr(exc, "code", None) in {10062, 40060}:
                LOGGER.warning("Ignoring transient slash close interaction error: %s", exc)
                return
            LOGGER.exception("Ticket slash close failed thread_id=%s", thread.id)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "티켓을 닫는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        "티켓을 닫는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                        ephemeral=True,
                    )
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._persistent_view_registered:
            self.bot.add_view(TicketEntryView())
            self.bot.add_view(TicketTypeView())
            self.bot.add_view(TicketCloseView())
            self._persistent_view_registered = True

        channel = self.bot.get_channel(config.TICKET_CATEGORY_ID)
        await ticket_service.ensure_ticket_panel(channel)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await ticket_service.notify_ticket_thread_message(message, self.bot)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        ticket_service.close_open_tickets_for_thread(thread.id, status="deleted")

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent):
        thread_id = getattr(payload, "thread_id", None)
        if thread_id is None:
            return
        ticket_service.close_open_tickets_for_thread(thread_id, status="deleted")


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
