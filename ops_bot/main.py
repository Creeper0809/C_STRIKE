import asyncio
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

from cogs import role_policy
import config
from db import init_db
from logging_utils import configure_logging

BASE_DIR = Path(__file__).resolve().parent.parent
EXTENSION_BASE = "cogs"
EXTENSIONS = [
    f"{EXTENSION_BASE}.audit",
    f"{EXTENSION_BASE}.error",
    f"{EXTENSION_BASE}.internal_api",
    f"{EXTENSION_BASE}.ticket",
    f"{EXTENSION_BASE}.register",
    f"{EXTENSION_BASE}.flag",
    f"{EXTENSION_BASE}.problem",
    f"{EXTENSION_BASE}.ranking",
    f"{EXTENSION_BASE}.team",
    f"{EXTENSION_BASE}.notify",
    f"{EXTENSION_BASE}.message",
]

LOGGER = logging.getLogger("ops.bot")
LOGS_DIR = configure_logging(BASE_DIR)


class _SingleInstanceLock:
    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fp = None

    def acquire(self) -> bool:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self._lock_path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                self._fp.seek(0)
                self._fp.write(" ")
                self._fp.flush()
                self._fp.seek(0)
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fp.seek(0)
            self._fp.truncate()
            self._fp.write(str(os.getpid()))
            self._fp.flush()
            return True
        except OSError:
            self.release()
            return False

    def release(self) -> None:
        if self._fp is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fp.seek(0)
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fp.close()
        finally:
            self._fp = None


class OpsBot(commands.Bot):
    async def setup_hook(self):
        init_db()
        LOGGER.info("DB initialization completed")
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            LOGGER.info("Loaded extension %s", extension)

        guild = discord.Object(id=config.GUILD_ID)
        # Keep a single guild-scoped command set and actively clear stale global commands
        # so Discord does not show duplicate slash commands from old global registrations.
        self.tree.clear_commands(guild=guild)
        self.tree.copy_global_to(guild=guild)
        LOGGER.info("Prepared guild command sync for guild %s", config.GUILD_ID)

        for attempt in range(1, 4):
            try:
                synced = await self.tree.sync(guild=guild)
                LOGGER.info(
                    "Synced %s slash command(s) to guild %s",
                    len(synced),
                    config.GUILD_ID,
                )
                self.tree.clear_commands(guild=None)
                removed_globals = await self.tree.sync()
                LOGGER.info(
                    "Synced global commands after cleanup; remaining global command count=%s",
                    len(removed_globals),
                )
                break
            except discord.DiscordServerError as exc:
                if attempt == 3:
                    LOGGER.exception("Slash command sync failed after retries: %s", exc)
                    break
                LOGGER.warning("Slash command sync retry %s for guild %s", attempt, config.GUILD_ID)
                await asyncio.sleep(2 * attempt)


intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = OpsBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    LOGGER.info("Bot ready as %s", bot.user)
    LOGGER.info("%s | Slash commands synced", bot.user)


@bot.event
async def on_member_join(member: discord.Member):
    if role_policy.is_operator(member):
        return
    try:
        await role_policy.ensure_auth_roles(member.guild)
        await role_policy.apply_auth_state(
            member,
            authenticated=False,
            reason="New member requires C-GUARD verification",
        )
    except Exception:
        LOGGER.exception("Failed to apply default limited role for member_id=%s", member.id)


def main() -> None:
    instance_lock = _SingleInstanceLock(BASE_DIR / ".tmp" / "ops_bot.instance.lock")
    if not instance_lock.acquire():
        LOGGER.error("Another ops bot process is already running. Exiting duplicate process.")
        return
    LOGGER.info("Starting ops bot with logs in %s", LOGS_DIR)
    try:
        bot.run(config.DISCORD_TOKEN)
    finally:
        instance_lock.release()


if __name__ == "__main__":
    main()
