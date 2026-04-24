from __future__ import annotations

import asyncio
import json
import logging
from urllib import error as urllib_error
from urllib import request as urllib_request

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


LOGGER = logging.getLogger("ops.bot.register")
REGISTER_PANEL_MARKER = "[OPS_REGISTER_PANEL_V1]"


async def _delete_interaction_response_later(
    interaction: discord.Interaction,
    delay_seconds: int,
) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        await interaction.delete_original_response()
    except Exception:
        pass


def issue_auth_link(student_id: str, interaction: discord.Interaction) -> str:
    payload = json.dumps(
        {
            "student_id": student_id,
            "discord_user_id": str(interaction.user.id),
            "discord_name": getattr(interaction.user, "display_name", str(interaction.user)),
        }
    ).encode("utf-8")
    req = urllib_request.Request(
        config.REGISTER_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Bot-API-Key": config.REGISTER_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            detail = json.loads(body) if body else {}
        except json.JSONDecodeError:
            detail = {"error": body or exc.reason}
        raise RuntimeError(detail.get("error", str(exc))) from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"인증 서버에 연결할 수 없습니다: {exc.reason}") from exc

    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("인증 서버 응답을 해석할 수 없습니다.") from exc

    verify_url = str(data.get("verify_url", "")).strip()
    if not verify_url:
        raise RuntimeError("인증 링크가 응답에 없습니다.")
    return verify_url


class AuthLinkView(discord.ui.View):
    def __init__(self, verify_url: str) -> None:
        super().__init__(timeout=60)
        self.verify_url = verify_url

    @discord.ui.button(label="인증 링크 보기", style=discord.ButtonStyle.primary)
    async def show_auth_link(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(self.verify_url, ephemeral=True)
        asyncio.create_task(_delete_interaction_response_later(interaction, 60))
        try:
            await interaction.message.delete()
        except Exception:
            pass


class RegisterModal(discord.ui.Modal, title="포털 인증"):
    student_id = discord.ui.TextInput(
        label="학번",
        placeholder="예: 20240001",
        required=True,
        max_length=32,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        student_id = str(self.student_id).strip()
        if not student_id:
            await interaction.response.send_message("학번을 입력해주세요.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            verify_url = issue_auth_link(student_id, interaction)
        except RuntimeError as exc:
            LOGGER.warning("Register auth link issue failed user_id=%s error=%s", interaction.user.id, exc)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            LOGGER.exception("Unexpected register flow failure user_id=%s", interaction.user.id)
            await interaction.followup.send("인증 링크 발급 중 오류가 발생했습니다.", ephemeral=True)
            return

        if isinstance(interaction.user, (discord.Member, discord.User)):
            audit.log(
                None,
                interaction.user,
                "REGISTER_LINK_ISSUED",
                f"student_id={student_id}",
            )

        await interaction.edit_original_response(
            content="인증 링크를 발급했습니다. `인증 링크 보기` 버튼으로 확인하세요.",
            view=AuthLinkView(verify_url),
        )
        asyncio.create_task(_delete_interaction_response_later(interaction, 60))


class RegisterEntryView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="인증하기",
        style=discord.ButtonStyle.success,
        custom_id="open_register_btn",
    )
    async def open_register(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RegisterModal())


def _build_register_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="포털 인증 안내",
        description=(
            "대회 포털을 이용하려면 아래 `인증하기` 버튼을 눌러 학번을 입력해주세요.\n"
            "봇이 디스코드 계정을 자동으로 연결하고 1회용 로그인 링크를 발급합니다."
        ),
        color=discord.Color.green(),
    )
    embed.add_field(
        name="인증 절차",
        value=(
            "1. 버튼 클릭\n"
            "2. 학번 입력\n"
            "3. 발급 링크 접속\n"
            "4. 포털 세션 발급"
        ),
        inline=False,
    )
    embed.set_footer(text=REGISTER_PANEL_MARKER)
    return embed


async def ensure_register_panel(channel: discord.abc.Messageable | None) -> None:
    if channel is None or not hasattr(channel, "history"):
        return

    panel_embed = _build_register_panel_embed()
    async for msg in channel.history(limit=50):
        if not msg.author.bot or not msg.embeds:
            continue
        footer = msg.embeds[0].footer.text if msg.embeds[0].footer else ""
        if footer == REGISTER_PANEL_MARKER:
            try:
                await msg.edit(embed=panel_embed, view=RegisterEntryView())
            except Exception:
                LOGGER.exception("Failed to update register panel")
            return

    panel = await channel.send(embed=panel_embed, view=RegisterEntryView())
    if hasattr(panel, "pin"):
        try:
            await panel.pin(reason="ops register panel")
        except Exception:
            LOGGER.exception("Failed to pin register panel")


class RegisterCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._persistent_view_registered = False

    def cog_unload(self) -> None:
        try:
            self.bot.tree.remove_command("인증")
        except Exception:
            LOGGER.exception("Failed to remove register slash command")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._persistent_view_registered:
            self.bot.add_view(RegisterEntryView())
            self._persistent_view_registered = True

        channel = None
        if config.REGISTER_CHANNEL_ID:
            channel = self.bot.get_channel(config.REGISTER_CHANNEL_ID)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(config.REGISTER_CHANNEL_ID)
                except Exception:
                    LOGGER.exception("Failed to fetch register channel %s", config.REGISTER_CHANNEL_ID)
                    channel = None

        if channel is None:
            for guild in self.bot.guilds:
                for guild_channel in guild.text_channels:
                    if guild_channel.name == config.REGISTER_CHANNEL_NAME:
                        channel = guild_channel
                        break
                if channel is not None:
                    break

        await ensure_register_panel(channel)

    @app_commands.command(name="인증", description="포털 인증 링크를 발급합니다.")
    async def register_slash(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RegisterModal())


async def setup(bot: commands.Bot) -> None:
    cog = RegisterCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.register_slash, override=True)
