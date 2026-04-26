import logging

import discord

_TICKET_SERVICE_IMPORT_NAMES = {"ticket_service", "cogs"}
LOGGER = logging.getLogger("ops.bot.ticket_views")

try:
    from . import ticket_service
except ModuleNotFoundError as exc:
    if exc.name not in _TICKET_SERVICE_IMPORT_NAMES:
        raise
    import cogs.ticket_service as ticket_service


CATEGORY_OPTIONS = [
    discord.SelectOption(label="문의", value="inquiry"),
    discord.SelectOption(label="신고", value="declaration"),
    discord.SelectOption(label="이의제기", value="objection"),
]


def _is_transient_interaction_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code in {10062, 40060}:
        return True
    text = str(exc)
    return "10062" in text or "40060" in text


class TicketDescriptionModal(discord.ui.Modal, title="티켓 설명 입력"):
    description = discord.ui.TextInput(
        label="티켓 설명",
        placeholder="상황이나 요청 내용을 자세히 적어주세요.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, thread_id: int, category: str):
        super().__init__()
        self.thread_id = thread_id
        self.category = category

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await ticket_service.submit_ticket_description(
                interaction=interaction,
                thread_id=self.thread_id,
                category=self.category,
                description_text=str(self.description),
            )
        except Exception as exc:
            if _is_transient_interaction_error(exc):
                LOGGER.warning("Ignoring transient modal submit interaction error: %s", exc)
                return
            raise


class TicketTypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="티켓 유형을 선택해 주세요.",
        options=CATEGORY_OPTIONS,
        custom_id="ticket_select",
    )
    async def select_category(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not select.values:
            await interaction.response.send_message("티켓 유형을 먼저 선택해 주세요.", ephemeral=True)
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message(
                "티켓 스레드에서만 유형을 선택할 수 있습니다.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(TicketDescriptionModal(thread.id, select.values[0]))


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="티켓 닫기",
        style=discord.ButtonStyle.danger,
        custom_id="close_ticket_btn",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message(
                "티켓 스레드에서만 종료할 수 있습니다.",
                ephemeral=True,
            )
            return

        try:
            await ticket_service.close_ticket(interaction, thread, interaction.message)
        except Exception as exc:
            if _is_transient_interaction_error(exc):
                LOGGER.warning("Ignoring transient close interaction error: %s", exc)
                return
            raise


class TicketApprovalDecisionView(discord.ui.View):
    def __init__(self, *, guild_id: int, thread_id: int, ticket_id: str):
        super().__init__(timeout=86400)
        self.guild_id = int(guild_id)
        self.thread_id = int(thread_id)
        self.ticket_id = str(ticket_id)

    @discord.ui.button(label="승인", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await ticket_service.process_ticket_decision(
            interaction=interaction,
            guild_id=self.guild_id,
            thread_id=self.thread_id,
            ticket_id=self.ticket_id,
            approved=True,
        )
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass

    @discord.ui.button(label="거절", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        available = await ticket_service.is_ticket_decision_available(
            client=interaction.client,
            guild_id=self.guild_id,
            thread_id=self.thread_id,
            ticket_id=self.ticket_id,
        )
        if not available:
            await interaction.response.send_message("이 티켓은 삭제되었습니다.", ephemeral=True)
            if interaction.message is not None:
                try:
                    await interaction.message.edit(view=None)
                except Exception:
                    pass
            return

        await interaction.response.send_modal(
            TicketRejectReasonModal(
                guild_id=self.guild_id,
                thread_id=self.thread_id,
                ticket_id=self.ticket_id,
                source_message=interaction.message,
            )
        )


class TicketRejectReasonModal(discord.ui.Modal, title="티켓 거절 사유"):
    reason = discord.ui.TextInput(
        label="거절 사유",
        placeholder="거절 사유를 입력해 주세요.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )

    def __init__(
        self,
        *,
        guild_id: int,
        thread_id: int,
        ticket_id: str,
        source_message: discord.Message | None,
    ):
        super().__init__()
        self.guild_id = int(guild_id)
        self.thread_id = int(thread_id)
        self.ticket_id = str(ticket_id)
        self.source_message = source_message

    async def on_submit(self, interaction: discord.Interaction):
        await ticket_service.process_ticket_decision(
            interaction=interaction,
            guild_id=self.guild_id,
            thread_id=self.thread_id,
            ticket_id=self.ticket_id,
            approved=False,
            rejection_reason=str(self.reason),
        )
        if self.source_message is not None:
            try:
                await self.source_message.edit(view=None)
            except Exception:
                pass


class TicketEntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="열기",
        style=discord.ButtonStyle.primary,
        custom_id="open_ticket_btn",
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await ticket_service.start_ticket_open_flow(interaction)
        except Exception as exc:
            if _is_transient_interaction_error(exc):
                LOGGER.warning("Ignoring transient open interaction error: %s", exc)
                return
            LOGGER.exception("Ticket open flow failed")
            try:
                await interaction.followup.send(
                    "티켓 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                    ephemeral=True,
                )
            except Exception:
                pass
