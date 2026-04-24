import asyncio
import logging
import re
from collections import defaultdict

import discord

if "." in (__package__ or ""):
    from .. import config, db
    from . import ticket_log
else:
    import config
    import db
    import cogs.ticket_log as ticket_log

PANEL_MARKER = "[OPS_TICKET_PANEL_V1]"
PANEL_FOOTER_TEXT = "OPS 티켓 안내"
DEFAULT_TICKET_CATEGORY = "pending"
_TICKET_ID_PATTERN = re.compile(r"\*\*Ticket ID:\*\*\s*`([^`]+)`")
LOGGER = logging.getLogger("ops.bot.ticket_service")
_OPEN_FLOW_LOCKS: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_SUBMISSION_LOCKS: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_CATEGORY_LABELS = {
    "pending": "대기",
    "inquiry": "문의",
    "declaration": "신고",
    "objection": "이의제기",
}


def _load_views():
    if "." in (__package__ or ""):
        from .ticket_views import TicketApprovalDecisionView, TicketCloseView, TicketEntryView, TicketTypeView
    else:
        from cogs.ticket_views import TicketApprovalDecisionView, TicketCloseView, TicketEntryView, TicketTypeView
    return TicketTypeView, TicketCloseView, TicketEntryView, TicketApprovalDecisionView


def _new_ticket_identity(category: str) -> tuple[str, str]:
    return db.allocate_ticket_identity(category)


async def resolve_thread(guild: discord.Guild, thread_id: int) -> discord.Thread | None:
    thread = guild.get_thread(thread_id)
    if thread is not None:
        return thread
    try:
        fetched = await guild.fetch_channel(thread_id)
        return fetched if isinstance(fetched, discord.Thread) else None
    except (discord.NotFound, ValueError):
        return None
    except Exception:
        return None


def _clean_text(value: str | None, fallback: str = "-") -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return fallback
    if normalized.lower() in {"null", "none", "nan", "undefined"}:
        return fallback
    return normalized


def _safe_requester_name(user: discord.abc.User) -> str:
    display_name = _clean_text(getattr(user, "display_name", None), fallback="")
    if display_name:
        return display_name
    username = _clean_text(getattr(user, "name", None), fallback="")
    if username:
        return username
    return str(getattr(user, "id", "-"))


def _category_label(category: str) -> str:
    key = _clean_text(category, fallback="기타").lower()
    return _CATEGORY_LABELS.get(key, key)


