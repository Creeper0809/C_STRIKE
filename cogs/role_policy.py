from __future__ import annotations

import logging

import discord

try:
    import config
except ModuleNotFoundError as exc:
    if exc.name != "config":
        raise
    from .. import config


LOGGER = logging.getLogger("ops.bot.role_policy")


def _find_role(guild: discord.Guild, role_id: int | None, role_name: str) -> discord.Role | None:
    if role_id:
        role = guild.get_role(role_id)
        if role is not None:
            return role
    normalized_name = str(role_name or "").strip()
    if not normalized_name:
        return None
    return discord.utils.get(guild.roles, name=normalized_name)


def _find_first_role_by_names(guild: discord.Guild, candidates: tuple[str, ...]) -> discord.Role | None:
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized:
            continue
        role = discord.utils.get(guild.roles, name=normalized)
        if role is not None:
            return role
    return None


def operator_role(guild: discord.Guild) -> discord.Role | None:
    return _find_role(guild, config.OPERATOR_ROLE_ID, config.OPERATOR_ROLE_NAME)


async def ensure_operator_role(guild: discord.Guild) -> discord.Role:
    """운영포털이 운영자 추가 시 호출 — 기존 역할 발견 시 administrator/hoist/mentionable 자동 보정, 없으면 관리자 권한+주황색으로 신규 생성."""
    existing = operator_role(guild)
    if existing is not None:
        # administrator/hoist/mentionable 보정 (Discord UI에서 별도 섹션으로 분리되도록)
        needs_update = {}
        if not existing.permissions.administrator:
            needs_update["permissions"] = discord.Permissions(administrator=True)
        if not existing.hoist:
            needs_update["hoist"] = True
        if not existing.mentionable:
            needs_update["mentionable"] = True
        if needs_update:
            try:
                await existing.edit(
                    **needs_update,
                    reason="운영포털 운영진 역할 속성 자동 보정",
                )
                LOGGER.warning(
                    "Upgraded operator role attrs=%s name=%s role_id=%s guild_id=%s",
                    list(needs_update.keys()), existing.name, existing.id, guild.id,
                )
            except discord.Forbidden:
                LOGGER.exception(
                    "Missing permissions to upgrade operator role name=%s role_id=%s",
                    existing.name, existing.id,
                )
            except discord.HTTPException:
                LOGGER.exception(
                    "HTTP error while upgrading operator role name=%s role_id=%s",
                    existing.name, existing.id,
                )
        return existing

    role_name = str(config.OPERATOR_ROLE_NAME or "운영자").strip() or "운영자"
    try:
        role = await guild.create_role(
            name=role_name,
            permissions=discord.Permissions(administrator=True),
            reason="운영포털 운영진 자동 생성",
            mentionable=True,
            hoist=True,
        )
        LOGGER.warning("Created operator role name=%s guild_id=%s role_id=%s", role_name, guild.id, role.id)
        return role
    except discord.Forbidden:
        LOGGER.exception("Missing permissions to create operator role name=%s guild_id=%s", role_name, guild.id)
        raise
    except discord.HTTPException:
        LOGGER.exception("HTTP error while creating operator role name=%s guild_id=%s", role_name, guild.id)
        raise


def authenticated_role(guild: discord.Guild) -> discord.Role | None:
    role = _find_role(guild, config.AUTHENTICATED_ROLE_ID, config.AUTHENTICATED_ROLE_NAME)
    if role is not None:
        return role
    return _find_first_role_by_names(
        guild,
        (
            "c-guard 인증",
            "C-GUARD 인증",
            "cguard 인증",
            "인증 참가자",
            "인증된 사용자",
        ),
    )


def unauthenticated_role(guild: discord.Guild) -> discord.Role | None:
    role = _find_role(guild, config.UNAUTHENTICATED_ROLE_ID, config.UNAUTHENTICATED_ROLE_NAME)
    if role is not None:
        return role
    return _find_first_role_by_names(
        guild,
        (
            "c-guard 제한",
            "C-GUARD 제한",
            "cguard 제한",
            "제한됨",
            "인증되지 않은 사용자",
        ),
    )


async def ensure_auth_roles(guild: discord.Guild) -> None:
    auth_name = str(config.AUTHENTICATED_ROLE_NAME or "").strip()
    unauth_name = str(config.UNAUTHENTICATED_ROLE_NAME or "").strip()

    if authenticated_role(guild) is None and auth_name:
        try:
            await guild.create_role(name=auth_name, reason="Ensure authenticated role exists")
            LOGGER.warning("Created missing authenticated role name=%s guild_id=%s", auth_name, guild.id)
        except Exception:
            LOGGER.exception("Failed to create authenticated role name=%s guild_id=%s", auth_name, guild.id)

    if unauthenticated_role(guild) is None and unauth_name:
        try:
            await guild.create_role(name=unauth_name, reason="Ensure unauthenticated role exists")
            LOGGER.warning("Created missing unauthenticated role name=%s guild_id=%s", unauth_name, guild.id)
        except Exception:
            LOGGER.exception("Failed to create unauthenticated role name=%s guild_id=%s", unauth_name, guild.id)


def is_operator(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    role = operator_role(member.guild)
    return role is not None and role in member.roles


async def apply_auth_state(member: discord.Member, *, authenticated: bool, reason: str) -> None:
    auth_role = authenticated_role(member.guild)
    unauth_role = unauthenticated_role(member.guild)
    if auth_role is None:
        LOGGER.warning("Authenticated role not found in guild_id=%s", member.guild.id)
    if unauth_role is None:
        LOGGER.warning("Unauthenticated role not found in guild_id=%s", member.guild.id)

    roles_to_add: list[discord.Role] = []
    roles_to_remove: list[discord.Role] = []

    if authenticated:
        if auth_role is not None and auth_role not in member.roles:
            roles_to_add.append(auth_role)
        if unauth_role is not None and unauth_role in member.roles:
            roles_to_remove.append(unauth_role)
    else:
        if unauth_role is not None and unauth_role not in member.roles:
            roles_to_add.append(unauth_role)
        if auth_role is not None and auth_role in member.roles:
            roles_to_remove.append(auth_role)

    if roles_to_add:
        await member.add_roles(*roles_to_add, reason=reason)
    if roles_to_remove:
        await member.remove_roles(*roles_to_remove, reason=reason)

    if not roles_to_add and not roles_to_remove:
        LOGGER.debug("No role changes needed for member_id=%s authenticated=%s", member.id, authenticated)
