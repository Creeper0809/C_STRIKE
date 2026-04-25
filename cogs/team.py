from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    import config
except ModuleNotFoundError as exc:
    if exc.name != "config":
        raise
    from .. import config

try:
    from db import connection as db_connection
    from db import team as db_team
except ModuleNotFoundError as exc:
    if exc.name not in {"db", "connection", "team"}:
        raise
    from ..db import connection as db_connection
    from ..db import team as db_team

try:
    from . import audit
except ModuleNotFoundError as exc:
    if exc.name not in {"audit", "cogs"}:
        raise
    import cogs.audit as audit


LOGGER = logging.getLogger("ops.bot.team")
KST = timezone(timedelta(hours=9), name="KST")


@dataclass(slots=True)
class TeamError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def _now_kst_naive() -> datetime:
    return datetime.now(timezone.utc)


def _competition_id() -> str | None:
    value = str(getattr(config, "BOT_COMPETITION_ID", "") or "").strip()
    return value or None


def _can_use_ops_command(user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    permissions = user.guild_permissions
    return permissions.administrator or permissions.manage_guild


def _require_operator(user: discord.abc.User) -> None:
    if not _can_use_ops_command(user):
        raise TeamError("운영자 권한이 필요합니다.")


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _status_label(status: object) -> str:
    normalized = str(status or "-").strip().lower()
    return {
        "open": "모집중",
        "closed": "마감",
        "deleted": "삭제됨",
        "approved": "승인",
        "pending": "대기",
        "rejected": "거절",
        "kicked": "추방",
        "scheduled": "예약됨",
        "sent": "발송됨",
        "failed": "실패",
    }.get(normalized, str(status or "-").strip() or "-")


def _role_label(role: object) -> str:
    return {"captain": "팀장", "member": "팀원"}.get(str(role or "member").lower(), str(role or "member"))


def _role_mention(role_id: object) -> str:
    normalized = str(role_id or "").strip()
    return f"<@&{normalized}>" if normalized else "역할 미연결"


def _parse_schedule_time(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    raise TeamError("시간 형식은 `YYYY-MM-DD HH:MM` 또는 `YYYY-MM-DD HH:MM:SS`로 입력해주세요.")


def _parse_db_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    return _parse_schedule_time(raw[:19]) if raw else None


def _team_of_user_or_raise(user_id: str) -> dict:
    team = db_team.get_user_team(str(user_id), competition_id=_competition_id())
    if not team:
        raise TeamError("현재 소속된 팀을 찾을 수 없습니다.")
    return team


def _team_of_member_roles_or_raise(member: discord.abc.User, guild_id: str) -> dict:
    if not isinstance(member, discord.Member):
        raise TeamError("?꾩옱 ?뚯냽?????李얠쓣 ???놁뒿?덈떎.")

    roles = list(getattr(member, "roles", []) or [])
    roles.sort(key=lambda role: int(getattr(role, "position", 0) or 0), reverse=True)
    competition_id = _competition_id()
    for role in roles:
        role_id = str(getattr(role, "id", "") or "").strip()
        if not role_id:
            continue
        team = db_team.get_team_by_role(guild_id, role_id)
        if not team:
            continue
        if competition_id and str(team.get("competition_id") or "").strip() != competition_id:
            continue
        return team
    raise TeamError("?꾩옱 ?뚯냽?????李얠쓣 ???놁뒿?덈떎.")


def _team_from_role_or_raise(guild_id: str, role: discord.Role) -> dict:
    team = db_team.get_team_by_role(guild_id, str(role.id))
    if not team:
        raise TeamError(f"{role.mention} 역할에 연결된 운영 DB 팀이 없습니다.")
    return team


def _collect_unique_roles(*roles: discord.Role | None) -> list[discord.Role]:
    result: list[discord.Role] = []
    seen: set[int] = set()
    for role in roles:
        if role is None:
            continue
        if role.id in seen:
            continue
        seen.add(role.id)
        result.append(role)
    if not result:
        raise TeamError("최소 한 개 팀 역할은 선택해야 합니다.")
    return result


def _team_list_embed(teams: list[dict]) -> discord.Embed:
    ranked = sorted(teams, key=lambda row: (str(row.get("team_code") or ""), str(row.get("name") or "").lower()))
    total_members = sum(_safe_int(row.get("member_count")) for row in ranked)
    linked_roles = sum(1 for row in ranked if str(row.get("discord_role_id") or "").strip())
    embed = discord.Embed(
        title="팀 목록",
        description=f"등록 팀 `{len(ranked)}`개 | 승인 멤버 `{total_members}`명 | 역할 연결 `{linked_roles}`개",
        color=discord.Color.teal(),
    )
    lines = []
    for idx, row in enumerate(ranked[:25], start=1):
        name = str(row.get("name") or f"team-{idx}").strip()
        code = str(row.get("team_code") or "-").strip()
        role = _role_mention(row.get("discord_role_id"))
        vpn = "예" if row.get("vpn_profile_issued") else "아니오"
        lines.append(
            f"`{code}` **{name}**\n"
            f"멤버 `{_safe_int(row.get('member_count'))}`명 | 상태 `{_status_label(row.get('status'))}` | VPN `{vpn}` | {role}"
        )
    embed.add_field(name="Teams", value="\n\n".join(lines) or "등록된 팀이 없습니다.", inline=False)
    embed.set_footer(text=f"상위 25개만 표시합니다. 전체 {len(ranked)}개" if len(ranked) > 25 else "팀 코드 순으로 정렬합니다.")
    return embed


def _team_info_embed(detail: dict, role: discord.Role | None = None) -> discord.Embed:
    name = str(detail.get("name") or "unknown").strip()
    members = list(detail.get("members") or [])
    approved_members = [row for row in members if str(row.get("status", "")).lower() == "approved"]
    total_score = sum(_safe_int(row.get("personal_score")) for row in approved_members)
    role_id = str(detail.get("discord_role_id") or "").strip()
    embed = discord.Embed(
        title=f"팀 정보 - {name}",
        description=f"팀 코드 `{detail.get('team_code', '-')}` | 개인 합산 `{total_score}`점 | 승인 멤버 `{len(approved_members)}`명",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="상태", value=f"`{_status_label(detail.get('status'))}`", inline=True)
    embed.add_field(name="팀 코드", value=f"`{detail.get('team_code', '-')}`", inline=True)
    embed.add_field(name="멤버", value=f"`{detail.get('member_count', 0)}`명", inline=True)
    embed.add_field(name="팀장 Discord ID", value=f"`{detail.get('captain_discord_id', '-')}`", inline=True)
    embed.add_field(name="Discord 역할", value=role.mention if role else _role_mention(role_id), inline=True)
    embed.add_field(name="역할 ID", value=f"`{role.id if role else role_id or '-'}`", inline=True)
    embed.add_field(name="역할 멤버 수", value=f"`{len(role.members)}`명" if role else "`-`", inline=True)
    embed.add_field(name="VPN 발급", value=f"`{'예' if detail.get('vpn_profile_issued') else '아니오'}`", inline=True)
    embed.add_field(name="서브넷", value=f"`{detail.get('subnet') or '-'}`", inline=True)
    embed.add_field(name="게이트웨이", value=f"`{detail.get('gateway_ip') or '-'}`", inline=True)
    if members:
        lines = []
        for idx, row in enumerate(members[:30], start=1):
            username = str(row.get("discord_username") or row.get("discord_user_id") or "unknown").strip()
            lines.append(
                f"`{idx:>2}` **{username}** | `{_safe_int(row.get('personal_score')):>4}`점 | "
                f"{_role_label(row.get('role'))} | `{_status_label(row.get('status'))}`"
            )
        embed.add_field(name="멤버", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="멤버", value="등록된 멤버가 없습니다.", inline=False)
    return embed


def _schedule_embed(title: str, rows: list[dict], kind: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.green())
    if not rows:
        embed.description = "표시할 항목이 없습니다."
        return embed
    lines = []
    for idx, row in enumerate(rows[:25], start=1):
        team_name = str(row.get("team_name") or row.get("team_id") or "-")
        role_name = str(row.get("discord_role_name") or row.get("discord_role_id") or "-")
        status = _status_label(row.get("status"))
        if kind == "예약":
            when = f"시작 `{str(row.get('start_at') or '-')[:16]}` | 종료 `{str(row.get('end_at') or '-')[:16]}`"
            extra = ""
        else:
            when = f"예약 `{str(row.get('scheduled_at') or '즉시')[:16]}`"
            extra = f" | 제목 `{str(row.get('title') or kind)}`"
            if kind == "힌트":
                has_text = bool(str(row.get("description") or "").strip())
                has_file = bool(str(row.get("file_url") or "").strip())
                hint_type = "text_file" if has_text and has_file else "file" if has_file else "text"
                extra += f" | 타입 `{hint_type}`"
        lines.append(f"`{idx}` **{team_name}** | 역할 `{role_name}` | {when}{extra} | `{status}`")
    embed.description = "\n".join(lines)
    return embed


async def _resolve_channel(bot: commands.Bot, guild: discord.Guild, channel_id: str | None):
    if channel_id and str(channel_id).isdigit():
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        if hasattr(channel, "send"):
            return channel
    candidates = [channel for channel in guild.text_channels if "ops" in channel.name.lower() or "공지" in channel.name]
    if candidates:
        return candidates[0]
    return guild.system_channel


def _build_team_embed(row: dict, title: str, description: str, severity: str | None = None) -> discord.Embed:
    color = {
        "critical": discord.Color.red(),
        "error": discord.Color.red(),
        "warning": discord.Color.orange(),
        "info": discord.Color.blurple(),
        "notice": discord.Color.blurple(),
    }.get(str(severity or "").lower(), discord.Color.blurple())
    embed = discord.Embed(title=title, description=description, color=color, timestamp=_now_kst_naive())
    team_name = str(row.get("team_name") or row.get("name") or "").strip()
    team_code = str(row.get("team_code") or "").strip()
    if team_name or team_code:
        embed.add_field(name="팀", value=f"{team_name or '-'} (`{team_code or '-'}`)", inline=True)
    if row.get("file_url"):
        embed.add_field(name="첨부 파일", value=f"[{row.get('file_name') or 'file'}]({row.get('file_url')})", inline=False)
    return embed


async def send_team_notification(
    bot: commands.Bot,
    *,
    guild_id: str,
    role_id: str,
    title: str,
    description: str,
    channel_id: str | None = None,
    severity: str | None = None,
    team_payload: dict | None = None,
) -> int:
    guild = bot.get_guild(int(guild_id)) if str(guild_id).isdigit() else None
    if guild is None:
        raise RuntimeError(f"Guild not found: {guild_id}")
    channel = await _resolve_channel(bot, guild, channel_id)
    if channel is None:
        raise RuntimeError("발송할 채널을 찾을 수 없습니다.")
    embed = _build_team_embed(dict(team_payload or {}), title, description, severity)
    await channel.send(content=_role_mention(role_id), embed=embed)
    return 1


class TeamCog(commands.GroupCog, group_name="팀", group_description="팀 조회, 공지, 힌트, 예약 관리"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        if not self.team_schedule_worker.is_running():
            self.team_schedule_worker.start()

    async def cog_unload(self) -> None:
        self.team_schedule_worker.cancel()

    async def _send_error(self, interaction: discord.Interaction, exc: Exception) -> None:
        message = str(exc) if str(exc).strip() else "팀 처리 중 오류가 발생했습니다."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def _channel_payload(self, interaction: discord.Interaction) -> tuple[str | None, str | None]:
        channel = interaction.channel
        return (str(channel.id), getattr(channel, "name", None)) if channel else (None, None)

    async def _send_schedule_row(self, table: str, row: dict, *, kind: str) -> None:
        role_id = str(row.get("discord_role_id") or "").strip()
        if not role_id:
            raise RuntimeError("discord_role_id is empty")
        title = str(row.get("title") or ("팀 힌트" if kind == "hint" else "팀 공지"))
        description = str(row.get("description") or ("첨부 파일을 확인해주세요." if kind == "hint" else ""))
        await send_team_notification(
            self.bot,
            guild_id=str(row.get("guild_id") or config.GUILD_ID),
            role_id=role_id,
            title=title,
            description=description,
            channel_id=str(row.get("channel_id") or "").strip() or None,
            team_payload=row,
        )
        db_team.mark_schedule_sent(table, int(row["id"]))

    async def _run_countdown(self, row: dict, phase: str) -> None:
        guild = self.bot.get_guild(int(str(row.get("guild_id") or config.GUILD_ID)))
        if guild is None:
            raise RuntimeError("guild not found")
        channel = await _resolve_channel(self.bot, guild, str(row.get("channel_id") or "").strip() or None)
        if channel is None:
            raise RuntimeError("channel not found")
        if phase == "start":
            await channel.send(content=_role_mention(row.get("discord_role_id")), embed=discord.Embed(title="게임이 시작되었습니다.", color=discord.Color.green()))
            for value in range(5, 0, -1):
                await channel.send(f"{value}...")
                await asyncio.sleep(1)
            db_team.mark_countdown_started(int(row["id"]))
        else:
            await channel.send(content=_role_mention(row.get("discord_role_id")))
            for value in range(5, 0, -1):
                await channel.send(f"{value}...")
                await asyncio.sleep(1)
            await channel.send(embed=discord.Embed(title="게임이 종료되었습니다.", color=discord.Color.red()))
            db_team.mark_countdown_ended(int(row["id"]))

    @tasks.loop(seconds=30)
    async def team_schedule_worker(self) -> None:
        now = _now_kst_naive()
        for row in db_team.list_due_team_announcements(now, limit=20):
            try:
                await self._send_schedule_row("team_announcements", row, kind="announcement")
            except Exception as exc:
                LOGGER.exception("Failed to send team announcement id=%s", row.get("id"))
                db_team.mark_schedule_failed("team_announcements", int(row["id"]), str(exc))
        for row in db_team.list_due_team_hints(now, limit=20):
            try:
                await self._send_schedule_row("team_hints", row, kind="hint")
            except Exception as exc:
                LOGGER.exception("Failed to send team hint id=%s", row.get("id"))
                db_team.mark_schedule_failed("team_hints", int(row["id"]), str(exc))
        for row in db_team.list_due_team_countdowns(now, limit=20):
            try:
                start_at = _parse_db_time(row.get("start_at"))
                end_at = _parse_db_time(row.get("end_at"))
                if start_at and not row.get("started_at") and start_at <= now:
                    await self._run_countdown(row, "start")
                if end_at and not row.get("ended_at") and end_at <= now:
                    await self._run_countdown(row, "end")
            except Exception as exc:
                LOGGER.exception("Failed to run team countdown id=%s", row.get("id"))
                db_team.mark_schedule_failed("team_countdown_schedules", int(row["id"]), str(exc))

    @team_schedule_worker.before_loop
    async def before_team_schedule_worker(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="목록", description="운영 DB의 팀 목록을 조회합니다.")
    @app_commands.checks.cooldown(1, 10.0)
    async def list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            teams = db_team.list_teams(competition_id=_competition_id(), limit=200)
            await interaction.followup.send(embed=_team_list_embed(teams), ephemeral=True)
        except Exception as exc:
            await self._send_error(interaction, exc)

    @app_commands.command(name="정보", description="팀 상세 정보를 조회합니다.")
    @app_commands.describe(team="조회할 팀 역할입니다. 비우면 본인 소속 팀을 조회합니다.")
    @app_commands.checks.cooldown(1, 5.0)
    async def info(self, interaction: discord.Interaction, team: discord.Role | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if team is not None:
                target = _team_from_role_or_raise(str(interaction.guild_id), team)
            else:
                target = _team_of_member_roles_or_raise(interaction.user, str(interaction.guild_id))
                if target.get("discord_role_id") and interaction.guild is not None:
                    team = interaction.guild.get_role(int(str(target["discord_role_id"])))
            detail = db_team.get_team_detail(str(target.get("id")))
            if not detail:
                raise TeamError("팀 상세 정보를 불러오지 못했습니다.")
            if target.get("discord_role_id"):
                detail["discord_role_id"] = target.get("discord_role_id")
            await interaction.followup.send(embed=_team_info_embed(detail, team), ephemeral=True)
        except Exception as exc:
            await self._send_error(interaction, exc)

    @app_commands.command(name="공지", description="팀 공지를 즉시 발송하거나 예약합니다.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.rename(time="시간", title="제목", description="설명", team="팀1", team2="팀2", team3="팀3", team4="팀4", team5="팀5")
    async def announce(
        self,
        interaction: discord.Interaction,
        time: str,
        title: str,
        description: str,
        team: discord.Role | None = None,
        team2: discord.Role | None = None,
        team3: discord.Role | None = None,
        team4: discord.Role | None = None,
        team5: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _require_operator(interaction.user)
            roles = _collect_unique_roles(team, team2, team3, team4, team5)
            scheduled_at = _parse_schedule_time(time)
            channel_id, channel_name = self._channel_payload(interaction)
            row_ids: list[int] = []
            sent_count = 0
            for role in roles:
                target = _team_from_role_or_raise(str(interaction.guild_id), role)
                row_id = db_team.create_team_announcement(
                    guild_id=str(interaction.guild_id),
                    channel_id=channel_id,
                    channel_name=channel_name,
                    team_id=str(target["id"]),
                    discord_role_id=str(role.id),
                    title=title,
                    description=description,
                    scheduled_at=scheduled_at,
                    status="scheduled",
                    created_by_id=str(interaction.user.id),
                    created_by_name=str(interaction.user),
                )
                row_ids.append(row_id)
                if scheduled_at is None:
                    await send_team_notification(
                        self.bot,
                        guild_id=str(interaction.guild_id),
                        role_id=str(role.id),
                        title=title,
                        description=description,
                        channel_id=channel_id,
                        team_payload=target,
                    )
                    db_team.mark_schedule_sent("team_announcements", row_id)
                    sent_count += 1
            if scheduled_at is None:
                await interaction.followup.send(f"팀 공지를 `{sent_count}`개 팀에 발송했습니다.", ephemeral=True)
            else:
                await interaction.followup.send(
                    f"팀 공지를 `{scheduled_at:%Y-%m-%d %H:%M}`에 `{len(row_ids)}`개 팀으로 예약했습니다. ID: `{', '.join(str(v) for v in row_ids)}`",
                    ephemeral=True,
                )
        except Exception as exc:
            await self._send_error(interaction, exc)

    @app_commands.command(name="공지목록", description="팀 공지 예약 목록을 조회합니다.")
    @app_commands.default_permissions(manage_guild=True)
    async def announcement_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _require_operator(interaction.user)
            rows = db_team.list_team_announcements(str(interaction.guild_id), limit=50)
            await interaction.followup.send(embed=_schedule_embed("팀 공지 목록", rows, "공지"), ephemeral=True)
        except Exception as exc:
            await self._send_error(interaction, exc)

    @app_commands.command(name="공지삭제", description="예약된 팀 공지를 삭제합니다.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.rename(number="삭제번호")
    async def announcement_delete(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 200]) -> None:
        await self._delete_schedule(interaction, "team_announcements", db_team.get_team_announcement_by_number, "TEAM_ANNOUNCEMENT_DELETE", "팀 공지", int(number))

    @app_commands.command(name="힌트배부", description="팀 힌트를 즉시 배부하거나 예약합니다.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.rename(time="시간", title="제목", description="설명", file="파일", team="팀1", team2="팀2", team3="팀3", team4="팀4", team5="팀5")
    async def hint_send(
        self,
        interaction: discord.Interaction,
        time: str,
        title: str,
        description: str,
        file: discord.Attachment | None = None,
        team: discord.Role | None = None,
        team2: discord.Role | None = None,
        team3: discord.Role | None = None,
        team4: discord.Role | None = None,
        team5: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _require_operator(interaction.user)
            roles = _collect_unique_roles(team, team2, team3, team4, team5)
            scheduled_at = _parse_schedule_time(time)
            channel_id, channel_name = self._channel_payload(interaction)
            row_ids: list[int] = []
            sent_count = 0
            for role in roles:
                target = _team_from_role_or_raise(str(interaction.guild_id), role)
                row_id = db_team.create_team_hint(
                    guild_id=str(interaction.guild_id),
                    channel_id=channel_id,
                    channel_name=channel_name,
                    team_id=str(target["id"]),
                    discord_role_id=str(role.id),
                    title=title,
                    description=str(description or "").strip() or None,
                    file_url=file.url if file else None,
                    file_name=file.filename if file else None,
                    scheduled_at=scheduled_at,
                    status="scheduled",
                    created_by_id=str(interaction.user.id),
                    created_by_name=str(interaction.user),
                )
                row_ids.append(row_id)
                if scheduled_at is None:
                    payload = dict(target)
                    payload.update({"file_url": file.url if file else None, "file_name": file.filename if file else None})
                    await send_team_notification(
                        self.bot,
                        guild_id=str(interaction.guild_id),
                        role_id=str(role.id),
                        title=title,
                        description=str(description or "").strip() or "첨부 파일을 확인해주세요.",
                        channel_id=channel_id,
                        team_payload=payload,
                    )
                    db_team.mark_schedule_sent("team_hints", row_id)
                    sent_count += 1
            if scheduled_at is None:
                await interaction.followup.send(f"팀 힌트를 `{sent_count}`개 팀에 배부했습니다.", ephemeral=True)
            else:
                await interaction.followup.send(
                    f"팀 힌트를 `{scheduled_at:%Y-%m-%d %H:%M}`에 `{len(row_ids)}`개 팀으로 예약했습니다. ID: `{', '.join(str(v) for v in row_ids)}`",
                    ephemeral=True,
                )
        except Exception as exc:
            await self._send_error(interaction, exc)

    @app_commands.command(name="힌트목록", description="팀 힌트 예약 목록을 조회합니다.")
    @app_commands.default_permissions(manage_guild=True)
    async def hint_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _require_operator(interaction.user)
            rows = db_team.list_team_hints(str(interaction.guild_id), limit=50)
            await interaction.followup.send(embed=_schedule_embed("팀 힌트 목록", rows, "힌트"), ephemeral=True)
        except Exception as exc:
            await self._send_error(interaction, exc)

    @app_commands.command(name="힌트삭제", description="예약된 팀 힌트를 삭제합니다.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.rename(number="삭제번호")
    async def hint_delete(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 200]) -> None:
        await self._delete_schedule(interaction, "team_hints", db_team.get_team_hint_by_number, "TEAM_HINT_DELETE", "팀 힌트", int(number))

    @app_commands.command(name="예약", description="팀별 게임 시작/종료 카운트다운을 예약합니다.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.rename(start_time="시작시간", end_time="종료시간", team="팀1", team2="팀2", team3="팀3", team4="팀4", team5="팀5")
    async def countdown(
        self,
        interaction: discord.Interaction,
        start_time: str,
        end_time: str | None = None,
        team: discord.Role | None = None,
        team2: discord.Role | None = None,
        team3: discord.Role | None = None,
        team4: discord.Role | None = None,
        team5: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _require_operator(interaction.user)
            start_at = _parse_schedule_time(start_time)
            end_at = _parse_schedule_time(end_time)
            if start_at is None:
                raise TeamError("시작시간은 필수입니다.")
            if start_at and end_at and end_at < start_at:
                raise TeamError("종료시각은 시작시각보다 빠를 수 없습니다.")
            roles = _collect_unique_roles(team, team2, team3, team4, team5)
            channel_id, channel_name = self._channel_payload(interaction)
            row_ids: list[int] = []
            for role in roles:
                target = _team_from_role_or_raise(str(interaction.guild_id), role)
                row_id = db_team.create_team_countdown_schedule(
                    guild_id=str(interaction.guild_id),
                    channel_id=channel_id,
                    channel_name=channel_name,
                    team_id=str(target["id"]),
                    discord_role_id=str(role.id),
                    start_at=start_at,
                    end_at=end_at,
                    status="scheduled",
                    created_by_id=str(interaction.user.id),
                    created_by_name=str(interaction.user),
                )
                row_ids.append(row_id)
            await interaction.followup.send(
                f"팀 게임 예약을 `{len(row_ids)}`개 팀으로 저장했습니다. ID: `{', '.join(str(v) for v in row_ids)}`",
                ephemeral=True,
            )
        except Exception as exc:
            await self._send_error(interaction, exc)

    @app_commands.command(name="예약목록", description="팀별 게임 시작/종료 예약 목록을 조회합니다.")
    @app_commands.default_permissions(manage_guild=True)
    async def countdown_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _require_operator(interaction.user)
            rows = db_team.list_team_countdown_schedules(str(interaction.guild_id), limit=50)
            await interaction.followup.send(embed=_schedule_embed("팀 예약 목록", rows, "예약"), ephemeral=True)
        except Exception as exc:
            await self._send_error(interaction, exc)

    @app_commands.command(name="예약삭제", description="팀별 게임 예약을 삭제합니다.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.rename(number="삭제번호")
    async def countdown_delete(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 200]) -> None:
        await self._delete_schedule(interaction, "team_countdown_schedules", db_team.get_team_countdown_by_number, "TEAM_COUNTDOWN_DELETE", "팀 예약", int(number))

    async def _delete_schedule(self, interaction: discord.Interaction, table: str, getter, action: str, label: str, number: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _require_operator(interaction.user)
            row = getter(str(interaction.guild_id), number)
            if not row:
                raise TeamError(f"해당 번호의 {label}을 찾을 수 없습니다.")
            if not db_team.soft_delete_schedule(table, str(interaction.guild_id), int(row["id"])):
                raise TeamError("예약 상태의 항목만 삭제할 수 있습니다.")
            audit.log(None, interaction.user, action, f"id={row['id']}, number={number}, team_id={row.get('team_id')}")
            await interaction.followup.send(f"{label}을 삭제했습니다. ID: `{row['id']}`", ephemeral=True)
        except Exception as exc:
            await self._send_error(interaction, exc)


async def setup(bot: commands.Bot) -> None:
    db_connection.init_db()
    await bot.add_cog(TeamCog(bot))