async def _send_ephemeral_safe(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException as exc:
        if getattr(exc, "code", None) in {10062, 40060}:
            LOGGER.warning("Interaction reply skipped due to expired/acknowledged interaction: %s", exc)
            return
        raise


async def _resolve_existing_user_threads(guild: discord.Guild, user_id: str) -> list[discord.Thread]:
    existing_threads: list[discord.Thread] = []
    open_tickets = db.get_open_tickets_by_user(user_id)

    for ticket in open_tickets:
        thread_id = ticket.get("discord_thread_id")
        ticket_id = ticket.get("ticket_id")
        if not thread_id:
            if ticket_id:
                db.close_ticket(ticket_id, status="deleted")
            continue
        try:
            thread_int_id = int(thread_id)
        except (TypeError, ValueError):
            if ticket_id:
                db.close_ticket(ticket_id, status="deleted")
            continue

        thread = await resolve_thread(guild, thread_int_id)
        if thread is None:
            if ticket_id:
                db.close_ticket(ticket_id, status="deleted")
            continue
        if thread.archived or thread.locked or thread.parent_id != config.TICKET_CATEGORY_ID:
            if ticket_id:
                db.close_ticket(ticket_id, status="closed")
            continue
        existing_threads.append(thread)

    return existing_threads


async def start_ticket_open_flow(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    channel = guild.get_channel(config.TICKET_CATEGORY_ID) if guild else None
    if channel is None:
        await _send_ephemeral_safe(
            interaction,
            "티켓 채널이 아직 설정되지 않았습니다. `.env`의 `TICKET_CATEGORY_ID`를 확인해 주세요.",
        )
        return
    if not hasattr(channel, "create_thread"):
        await _send_ephemeral_safe(
            interaction,
            "지정한 채널에서는 스레드를 만들 수 없습니다. `TICKET_CATEGORY_ID`를 다시 설정해 주세요.",
        )
        return
    if guild is None:
        await _send_ephemeral_safe(interaction, "서버 정보를 확인할 수 없어 티켓을 처리하지 못했습니다.")
        return

    user_id = str(interaction.user.id)
    async with _OPEN_FLOW_LOCKS[user_id]:
        existing_threads = await _resolve_existing_user_threads(guild, user_id)
        if len(existing_threads) >= 1:
            await _send_ephemeral_safe(interaction, f"이미 열린 티켓이 있습니다. <#{existing_threads[0].id}>")
            return

        thread = await channel.create_thread(
            name=f"[{DEFAULT_TICKET_CATEGORY}] {interaction.user.display_name}",
            type=discord.ChannelType.private_thread,
        )
        await thread.add_user(interaction.user)

        ticket_id, ticket_number = _new_ticket_identity(DEFAULT_TICKET_CATEGORY)
        try:
            db.store_ticket(
                ticket_id,
                ticket_number,
                str(thread.id),
                user_id,
                DEFAULT_TICKET_CATEGORY,
                interaction.user.display_name,
            )
        except Exception:
            LOGGER.exception("Failed to store placeholder ticket")
            try:
                await thread.delete(reason=f"Ticket registration failed for {interaction.user}")
            except Exception:
                pass
            await _send_ephemeral_safe(interaction, "티켓 ID를 발급하지 못했습니다. 잠시 후 다시 시도해 주세요.")
            return

        TicketTypeView, _, _, _ = _load_views()
        try:
            await thread.send("티켓 유형을 선택해 주세요.", view=TicketTypeView())
        except Exception:
            db.close_ticket(ticket_id, status="error")
            try:
                await thread.delete(reason=f"Ticket setup failed for {interaction.user}")
            except Exception:
                pass
            LOGGER.exception("Failed to send ticket type selection message")
            await _send_ephemeral_safe(interaction, "티켓 생성 중 오류가 발생했습니다. 다시 시도해 주세요.")
            return

    await _send_ephemeral_safe(interaction, "티켓이 생성되었습니다.")

    await ticket_log.emit_ticket_audit(
        guild,
        interaction.user,
        "TICKET_THREAD_CREATED",
        ticket_id,
        f"thread_id={thread.id}",
    )


def _placeholder_ticket_id_for_thread(thread_id: int) -> str | None:
    ticket = db.get_open_ticket_by_thread_id(str(thread_id))
    if not ticket:
        return None
    description = str(ticket.get("description") or "").strip()
    if description:
        return None
    ticket_id = ticket.get("ticket_id")
    return str(ticket_id) if ticket_id else None


async def _ticket_id_for_submission(thread: discord.Thread, user: discord.abc.User, category: str) -> str | None:
    placeholder_ticket_id = _placeholder_ticket_id_for_thread(thread.id)
    if placeholder_ticket_id:
        db.assign_ticket_number_for_category(placeholder_ticket_id, category)
        db.update_ticket_category(placeholder_ticket_id, category)
        return placeholder_ticket_id

    existing_open = db.get_open_ticket_by_thread_id(str(thread.id))
    existing_ticket_id = existing_open.get("ticket_id") if existing_open else None
    if existing_ticket_id:
        return str(existing_ticket_id)

    ticket_id, ticket_number = _new_ticket_identity(category)
    display_name = getattr(user, "display_name", str(user))
    db.store_ticket(ticket_id, ticket_number, str(thread.id), str(user.id), category, display_name)
    return ticket_id


async def _get_notifier_user(
    guild: discord.Guild,
    client: discord.Client | None = None,
) -> discord.abc.User | None:
    setting = db.get_ticket_notifier(str(guild.id))
    if not setting:
        return None
    notifier_id = str(setting.get("notifier_user_id") or "").strip()
    if not notifier_id.isdigit():
        return None
    member = guild.get_member(int(notifier_id))
    if member is not None:
        return member
    try:
        fetched_member = await guild.fetch_member(int(notifier_id))
        if fetched_member is not None:
            return fetched_member
    except Exception:
        pass
    if client is not None:
        user = client.get_user(int(notifier_id))
        if user is not None:
            return user
        try:
            return await client.fetch_user(int(notifier_id))
        except Exception:
            return None
    return None


async def _send_ticket_decision_dm(
    *,
    interaction: discord.Interaction,
    guild: discord.Guild,
    thread: discord.Thread,
    ticket_id: str,
    category: str,
    description_text: str,
) -> None:
    notifier = await _get_notifier_user(guild, interaction.client)
    if notifier is None:
        return
    _, _, _, TicketApprovalDecisionView = _load_views()
    embed = discord.Embed(
        title="티켓 승인/거절 요청",
        description=(
            f"**Ticket ID:** `{ticket_id}`\n"
            f"**요청자:** {_safe_requester_name(interaction.user)}\n"
            f"**유형:** {_category_label(category)}\n"
            f"**스레드:** <#{thread.id}>\n"
            f"**설명:**\n{_clean_text(description_text, fallback='(설명 없음)')}"
        ),
        color=discord.Color.blurple(),
    )
    view = TicketApprovalDecisionView(guild_id=guild.id, thread_id=thread.id, ticket_id=ticket_id)
    try:
        await notifier.send(embed=embed, view=view)
    except Exception:
        LOGGER.exception("Failed to send ticket decision DM notifier=%s", notifier.id)


async def submit_ticket_description(
    interaction: discord.Interaction,
    thread_id: int,
    category: str,
    description_text: str,
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if guild is None:
        await _send_ephemeral_safe(interaction, "서버 정보를 확인할 수 없어 티켓을 처리하지 못했습니다.")
        return
    thread = await resolve_thread(guild, thread_id)
    if thread is None:
        await _send_ephemeral_safe(interaction, "기존 티켓 스레드를 찾지 못했습니다. 티켓을 다시 열어 주세요.")
        return

    async with _SUBMISSION_LOCKS[thread.id]:
        ticket_id = await _ticket_id_for_submission(thread, interaction.user, category)
        if not ticket_id:
            await _send_ephemeral_safe(interaction, "티켓 ID를 발급하지 못했습니다. 잠시 후 다시 시도해 주세요.")
            return

        requester_name = _safe_requester_name(interaction.user)
        category_text = _category_label(category)
        normalized_desc = _clean_text(description_text, fallback="(설명 없음)")
        db.update_ticket_submission(ticket_id, category, normalized_desc, title=f"[{category}] {requester_name}")

        _, TicketCloseView, _, _ = _load_views()
        await thread.send(
            embed=discord.Embed(
                title="티켓",
                description=(
                    f"**Ticket ID:** `{ticket_id}`\n"
                    f"**요청자:** {requester_name}\n"
                    f"**유형:** {category_text}\n"
                    f"**설명:**\n{normalized_desc}"
                ),
                color=discord.Color.green(),
            ),
            view=TicketCloseView(),
        )

        await _send_ticket_decision_dm(
            interaction=interaction,
            guild=guild,
            thread=thread,
            ticket_id=ticket_id,
            category=category,
            description_text=normalized_desc,
        )

    await ticket_log.emit_ticket_audit(
        guild,
        interaction.user,
        "TICKET_CREATED",
        ticket_id,
        f"{category}: {description_text}",
    )

    try:
        await interaction.delete_original_response()
    except Exception:
        pass


async def is_ticket_decision_available(
    *,
    client: discord.Client,
    guild_id: int,
    thread_id: int,
    ticket_id: str,
) -> bool:
    ticket = db.get_ticket(ticket_id)
    ticket_status = str((ticket or {}).get("status") or "").strip().lower()
    if not ticket or ticket_status in {"closed", "deleted", "error"}:
        return False

    guild = client.get_guild(int(guild_id))
    if guild is None:
        return False

    thread = guild.get_thread(int(thread_id))
    if thread is not None:
        return True

    try:
        fetched = await guild.fetch_channel(int(thread_id))
        return isinstance(fetched, discord.Thread)
    except Exception:
        return False


async def process_ticket_decision(
    *,
    interaction: discord.Interaction,
    guild_id: int,
    thread_id: int,
    ticket_id: str,
    approved: bool,
    rejection_reason: str | None = None,
) -> None:
    setting = db.get_ticket_notifier(str(guild_id))
    notifier_id = str((setting or {}).get("notifier_user_id") or "").strip()
    if not notifier_id or notifier_id != str(interaction.user.id):
        await _send_ephemeral_safe(interaction, "권한이 없습니다.")
        return

    ticket = db.get_ticket(ticket_id)
    ticket_status = str((ticket or {}).get("status") or "").strip().lower()
    if not ticket or ticket_status in {"closed", "deleted", "error"}:
        await _send_ephemeral_safe(interaction, "이미 삭제된 스레드입니다.")
        return

    guild = interaction.client.get_guild(int(guild_id))
    thread: discord.Thread | None = None
    if guild is not None:
        thread = guild.get_thread(int(thread_id))
        if thread is None:
            try:
                fetched = await guild.fetch_channel(int(thread_id))
                if isinstance(fetched, discord.Thread):
                    thread = fetched
            except Exception:
                thread = None

    # User may close/delete the thread before operator approves/rejects from DM.
    if thread is None:
        db.close_ticket(ticket_id, status="deleted")
        await _send_ephemeral_safe(interaction, "이미 삭제된 스레드입니다.")
        return

    normalized_reason = None
    if not approved:
        normalized_reason = str(rejection_reason or "").strip() or "(거절 사유 미입력)"
    db.set_ticket_approval(
        ticket_id,
        approved,
        normalized_reason,
        resolved_by=_safe_requester_name(interaction.user),
    )

    status_text = "승인" if approved else "거절"
    result_embed = discord.Embed(
        title="티켓 처리 결과",
        description=f"티켓이 {status_text}되었습니다.",
        color=discord.Color.green() if approved else discord.Color.red(),
    )
    result_embed.add_field(name="상태", value=status_text, inline=True)
    result_embed.add_field(name="Ticket ID", value=f"`{ticket_id}`", inline=True)
    result_embed.add_field(
        name="처리자",
        value=getattr(interaction.user, "mention", f"`{interaction.user.id}`"),
        inline=False,
    )
    if not approved:
        result_embed.add_field(name="거절 사유", value=normalized_reason or "-", inline=False)
    await thread.send(embed=result_embed)

    await _send_ephemeral_safe(interaction, "처리 완료")

async def notify_ticket_thread_message(
    message: discord.Message,
    client: discord.Client | None = None,
) -> None:
    if message.author.bot:
        return
    thread = message.channel
    if not isinstance(thread, discord.Thread):
        return
    ticket = db.get_latest_ticket_by_thread_id(str(thread.id))
    if not ticket:
        return
    if str(ticket.get("status") or "").lower() in {"closed", "deleted", "error"}:
        return
    guild = message.guild
    if guild is None:
        return
    notifier = await _get_notifier_user(guild, client)
    if notifier is None:
        return
    if notifier.id == message.author.id:
        return
    text = (
        "해당 티켓 스레드에 새 메시지가 올라왔습니다.\n"
        f"채널: <#{thread.id}>\n"
        f"작성자: {message.author.display_name}\n"
        f"내용: {message.content[:500] if message.content else '(텍스트 없음)'}"
    )
    try:
        await notifier.send(text)
    except Exception:
        LOGGER.exception("Failed to send ticket thread message DM notifier=%s", notifier.id)


def _extract_ticket_id_from_message(message: discord.Message | None) -> str | None:
    if message is None or not message.embeds:
        return None
    description = message.embeds[0].description or ""
    match = _TICKET_ID_PATTERN.search(description)
    if not match:
        return None
    return match.group(1).strip()


def _build_closed_ticket_embed(source_embed: discord.Embed, ticket_id: str | None) -> discord.Embed:
    closed_embed = discord.Embed.from_dict(source_embed.to_dict())
    closed_embed.color = discord.Color.dark_grey()

    description = closed_embed.description or ""
    status_line = "**상태:** 종료됨"
    ticket_line = f"**Ticket ID:** `{ticket_id}`" if ticket_id else None

    if ticket_line and ticket_line in description:
        description = description.replace(ticket_line, f"{ticket_line}\n{status_line}", 1)
    elif description:
        description = f"{status_line}\n{description}"
    else:
        description = status_line
    closed_embed.description = description

    footer_text = source_embed.footer.text if source_embed.footer else ""
    closed_embed.set_footer(text="종료된 티켓" if not footer_text else f"{footer_text} | 종료")
    return closed_embed


def _is_open_ticket_embed(message: discord.Message) -> bool:
    if not message.embeds:
        return False
    embed = message.embeds[0]
    description = embed.description or ""
    if not _TICKET_ID_PATTERN.search(description):
        return False
    footer_text = embed.footer.text if embed.footer else ""
    if "Closed" in footer_text:
        return False
    if "**Status:** Closed" in description or "**상태:** 종료됨" in description:
        return False
    return True


async def _count_open_ticket_embeds(thread: discord.Thread) -> int:
    count = 0
    async for history_message in thread.history(limit=100):
        if _is_open_ticket_embed(history_message):
            count += 1
    return count


async def close_ticket(
    interaction: discord.Interaction,
    thread: discord.Thread,
    message: discord.Message | None = None,
) -> None:
    open_embed_count = await _count_open_ticket_embeds(thread)
    ticket_id = _extract_ticket_id_from_message(message)
    if ticket_id is None:
        ticket = db.get_open_ticket_by_thread_id(str(thread.id))
        ticket_id = str(ticket["ticket_id"]) if ticket and ticket.get("ticket_id") else None

    if ticket_id:
        db.close_ticket(ticket_id, status="closed")

    await ticket_log.emit_ticket_audit(
        interaction.guild,
        interaction.user,
        "TICKET_CLOSED",
        ticket_id,
        f"thread_id={thread.id}",
    )

    if open_embed_count >= 2 and message is not None and message.embeds:
        closed_embed = _build_closed_ticket_embed(message.embeds[0], ticket_id)
        try:
            if interaction.response.is_done():
                await message.edit(embed=closed_embed, view=None)
            else:
                await interaction.response.edit_message(embed=closed_embed, view=None)
        except discord.HTTPException as exc:
            if getattr(exc, "code", None) not in {10062, 40060}:
                raise
            LOGGER.warning("Ticket close edit skipped due to interaction state: %s", exc)
        await _send_ephemeral_safe(
            interaction,
            "이 스레드에는 티켓이 2개 이상 있어 현재 티켓 임베드만 닫았습니다.",
        )
        return

    await _send_ephemeral_safe(interaction, "티켓을 닫고 스레드를 삭제합니다.")
    try:
        await thread.delete(reason=f"Ticket closed by {interaction.user}")
    except discord.Forbidden:
        await _send_ephemeral_safe(interaction, "스레드를 삭제할 권한이 없어 티켓을 완전히 닫지 못했습니다.")
    except Exception as exc:
        await _send_ephemeral_safe(interaction, f"스레드 삭제 중 오류가 발생했습니다: {exc}")


async def ensure_ticket_panel(channel: discord.abc.Messageable | None) -> None:
    if channel is None or not hasattr(channel, "history"):
        return
    _, _, TicketEntryView, _ = _load_views()
    panel_embed = discord.Embed(
        title="티켓 접수 안내",
        description=(
            "문제 또는 요청사항이 있으면 아래 버튼으로 티켓을 생성해 주세요.\n"
            "유형을 고르고 설명을 입력하면 전용 티켓 스레드가 만들어집니다."
        ),
        color=discord.Color.blurple(),
    )
    panel_embed.set_footer(text=PANEL_FOOTER_TEXT)

    async for msg in channel.history(limit=50):
        if not msg.author.bot or not msg.embeds:
            continue
        footer = msg.embeds[0].footer.text if msg.embeds[0].footer else ""
        if footer in {PANEL_MARKER, PANEL_FOOTER_TEXT}:
            try:
                await msg.edit(embed=panel_embed, view=TicketEntryView())
            except Exception:
                pass
            return

    panel = await channel.send(embed=panel_embed, view=TicketEntryView())
    if hasattr(panel, "pin"):
        try:
            await panel.pin(reason="ops ticket panel")
        except Exception:
            pass


def close_open_tickets_for_thread(thread_id: int | str, status: str = "closed") -> int:
    closed_count = 0
    open_tickets = db.get_open_tickets_by_thread_id(str(thread_id))
    for ticket in open_tickets:
        ticket_id = ticket.get("ticket_id")
        if not ticket_id:
            continue
        db.close_ticket(str(ticket_id), status=status)
        closed_count += 1
    if closed_count:
        LOGGER.info(
            "Auto-closed %s open ticket(s) for deleted thread_id=%s with status=%s",
            closed_count,
            thread_id,
            status,
        )
    return closed_count
